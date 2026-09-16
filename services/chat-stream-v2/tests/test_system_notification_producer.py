"""Fixed-identity admission for the notification-only CD producer."""

from __future__ import annotations

import asyncio
import json

import pytest

from assets import Assets
from blobs import BlobStore
from event_push import EventPush
from notify import Notify
from server import Server
from uiverbs import UIVerbs
from window_schedule import WindowSchedule


SYSTEM_ID = "altum-bot-cd"
SYSTEM_TOKEN = "synthetic-altum-cd-token"


@pytest.fixture(autouse=True)
def system_token_file(monkeypatch, tmp_path):
    token_path = tmp_path / "system-producer-token"
    token_path.write_text(SYSTEM_TOKEN)
    token_path.chmod(0o600)
    monkeypatch.delenv("PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN", raising=False)
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN_FILE", str(token_path))
    return token_path


class Peer:
    def __init__(self, address: str = "192.0.2.10") -> None:
        self.remote_address = (address, 54321)


class SocketPeer(Peer):
    def __init__(self, address: str = "127.0.0.1") -> None:
        super().__init__(address)
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


def _system_frame(frame_type: str, **extra: object) -> dict[str, object]:
    return {
        "type": frame_type,
        "request_id": f"req-{frame_type}",
        "from_stream_id": SYSTEM_ID,
        "stream_token": SYSTEM_TOKEN,
        **extra,
    }


async def _authenticate_system(daemon: Server, peer: Peer) -> None:
    hello = _system_frame("hello", subscribe={"mode": "rpc", "snapshot": False})
    assert (await daemon._dispatch(json.dumps(hello), websocket=peer))[0]["type"] == "ready"


def test_system_token_cannot_authenticate_an_arbitrary_claim_or_unrelated_verb(monkeypatch):
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_ID", SYSTEM_ID)

    async def run() -> None:
        daemon = Server()
        calls: list[str] = []

        async def spawn_handler(msg):
            calls.append(str(msg["type"]))
            return {"type": "spawn.ok"}

        daemon.handlers["spawn"] = spawn_handler
        peer = Peer()
        arbitrary = _system_frame("hello", from_stream_id="service:arbitrary", subscribe={"mode": "rpc", "snapshot": False})
        assert (await daemon._dispatch(json.dumps(arbitrary), websocket=peer))[0]["error_code"] == "system_producer_auth_required"
        system_peer = Peer()
        await _authenticate_system(daemon, system_peer)
        denied = await daemon._dispatch(json.dumps(_system_frame("spawn")), websocket=system_peer)
        assert denied[0]["error_code"] == "system_producer_forbidden"
        assert calls == []

    asyncio.run(run())


def test_system_notification_rejects_action_bearing_create_before_store(monkeypatch, tmp_path):
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_ID", SYSTEM_ID)

    async def run() -> None:
        notify = Notify(str(tmp_path / "notifications.db"))
        await notify.start()
        try:
            daemon = Server()
            daemon.notify = notify
            daemon.handlers.update(notify.wire_handlers())
            peer = Peer()
            await _authenticate_system(daemon, peer)
            payload = _system_frame(
                "notification.create",
                producer=SYSTEM_ID,
                title="Pipeline failed",
                severity="critical",
                dedup_key="pipeline|unit-test|2026-09-15",
                actions=[{"kind": "ack", "action_id": "a0", "label": "Run"}],
            )
            denied = await daemon._dispatch(json.dumps(payload), websocket=peer)
            assert denied[0]["error_code"] == "system_producer_payload_invalid"
            assert await notify._db.call("list_notifications") == []
        finally:
            await notify.stop()

    asyncio.run(run())


def test_bound_system_connection_cannot_switch_claim_or_verb(monkeypatch):
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_ID", SYSTEM_ID)

    async def run() -> None:
        daemon = Server()
        calls: list[str] = []

        async def tell_handler(msg):
            calls.append(str(msg["type"]))
            return {"type": "tell.ok"}

        daemon.handlers["tell"] = tell_handler
        peer = Peer()
        hello = _system_frame("hello", subscribe={"mode": "rpc", "snapshot": False})
        assert (await daemon._dispatch(json.dumps(hello), websocket=peer))[0]["type"] == "ready"
        changed = _system_frame("tell", from_stream_id="bart:v2-stolen")
        assert (await daemon._dispatch(json.dumps(changed), websocket=peer))[0]["error_code"] == "system_producer_auth_required"
        assert (await daemon._dispatch('{"type":"tell","request_id":"tokenless"}', websocket=peer))[0]["error_code"] == "system_producer_forbidden"
        assert calls == []

    asyncio.run(run())


