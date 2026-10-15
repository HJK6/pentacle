"""The daily brief's dedicated credential reaches only two configured routes."""
from __future__ import annotations

import asyncio
import json

import pytest
import server as server_module

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from comms import Comms
from server import Server
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store
from test_system_notification_producer import Peer
from test_tell_immediate_delivery import ScriptedClaude, IDLE_PLACEHOLDER

ACTOR = "examplehost:wmi-daily-brief"
TOKEN = "synthetic-independent-brief-token"
COMPOSITE = "desk:assistant"
OPS = "localhost:ops"
FD = "localhost:frontdesk"
CONFIG_ENV = "PENTACLE_WMI_BRIEF_PRODUCER_CONFIG"


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    token = tmp_path / "brief-token"
    token.write_text(TOKEN)
    token.chmod(0o600)
    config = tmp_path / "brief-config.json"
    config.write_text(json.dumps({
        "stream_id": ACTOR, "token_file": str(token),
        "composite_stream_id": COMPOSITE, "ops_stream_id": OPS,
    }))
    config.chmod(0o600)
    monkeypatch.setenv(CONFIG_ENV, str(config))
    return token, config


def frame(verb="tell", **changes):
    msg = {"type": verb, "request_id": "brief-request", "from_stream_id": ACTOR,
           "stream_token": TOKEN}
    if verb == "hello":
        msg["subscribe"] = {"snapshot": False, "mode": "rpc"}
    elif verb == "tell":
        msg.update(tell_id="brief-tell", to_stream_id=COMPOSITE, text="GATE synthetic brief",
                   ttl_seconds=3600, sanitize=True)
    return {**msg, **changes}


async def dispatch(server, peer, msg):
    return (await server._dispatch(json.dumps(msg), websocket=peer))[0]


async def authenticate(server, peer):
    reply = await dispatch(server, peer, frame("hello"))
    assert reply["type"] == "ready", reply


class Harness:
    def __init__(self, path, *, bound=True):
        self.store = Store(str(path))
        self.bound = bound

    async def __aenter__(self):
        self.store.start()
        self.tmux = ScriptedClaude(start=IDLE_PLACEHOLDER)
        self.sessions = Sessions(self.store, tmux=self.tmux, local_host="localhost")
        self.comms = Comms(self.store, self.sessions, SpawnCtl(self.store, self.sessions, tmux=self.tmux))
        await self.sessions.open("localhost", "ops", provider="claude")
        if self.bound:
            await self.sessions.open("localhost", "frontdesk", provider="claude")
        seat = await self.store.fetch_session("localhost", "frontdesk")
        self.composite = AssistantComposite(self.store, config=AssistantCompositeConfig(
            enabled=True, name="briefdesk", stream_id=COMPOSITE,
            direct_primary_stream_id=FD,
            direct_primary_generation=seat["session_generation"] if seat else "pending",
        ))
        await self.composite.load_binding()
        await self.composite.ensure_projection()
        await self.sessions.refresh()
        self.server = Server(store=self.store, sessions=self.sessions, comms=self.comms, local_host="localhost")
        self.server.assistant_composites = {"briefdesk": self.composite}
        return self

    async def __aexit__(self, *exc):
        self.store.stop()


@pytest.mark.parametrize("target", [COMPOSITE, OPS])
def test_file_principal_tell_uses_real_delivery_ledger_and_sender_stamp(credentials, tmp_path, target):
    async def run():
        async with Harness(tmp_path / "brief.db") as h:
            peer = Peer()
            await authenticate(h.server, peer)
            result = await dispatch(h.server, peer, frame(to_stream_id=target))
            assert result["type"] == "tell.ok", result
            assert result["delivery_status"] == "delivered"
            expected = f"[from {ACTOR}] [tell:brief-tell]\nGATE synthetic brief"
            assert h.tmux.pastes == [expected]
            ledger = await h.store.get_tell_delivery("brief-tell")
            assert ledger["delivery"]["from_stream_id"] == ACTOR
            assert ledger["delivery"]["text"] == expected
            assert ledger["delivery"]["to_stream_id"] == (FD if target == COMPOSITE else OPS)
    asyncio.run(run())


def test_bound_principal_cannot_call_any_other_verb(credentials, tmp_path):
    async def run():
        async with Harness(tmp_path / "verbs.db") as h:
            peer = Peer()
            await authenticate(h.server, peer)
            # Include module-installed verbs and an unknown verb as well as the
            # real production Server dispatch table.
            for verb in sorted(set(h.server.handlers) | {"asset.publish", "notification.create", "future.verb"}):
                if verb in {"hello", "tell"}:
                    continue
                reply = await dispatch(h.server, peer, frame(verb))
                assert reply["error_code"] == "system_producer_forbidden", (verb, reply)
            assert not h.tmux.pastes
            assert await h.store.claim_composite_tells(name="briefdesk") == []
    asyncio.run(run())


