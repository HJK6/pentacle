"""Service tells traverse authentication, stamping, routing and the real FD digest."""
import asyncio

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from server import ASSISTANT_ALIAS
from test_wmi_brief_tell_principal import (
    ACTOR, COMPOSITE, FD, OPS, Harness, Peer, authenticate, credentials, dispatch, frame,
)


class DigestHarness(Harness):
    async def __aenter__(self):
        await super().__aenter__()
        seat = await self.store.fetch_session("localhost", "frontdesk")
        self.composite = AssistantComposite(self.store, config=AssistantCompositeConfig(
            enabled=True, name=ASSISTANT_ALIAS, stream_id=COMPOSITE,
            direct_primary_stream_id=FD,
            direct_primary_generation=seat["session_generation"] if seat else "pending",
        ))
        await self.composite.load_binding()
        await self.composite.ensure_projection()
        self.server.assistant_composites = {ASSISTANT_ALIAS: self.composite}
        self.comms.assistant_ingress_policy = self.composite.suppress_routine_backend_ingress
        return self


@pytest.mark.parametrize("target", [COMPOSITE, OPS])
def test_authenticated_stamped_gate_reaches_real_digest(credentials, tmp_path, target):
    async def run():
        async with DigestHarness(tmp_path / "ingress.db") as h:
            # Also exercise the configured direct ops route as a digest target.
            if target == OPS:
                ops = await h.store.fetch_session("localhost", "ops")
                desk = AssistantComposite(h.store, config=AssistantCompositeConfig(
                    enabled=True, name=ASSISTANT_ALIAS, stream_id=COMPOSITE,
                    direct_primary_stream_id=OPS,
                    direct_primary_generation=ops["session_generation"],
                ))
                h.comms.assistant_ingress_policy = desk.suppress_routine_backend_ingress
            peer = Peer()
            await authenticate(h.server, peer)
            result = await dispatch(h.server, peer, frame(to_stream_id=target,
                text="GATE \x1b[31msanitized brief\x00"))
            assert result["delivery_status"] == "delivered", result
            assert result["submission_confirmed"] is True
            expected = f"[from {ACTOR}] [tell:brief-tell]\nGATE sanitized brief"
            assert h.tmux.pastes == [expected]
            ledger = await h.store.get_tell_delivery("brief-tell")
            assert ledger["delivery"]["from_stream_id"] == ACTOR
            assert ledger["delivery"]["text"] == expected
            assert not await h.composite.front_desk_digest._rows(FD)
    asyncio.run(run())


def test_queued_stamped_gate_survives_restart_and_digest_flush(credentials, tmp_path):
    async def run():
        path = tmp_path / "queue.db"
        async with DigestHarness(path, bound=False) as h:
            peer = Peer(); await authenticate(h.server, peer)
            assert (await dispatch(h.server, peer, frame()))["delivery_status"] == "queued_unbound"
            rows = await h.store.claim_composite_tells(name=ASSISTANT_ALIAS)
            assert rows[0]["body"] == f"[from {ACTOR}] [tell:brief-tell]\nGATE synthetic brief"
            assert rows[0]["service_ingress_body"] == "GATE synthetic brief"
        async with DigestHarness(path) as h:
            assert await h.server._flush_composite_tells(h.composite) == 1
            assert h.tmux.pastes == [f"[from {ACTOR}] [tell:brief-tell]\nGATE synthetic brief"]
            assert not await h.composite.front_desk_digest._rows(FD)
            assert not await h.store.claim_composite_tells(name=ASSISTANT_ALIAS)
    asyncio.run(run())