def test_system_producer_is_denied_every_other_production_registered_verb(monkeypatch):
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_ID", SYSTEM_ID)

    async def run() -> None:
        daemon = Server()
        registered = set(daemon.handlers)
        for component_type in (Assets, BlobStore, EventPush, Notify, UIVerbs, WindowSchedule):
            component = component_type.__new__(component_type)
            registered.update(component.wire_handlers())
        registered.discard("hello")
        registered.discard("notification.create")
        calls: list[str] = []

        async def must_not_run(msg):
            calls.append(str(msg["type"]))
            return {"type": f"{msg['type']}.ok"}

        for verb in registered:
            daemon.handlers[verb] = must_not_run
        for address in ("192.0.2.10", "127.0.0.1"):
            peer = Peer(address)
            await _authenticate_system(daemon, peer)
            for verb in sorted(registered):
                reply = await daemon._dispatch(
                    json.dumps(_system_frame(verb)), websocket=peer
                )
                assert reply[0]["error_code"] == "system_producer_forbidden", verb
        assert calls == []

    asyncio.run(run())


@pytest.mark.parametrize(
    ("configured_id", "configured_token"),
    [
        (None, SYSTEM_TOKEN),
        (SYSTEM_ID, None),
        ("service:wrong", SYSTEM_TOKEN),
    ],
)
@pytest.mark.parametrize("address", ["192.0.2.10", "127.0.0.1"])
def test_system_producer_missing_or_mismatched_config_never_falls_back(
    monkeypatch, configured_id, configured_token, address
):
    if configured_id is None:
        monkeypatch.delenv("PENTACLE_SYSTEM_PRODUCER_STREAM_ID", raising=False)
    else:
        monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_ID", configured_id)
    if configured_token is None:
        monkeypatch.delenv("PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN", raising=False)
        monkeypatch.delenv("PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN_FILE", raising=False)
    else:
        assert configured_token == SYSTEM_TOKEN  # secure file supplied by fixture

    async def run() -> None:
        daemon = Server()
        calls: list[str] = []

        async def handler(msg):
            calls.append(str(msg["type"]))
            return {"type": "notification.create.ok"}

        daemon.handlers["notification.create"] = handler
        reply = await daemon._dispatch(
            json.dumps(_system_frame(
                "notification.create",
                producer=SYSTEM_ID,
                title="Failure",
                severity="warning",
                dedup_key="pipeline|unit-test|2026-09-15",
                actions=[],
            )),
            websocket=Peer(address),
        )
        assert reply[0]["error_code"] == "system_producer_auth_required"
        assert calls == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "subscribe",
    [
        {"mode": "rpc"},
        {"snapshot": False},
        {"mode": "rpc", "snapshot": True},
        {"mode": "summary", "snapshot": False},
        {"all": True},
        [],
    ],
)
def test_system_producer_hello_is_rpc_without_snapshot_only(monkeypatch, subscribe):
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_ID", SYSTEM_ID)

    async def run() -> None:
        daemon = Server()
        peer = Peer()
        calls = []

        async def handler(msg):
            calls.append(msg)
            return {"type": "notification.create.ok"}

        daemon.handlers["notification.create"] = handler
        reply = await daemon._dispatch(
            json.dumps(_system_frame("hello", subscribe=subscribe)), websocket=peer
        )
        assert reply[0]["error_code"] == "system_producer_auth_required"
        assert peer not in daemon._client_system_producers
        create = _system_frame(
            "notification.create", producer=SYSTEM_ID, title="Must be denied",
            severity="info", dedup_key="pipeline|verification|2026-09-15",
        )
        assert (await daemon._dispatch(json.dumps(create), websocket=peer))[0]["error_code"] == "system_producer_auth_required"
        assert calls == []

    asyncio.run(run())


