"""Disposable wire proofs for connection diagnostics, never a live daemon.

These tests deliberately require ordinary loopback sockets. A restricted runner
must report its socket denial and defer execution; it must not silently skip.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import datetime
import hashlib
import json
import logging
import re
import ssl
import uuid

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from _shared import operator_auth
from server import CLIENT_SEND_QUEUE_MAX, Server
from sessions import Sessions
from store import STREAM_TOKEN_HASH_VERSION, Store

LOGGER = "chat_streamd_v2.server"
SEAT = "fixture:socket-seat"
TOKEN = "synthetic-socket-seat-token-private"
BUCKETS = {
    "snapshot", "session.inventory", "work_lanes.inventory", "host.status",
    "working.state", "schedule.inventory", "hosts.stats", "limits.update",
    "chat.event", "pong", "other",
}
COUNTERS = {
    "broadcast_enqueued", "broadcast_sent", "broadcast_sent_bytes", "direct_sent",
    "direct_sent_bytes", "coalesced", "deduped",
}
COMMON = {
    "schema", "event", "conn_id", "age_ms", "transport", "tls", "client_kind",
    "client_name", "client_metadata_source", "app_build", "stream_id",
}
QUEUE = {"queue_depth", "queue_max", "queue_peak", "queued_by_type"}
TIMING = {
    "last_rx_age_ms", "last_tx_age_ms", "last_ping_age_ms", "last_pong_age_ms",
    "send_lock_wait_max_ms", "send_call_max_ms",
}
AUTH = {"auth_method", "auth_stage", "auth_elapsed_ms", "auth_suppressed"}
EVENT_FIELDS = {
    "connect": {"queue_max"},
    "auth_ok": AUTH | {"events_mode", "snapshot_requested", "work_lanes_v1"},
    "auth_fail": AUTH | {"reason"},
    "slow_consumer": QUEUE | TIMING | {
        "traffic", "phase", "episode", "episode_ms", "pressure_episodes_suppressed",
    },
    "force_close": QUEUE | TIMING | {
        "traffic", "initiator", "cause", "cause_source", "close_code", "close_reason",
    },
    "close": QUEUE | TIMING | {
        "traffic", "initiator", "termination", "close_code", "close_reason",
        "close_sent_code", "close_received_code", "close_sent_reason",
        "close_received_reason", "duration_ms", "auth_state", "rx_messages",
        "rx_bytes", "tx_messages", "tx_bytes", "queue_snapshot_age_ms",
        "auth_suppressed_total", "pressure_episodes_suppressed_total",
    },
}


def _uint(value):
    assert type(value) is int and value >= 0


def _records(caplog):
    result = []
    for record in caplog.records:
        if not record.getMessage().startswith("conn_diag "):
            continue
        assert record.name == LOGGER and record.exc_info is None
        text = record.getMessage()
        assert "\n" not in text and "\r" not in text
        event = json.loads(text[len("conn_diag "):])
        assert text[len("conn_diag "):] == json.dumps(event, separators=(",", ":"), allow_nan=False)
        assert event["event"] in EVENT_FIELDS
        assert set(event) == COMMON | EVENT_FIELDS[event["event"]]
        assert type(event["schema"]) is int and event["schema"] == 1
        assert re.fullmatch(r"[0-9a-f]{32}", event["conn_id"])
        assert uuid.UUID(hex=event["conn_id"]).version == 4
        _uint(event["age_ms"])
        assert event["transport"] in {"unix", "loopback", "tailnet", "other", "unknown"}
        assert type(event["tls"]) is bool
        assert event["client_kind"] in {"mobile", "web", "desktop", "cli", "service", "unknown"}
        assert event["client_name"] in {
            "pentacle-mobile", "pentacle-web", "pentacle", "agent-orch", "system-producer", "unknown",
        }
        assert event["client_metadata_source"] in {"unavailable", "hello_claim", "verified_credential"}
        if event["stream_id"] is not None:
            assert re.fullmatch(r"[A-Za-z0-9._-]+:[A-Za-z0-9._-]+", event["stream_id"])
            assert len(event["stream_id"]) <= 128
        if event["app_build"] is not None:
            assert type(event["app_build"]) is str
            assert re.fullmatch(r"(?:[0-9a-f]{7,40}|[0-9]{1,10}|[0-9]{1,10}\.[0-9]{1,10}\.[0-9]{1,10}(?:\+[0-9]{1,10})?)", event["app_build"])
        if event["event"] == "connect":
            _uint(event["queue_max"])
            assert event["age_ms"] == 0
            assert event["client_kind"] == event["client_name"] == "unknown"
            assert event["client_metadata_source"] == "unavailable"
            assert event["app_build"] is None and event["stream_id"] is None
        if event["event"].startswith("auth_"):
            assert event["auth_method"] in {"operator_v2", "scoped_v2", "seat_token", "system_token", "local_admin", "loopback", "none"}
            assert event["auth_stage"] in {"hello", "request", "revalidation"}
            _uint(event["auth_elapsed_ms"])
            _uint(event["auth_suppressed"])
            if event["event"] == "auth_ok":
                assert event["events_mode"] in {"full", "summary", "unknown"}
                assert event["snapshot_requested"] is None or type(event["snapshot_requested"]) is bool
                assert event["work_lanes_v1"] is None or type(event["work_lanes_v1"]) is bool
            else:
                assert event["reason"] in {
                    "absent", "malformed", "expired", "wrong-seat", "internal-error",
                    "operator_auth_invalid", "authentication_required", "operator_auth_required",
                    "system_producer_auth_required", "revoked", "tls_required",
                }
        if event["event"] == "slow_consumer":
            assert event["phase"] in {"enter", "recover"}
            for key in ("episode", "episode_ms", "pressure_episodes_suppressed"):
                _uint(event[key])
        if event["event"] in {"close", "force_close"}:
            assert event["initiator"] in ({"server", "peer", "unknown"} if event["event"] == "close" else {"server", "peer"})
            for key in ("close_code", "close_sent_code", "close_received_code"):
                if key in event:
                    assert event[key] is None or type(event[key]) is int
            for key in ("close_reason", "close_sent_reason", "close_received_reason"):
                if key in event:
                    assert event[key] in {
                        "empty", "normal", "going_away", "slow_consumer", "liveness_force_close",
                        "focused_heartbeat_timeout", "keepalive_ping_timeout", "redacted", "unknown",
                    }
        if event["event"] == "force_close":
            assert event["cause"] in {"slow_consumer", "liveness"}
            assert event["cause_source"] in {"server_policy", "peer_close_frame", "protocol_close"}
        if "traffic" in event:
            assert set(event["traffic"]) == BUCKETS
            assert set(event["queued_by_type"]) == BUCKETS
            for values in event["traffic"].values():
                assert set(values) == COUNTERS
                for value in values.values():
                    _uint(value)
            for value in event["queued_by_type"].values():
                _uint(value)
            assert sum(event["queued_by_type"].values()) == event["queue_depth"]
            for key in QUEUE - {"queued_by_type"}:
                _uint(event[key])
            for key in TIMING:
                if event[key] is not None:
                    _uint(event[key])
        expected_level = logging.WARNING if event["event"] in {"auth_fail", "force_close"} or (
            event["event"] == "slow_consumer" and event["phase"] == "enter"
        ) else logging.INFO
        assert record.levelno == expected_level
        if event["event"] == "close":
            assert event["termination"] in {"handshake", "transport_lost", "welcome_failed", "writer_failed", "handler_cancelled", "handler_error"}
            assert event["auth_state"] in {"never", "accepted", "failed"}
            for key in ("duration_ms", "rx_messages", "rx_bytes", "tx_messages", "tx_bytes", "queue_snapshot_age_ms", "auth_suppressed_total", "pressure_episodes_suppressed_total"):
                _uint(event[key])
            assert event["duration_ms"] == event["age_ms"]
            assert event["tx_messages"] == sum(
                v["direct_sent"] + v["broadcast_sent"] for v in event["traffic"].values()
            )
            assert event["tx_bytes"] == sum(
                v["direct_sent_bytes"] + v["broadcast_sent_bytes"] for v in event["traffic"].values()
            )
        result.append(event)
    return result


async def _until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.001)


def _self_signed(tmp_path):
    # Same small certificate boundary as test_dot_tls_transport_socket.py.
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "fixture.crt", tmp_path / "fixture.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    key_path.chmod(0o600)
    return str(cert_path), str(key_path)


@asynccontextmanager
async def _daemon(tmp_path, caplog, *, tls=False, dot=False):
    store = Store(str(tmp_path / "fixture.sqlite3"))
    store.start()
    server = None
    try:
        await store.open_session("fixture", "socket-seat", provider="codex", pane_status="pane_alive")
        await store.grant_stream_token(
            "fixture", "socket-seat", hashlib.sha256(TOKEN.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION,
        )
        sessions = Sessions(store, local_host="fixture")
        await sessions.refresh()
        options = {}
        if tls:
            cert, key = _self_signed(tmp_path)
            options.update(dot_tls_port=0, dot_tls_cert=cert, dot_tls_key=key, dot_tls_binds=["127.0.0.1"])
        server = Server(
            host="127.0.0.1", port=0, store=store, sessions=sessions,
            local_host="fixture", dot_principal_stream_ids=[SEAT] if dot else [], **options,
        )
        server.operator_credential_registry = operator_auth.OperatorCredentialRegistry(tmp_path / "credentials.json")
        server.operator_credential_registry.initialize()
        await server.bind()
        # The contract concerns accepted connections, not startup's bind log.
        # Capture ALL logger output during each connection, including libraries;
        # don't enable websocket frame-dump DEBUG (which logs payloads by design).
        caplog.set_level(logging.INFO)
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        caplog.set_level(logging.INFO, logger="websockets")
        caplog.clear()
        yield server
    finally:
        try:
            if server is not None:
                await server.close()
        finally:
            store.stop()
        if server is not None:
            assert not server._clients
            assert not server._client_send_queues
            assert not server._client_writer_tasks
            assert not server._connection_trust
            assert not server._tls_connections
            assert not getattr(server, "_connection_diagnostics", {})


@dataclass
class _Wire:
    ws: object
    peer: object
    welcome: dict
    received: list = field(default_factory=list)
    sent: list = field(default_factory=list)

    async def send(self, frame):
        raw = frame if isinstance(frame, (str, bytes)) else json.dumps(frame, ensure_ascii=False)
        self.sent.append(raw)
        await self.ws.send(raw)

    async def recv(self):
        raw = await asyncio.wait_for(self.ws.recv(), 5)
        self.received.append(raw)
        return json.loads(raw)

    async def rpc(self, verb, **fields):
        request_id = fields.pop("request_id", "fixture-" + str(len(self.sent)))
        await self.send({"type": verb, "request_id": request_id, **fields})
        while True:
            frame = await self.recv()
            if frame.get("request_id") == request_id:
                return frame

    async def hello(self, *, envelope=None, **fields):
        hello = {"type": "hello", "client": "pentacle", **fields}
        if envelope is not None:
            identity = operator_auth.decode_envelope(envelope)
            hello["client"] = identity["client_kind"]
            hello["auth_v2"] = {
                "scheme": operator_auth.AUTH_SCHEME,
                "credential_id": identity["credential_id"],
                "proof": operator_auth.make_proof(
                    identity["proof_key"], self.welcome["auth"]["operator"]["nonce"],
                    identity["credential_id"], identity["client_kind"],
                ),
            }
        await self.send(hello)
        frames = []
        while True:
            frame = await self.recv()
            frames.append(frame)
            if frame["type"] in {"snapshot", "ready", "hello.error"}:
                return frames


@asynccontextmanager
async def _connection(server, *, tls=False, **kwargs):
    context = None
    port = server.port
    if tls:
        port = server.dot_tls_port
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    async with connect(
        f"{'wss' if tls else 'ws'}://127.0.0.1:{port}", ssl=context,
        ping_interval=None, **kwargs,
    ) as ws:
        raw = await asyncio.wait_for(ws.recv(), 5)
        welcome = json.loads(raw)
        assert welcome["type"] == "welcome"
        peer = next(p for p in server._clients if p.remote_address[1] == ws.local_address[1])
        wire = _Wire(ws, peer, welcome, [raw])
        yield wire
    await _until(lambda: peer not in server._clients)
    await asyncio.sleep(0)


def _lifecycle(caplog, conn_id=None):
    records = _records(caplog)
    if conn_id is not None:
        records = [r for r in records if r["conn_id"] == conn_id]
    connects = [r for r in records if r["event"] == "connect"]
    assert len(connects) == 1, "each accepted wire connection must log one connect before its welcome"
    assert records[0] is connects[0]
    closes = [r for r in records if r["event"] == "close"]
    assert len(closes) == 1, "handler termination must produce exactly one terminal close"
    assert records[-1] is closes[0]
    assert {r["conn_id"] for r in records} == {connects[0]["conn_id"]}
    return records, closes[0]


@pytest.mark.parametrize("code", [1000, 1001])
def test_loopback_lifecycle_reconnect_and_normal_close(tmp_path, caplog, code):
    async def run():
        ids = []
        async with _daemon(tmp_path, caplog) as server:
            for _ in range(2):
                start = len(_records(caplog))
                async with _connection(server) as wire:
                    assert not [r for r in _records(caplog)[start:] if r["event"] == "auth_ok"]
                    frames = await wire.hello(subscribe={"snapshot": False, "mode": "rpc"}, build_sha="abcdef123")
                    assert frames == [{"type": "ready", "snapshot": False, "events_mode": "full"}]
                    assert (await wire.rpc("ping"))["type"] == "pong"
                    await wire.ws.close(code=code)
                assert not getattr(server, "_connection_diagnostics", {})
                records = _records(caplog)[start:]
                assert records, "accepted connections require structured lifecycle diagnostics"
                events, closed = _lifecycle(caplog, records[0]["conn_id"])
                ids.append(closed["conn_id"])
                assert [e["event"] for e in events] == ["connect", "auth_ok", "close"]
                assert events[0]["age_ms"] == 0
                assert events[0]["client_kind"] == "unknown" and events[0]["app_build"] is None
                assert events[1]["auth_method"] == "loopback"
                assert events[1]["client_kind"] == "desktop"
                assert events[1]["client_metadata_source"] == "hello_claim"
                assert events[1]["app_build"] == "abcdef123"
                assert closed["transport"] == "loopback" and closed["tls"] is False
                assert closed["initiator"] == "peer" and closed["termination"] == "handshake"
                assert closed["close_code"] == closed["close_sent_code"] == closed["close_received_code"] == code
                assert closed["close_reason"] == "empty" and closed["auth_state"] == "accepted"
            assert len(set(ids)) == 2
    asyncio.run(run())


@pytest.mark.parametrize("identity", ["mobile", "desktop", "seat", "scoped", "service", "anonymous"])
def test_real_authentication_matrix(tmp_path, caplog, monkeypatch, identity):
    async def run():
        async with _daemon(tmp_path, caplog) as server:
            envelope = None
            fields = {"subscribe": {"snapshot": False, "mode": "rpc", "events_mode": "summary"}}
            expected_method, expected_kind, expected_stream = "loopback", "unknown", None
            if identity in {"mobile", "desktop", "scoped"}:
                kind = "pentacle" if identity == "desktop" else "pentacle-mobile"
                scope = {"stream": SEAT} if identity == "scoped" else None
                _, envelope = server.operator_credential_registry.issue(kind, label="socket fixture", scope=scope)
                expected_method = "scoped_v2" if scope else "operator_v2"
                expected_kind = "desktop" if identity == "desktop" else "mobile"
                expected_stream = SEAT if scope else None
            elif identity == "seat":
                fields.update(client="agent-orch", from_stream_id=SEAT, stream_token=TOKEN)
                expected_method, expected_kind, expected_stream = "seat_token", "cli", SEAT
            elif identity == "service":
                path = tmp_path / "service-token"
                path.write_text("synthetic-private-service-token")
                path.chmod(0o600)
                monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN_FILE", str(path))
                monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_ID", "altum-bot-cd")
                fields.update(client="untrusted-service-name", from_stream_id="altum-bot-cd", stream_token=path.read_text())
                expected_method, expected_kind = "system_token", "service"
            else:
                fields["client"] = "unrecognized-client"
            async with _connection(server) as wire:
                assert not [r for r in _records(caplog) if r["event"].startswith("auth_")]
                frames = await wire.hello(envelope=envelope, app_build="1.2.3+7", **fields)
                if identity == "scoped":
                    assert [f["type"] for f in frames] == ["hello", "snapshot"]
                    assert frames[-1]["sessions"] == [] and frames[-1]["hosts"] == {}
                else:
                    assert frames == [{"type": "ready", "snapshot": False, "events_mode": "summary"}]
                # A service's existing restrictions are unchanged; a denied
                # verb is authorization, not failed credential verification.
                for _ in range(3):
                    reply = await wire.rpc("ping")
                    if identity == "service":
                        assert reply["error_code"] == "system_producer_forbidden"
                    else:
                        assert reply["type"] == "pong"
            records, closed = _lifecycle(caplog)
            auth = [r for r in records if r["event"].startswith("auth_")]
            assert len(auth) == 1 and auth[0]["event"] == "auth_ok"
            assert auth[0]["auth_method"] == expected_method and auth[0]["auth_stage"] == "hello"
            assert auth[0]["client_kind"] == expected_kind and auth[0]["stream_id"] == expected_stream
            expected_name = {"mobile": "pentacle-mobile", "desktop": "pentacle", "seat": "agent-orch", "scoped": "pentacle-mobile", "service": "system-producer", "anonymous": "unknown"}[identity]
            assert auth[0]["client_name"] == expected_name
            assert auth[0]["client_metadata_source"] == ("verified_credential" if identity in {"mobile", "desktop", "scoped", "service"} else "hello_claim")
            assert auth[0]["events_mode"] == "summary" and auth[0]["snapshot_requested"] is False
            assert auth[0]["work_lanes_v1"] is False
            assert closed["app_build"] == "1.2.3+7" and closed["auth_state"] == "accepted"
    asyncio.run(run())


@pytest.mark.parametrize("seat_request", [False, True])
def test_prehello_disconnect_and_seat_request_without_hello(tmp_path, caplog, seat_request):
    async def run():
        async with _daemon(tmp_path, caplog) as server:
            async with _connection(server) as wire:
                for _ in range(3):
                    fields = {"from_stream_id": SEAT, "stream_token": TOKEN} if seat_request else {}
                    assert (await wire.rpc("ping", **fields))["type"] == "pong"
            records, closed = _lifecycle(caplog)
            auth = [r for r in records if r["event"] == "auth_ok"]
            if seat_request:
                assert len(auth) == 1 and auth[0]["auth_method"] == "seat_token"
                assert auth[0]["auth_stage"] == "request" and auth[0]["stream_id"] == SEAT
                assert auth[0]["snapshot_requested"] is None and auth[0]["work_lanes_v1"] is None
                assert closed["auth_state"] == "accepted"
            else:
                assert [r["event"] for r in records] == ["connect", "close"]
                assert closed["auth_state"] == "never"
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["absent", "malformed", "expired", "wrong-seat", "operator_auth_invalid"])
def test_explicit_invalid_credentials_remain_observable_on_loopback(tmp_path, caplog, failure):
    async def run():
        async with _daemon(tmp_path, caplog) as server:
            async with _connection(server) as wire:
                if failure == "operator_auth_invalid":
                    cid, envelope = server.operator_credential_registry.issue("pentacle-mobile", label="invalid-proof fixture")
                    identity = operator_auth.decode_envelope(envelope)
                    bad_proof = operator_auth.make_proof(identity["proof_key"], operator_auth.new_nonce()[0], cid, "pentacle-mobile")
                    result = await wire.hello(client="pentacle-mobile", auth_v2={
                        "scheme": operator_auth.AUTH_SCHEME, "credential_id": cid, "proof": bad_proof,
                    })
                    assert result == [{"type": "hello.error", "error_code": failure}]
                    # Logging rejection must not invent a disconnect.
                    assert (await wire.rpc("ping"))["type"] == "pong"
                else:
                    fields = {"from_stream_id": SEAT}
                    if failure == "malformed":
                        fields["stream_token"] = {"synthetic-private": "not a token"}
                    elif failure == "expired":
                        fields["stream_token"] = "synthetic-unrecognized-token"
                    elif failure == "wrong-seat":
                        fields.update(stream_token=TOKEN, from_stream_id="unverified:private-claim")
                    # Existing loopback ping exemption still permits the RPC.
                    assert (await wire.rpc("ping", **fields))["type"] == "pong"
            records, closed = _lifecycle(caplog)
            failures = [r for r in records if r["event"] == "auth_fail"]
            assert len(failures) == 1 and failures[0]["reason"] == failure
            assert failures[0]["stream_id"] is None
            assert not [r for r in records if r["event"] == "auth_ok"]
            assert closed["auth_state"] == "failed"
    asyncio.run(run())


def test_scoped_revocation_and_authorization_denial_are_distinct(tmp_path, caplog):
    async def run():
        async with _daemon(tmp_path, caplog) as server:
            cid, envelope = server.operator_credential_registry.issue("pentacle-mobile", label="revocation fixture", scope={"stream": SEAT})
            async with _connection(server) as wire:
                assert (await wire.hello(envelope=envelope))[-1]["type"] == "snapshot"
                assert (await wire.rpc("list_sessions"))["error_code"] == "scope_denied"
                assert not [r for r in _records(caplog) if r["event"] == "auth_fail"]
                server.operator_credential_registry.revoke(cid)
                for _ in range(3):
                    assert (await wire.rpc("ping"))["error_code"] == "authentication_required"
            records, closed = _lifecycle(caplog)
            failures = [r for r in records if r["event"] == "auth_fail"]
            assert len(failures) == 1 and failures[0]["reason"] == "revoked"
            assert failures[0]["stream_id"] == SEAT and closed["auth_state"] == "failed"
    asyncio.run(run())


def test_fixed_file_service_rotation_revokes_bound_connection(tmp_path, caplog, monkeypatch):
    async def run():
        path = tmp_path / "service-private-token"
        path.write_text("synthetic-private-bot-service-token")
        path.chmod(0o600)
        monkeypatch.setenv("PENTACLE_BOT_MESSAGING_STREAM_TOKEN_FILE", str(path))
        async with _daemon(tmp_path, caplog) as server:
            async with _connection(server) as wire:
                frames = await wire.hello(from_stream_id="bot-messaging-desk", stream_token=path.read_text(), subscribe={"snapshot": False, "mode": "rpc"})
                assert frames[0]["type"] == "ready"
                path.write_text("synthetic-rotated-service-token")
                for _ in range(3):
                    assert (await wire.rpc("ping"))["error_code"] == "system_producer_auth_required"
            records, closed = _lifecycle(caplog)
            assert [r["auth_method"] for r in records if r["event"] == "auth_ok"] == ["system_token"]
            failure = [r for r in records if r["event"] == "auth_fail"]
            assert len(failure) == 1 and failure[0]["auth_method"] == "system_token"
            assert failure[0]["reason"] in {"revoked", "wrong-seat"}
            assert closed["stream_id"] is None and closed["client_name"] == "system-producer"
    asyncio.run(run())


@pytest.mark.parametrize("tls", [False, True])
def test_real_tls_fact_and_plain_transport_refusal(tmp_path, caplog, tls):
    async def run():
        async with _daemon(tmp_path, caplog, tls=True, dot=True) as server:
            async with _connection(server, tls=tls) as wire:
                if tls:
                    frames = await wire.hello(client="agent-orch", from_stream_id=SEAT, stream_token=TOKEN)
                    assert [f["type"] for f in frames] == ["hello", "snapshot"]
                    assert frames[-1]["sessions"] == [] and frames[-1]["hosts"] == {}
                    assert (await wire.rpc("list_sessions"))["error_code"] == "dot_scope_denied"
                else:
                    reply = await wire.rpc("list_sessions", from_stream_id=SEAT, stream_token=TOKEN, transport_tls=True)
                    assert reply["error_code"] == "external_requires_tls"
            records, closed = _lifecycle(caplog)
            assert all(r["tls"] is tls and r["transport"] == "loopback" for r in records)
            if tls:
                assert [r["auth_method"] for r in records if r["event"] == "auth_ok"] == ["seat_token"]
                assert not [r for r in records if r["event"] == "auth_fail"]
                assert closed["stream_id"] == SEAT
            else:
                assert [r["reason"] for r in records if r["event"] == "auth_fail"] == ["tls_required"]
    asyncio.run(run())


@pytest.mark.parametrize("reason,force", [
    ("liveness_force_close", True), ("focused_heartbeat_timeout", True),
    ("private-close-content\r\nforged-log-line", False), ("", False),
])
def test_peer_4000_requires_exact_liveness_evidence(tmp_path, caplog, reason, force):
    async def run():
        async with _daemon(tmp_path, caplog) as server:
            async with _connection(server) as wire:
                await wire.ws.close(code=4000, reason=reason)
            records, closed = _lifecycle(caplog)
            forced = [r for r in records if r["event"] == "force_close"]
            assert len(forced) == int(force)
            if force:
                assert forced[0]["initiator"] == "peer" and forced[0]["cause"] == "liveness"
                assert forced[0]["cause_source"] == "peer_close_frame"
                assert forced[0]["close_reason"] == reason
                assert records[-2] is forced[0]
            assert closed["close_code"] == closed["close_sent_code"] == closed["close_received_code"] == 4000
            assert closed["close_reason"] == (reason if force else "redacted" if reason else "empty")
            assert closed["initiator"] == "peer" and closed["termination"] == "handshake"
            if not force and reason:
                assert reason not in caplog.text and "private-close-content" not in caplog.text
    asyncio.run(run())


def test_client_abort_is_transport_loss_without_invented_close_frame(tmp_path, caplog):
    async def run():
        async with _daemon(tmp_path, caplog) as server:
            async with _connection(server) as wire:
                wire.ws.transport.abort()
                await wire.ws.wait_closed()
            records, closed = _lifecycle(caplog)
            assert closed["termination"] == "transport_lost"
            assert closed["close_code"] in {1006, None}
            assert closed["close_sent_code"] is None and closed["close_received_code"] is None
            assert closed["close_reason"] == "unknown" and closed["initiator"] == "unknown"
            assert not [r for r in records if r["event"] == "force_close"]
    asyncio.run(run())


def test_daemon_shutdown_records_observed_server_going_away(tmp_path, caplog):
    async def run():
        async with _daemon(tmp_path, caplog) as server:
            async with _connection(server) as wire:
                await server.close()
                await wire.ws.wait_closed()
                assert wire.ws.close_code == 1001
            records, closed = _lifecycle(caplog)
            assert closed["initiator"] == "server" and closed["termination"] == "handshake"
            assert closed["close_sent_code"] == closed["close_received_code"] == 1001
            assert closed["close_reason"] == "empty"
            assert not [r for r in records if r["event"] == "force_close"]
    asyncio.run(run())


def test_existing_protocol_keepalive_timeout_has_server_evidence(tmp_path, caplog):
    async def run():
        async with _daemon(tmp_path, caplog) as server:
            async with _connection(server) as wire:
                peer = wire.peer
                # Fixture-local settings of the installed websockets keepalive,
                # not a daemon watchdog or synthetic ConnectionClosed exception.
                peer.keepalive_task.cancel()
                await asyncio.gather(peer.keepalive_task, return_exceptions=True)
                peer.ping_interval, peer.ping_timeout, peer.close_timeout = 0.01, 0.02, 0.05
                wire.ws.transport.pause_reading()
                try:
                    peer.start_keepalive()
                    await asyncio.wait_for(peer.wait_closed(), 3)
                finally:
                    wire.ws.transport.resume_reading()
                await wire.ws.wait_closed()
            records, closed = _lifecycle(caplog)
            forced = [r for r in records if r["event"] == "force_close"]
            assert len(forced) == 1
            assert forced[0]["initiator"] == "server" and forced[0]["cause"] == "liveness"
            assert forced[0]["cause_source"] == "protocol_close"
            assert forced[0]["close_code"] == 1011 and forced[0]["close_reason"] == "keepalive_ping_timeout"
            assert closed["close_sent_code"] == 1011 and closed["close_sent_reason"] == "keepalive_ping_timeout"
            assert records[-2] is forced[0]
    asyncio.run(run())


def _bytes(frames):
    return sum(len(raw) if isinstance(raw, bytes) else len(raw.encode("utf-8")) for raw in frames)


def test_wire_accounting_capability_filter_summary_and_delivered_dedup(tmp_path, caplog):
    async def run():
        wires = []
        async with _daemon(tmp_path, caplog) as server:
            async with _connection(server) as full, _connection(server) as summary:
                wires = [full, summary]
                for wire, capable, mode in ((full, True, "full"), (summary, False, "summary")):
                    frames = await wire.hello(
                        capabilities={"work_lanes_v1": capable},
                        subscribe={"snapshot": False, "events_mode": mode, "exclude_event_types": ["hosts.stats"]},
                    )
                    assert frames == [{"type": "ready", "snapshot": False, "events_mode": mode}]
                    await wire.send(b"{invalid-json")
                    assert (await wire.recv())["error_code"] == "bad_json"
                inventory = {"type": "session.inventory", "sessions": [{
                    "stream_id": SEAT, "host": "fixture", "session_name": "socket-seat",
                    "visibility": "visible", "private_full_only": "large private field",
                }]}
                lane = {"type": "work_lanes.inventory", "work_lanes": []}
                event = {"type": "chat.event", "event": {"stream_id": SEAT, "text": "héllø 🍵"}}
                await server.broadcast(inventory)
                await server.broadcast(lane)
                await server.broadcast(event)
                full_frames = [await full.recv() for _ in range(3)]
                summary_frames = [await summary.recv() for _ in range(2)]
                assert [f["type"] for f in full_frames] == ["session.inventory", "work_lanes.inventory", "chat.event"]
                assert [f["type"] for f in summary_frames] == ["session.inventory", "chat.event"]
                assert "private_full_only" in full_frames[0]["sessions"][0]
                assert "private_full_only" not in summary_frames[0]["sessions"][0]
                assert full_frames[-1] == summary_frames[-1] == event
                for wire in wires:
                    assert (await wire.rpc("ping", request_id="unicode-é-🍵"))["type"] == "pong"
                await server.broadcast(inventory)
                # A ping reply is a wire barrier: no duplicate inventory may be
                # queued ahead of it. Work-lane filtering must remain per-client.
                for wire in wires:
                    before = len(wire.received)
                    assert (await wire.rpc("ping"))["type"] == "pong"
                    assert len(wire.received) == before + 1
            records = _records(caplog)
            closes = [r for r in records if r["event"] == "close"]
            assert len(closes) == 2
            for wire, mode, lane_count in ((full, "full", 1), (summary, "summary", 0)):
                auth = next(r for r in records if r["event"] == "auth_ok" and r["events_mode"] == mode)
                _, closed = _lifecycle(caplog, auth["conn_id"])
                assert closed["rx_messages"] == len(wire.sent)
                assert closed["rx_bytes"] == _bytes(wire.sent)
                assert closed["tx_messages"] == len(wire.received)
                assert closed["tx_bytes"] == _bytes(wire.received)
                traffic = closed["traffic"]
                assert traffic["session.inventory"]["broadcast_enqueued"] == 1
                assert traffic["session.inventory"]["broadcast_sent"] == 1
                assert traffic["session.inventory"]["deduped"] == 1
                assert traffic["work_lanes.inventory"]["broadcast_sent"] == lane_count
                assert traffic["work_lanes.inventory"]["broadcast_enqueued"] == lane_count
                assert traffic["chat.event"]["broadcast_sent"] == 1
                assert traffic["pong"]["direct_sent"] == 2
                assert closed["queue_depth"] == 0
                assert closed["last_ping_age_ms"] >= closed["last_pong_age_ms"] >= 0
    asyncio.run(run())


def test_bootstrap_counts_snapshot_once_and_no_auth_on_bad_json(tmp_path, caplog):
    async def run():
        async with _daemon(tmp_path, caplog) as server:
            async with _connection(server) as wire:
                await wire.send("{private-malformed-body")
                assert (await wire.recv()) == {"type": "protocol.error", "error_code": "bad_json"}
                assert not [r for r in _records(caplog) if r["event"].startswith("auth_")]
                frames = await wire.hello()
                assert [f["type"] for f in frames] == ["hello", "snapshot"]
                assert (await wire.rpc("ping"))["type"] == "pong"
            _, closed = _lifecycle(caplog)
            assert closed["rx_messages"] == len(wire.sent) and closed["rx_bytes"] == _bytes(wire.sent)
            assert closed["tx_messages"] == len(wire.received) and closed["tx_bytes"] == _bytes(wire.received)
            assert closed["traffic"]["snapshot"]["direct_sent"] == 1
            assert closed["traffic"]["hosts.stats"]["direct_sent"] == 1
            assert closed["traffic"]["work_lanes.inventory"]["direct_sent"] == 0
    asyncio.run(run())


async def _subscribe(wire):
    frames = await wire.hello(subscribe={"snapshot": False, "exclude_event_types": ["hosts.stats"]})
    assert frames == [{"type": "ready", "snapshot": False, "events_mode": "full"}]


def _chat(index):
    return {"type": "chat.event", "event": {"stream_id": SEAT, "text": f"private-append-only-{index}", "daemon_seq": index}}


@pytest.mark.parametrize("compressible", [False, True])
def test_actual_stalled_send_pressure_recovery_and_coalescing(tmp_path, caplog, compressible):
    async def run():
        async with _daemon(tmp_path, caplog) as server:
            async with _connection(server) as wire:
                await _subscribe(wire)
                peer = wire.peer
                assert server._client_send_queues[peer].maxsize == CLIENT_SEND_QUEUE_MAX == 256
                peer.pause_writing()
                try:
                    for index in range(205):
                        frame = {"type": "session.inventory", "sessions": [], "fixture_revision": index} if compressible else _chat(index)
                        await server.broadcast(frame)
                    queue = server._client_send_queues[peer]
                    assert peer.paused and not server._client_writer_tasks[peer].done()
                    assert queue.qsize() == (1 if compressible else 204)
                    events = [r for r in _records(caplog) if r["event"] == "slow_consumer"]
                    assert [r["phase"] for r in events] == (["enter", "recover"] if compressible else ["enter"])
                    assert events[0]["queue_depth"] == events[0]["queue_peak"] == 204
                    assert events[0]["queue_max"] == 256
                    assert events[0]["episode"] == 1
                    # The removed in-flight frame is not in Q and is not sent
                    # until the real websockets send/drain call completes.
                    bucket = "session.inventory" if compressible else "chat.event"
                    assert events[0]["queued_by_type"][bucket] == 204
                    assert events[0]["traffic"][bucket]["broadcast_sent"] == 0
                    await asyncio.sleep(0.02)
                finally:
                    if peer.paused:
                        peer.resume_writing()
                delivered = [await wire.recv() for _ in range(2 if compressible else 205)]
                if compressible:
                    assert [f["fixture_revision"] for f in delivered] == [0, 204]
                else:
                    assert [f["event"]["daemon_seq"] for f in delivered] == list(range(205))
                assert (await wire.rpc("ping"))["type"] == "pong"
            records, closed = _lifecycle(caplog)
            pressure = [r for r in records if r["event"] == "slow_consumer"]
            assert [r["phase"] for r in pressure] == ["enter", "recover"]
            assert pressure[1]["queue_depth"] <= 128 and pressure[1]["episode"] == 1
            assert not [r for r in records if r["event"] == "force_close"]
            bucket = "session.inventory" if compressible else "chat.event"
            assert closed["traffic"][bucket]["broadcast_enqueued"] == 205
            assert closed["traffic"][bucket]["broadcast_sent"] == (2 if compressible else 205)
            assert closed["traffic"][bucket]["coalesced"] == (203 if compressible else 0)
            assert closed["send_call_max_ms"] >= 15
            assert closed["queue_peak"] == 204 and closed["queue_depth"] == 0
    asyncio.run(run())


def test_stalled_reader_overflow_isolated_from_healthy_reader(tmp_path, caplog):
    async def run():
        async with _daemon(tmp_path, caplog) as server:
            async with _connection(server) as healthy, _connection(server) as stalled:
                await _subscribe(healthy)
                await _subscribe(stalled)
                peer = stalled.peer
                peer.pause_writing()
                count = 270
                async def read_healthy():
                    return [await healthy.recv() for _ in range(count)]
                reader = asyncio.create_task(read_healthy())
                try:
                    for index in range(count):
                        await server.broadcast(_chat(index))
                    assert peer not in server._clients
                    assert healthy.peer in server._clients
                    delivered = await reader
                    assert [f["event"]["daemon_seq"] for f in delivered] == list(range(count))
                    assert (await healthy.rpc("ping"))["type"] == "pong"
                finally:
                    if peer.paused:
                        peer.resume_writing()
                    if not reader.done():
                        reader.cancel()
                        await asyncio.gather(reader, return_exceptions=True)
                with pytest.raises(ConnectionClosed):
                    while True:
                        await stalled.recv()
                assert stalled.ws.close_code == 1011 and stalled.ws.close_reason == "slow_consumer"
            records = _records(caplog)
            forced = [r for r in records if r["event"] == "force_close"]
            assert len(forced) == 1
            force = forced[0]
            events, closed = _lifecycle(caplog, force["conn_id"])
            assert force["initiator"] == "server" and force["cause"] == "slow_consumer"
            assert force["cause_source"] == "server_policy" and force["close_code"] == 1011
            assert force["close_reason"] == "slow_consumer"
            assert force["queue_depth"] == force["queue_max"] == force["queue_peak"] == 256
            assert force["traffic"]["chat.event"]["broadcast_enqueued"] == 257
            assert force["traffic"]["chat.event"]["broadcast_sent"] == 0
            assert closed["close_sent_code"] == closed["close_received_code"] == 1011
            assert closed["queue_depth"] == 256 and closed["queue_snapshot_age_ms"] >= 0
            assert [r["phase"] for r in events if r["event"] == "slow_consumer"] == ["enter"]
            assert events[-2] == force
            healthy_close = next(r for r in records if r["event"] == "close" and r["conn_id"] != force["conn_id"])
            assert healthy_close["traffic"]["chat.event"]["broadcast_sent"] == count
            assert healthy_close["close_code"] == 1000
    asyncio.run(run())


def test_socket_privacy_captures_all_loggers_not_only_diagnostics(tmp_path, caplog, monkeypatch):
    async def run():
        sentinels = {
            "token": "synthetic-PRIVATE-token-sentinel",
            "legacy": "synthetic-PRIVATE-legacy-token",
            "admin": "synthetic-PRIVATE-admin-token",
            "device": "synthetic-PRIVATE-device-UUID",
            "body": "synthetic-PRIVATE-payload-π",
            "path": "/private/synthetic-do-not-log/file.txt",
            "contents": "synthetic-PRIVATE-file-contents",
            "client": "synthetic-PRIVATE-client\r\nforged-record",
            "claim": "unverified:PRIVATE-seat-claim",
            "request": "synthetic-PRIVATE-request-id",
            "verb": "synthetic-PRIVATE-unknown-verb",
            "close": "synthetic-PRIVATE-close\r\nforged-record",
            "ip": "198.51.100.211",
        }
        sentinels["hash"] = hashlib.sha256(sentinels["token"].encode()).hexdigest()
        async with _daemon(tmp_path, caplog) as server:
            async with _connection(server) as wire:
                sentinels["nonce"] = wire.welcome["auth"]["operator"]["nonce"]
                sentinels["peer"] = str(wire.peer.remote_address[0])
                sentinels["peer_address"] = repr(wire.peer.remote_address)
                cid, envelope = server.operator_credential_registry.issue("pentacle-mobile", label="privacy fixture")
                identity = operator_auth.decode_envelope(envelope)
                sentinels["credential"] = cid
                sentinels["secret"] = operator_auth.encode_b64url(identity["proof_key"])
                sentinels["proof"] = operator_auth.make_proof(identity["proof_key"], sentinels["nonce"], cid, "pentacle-mobile")
                frames = await wire.hello(
                    client=sentinels["client"], build_sha=sentinels["hash"], app_build=sentinels["token"],
                    build_number={"private": sentinels["device"]}, stream_token=sentinels["token"],
                    token=sentinels["legacy"], local_admin_token=sentinels["admin"],
                    from_stream_id=sentinels["claim"], request_id=sentinels["request"],
                    auth_v2={"scheme": operator_auth.AUTH_SCHEME, "credential_id": cid, "proof": sentinels["proof"]},
                    body=sentinels["body"], path=sentinels["path"], contents=sentinels["contents"],
                    device_id=sentinels["device"], peer=sentinels["ip"],
                )
                assert frames[-1]["error_code"] == "operator_auth_invalid"
                result = await wire.rpc(sentinels["verb"], request_id=sentinels["request"], text=sentinels["body"])
                assert result["error_code"] == "unsupported_in_v2"
                await wire.send("{" + sentinels["body"])
                assert (await wire.recv())["error_code"] == "bad_json"
                def broken_sessions():
                    raise RuntimeError(sentinels["contents"] + sentinels["path"])
                monkeypatch.setattr(server.sessions, "list_open", broken_sessions)
                assert (await wire.rpc("list_sessions", request_id=sentinels["request"]))["error_code"] == "internal_error"
                await wire.ws.close(code=4000, reason=sentinels["close"])
            all_output = caplog.text
            for name, value in sentinels.items():
                assert value not in all_output, f"{name} escaped into captured logger output"
            records, closed = _lifecycle(caplog)
            assert closed["client_name"] == closed["client_kind"] == "unknown"
            assert closed["app_build"] is None and closed["stream_id"] is None
            assert closed["close_reason"] == "redacted"
            assert not [r for r in records if r["event"] == "force_close"]
    asyncio.run(run())