@pytest.mark.parametrize("patch", [
    {"to_stream_id": FD}, {"to_stream_id": "other:assistant"}, {"to_stream_id": None},
    {"text": "RESEND GATE brief"}, {"text": "GATEWAY brief"}, {"text": " GATE brief"},
    {"text": "GATE "}, {"text": "GATE \x1b[31m\x00"}, {"text": None},
    {"text": "GATE \ud800"}, {"text": "GATE " + "x" * 131068},
    {"text": "GATE " + "é" * 65534},
    {"request_id": ""}, {"request_id": "x" * 121}, {"request_id": "line\nbreak"},
    {"tell_id": ""}, {"tell_id": "\x1b"}, {"tell_id": "x" * 121},
    {"ttl_seconds": 86400}, {"ttl_seconds": 3600.0}, {"ttl_seconds": True},
    {"sanitize": False}, {"sanitize": 1},
    {"message": "GATE alternate"}, {"urgent": True}, {"host": "elsewhere"},
    {"_auth_context": {"operator_authenticated": True}},
    {"_assistant_composite_backend_dispatch": True},
])
def test_bad_envelope_has_no_delivery_or_queue_effect(credentials, tmp_path, patch):
    async def run():
        async with Harness(tmp_path / "invalid.db", bound=False) as h:
            peer = Peer("127.0.0.1")
            await authenticate(h.server, peer)
            reply = await dispatch(h.server, peer, frame(**patch))
            assert reply["error_code"] == "system_producer_payload_invalid", reply
            assert not h.tmux.pastes
            assert await h.store.get_tell_delivery("brief-tell") is None
            assert await h.store.claim_composite_tells(name="briefdesk") == []
    asyncio.run(run())


@pytest.mark.parametrize("patch", [
    {"from_stream_id": "other:wmi-daily-brief"}, {"from_stream_id": OPS},
    {"stream_token": "incorrect"}, {"stream_token": ""},
    {"subscribe": {"snapshot": True, "mode": "rpc"}},
    {"subscribe": {"snapshot": False, "mode": "full"}},
])
@pytest.mark.parametrize("address", ["127.0.0.1", "192.0.2.10"])
def test_hello_cannot_borrow_loopback_identity_or_subscription(credentials, patch, address):
    async def run():
        server = Server()
        peer = Peer(address)
        reply = await dispatch(server, peer, frame("hello", **patch))
        assert reply["error_code"] == "system_producer_auth_required", reply
        assert peer not in server._client_system_producers
    asyncio.run(run())


@pytest.mark.parametrize("mutation", [
    "token_removed", "token_rotated", "token_insecure", "token_empty",
    "config_removed", "config_insecure", "config_disabled", "config_rebound", "identity_changed",
])
def test_revocation_invalidates_bound_tokenless_and_fresh_sockets(credentials, monkeypatch, mutation):
    token, config = credentials
    async def run():
        server = Server()
        peer = Peer("127.0.0.1")
        await authenticate(server, peer)
        if mutation == "token_removed": token.unlink()
        elif mutation == "token_rotated": token.write_text("rotated-synthetic-token")
        elif mutation == "token_insecure": token.chmod(0o644)
        elif mutation == "token_empty": token.write_text("")
        elif mutation == "config_removed": config.unlink()
        elif mutation == "config_insecure": config.chmod(0o644)
        elif mutation == "config_disabled": monkeypatch.delenv(CONFIG_ENV)
        else:
            value = json.loads(config.read_text())
            value["ops_stream_id" if mutation == "config_rebound" else "stream_id"] = (
                "localhost:newops" if mutation == "config_rebound" else "other:wmi-daily-brief")
            config.write_text(json.dumps(value))
        for msg in (frame(), {k: v for k, v in frame().items() if k != "stream_token"}, frame("ping")):
            assert (await dispatch(server, peer, msg))["error_code"] == "system_producer_auth_required"
        # Fresh connections may adopt a valid changed destination configuration;
        # stale sockets still cannot. Removed/revoked credentials cannot hello.
        fresh = await dispatch(server, Peer(), frame("hello"))
        assert fresh["type"] == ("ready" if mutation == "config_rebound" else "hello.error")
    asyncio.run(run())