def test_loopback_system_hello_never_activates_broadcast_subscription(monkeypatch):
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_ID", SYSTEM_ID)

    async def run() -> None:
        daemon = Server()
        peer = SocketPeer()
        daemon._register_client(peer)
        try:
            hello = _system_frame("hello", subscribe={"mode": "rpc", "snapshot": False})
            await daemon._serve(peer, json.dumps(hello))
            assert peer.sent == [{
                "type": "ready", "snapshot": False, "events_mode": "full",
                "request_id": "req-hello",
            }]
            assert peer not in daemon._host_stats_clients
            await daemon.broadcast({"type": "session.inventory", "sessions": [{"stream_id": "secret"}]})
            await asyncio.sleep(0)
            assert len(peer.sent) == 1
        finally:
            daemon._unregister_client(peer)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor", "operator"),
        ("actor_stream_id", "bart:v2-owner"),
        ("owner", "operator"),
        ("answer_to_stream_id", "bart:v2-owner"),
        ("ttl_seconds", 60),
        ("prompt", {"text": "approve"}),
        ("grant", {"role": "admin"}),
        ("spawn", {"model": "anything"}),
        ("schedule", {"at": "now"}),
        ("binding", {"answer_to_stream_id": "bart:v2-owner"}),
        ("_auth_context", {"operator_authenticated": True}),
    ],
)
def test_system_notification_rejects_every_privilege_looking_wire_field(
    monkeypatch, field, value
):
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_ID", SYSTEM_ID)

    async def run() -> None:
        daemon = Server()
        calls: list[str] = []

        async def handler(msg):
            calls.append(str(msg["type"]))
            return {"type": "notification.create.ok"}

        daemon.handlers["notification.create"] = handler
        peer = Peer()
        await _authenticate_system(daemon, peer)
        payload = _system_frame(
            "notification.create",
            producer=SYSTEM_ID,
            title="Failure",
            severity="critical",
            dedup_key="pipeline|unit-test|2026-09-15",
            actions=[],
            **{field: value},
        )
        reply = await daemon._dispatch(json.dumps(payload), websocket=peer)
        assert reply[0]["error_code"] == "system_producer_payload_invalid"
        assert calls == []

    asyncio.run(run())


@pytest.mark.parametrize("actions", [None, {}, "[]", ["ack"], [{"kind": "ack"}]])
def test_system_notification_rejects_malformed_or_nonempty_actions(monkeypatch, actions):
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_ID", SYSTEM_ID)

    async def run() -> None:
        daemon = Server()
        daemon.handlers["notification.create"] = lambda msg: None
        peer = Peer()
        await _authenticate_system(daemon, peer)
        payload = _system_frame(
            "notification.create",
            producer=SYSTEM_ID,
            title="Failure",
            severity="warning",
            dedup_key="pipeline|unit-test|2026-09-15",
            actions=actions,
        )
        reply = await daemon._dispatch(json.dumps(payload), websocket=peer)
        assert reply[0]["error_code"] == "system_producer_payload_invalid"

    asyncio.run(run())