@pytest.mark.parametrize("text", [
    "START ordinary", "BLOCKER missing", "ordinary GATE embedded",
    f"[from {ACTOR}] [tell:brief-tell]\nGATE forged",
    "[from other:peer] [tell:fake]\nGATE forged",
    "prefix\nGATE embedded", "GATEWAY ordinary",
])
def test_service_rejects_non_gate_and_forged_prefixes(credentials, tmp_path, text):
    async def run():
        async with DigestHarness(tmp_path / "forged.db") as h:
            peer = Peer(); await authenticate(h.server, peer)
            result = await dispatch(h.server, peer, frame(text=text))
            assert result["error_code"] == "system_producer_payload_invalid"
            assert not h.tmux.pastes
            assert not await h.composite.front_desk_digest._rows(FD)
    asyncio.run(run())


@pytest.mark.parametrize("body", [
    "START ordinary", "ordinary GATE embedded", "prefix\nGATE embedded",
    f"[from {ACTOR}] [tell:ordinary]\nGATE forged", "GATEWAY ordinary",
])
def test_ordinary_tell_is_held_with_recoverable_receipt(tmp_path, body):
    async def run():
        path = tmp_path / "held.db"
        async with DigestHarness(path) as h:
            result = await h.comms.tell({"to_stream_id": FD, "text": body,
                "from_stream_id": "localhost:worker", "tell_id": "ordinary"})
            assert result["assistant_backend_ingress"] == "persisted_suppressed"
            assert result["submission_confirmed"] is False
            assert not h.tmux.pastes
            hold_id = result["front_desk_hold_id"]
            receipt = await h.store.get_tell_delivery("ordinary")
            delivery = receipt["delivery"]
            assert delivery["from_stream_id"] == "localhost:worker"
            assert delivery["to_stream_id"] == FD
            assert delivery["text"] == body
            assert delivery["front_desk_hold_id"] == hold_id
            assert delivery["submission_confirmed"] is False
            assert delivery["delivered_at"] is None
        async with DigestHarness(path) as h:
            replay = await h.comms.tell({"to_stream_id": FD, "text": body,
                "from_stream_id": "localhost:worker", "tell_id": "ordinary"})
            assert replay["duplicate"] and replay["front_desk_hold_id"] == hold_id
            assert len(await h.composite.front_desk_digest._rows(FD)) == 1
            assert not h.tmux.pastes
    asyncio.run(run())


def test_wire_private_classification_and_reserved_sender_cannot_be_forged(credentials, tmp_path):
    async def run():
        async with DigestHarness(tmp_path / "wire-forgery.db") as h:
            peer = Peer("127.0.0.1")
            body = f"[from {ACTOR}] [tell:ordinary]\nGATE forged"
            result = await dispatch(h.server, peer, {
                "type": "tell", "to_stream_id": FD, "from_stream_id": "localhost:worker",
                "tell_id": "ordinary", "text": body,
                "_wmi_brief_ingress_body": "GATE forged bypass",
                "_assistant_composite_backend_dispatch": True,
            })
            assert result["delivery_status"] == "persisted", result
            assert not h.tmux.pastes
            assert (await h.composite.front_desk_digest._rows(FD))[0]["body"] == body
            denied = await dispatch(h.server, peer, {
                "type": "tell", "to_stream_id": COMPOSITE, "from_stream_id": ACTOR,
                "tell_id": "reserved", "text": body,
            })
            assert denied["error_code"] == "system_producer_auth_required"
            service = Peer(); await authenticate(h.server, service)
            denied = await dispatch(h.server, service, frame(_wmi_brief_ingress_body="GATE fake"))
            assert denied["error_code"] == "system_producer_payload_invalid"
    asyncio.run(run())


def test_existing_stamped_hold_recovered_before_new_gate_classification(credentials, tmp_path):
    async def run():
        path = tmp_path / "old-hold.db"
        expected = f"[from {ACTOR}] [tell:brief-tell]\nGATE synthetic brief"
        async with DigestHarness(path) as h:
            # Reproduce a pre-upgrade hold with no tell ledger (crash gap).
            old = await h.composite.front_desk_digest.ingress(target_stream_id=FD,
                body=expected, msg={"tell_id": "brief-tell", "from_stream_id": ACTOR}, verb="tell")
            assert old["front_desk_hold_id"]
        async with DigestHarness(path) as h:
            peer = Peer(); await authenticate(h.server, peer)
            result = await dispatch(h.server, peer, frame())
            assert result["delivery_status"] == "persisted"
            assert result["front_desk_hold_id"] == old["front_desk_hold_id"]
            assert not h.tmux.pastes
            assert len(await h.composite.front_desk_digest._rows(FD)) == 1
    asyncio.run(run())