@pytest.mark.parametrize("patch", [
    {"extra": True}, {"stream_id": "examplehost:arbitrary-service"},
    {"token_file": "relative"}, {"composite_stream_id": []},
    {"ops_stream_id": COMPOSITE}, {"ops_stream_id": ACTOR},
])
def test_invalid_private_config_is_disabled(credentials, patch):
    _, config = credentials
    value = json.loads(config.read_text()); value.update(patch)
    config.write_text(json.dumps(value))
    async def run():
        reply = await dispatch(Server(), Peer("127.0.0.1"), frame("hello"))
        assert reply["error_code"] == "system_producer_auth_required", reply
    asyncio.run(run())


def test_distinct_existing_service_credentials_and_reserved_report_identity(credentials, tmp_path, monkeypatch):
    from test_report_producer_principal import write_config
    actors = [
        (server_module.FIXED_SYSTEM_PRODUCER_STREAM_ID, "PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN_FILE"),
        (server_module.WMI_BACKUP_PRODUCER_STREAM_ID, "PENTACLE_WMI_BACKUP_STREAM_TOKEN_FILE"),
        (server_module.BOT_MESSAGING_PRODUCER_STREAM_ID, "PENTACLE_BOT_MESSAGING_STREAM_TOKEN_FILE"),
    ]
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_ID", actors[0][0])
    for index, (_, env) in enumerate(actors):
        path = tmp_path / f"other-{index}"
        path.write_text(f"synthetic-other-{index}"); path.chmod(0o600)
        monkeypatch.setenv(env, str(path))
    report_token = tmp_path / "report-token"
    report_token.write_text("synthetic-report-token"); report_token.chmod(0o600)
    report_config = write_config(tmp_path, token_file=str(report_token))
    monkeypatch.setenv("PENTACLE_REPORT_PRODUCER_CONFIG", str(report_config))
    async def run():
        server = Server()
        for index, (actor, _) in enumerate(actors):
            peer = Peer()
            hello = frame("hello", from_stream_id=actor, stream_token=f"synthetic-other-{index}")
            assert (await dispatch(server, peer, hello))["type"] == "ready"
            denied = await dispatch(server, peer, frame(from_stream_id=actor, stream_token=f"synthetic-other-{index}"))
            assert denied["error_code"] == "system_producer_forbidden"
        peer = Peer()
        assert (await dispatch(server, peer, frame("hello", from_stream_id="examplehost:daily-report",
                                                  stream_token="synthetic-report-token")))["type"] == "ready"
        assert (await dispatch(server, peer, frame()))["error_code"] == "system_producer_auth_required"
        assert (await dispatch(server, peer, frame(from_stream_id="examplehost:daily-report",
                                                  stream_token="synthetic-report-token")))["error_code"] == "system_producer_forbidden"
        # Colliding credentials refuse the brief, even with a valid private config.
        credentials[0].write_text("synthetic-other-0")
        assert (await dispatch(server, Peer(), frame("hello", stream_token="synthetic-other-0")))["error_code"] == "system_producer_auth_required"
        credentials[0].write_text(TOKEN)
        # A report config cannot give the reserved brief identity another verb.
        value = json.loads(report_config.read_text()); value["stream_id"] = ACTOR
        report_config.write_text(json.dumps(value))
        assert (await dispatch(server, Peer(), frame("hello")))["error_code"] == "system_producer_auth_required"
    asyncio.run(run())