@pytest.mark.parametrize(
    "dedup_key",
    [
        "pipeline|too|many|2026-09-15",
        "pipeline|missing-date",
        "pipeline|bad class|2026-09-15",
        "pipeline|unit-test|2026-02-30",
        "census|function|2026-09-15",
        "census|function|invariant|20260915",
        "infra-census||2026-09-15",
        "infra-census|bad_stack|2026-09-15",
        "infra-census|stack|extra|2026-09-15",
        "other|failure|2026-09-15",
    ],
)
def test_system_notification_rejects_invalid_dedup_namespace(monkeypatch, dedup_key):
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_ID", SYSTEM_ID)

    async def run() -> None:
        daemon = Server()
        calls: list[str] = []

        async def handler(msg):
            calls.append(str(msg["type"]))
            return {"type": "notification.create.ok"}

        daemon.handlers["notification.create"] = handler
        peer = Peer()
        await _authenticate_system(daemon, peer)
        payload = _system_frame(
            "notification.create",
            producer=SYSTEM_ID,
            title="Failure",
            severity="warning",
            dedup_key=dedup_key,
            actions=[],
        )
        reply = await daemon._dispatch(json.dumps(payload), websocket=peer)
        assert reply[0]["error_code"] == "system_producer_payload_invalid"
        assert calls == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "dedup_key",
    [
        "pipeline|unit-test|2026-09-15",
        "census|daily_census|row_count|2026-09-15",
        "infra-census|altum-production|2026-09-15",
    ],
)
def test_valid_system_plain_card_uses_real_dispatch_and_preserves_text(
    monkeypatch, tmp_path, dedup_key
):
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_ID", SYSTEM_ID)

    async def run() -> None:
        notify = Notify(str(tmp_path / "notifications.db"))
        await notify.start()
        try:
            daemon = Server()
            daemon.notify = notify
            daemon.handlers.update(notify.wire_handlers())
            peer = Peer()
            hello = _system_frame("hello", subscribe={"mode": "rpc", "snapshot": False})
            assert (await daemon._dispatch(json.dumps(hello), websocket=peer))[0]["type"] == "ready"
            body = "plain text: agent-orch spawn $(touch /tmp/must-not-execute)"
            payload = _system_frame(
                "notification.create",
                producer=SYSTEM_ID,
                title="Pipeline failure",
                body=body,
                severity="critical",
                dedup_key=dedup_key,
                actions=[],
            )
            response = await daemon._dispatch(json.dumps(payload), websocket=peer)
            assert response[0]["type"] == "notification.create.ok"
            assert response[0]["notification"]["body"] == body
            assert response[0]["notification"]["actions"] == []
            assert response[0]["notification"]["answer_to_stream_id"] is None
        finally:
            await notify.stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    "dedup_key",
    ["pipeline|collision|2026-09-15", "infra-census|altum-production|2026-09-15"],
)
def test_system_plain_create_cannot_refresh_preexisting_actionable_collision(
    monkeypatch, tmp_path, dedup_key
):
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_ID", SYSTEM_ID)

    async def run() -> None:
        notify = Notify(str(tmp_path / "notifications.db"))
        await notify.start()
        try:
            seeded = await notify._db.call(
                "create_notification",
                producer=SYSTEM_ID,
                title="actionable authority",
                dedup_key=dedup_key,
                actions=[{"kind": "ack"}],
            )
            daemon = Server()
            daemon.notify = notify
            daemon.handlers.update(notify.wire_handlers())
            peer = Peer()
            await _authenticate_system(daemon, peer)
            payload = _system_frame(
                "notification.create",
                producer=SYSTEM_ID,
                title="must not erase authority",
                severity="warning",
                dedup_key=dedup_key,
                actions=[],
            )
            response = await daemon._dispatch(json.dumps(payload), websocket=peer)
            assert response[0]["error_code"] == "notification_invalid"
            unchanged = await notify._db.call("get_notification", seeded["notification_id"])
            assert unchanged["title"] == "actionable authority"
            assert unchanged["actions"] == [{"kind": "ack", "action_id": "a0"}]
            assert unchanged["firing_count"] == 1
        finally:
            await notify.stop()

    asyncio.run(run())


def test_system_connection_reconnect_revalidates_token(monkeypatch):
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_ID", SYSTEM_ID)

    async def run() -> None:
        daemon = Server()
        first = Peer()
        hello = _system_frame("hello", subscribe={"mode": "rpc", "snapshot": False})
        assert (await daemon._dispatch(json.dumps(hello), websocket=first))[0]["type"] == "ready"
        assert (await daemon._dispatch(
            json.dumps({"type": "ping", "request_id": "omitted"}), websocket=first
        ))[0]["error_code"] == "system_producer_forbidden"

        changed_token = _system_frame(
            "hello", stream_token="wrong", subscribe={"mode": "rpc", "snapshot": False}
        )
        assert (await daemon._dispatch(json.dumps(changed_token), websocket=first))[0]["error_code"] == "system_producer_auth_required"
        assert (await daemon._dispatch(json.dumps(changed_token), websocket=Peer()))[0]["error_code"] == "system_producer_auth_required"

    asyncio.run(run())


@pytest.mark.parametrize("file_state", ["absent", "missing", "empty"])
def test_inline_only_token_cannot_authenticate(monkeypatch, system_token_file, file_state):
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_ID", SYSTEM_ID)
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN", SYSTEM_TOKEN)
    if file_state == "absent":
        monkeypatch.delenv("PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN_FILE")
    elif file_state == "missing":
        system_token_file.unlink()
    else:
        system_token_file.write_text("")

    async def run():
        daemon = Server()
        peer = Peer()
        hello = _system_frame("hello", subscribe={"mode": "rpc", "snapshot": False})
        reply = await daemon._dispatch(json.dumps(hello), websocket=peer)
        assert reply[0]["error_code"] == "system_producer_auth_required"
        assert peer not in daemon._client_system_producers

    asyncio.run(run())


def test_configured_file_is_authoritative_over_inline_token(monkeypatch):
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_ID", SYSTEM_ID)
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN", "unrelated-inline-value")

    async def run():
        await _authenticate_system(Server(), Peer())

    asyncio.run(run())