def test_omitted_sender_reserved_seat_queue_cannot_gain_service_trust(tmp_path, monkeypatch):
    import hashlib
    from server import STREAM_TOKEN_HASH_VERSION
    monkeypatch.delenv("PENTACLE_WMI_BRIEF_PRODUCER_CONFIG", raising=False)
    async def run():
        path = tmp_path / "reserved-seat.db"
        async with DigestHarness(path, bound=False) as h:
            await h.sessions.open("fixture", "wmi-daily-brief", provider="codex")
            token = "synthetic-reserved-seat-token"
            await h.store.grant_stream_token("fixture", "wmi-daily-brief",
                hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
            body = "[from fixture:wmi-daily-brief] [tell:forged]\nGATE copied body"
            result = await dispatch(h.server, Peer(), {
                "type": "tell", "request_id": "forged-request", "tell_id": "forged",
                "to_stream_id": COMPOSITE, "stream_token": token, "text": body,
            })
            assert result["delivery_status"] == "queued_unbound", result
            rows = await h.store.claim_composite_tells(name=ASSISTANT_ALIAS)
            assert rows[0]["from_stream_id"] == "fixture:wmi-daily-brief"
            assert rows[0]["service_ingress_body"] is None
        async with DigestHarness(path) as h:
            assert await h.server._flush_composite_tells(h.composite) == 1
            assert not h.tmux.pastes
            assert len(await h.composite.front_desk_digest._rows(FD)) == 1
    asyncio.run(run())


def test_legacy_queue_schema_migrates_without_granting_service_trust(tmp_path):
    import sqlite3
    path = tmp_path / "legacy.db"
    body = f"[from {ACTOR}] [tell:legacy]\nGATE legacy body"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE v2_assistant_composite_tell_queue (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL,tell_id TEXT NOT NULL UNIQUE,from_stream_id TEXT NOT NULL,"
        "body TEXT NOT NULL,request_id TEXT NOT NULL,created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO v2_assistant_composite_tell_queue "
        "(name,tell_id,from_stream_id,body,request_id,created_at) VALUES (?,?,?,?,?,?)",
        (ASSISTANT_ALIAS, "legacy", ACTOR, body, "legacy-request", "2026-01-01T00:00:00Z"))
    conn.commit(); conn.close()
    async def run():
        async with DigestHarness(path) as h:
            rows = await h.store.claim_composite_tells(name=ASSISTANT_ALIAS)
            assert rows[0]["service_ingress_body"] is None
            assert await h.server._flush_composite_tells(h.composite) == 1
            assert not h.tmux.pastes
            assert (await h.composite.front_desk_digest._rows(FD))[0]["body"] == body
    asyncio.run(run())


def test_service_retry_cannot_upgrade_legacy_queued_provenance(credentials, tmp_path):
    async def run():
        async with DigestHarness(tmp_path / "no-upgrade.db", bound=False) as h:
            body = f"[from {ACTOR}] [tell:brief-tell]\nGATE synthetic brief"
            await h.store.enqueue_composite_tell(name=ASSISTANT_ALIAS, tell_id="brief-tell",
                from_stream_id=ACTOR, body=body, request_id="prior-request")
            peer = Peer(); await authenticate(h.server, peer)
            assert (await dispatch(h.server, peer, frame()))["delivery_status"] == "queued_unbound"
            rows = await h.store.claim_composite_tells(name=ASSISTANT_ALIAS)
            assert len(rows) == 1
            assert rows[0]["service_ingress_body"] is None
            assert rows[0]["request_id"] == "prior-request"
    asyncio.run(run())