def test_shared_rolling_rate_window_and_reconnect_cannot_reset(credentials, tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(server_module, "_monotonic", lambda: now[0])
    async def run():
        async with Harness(tmp_path / "rate.db", bound=False) as h:
            for index in range(6):
                peer = Peer()
                await authenticate(h.server, peer)
                reply = await dispatch(h.server, peer, frame(tell_id=f"brief-{index}"))
                assert reply["delivery_status"] == "queued_unbound", reply
            peer = Peer(); await authenticate(h.server, peer)
            reply = await dispatch(h.server, peer, frame(tell_id="seventh"))
            assert reply["error_code"] == "system_producer_rate_limited"
            assert len(await h.store.claim_composite_tells(name="briefdesk")) == 6
            now[0] += 60.0
            assert (await dispatch(h.server, peer, frame(tell_id="seventh")))["delivery_status"] == "queued_unbound"
    asyncio.run(run())


def test_full_brief_boundary_sanitization_and_resend_prefix(credentials, tmp_path):
    async def run():
        async with Harness(tmp_path / "full.db", bound=False) as h:
            peer = Peer(); await authenticate(h.server, peer)
            body = "GATE " + "é" * 65533 + "x"  # exactly 131072 UTF-8 bytes
            assert len(body.encode()) == 131072
            assert (await dispatch(h.server, peer, frame(text=body)))["delivery_status"] == "queued_unbound"
            retry = "GATE RESEND (first tell to FD not delivered) — GATE \x1b[31mbrief\x00"
            assert (await dispatch(h.server, peer, frame(text=retry, tell_id="retry")))["delivery_status"] == "queued_unbound"
            rows = await h.store.claim_composite_tells(name="briefdesk")
            assert rows[0]["body"].endswith(body)
            assert rows[1]["body"].endswith("— GATE brief")
            assert all(row["from_stream_id"] == ACTOR for row in rows)
    asyncio.run(run())


def test_queue_survives_store_restart_and_flush_keeps_sender(credentials, tmp_path):
    async def run():
        path = tmp_path / "restart.db"
        async with Harness(path, bound=False) as h:
            peer = Peer(); await authenticate(h.server, peer)
            first = await dispatch(h.server, peer, frame())
            assert first["queued"] and first["delivery_status"] == "queued_unbound"
            assert first["submission_confirmed"] is False
            await dispatch(h.server, peer, frame())  # same id does not add another row
            rows = await h.store.claim_composite_tells(name="briefdesk")
            assert len(rows) == 1 and rows[0]["from_stream_id"] == ACTOR
            assert not h.tmux.pastes
        async with Harness(path) as restarted:
            assert len(await restarted.store.claim_composite_tells(name="briefdesk")) == 1
            assert await restarted.server._flush_composite_tells(restarted.composite) == 1
            assert await restarted.store.claim_composite_tells(name="briefdesk") == []
            ledger = await restarted.store.get_tell_delivery("brief-tell")
            assert ledger["delivery"]["from_stream_id"] == ACTOR
            assert restarted.tmux.pastes == [f"[from {ACTOR}] [tell:brief-tell]\nGATE synthetic brief"]
    asyncio.run(run())


def test_disabled_composite_and_stale_generation_refuse(credentials, tmp_path):
    async def run():
        async with Harness(tmp_path / "stale.db") as h:
            peer = Peer(); await authenticate(h.server, peer)
            original = h.server.assistant_composites
            h.server.assistant_composites = {}
            assert (await dispatch(h.server, peer, frame()))["error_code"] == "system_producer_payload_invalid"
            h.server.assistant_composites = original
            prior = await h.store.fetch_session("localhost", "frontdesk")
            await h.store.update_session("localhost", "frontdesk", status="closed")
            changed = await h.store.open_session("localhost", "frontdesk", provider="claude")
            assert changed["session_generation"] != prior["session_generation"]
            assert (await dispatch(h.server, peer, frame()))["error_code"] == "assistant_direct_generation_conflict"
            assert not h.tmux.pastes
    asyncio.run(run())


def test_committed_composite_tell_preserves_do_not_resend_ack(credentials, tmp_path):
    async def run():
        async with Harness(tmp_path / "committed.db") as h:
            h.tmux.submit_on_paste = False
            peer = Peer(); await authenticate(h.server, peer)
            reply = await dispatch(h.server, peer, frame())
            assert reply["type"] == "tell.ok", reply
            assert reply["submission_confirmed"] is False
            assert reply["action_committed"] is True
            assert reply["do_not_resubmit"] is True
            assert reply["action_status"] == "committed"
            retry = await dispatch(h.server, peer, frame())
            assert retry["action_committed"] is True
            assert len(h.tmux.pastes) == 1
    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["missing", "malformed", "oversized", "non_object"])
def test_absent_or_unreadable_config_never_uses_seat_or_loopback(credentials, monkeypatch, mutation):
    _, config = credentials
    if mutation == "missing": monkeypatch.delenv(CONFIG_ENV)
    elif mutation == "malformed": config.write_text("{")
    elif mutation == "oversized": config.write_text(" " * 16385)
    else: config.write_text("[]")
    async def run():
        for address in ("127.0.0.1", "192.0.2.10"):
            reply = await dispatch(Server(), Peer(address), frame("hello"))
            assert reply["error_code"] == "system_producer_auth_required"
    asyncio.run(run())


def test_claim_and_internal_auth_cannot_replace_service_credential(credentials, tmp_path):
    async def run():
        async with Harness(tmp_path / "spoof.db") as h:
            peer = Peer("127.0.0.1")
            # Correct token without hello is not permission to tell.
            assert (await dispatch(h.server, peer, frame()))["error_code"] == "system_producer_auth_required"
            await authenticate(h.server, peer)
            for patch in ({"from_stream_id": OPS}, {"stream_token": "wrong"}):
                assert (await dispatch(h.server, peer, frame(**patch)))["error_code"] == "system_producer_auth_required"
            assert not h.tmux.pastes
    asyncio.run(run())
