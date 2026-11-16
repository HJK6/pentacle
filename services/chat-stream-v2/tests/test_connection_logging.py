"""Frozen conn_diag contract through real Server/Store hooks, without listeners.

The transport below controls only receive/send boundaries. Credentials are issued,
verified and revoked by the production registry and temporary Store APIs. No test
invokes an emitter as its subject or replaces an authentication decision.
"""
from __future__ import annotations

import asyncio
import ipaddress
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import socket as socket_module
from types import SimpleNamespace
import uuid

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from _shared import operator_auth
import local_admin
import server as server_module
from server import Server, _EncodedFrame
from sessions import Sessions
from store import STREAM_TOKEN_HASH_VERSION, Store


LOGGER = "chat_streamd_v2.server"
BUCKETS = {
    "snapshot", "session.inventory", "work_lanes.inventory", "host.status",
    "working.state", "schedule.inventory", "hosts.stats", "limits.update",
    "chat.event", "pong", "other",
}
COUNTERS = {
    "broadcast_enqueued", "broadcast_sent", "broadcast_sent_bytes", "direct_sent",
    "direct_sent_bytes", "coalesced", "deduped",
}
_END = object()


class ScriptedSocket:
    """No sockets or daemon policy: just an ordered, controllable wire boundary."""

    def __init__(self, address="127.0.0.1"):
        self.remote_address = (address, 45123) if address is not None else None
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.sent = []
        self.received = []
        self.send_entered = asyncio.Event()
        self.send_gate = None
        self.send_error = None
        self.protocol = SimpleNamespace(close_sent=None, close_rcvd=None,
                                        close_rcvd_then_sent=None)
        self.close_code = None
        self.close_calls = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is _END:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        self.received.append(item)
        return item

    async def send(self, payload):
        self.send_entered.set()
        if self.send_gate is not None:
            await self.send_gate.wait()
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(payload)
        self.outgoing.put_nowait(payload)

    async def close(self, *, code, reason):
        self.close_calls.append((code, reason))

    def feed(self, frame):
        raw = frame if isinstance(frame, (str, bytes)) else json.dumps(frame, ensure_ascii=False)
        self.incoming.put_nowait(raw)
        return raw

    def finish(self, *, sent=None, received=None, order=None, error=None, terminal=None):
        self.protocol.close_sent = sent
        self.protocol.close_rcvd = received
        self.protocol.close_rcvd_then_sent = order
        self.close_code = terminal
        self.incoming.put_nowait(error if error is not None else _END)

    async def receive(self, *, kind=None, request_id=None):
        async with asyncio.timeout(5):
            while True:
                frame = json.loads(await self.outgoing.get())
                if ((kind is None or frame.get("type") == kind)
                        and (request_id is None or frame.get("request_id") == request_id)):
                    return frame

    async def rpc(self, frame, *, kind=None):
        frame = dict(frame)
        frame.setdefault("request_id", uuid.uuid4().hex)
        self.feed(frame)
        return await self.receive(kind=kind, request_id=frame["request_id"])


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    # Baseline lacks the diagnostic clock alias; installing the clock must not
    # turn the meaningful missing-event RED into a missing-attribute error.
    monkeypatch.setattr(server_module, "_monotonic", clock, raising=False)
    return clock


@pytest.fixture
def daemon(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    store = Store(str(tmp_path / "fixture.sqlite3"))
    store.start()
    sessions = Sessions(store, local_host="fixture")
    server = Server(store=store, sessions=sessions, local_host="fixture")
    server.operator_credential_registry = operator_auth.OperatorCredentialRegistry(tmp_path / "operators.json")
    try:
        yield server
    finally:
        store.stop()


@asynccontextmanager
async def connected(daemon, socket=None, *, tls=False):
    socket = socket or ScriptedSocket()
    callback = daemon._handle_tls_client if tls else daemon._handle_client
    task = asyncio.create_task(callback(socket))
    try:
        welcome = await socket.receive(kind="welcome")
        yield socket, welcome, task
    finally:
        if not task.done():
            socket.finish()
        await task
        # A callback already scheduled at successful teardown may still discard
        # its completed request; give that existing callback one event-loop turn.
        await asyncio.sleep(0)


def records(caplog, event=None):
    parsed = []
    for record in caplog.records:
        message = record.getMessage()
        if record.name == LOGGER and message.startswith("conn_diag "):
            assert "\n" not in message and "\r" not in message
            obj = json.loads(message.removeprefix("conn_diag "))
            if event is None or obj["event"] == event:
                parsed.append(obj)
    return parsed


def one(caplog, event):
    events = records(caplog, event)
    assert len(events) == 1, f"expected exactly one {event} conn_diag, found {events}"
    return events[0]


def auth_records(caplog):
    return [record for record in records(caplog) if record["event"] in {"auth_ok", "auth_fail"}]


def assert_accounting(close):
    assert set(close["traffic"]) == BUCKETS
    assert set(close["queued_by_type"]) == BUCKETS
    assert sum(close["queued_by_type"].values()) == close["queue_depth"]
    for bucket in close["traffic"].values():
        assert set(bucket) == COUNTERS
        assert all(type(value) is int and value >= 0 for value in bucket.values())
    assert close["tx_messages"] == sum(
        bucket["direct_sent"] + bucket["broadcast_sent"] for bucket in close["traffic"].values()
    )
    assert close["tx_bytes"] == sum(
        bucket["direct_sent_bytes"] + bucket["broadcast_sent_bytes"] for bucket in close["traffic"].values()
    )
    assert close["duration_ms"] == close["age_ms"]


def assert_released(daemon, socket):
    assert socket not in daemon._clients
    assert socket not in daemon._client_send_queues
    assert socket not in daemon._client_send_locks
    assert socket not in daemon._client_writer_tasks
    assert socket not in daemon._connection_diagnostics


async def issue_seat(daemon, *, token="synthetic-seat-token", seat="seat"):
    assert await daemon.store.open_session("fixture", seat, provider="codex", pane_status="pane_alive")
    assert await daemon.store.grant_stream_token(
        "fixture", seat, hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION,
    ) == "ok"
    return token, f"fixture:{seat}"


def signed_hello(daemon, welcome, *, client="pentacle", scope=None):
    credential_id, envelope = daemon.operator_credential_registry.issue(client, scope=scope)
    identity = operator_auth.decode_envelope(envelope)
    message = {
        "type": "hello", "client": client,
        "subscribe": {"mode": "rpc", "snapshot": False, "events_mode": "summary"},
        "capabilities": {"work_lanes_v1": True},
        "auth_v2": {
            "scheme": operator_auth.AUTH_SCHEME, "credential_id": credential_id,
            "proof": operator_auth.make_proof(identity["proof_key"],
                welcome["auth"]["operator"]["nonce"], credential_id, client),
        },
    }
    return credential_id, message


def test_acceptance_prehello_disconnect_reconnect_and_idempotent_registration(daemon, caplog):
    async def run():
        for _ in range(3):
            async with connected(daemon) as (socket, welcome, task):
                assert welcome["type"] == "welcome"
                daemon._register_client(socket)
                daemon._register_client(socket)
                current = records(caplog, "connect")[-1:]
                assert current, "acceptance did not emit connect before welcome"
                assert current[0]["age_ms"] == 0
                assert current[0]["client_metadata_source"] == "unavailable"
                assert current[0]["client_name"] == current[0]["client_kind"] == "unknown"
                assert current[0]["app_build"] is current[0]["stream_id"] is None
                assert current[0]["transport"] == "loopback"
                assert current[0]["tls"] is False
                socket.finish(sent=Close(1000, ""), received=Close(1000, ""), order=True)
                await task
            assert_released(daemon, socket)
        events = records(caplog)
        assert [event["event"] for event in events] == ["connect", "close"] * 3
        identifiers = [event["conn_id"] for event in events[::2]]
        assert len(set(identifiers)) == 3
        for identifier in identifiers:
            assert uuid.UUID(hex=identifier).version == 4 and len(identifier) == 32
        for connect, close in zip(events[::2], events[1::2]):
            assert connect["conn_id"] == close["conn_id"]
            assert close["auth_state"] == "never"
            assert close["termination"] == "handshake"
            assert_accounting(close)
    asyncio.run(run())


def test_ping_bad_json_and_bad_envelope_before_hello_do_not_authenticate(daemon, caplog):
    async def run():
        async with connected(daemon) as (socket, _, _):
            assert (await socket.rpc({"type": "ping"}))["type"] == "pong"
            socket.feed("{broken🌿")
            assert (await socket.receive(kind="protocol.error"))["error_code"] == "bad_json"
            socket.feed(b"[1,2]")
            assert (await socket.receive(kind="protocol.error"))["error_code"] == "bad_envelope"
        assert not auth_records(caplog)
        close = one(caplog, "close")
        assert close["auth_state"] == "never"
        assert close["rx_messages"] == 3
        assert close["rx_bytes"] == sum(len(raw if isinstance(raw, bytes) else raw.encode()) for raw in socket.received)
        assert close["traffic"]["pong"]["direct_sent"] == 1
        assert_accounting(close)
    asyncio.run(run())


@pytest.mark.parametrize("client,kind", [
    ("pentacle-mobile", "mobile"), ("pentacle-web", "web"),
    ("pentacle", "desktop"), ("agent-orch", "cli"), ("unrecognized", "unknown"),
])
def test_loopback_hello_claims_and_negotiated_settings(daemon, caplog, client, kind):
    async def run():
        async with connected(daemon) as (socket, _, _):
            reply = await socket.rpc({"type": "hello", "client": client,
                "build_sha": "abcdef0123456789", "capabilities": {"work_lanes_v1": True},
                "subscribe": {"events_mode": "summary", "snapshot": False, "mode": "rpc"}})
            assert reply["type"] == "ready" and reply["snapshot"] is False
            assert (await socket.rpc({"type": "ping"}))["type"] == "pong"
        auth = one(caplog, "auth_ok")
        assert auth["auth_method"] == "loopback" and auth["auth_stage"] == "hello"
        assert auth["client_kind"] == kind
        assert auth["client_name"] == (client if kind != "unknown" else "unknown")
        assert auth["client_metadata_source"] == "hello_claim"
        assert auth["app_build"] == "abcdef0123456789"
        assert auth["events_mode"] == "summary"
        assert auth["snapshot_requested"] is False and auth["work_lanes_v1"] is True
        assert len(auth_records(caplog)) == 1
        assert [event["event"] for event in records(caplog)] == ["connect", "auth_ok", "close"]
    asyncio.run(run())


@pytest.mark.parametrize("client,scope,method", [
    ("pentacle", None, "operator_v2"),
    ("pentacle-mobile", None, "operator_v2"),
    ("pentacle-mobile", {"stream": "fixture:assistant"}, "scoped_v2"),
])
def test_real_operator_and_scoped_hello_consolidate_auth_checks(daemon, caplog, client, scope, method):
    async def run():
        async with connected(daemon, ScriptedSocket("198.51.100.20"), tls=True) as (socket, welcome, _):
            _, hello = signed_hello(daemon, welcome, client=client, scope=scope)
            hello["build_number"] = "0042"
            reply = await socket.rpc(hello)
            assert reply["type"] == ("hello" if scope else "ready")
            for _ in range(3):
                assert (await socket.rpc({"type": "ping"}))["type"] == "pong"
                await daemon._auth_context(socket, {})
            if scope:
                forbidden = await socket.rpc({"type": "list_sessions"})
                assert forbidden["error_code"] == "scope_denied"
        auth = one(caplog, "auth_ok")
        assert auth["auth_method"] == method and auth["auth_stage"] == "hello"
        assert auth["client_metadata_source"] == "verified_credential"
        assert auth["stream_id"] == (scope["stream"] if scope else None)
        assert auth["app_build"] == "0042" and auth["tls"] is True
        assert auth["transport"] == "other"
        assert not records(caplog, "auth_fail")
        assert one(caplog, "close")["auth_state"] == "accepted"
    asyncio.run(run())


@pytest.mark.parametrize("address", ["127.0.0.1", "198.51.100.20"])
def test_invalid_real_operator_proof_is_a_failure_even_on_loopback(daemon, caplog, address):
    async def run():
        async with connected(daemon, ScriptedSocket(address)) as (socket, welcome, _):
            _, hello = signed_hello(daemon, welcome)
            hello["auth_v2"]["proof"] = operator_auth.encode_b64url(b"x" * operator_auth.AUTH_PROOF_BYTES)
            reply = await socket.rpc(hello)
            assert reply["error_code"] == "operator_auth_invalid"
            assert (await socket.rpc({"type": "ping"}))["type"] == "pong"
        auth = one(caplog, "auth_fail")
        assert auth["reason"] == "operator_auth_invalid"
        assert auth["auth_method"] == "operator_v2" and auth["auth_stage"] == "hello"
        assert auth["stream_id"] is None
        assert not records(caplog, "auth_ok")
        assert one(caplog, "close")["auth_state"] == "failed"
    asyncio.run(run())


def test_nohello_seat_token_request_cached_success_and_background_expiry(daemon, caplog):
    async def run():
        token, owner = await issue_seat(daemon)
        async with connected(daemon, ScriptedSocket("198.51.100.20")) as (socket, _, _):
            reply = await socket.rpc({"type": "ping", "stream_token": token,
                                      "from_stream_id": owner})
            assert reply["type"] == "pong"
            for _ in range(4):
                assert (await socket.rpc({"type": "ping"}))["type"] == "pong"
                assert (await daemon._auth_context(socket, {}))["token_verified"]
            await daemon.store.mark_closed("fixture", "seat", closed_at="2026-10-09T00:00:00Z", pane_status="pane_dead")
            for _ in range(4):
                await daemon.broadcast({"type": "host.status", "host": "fixture", "online": True})
            refused = await socket.rpc({"type": "list_sessions"})
            assert refused["error_code"] == "authentication_required"
        accepted = one(caplog, "auth_ok")
        assert accepted["auth_method"] == "seat_token" and accepted["auth_stage"] == "request"
        assert accepted["stream_id"] == owner
        assert accepted["events_mode"] == "unknown"
        assert accepted["snapshot_requested"] is accepted["work_lanes_v1"] is None
        failures = records(caplog, "auth_fail")
        assert failures and failures[0]["reason"] == "expired"
        assert failures[0]["auth_stage"] == "revalidation"
        assert failures[0]["stream_id"] == owner
        assert len([failure for failure in failures if failure["reason"] == "expired"]) == 1
        assert one(caplog, "close")["auth_state"] == "failed"
    asyncio.run(run())


@pytest.mark.parametrize("case,reason", [
    ("absent", "absent"), ("malformed", "malformed"),
    ("expired", "expired"), ("wrong-seat", "wrong-seat"), ("internal", "internal-error"),
])
def test_explicit_invalid_seat_credentials_are_observable_despite_loopback_exemption(
        daemon, caplog, monkeypatch, case, reason):
    async def run():
        token, owner = await issue_seat(daemon)
        message = {"type": "ping", "stream_token": token, "from_stream_id": owner}
        if case == "absent":
            message["stream_token"] = None
        elif case == "malformed":
            message["stream_token"] = {"not": "a token"}
        elif case == "expired":
            message["stream_token"] = "synthetic-expired-token"
        elif case == "wrong-seat":
            message["from_stream_id"] = "fixture:unverified-claim"
        elif case == "internal":
            async def broken_read(_digest):
                raise OSError("synthetic backend failure")
            monkeypatch.setattr(daemon.store, "stream_token_state", broken_read)
        async with connected(daemon) as (socket, _, _):
            assert (await socket.rpc(message))["type"] == "pong"
        failure = one(caplog, "auth_fail")
        assert failure["auth_method"] == "seat_token" and failure["reason"] == reason
        assert failure["auth_stage"] == "request" and failure["stream_id"] is None
        assert not records(caplog, "auth_ok")
    asyncio.run(run())


def test_real_scoped_revocation_is_one_background_transition(daemon, caplog):
    async def run():
        async with connected(daemon, ScriptedSocket("198.51.100.20")) as (socket, welcome, _):
            cid, hello = signed_hello(daemon, welcome, client="pentacle-mobile", scope={"stream": "fixture:assistant"})
            assert (await socket.rpc(hello))["type"] == "hello"
            daemon.operator_credential_registry.revoke(cid)
            for _ in range(4):
                assert not (await daemon._auth_context(socket, {}))["scoped_principal"]
            assert (await socket.rpc({"type": "ping"}))["error_code"] == "authentication_required"
        failure = one(caplog, "auth_fail")
        assert failure["auth_method"] == "scoped_v2" and failure["reason"] == "revoked"
        assert failure["auth_stage"] == "revalidation"
        assert failure["stream_id"] == "fixture:assistant"
    asyncio.run(run())


def test_real_fixed_system_token_binding_and_invalid_explicit_attempt(daemon, caplog, monkeypatch, tmp_path):
    token = "synthetic-fixed-system-token"
    path = tmp_path / "system.token"
    path.write_text(token)
    path.chmod(0o600)
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_ID", server_module.FIXED_SYSTEM_PRODUCER_STREAM_ID)
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN_FILE", str(path))
    async def run():
        async with connected(daemon, ScriptedSocket("198.51.100.20")) as (socket, _, _):
            reply = await socket.rpc({"type": "hello", "from_stream_id": server_module.FIXED_SYSTEM_PRODUCER_STREAM_ID,
                "stream_token": token, "subscribe": {"mode": "rpc", "snapshot": False}})
            assert reply["type"] == "ready"
            for _ in range(3):
                assert (await daemon._auth_context(socket, {}))["service_authenticated"]
            denied = await socket.rpc({"type": "ping", "stream_token": "synthetic-wrong-token"})
            assert denied["error_code"] == "system_producer_auth_required"
        success = one(caplog, "auth_ok")
        assert success["auth_method"] == "system_token"
        assert success["client_kind"] == "service" and success["client_name"] == "system-producer"
        assert success["client_metadata_source"] == "verified_credential"
        assert success["stream_id"] is None
        failure = one(caplog, "auth_fail")
        assert failure["auth_method"] == "system_token"
        assert failure["reason"] in {"wrong-seat", "system_producer_auth_required"}
    asyncio.run(run())


def test_real_local_admin_verification_is_observed_without_extra_check(daemon, caplog, monkeypatch, tmp_path):
    path = tmp_path / "admin.token"
    local_admin.initialize(path)
    token = local_admin.read(path)
    verify = local_admin.verify
    calls = []
    def use_fixture_path(provided):
        calls.append(provided)
        return verify(provided, path)
    monkeypatch.setattr(local_admin, "verify", use_fixture_path)
    async def run():
        async with connected(daemon) as (socket, _, _):
            assert (await socket.rpc({"type": "ping", "local_admin_token": token}))["type"] == "pong"
        success = one(caplog, "auth_ok")
        assert success["auth_method"] == "local_admin" and success["auth_stage"] == "request"
        assert calls == [token]
    asyncio.run(run())


@pytest.mark.parametrize("case,reason,method", [
    ("hello", "authentication_required", "none"),
    ("request", "authentication_required", "none"),
    ("operator", "operator_auth_required", "seat_token"),
    ("tls", "tls_required", "seat_token"),
])
def test_existing_admission_refusals_report_the_enforced_reason(daemon, caplog, case, reason, method):
    async def run():
        message = {"type": "hello" if case == "hello" else "list_sessions"}
        if case in {"operator", "tls"}:
            token, owner = await issue_seat(daemon)
            message.update(stream_token=token, from_stream_id=owner)
            if case == "operator":
                message["type"] = "spawn_freeze"
            else:
                daemon.dot_principal_stream_ids = frozenset({owner})
                message["type"] = "ping"
        async with connected(daemon, ScriptedSocket("198.51.100.20")) as (socket, _, _):
            reply = await socket.rpc(message)
            expected_wire = "external_requires_tls" if case == "tls" else reason
            assert reply["error_code"] == expected_wire
        failure = one(caplog, "auth_fail")
        assert failure["reason"] == reason and failure["auth_method"] == method
        assert failure["auth_stage"] == ("hello" if case == "hello" else "request")
    asyncio.run(run())


def test_auth_rate_limit_fixed_window_suppression_and_lifecycle(daemon, caplog, clock):
    async def run():
        async with connected(daemon) as (socket, _, _):
            for index in range(8):
                assert (await socket.rpc({"type": "ping", "stream_token": f"synthetic-invalid-{index}"}))["type"] == "pong"
            emitted = records(caplog, "auth_fail")
            assert len(emitted) == 5
            assert all(event["auth_suppressed"] == 0 for event in emitted)
            clock.advance(59.999)
            await socket.rpc({"type": "ping", "stream_token": "synthetic-boundary-before"})
            assert len(records(caplog, "auth_fail")) == 5
            clock.now = 1060.0
            await socket.rpc({"type": "ping", "stream_token": "synthetic-boundary-after"})
            emitted = records(caplog, "auth_fail")
            assert len(emitted) == 6
            assert emitted[-1]["auth_suppressed"] == 4
            for index in range(7):
                await socket.rpc({"type": "ping", "stream_token": f"synthetic-next-{index}"})
        assert len(records(caplog, "auth_fail")) == 10
        assert one(caplog, "close")["auth_suppressed_total"] == 7
        one(caplog, "connect")
    asyncio.run(run())


def test_concurrent_auth_requests_have_invocation_local_elapsed_and_decisions(daemon, caplog, clock, monkeypatch):
    async def run():
        token, owner = await issue_seat(daemon)
        entered, release = asyncio.Event(), asyncio.Event()
        read = daemon.store.stream_token_state
        async def delayed_read(digest):
            entered.set()
            await release.wait()
            return await read(digest)
        monkeypatch.setattr(daemon.store, "stream_token_state", delayed_read)
        async with connected(daemon) as (socket, _, _):
            slow = asyncio.create_task(daemon._dispatch(json.dumps({"type": "ping", "stream_token": token,
                "from_stream_id": owner}), websocket=socket))
            await asyncio.wait_for(entered.wait(), 5)
            clock.advance(0.125)
            fast = await daemon._dispatch(json.dumps({"type": "ping", "stream_token": []}), websocket=socket)
            assert fast[0]["type"] == "pong"
            clock.advance(0.250)
            release.set()
            assert (await slow)[0]["type"] == "pong"
        fail = one(caplog, "auth_fail")
        success = one(caplog, "auth_ok")
        assert fail["reason"] == "malformed" and fail["auth_elapsed_ms"] == 0
        assert success["auth_method"] == "seat_token" and success["auth_elapsed_ms"] == 375
        assert success["stream_id"] == owner and fail["stream_id"] is None
        assert success["auth_stage"] == fail["auth_stage"] == "request"
    asyncio.run(run())


@pytest.mark.parametrize("sent,received,order,terminal,initiator,code,reason,force", [
    (Close(1000, ""), Close(1000, ""), True, 1000, "peer", 1000, "empty", None),
    (Close(1001, "going_away"), Close(1001, "going_away"), False, 1001, "server", 1001, "going_away", None),
    (Close(1000, "normal"), Close(1001, "going_away"), True, 1001, "peer", 1001, "going_away", None),
    (Close(1001, "going_away"), Close(1000, "normal"), False, 1000, "server", 1001, "going_away", None),
    (Close(4000, "liveness_force_close"), Close(4000, "liveness_force_close"), True, 4000, "peer", 4000, "liveness_force_close", "peer_close_frame"),
    (Close(4000, "focused_heartbeat_timeout"), Close(4000, "focused_heartbeat_timeout"), True, 4000, "peer", 4000, "focused_heartbeat_timeout", "peer_close_frame"),
    (Close(4000, "arbitrary private reason\r\nsecond line"), Close(4000, "arbitrary private reason\r\nsecond line"), True, 4000, "peer", 4000, "redacted", None),
    (None, Close(4000, ""), None, 4000, "unknown", 4000, "empty", None),
    (None, Close(1000, "normal"), None, 1000, "unknown", 1000, "normal", None),
    (None, None, None, 1006, "unknown", 1006, "unknown", None),
    (None, None, None, None, "unknown", None, "unknown", None),
])
def test_observed_close_evidence_matrix(daemon, caplog, sent, received, order, terminal,
                                      initiator, code, reason, force):
    async def run():
        async with connected(daemon) as (socket, _, task):
            socket.finish(sent=sent, received=received, order=order, terminal=terminal)
            await task
        close = one(caplog, "close")
        assert close["initiator"] == initiator
        assert close["close_code"] == code and close["close_reason"] == reason
        assert close["close_sent_code"] == (sent.code if sent else None)
        assert close["close_received_code"] == (received.code if received else None)
        assert close["termination"] == ("handshake" if sent is not None and received is not None else "transport_lost")
        assert close["close_sent_reason"] == ("unknown" if sent is None else reason if sent == received else sent.reason)
        assert close["close_received_reason"] == ("unknown" if received is None else reason if sent == received else received.reason or "empty")
        if force:
            forced = one(caplog, "force_close")
            assert forced["initiator"] == "peer" and forced["cause"] == "liveness"
            assert forced["cause_source"] == force
            assert forced["close_reason"] == reason and forced["close_code"] == 4000
            assert [event["event"] for event in records(caplog)][-2:] == ["force_close", "close"]
        else:
            assert not records(caplog, "force_close")
        assert_accounting(close)
        assert_released(daemon, socket)
    asyncio.run(run())


def test_caught_connectionclosed_is_evidence_without_protocol_attributes(daemon, caplog):
    async def run():
        async with connected(daemon) as (socket, _, task):
            del socket.protocol
            socket.incoming.put_nowait(ConnectionClosedError(
                Close(4000, "focused_heartbeat_timeout"), Close(4000, "focused_heartbeat_timeout"), True,
            ))
            await task
        close = one(caplog, "close")
        assert close["initiator"] == "peer" and close["close_code"] == 4000
        assert close["close_sent_code"] == close["close_received_code"] == 4000
        assert one(caplog, "force_close")["cause_source"] == "peer_close_frame"
        assert_released(daemon, socket)
    asyncio.run(run())


def test_existing_protocol_keepalive_evidence_reports_server_liveness(daemon, caplog):
    async def run():
        async with connected(daemon) as (socket, _, task):
            socket.finish(sent=Close(1011, "keepalive ping timeout"), terminal=1006)
            await task
        forced = one(caplog, "force_close")
        assert forced["initiator"] == "server"
        assert forced["cause"] == "liveness" and forced["cause_source"] == "protocol_close"
        assert forced["close_code"] == 1011 and forced["close_reason"] == "keepalive_ping_timeout"
        close = one(caplog, "close")
        assert close["close_sent_code"] == 1011 and close["close_received_code"] is None
        assert close["termination"] == "transport_lost"
        assert close["close_sent_reason"] == "keepalive_ping_timeout"
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["closed", "exception", "cancelled"])
def test_failed_or_cancelled_welcome_finalizes_without_successful_tx(daemon, caplog, failure):
    async def run():
        socket = ScriptedSocket()
        if failure == "closed":
            socket.send_error = ConnectionClosedError(None, None, None)
        elif failure == "exception":
            socket.send_error = RuntimeError("synthetic welcome failure")
        else:
            socket.send_gate = asyncio.Event()
        task = asyncio.create_task(daemon._handle_client(socket))
        if failure == "cancelled":
            await asyncio.wait_for(socket.send_entered.wait(), 5)
            task.cancel()
        if failure == "closed":
            await task
        else:
            with pytest.raises(asyncio.CancelledError if failure == "cancelled" else RuntimeError):
                await task
        close = one(caplog, "close")
        assert close["termination"] == ("handler_cancelled" if failure == "cancelled" else "welcome_failed")
        assert close["auth_state"] == "never"
        assert close["tx_messages"] == close["tx_bytes"] == 0
        assert close["last_tx_age_ms"] is None
        assert not records(caplog, "force_close")
        assert_accounting(close)
        assert_released(daemon, socket)
    asyncio.run(run())


@pytest.mark.parametrize("failure,termination", [("cancelled", "handler_cancelled"), ("exception", "handler_error")])
def test_handler_cancellation_and_error_preserve_exception_and_cleanup(daemon, caplog, failure, termination):
    async def run():
        socket = ScriptedSocket()
        task = asyncio.create_task(daemon._handle_client(socket))
        await socket.receive(kind="welcome")
        if failure == "cancelled":
            task.cancel()
        else:
            socket.incoming.put_nowait(RuntimeError("synthetic receive failure"))
        with pytest.raises(asyncio.CancelledError if failure == "cancelled" else RuntimeError):
            await task
        close = one(caplog, "close")
        assert close["termination"] == termination
        assert close["close_code"] is None and close["close_reason"] == "unknown"
        assert_released(daemon, socket)
    asyncio.run(run())


def test_writer_failure_unregister_is_not_close_and_snapshot_survives(daemon, caplog, clock):
    async def run():
        async with connected(daemon) as (socket, _, task):
            socket.send_entered.clear()
            socket.send_gate = asyncio.Event()
            socket.send_error = RuntimeError("synthetic writer failure")
            assert daemon._enqueue(socket, "chat.event", '{"type":"chat.event"}')
            await asyncio.wait_for(socket.send_entered.wait(), 5)
            assert daemon._enqueue(socket, "pong", '{"type":"pong"}')
            clock.advance(0.125)
            socket.send_gate.set()
            writer = daemon._client_writer_tasks.get(socket)
            if writer is not None:
                await writer
            assert socket not in daemon._clients
            assert not task.done() and not records(caplog, "close")
            daemon._unregister_client(socket)
            daemon._unregister_client(socket)
            one(caplog, "connect")
            assert socket in daemon._connection_diagnostics
            clock.advance(0.250)
            socket.finish()
            await task
        close = one(caplog, "close")
        assert close["termination"] == "writer_failed"
        assert close["queue_depth"] == 1 and close["queued_by_type"]["pong"] == 1
        assert close["queued_by_type"]["chat.event"] == 0
        assert close["queue_snapshot_age_ms"] == 250
        assert close["traffic"]["chat.event"]["broadcast_enqueued"] == 1
        assert close["traffic"]["chat.event"]["broadcast_sent"] == 0
        assert close["send_call_max_ms"] == 125
        assert close["tx_messages"] == 1  # the successful welcome only
        assert_accounting(close)
        assert_released(daemon, socket)
    asyncio.run(run())


def test_send_side_connectionclosed_does_not_finalize_until_receive_handler_ends(daemon, caplog, clock):
    async def run():
        async with connected(daemon) as (socket, _, task):
            socket.send_error = ConnectionClosedError(None, None, None)
            assert await daemon._send_direct(socket, [{"type": "pong"}]) is False
            assert not task.done() and not records(caplog, "close")
            one(caplog, "connect")
            assert socket in daemon._connection_diagnostics
            daemon._unregister_client(socket)
            clock.advance(0.250)
            socket.finish(terminal=1006)
            await task
        close = one(caplog, "close")
        assert close["termination"] == "transport_lost"
        assert close["tx_messages"] == 1
        assert close["traffic"]["pong"]["direct_sent"] == 0
        assert close["queue_snapshot_age_ms"] == 250
        assert_released(daemon, socket)
    asyncio.run(run())


def test_all_direct_send_loops_encoded_chunks_and_writer_have_exact_utf8_accounting(daemon, caplog):
    async def run():
        async with connected(daemon) as (socket, _, _):
            raw = json.dumps({"type": "snapshot", "sessions": [], "caption": "雪🌿"}, ensure_ascii=False)
            assert await daemon._send_direct(socket, raw)
            assert await daemon._send_direct(socket, [{"type": "session.inventory", "sessions": []}, {"type": "pong"}])
            closed = []
            async def contiguous():
                try:
                    yield _EncodedFrame("work_lanes.inventory", '{"type":"work_lanes.inventory","caption":"雪"}')
                    yield {"type": "chat.event", "text": "🌿"}
                finally:
                    closed.append("contiguous")
            async def interleaved():
                try:
                    yield _EncodedFrame("chat.event", '{"type":"chat.event","text":"🌿"}')
                    yield {"type": "unrecognized-frame", "value": "雪"}
                finally:
                    closed.append("interleaved")
            assert await daemon._send_direct(socket, contiguous())
            assert await daemon._send_direct(socket, interleaved(), interleave_history=True)
            assert closed == ["contiguous", "interleaved"]
            broadcast = '{"type":"chat.event","text":"雪🌿"}'
            assert daemon._enqueue(socket, "chat.event", broadcast)
            await socket.receive(kind="chat.event")
            # receive may select an earlier direct frame: wait for the actual
            # queued payload to complete before ending the connection.
            async with asyncio.timeout(5):
                while broadcast not in socket.sent:
                    await asyncio.sleep(0)
        close = one(caplog, "close")
        assert close["tx_messages"] == len(socket.sent) == 9
        assert close["tx_bytes"] == sum(len(payload.encode()) for payload in socket.sent)
        expected = {bucket: {"direct_sent": 0, "direct_sent_bytes": 0,
                             "broadcast_sent": 0, "broadcast_sent_bytes": 0} for bucket in BUCKETS}
        for payload in socket.sent:
            kind = json.loads(payload)["type"]
            bucket = kind if kind in BUCKETS else "other"
            prefix = "broadcast" if payload == broadcast else "direct"
            expected[bucket][f"{prefix}_sent"] += 1
            expected[bucket][f"{prefix}_sent_bytes"] += len(payload.encode())
        for bucket, values in expected.items():
            for key, value in values.items():
                assert close["traffic"][bucket][key] == value, (bucket, key)
        assert close["traffic"]["chat.event"]["broadcast_enqueued"] == 1
        assert close["queue_depth"] == 0 and close["queue_snapshot_age_ms"] == 0
        assert_accounting(close)
    asyncio.run(run())


def test_ping_receipt_and_pong_completion_have_distinct_ages_and_lock_send_timing(daemon, caplog, clock):
    async def run():
        async with connected(daemon) as (socket, _, task):
            lock = daemon._client_send_locks[socket]
            await lock.acquire()
            socket.send_entered.clear()
            socket.send_gate = asyncio.Event()
            clock.advance(1.0)
            socket.feed({"type": "ping", "request_id": "timed-ping"})
            # Ensure the actual ping handler has run and direct send is queued
            # at the held lock, not merely an unprocessed scripted input.
            async with asyncio.timeout(5):
                while not getattr(lock, "_waiters", None):
                    await asyncio.sleep(0)
            clock.advance(0.250)
            lock.release()
            await asyncio.wait_for(socket.send_entered.wait(), 5)
            clock.advance(0.500)
            socket.send_gate.set()
            assert (await socket.receive(request_id="timed-ping"))["type"] == "pong"
            clock.advance(0.125)
            socket.finish()
            await task
        close = one(caplog, "close")
        assert close["last_rx_age_ms"] == close["last_ping_age_ms"] == 875
        assert close["last_tx_age_ms"] == close["last_pong_age_ms"] == 125
        assert close["send_lock_wait_max_ms"] == 250
        assert close["send_call_max_ms"] == 500
        assert close["traffic"]["pong"]["direct_sent"] == 1
        assert_accounting(close)
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["direct", "writer"])
def test_cancelled_sends_record_timing_but_never_success(daemon, caplog, clock, mode):
    async def run():
        async with connected(daemon) as (socket, _, task):
            socket.send_entered.clear()
            socket.send_gate = asyncio.Event()
            if mode == "direct":
                sender = asyncio.create_task(daemon._send_direct(socket, [{"type": "pong"}]))
            else:
                assert daemon._enqueue(socket, "pong", '{"type":"pong"}')
                sender = daemon._client_writer_tasks[socket]
            await asyncio.wait_for(socket.send_entered.wait(), 5)
            clock.advance(0.375)
            sender.cancel()
            with pytest.raises(asyncio.CancelledError):
                await sender
            socket.finish()
            await task
        close = one(caplog, "close")
        assert close["send_call_max_ms"] == 375
        assert close["last_pong_age_ms"] is None
        assert close["traffic"]["pong"]["direct_sent"] == 0
        assert close["traffic"]["pong"]["broadcast_sent"] == 0
        assert close["tx_messages"] == 1
        assert_accounting(close)
    asyncio.run(run())


def test_detached_accepted_send_survives_disconnect_without_late_state_recreation(daemon, caplog):
    async def run():
        entered, finish = asyncio.Event(), asyncio.Event()
        completed = []
        async def accepted_send(message):
            entered.set()
            await finish.wait()
            completed.append(message["request_id"])
            return {"type": "send.result", "delivery": "landed"}
        daemon.handlers["send"] = accepted_send
        async with connected(daemon) as (socket, _, task):
            socket.feed({"type": "send", "request_id": "durable-request", "text": "synthetic payload"})
            await asyncio.wait_for(entered.wait(), 5)
            socket.finish()
            await task
            close_before = json.dumps(one(caplog, "close"), sort_keys=True)
            assert not completed
            assert_released(daemon, socket)
            pending = tuple(daemon._detached_send_tasks)
            assert pending
            finish.set()
            await asyncio.gather(*pending)
            await asyncio.sleep(0)
            assert completed == ["durable-request"]
            assert not daemon._detached_send_tasks
            assert json.dumps(one(caplog, "close"), sort_keys=True) == close_before
            assert_released(daemon, socket)
        assert len(socket.sent) == 1  # no reply is fabricated after disconnect
    asyncio.run(run())


def test_late_detached_transport_completion_cannot_mutate_final_record(daemon, caplog):
    async def run():
        async def accepted_send(message):
            return {"type": "send.result", "request_id": message["request_id"], "delivery": "landed"}
        daemon.handlers["send"] = accepted_send
        async with connected(daemon) as (socket, _, task):
            socket.send_entered.clear()
            socket.send_gate = asyncio.Event()
            socket.feed({"type": "send", "request_id": "late-completion"})
            await asyncio.wait_for(socket.send_entered.wait(), 5)
            pending = tuple(daemon._detached_send_tasks)
            assert pending
            socket.finish()
            await task
            close_before = json.dumps(one(caplog, "close"), sort_keys=True)
            assert one(caplog, "close")["tx_messages"] == 1
            socket.send_gate.set()
            await asyncio.gather(*pending)
            assert len(socket.sent) == 2  # pre-existing transport completion is preserved
            assert json.dumps(one(caplog, "close"), sort_keys=True) == close_before
            assert_released(daemon, socket)
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["serialization", "emission"])
def test_diagnostic_failure_does_not_change_authentication_or_wire_outcomes(daemon, caplog, monkeypatch, failure):
    async def run():
        attempts = []
        with monkeypatch.context() as patch:
            if failure == "serialization":
                dumps = json.dumps
                def broken_diagnostic_json(value, *args, **kwargs):
                    if isinstance(value, dict) and value.get("schema") == 1 and "conn_id" in value:
                        attempts.append("serialization")
                        raise ValueError("synthetic diagnostic serializer failure")
                    return dumps(value, *args, **kwargs)
                patch.setattr(server_module.json, "dumps", broken_diagnostic_json)
            else:
                emit = server_module.log._log
                def broken_diagnostic_log(level, message, *args, **kwargs):
                    if isinstance(message, str) and message.startswith("conn_diag "):
                        attempts.append("emission")
                        raise RuntimeError("synthetic diagnostic emitter failure")
                    return emit(level, message, *args, **kwargs)
                patch.setattr(server_module.log, "_log", broken_diagnostic_log)
            async with connected(daemon, ScriptedSocket("198.51.100.20")) as (socket, welcome, _):
                _, hello = signed_hello(daemon, welcome)
                reply = await socket.rpc(hello)
                assert reply["type"] == "ready" and reply["snapshot"] is False
                assert (await socket.rpc({"type": "ping"}))["type"] == "pong"
            assert attempts, "real connection hooks never attempted diagnostic output"
            assert_released(daemon, socket)
        # A healthy subsequent connection must emit normally; error containment
        # must not disable the logger globally or retain corrupt connection state.
        async with connected(daemon):
            pass
        assert records(caplog, "connect") and records(caplog, "close")
        assert not daemon._connection_diagnostics
    asyncio.run(run())


def test_real_file_service_token_rotation_is_one_revalidation_transition(daemon, caplog, monkeypatch, tmp_path):
    path = tmp_path / "rotating-service.token"
    path.write_text("synthetic-file-service-token")
    path.chmod(0o600)
    monkeypatch.setenv(server_module.WMI_BACKUP_STREAM_TOKEN_FILE_ENV, str(path))
    async def run():
        async with connected(daemon, ScriptedSocket("198.51.100.20")) as (socket, _, _):
            reply = await socket.rpc({"type": "hello", "stream_token": path.read_text(),
                "from_stream_id": server_module.WMI_BACKUP_PRODUCER_STREAM_ID,
                "subscribe": {"snapshot": False, "mode": "rpc"}})
            assert reply["type"] == "ready"
            assert (await daemon._auth_context(socket, {}))["service_authenticated"]
            path.write_text("synthetic-rotated-service-token")
            for _ in range(4):
                assert not (await daemon._auth_context(socket, {}))["service_authenticated"]
            assert (await socket.rpc({"type": "ping"}))["error_code"] == "system_producer_auth_required"
        success = one(caplog, "auth_ok")
        failure = one(caplog, "auth_fail")
        assert success["auth_method"] == failure["auth_method"] == "system_token"
        assert success["stream_id"] is failure["stream_id"] is None
        assert failure["auth_stage"] == "revalidation"
        assert failure["reason"] == "revoked"
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["direct", "writer"])
def test_cancelled_send_lock_wait_is_measured_without_send_call(daemon, caplog, clock, mode):
    async def run():
        async with connected(daemon) as (socket, _, _):
            lock = daemon._client_send_locks[socket]
            await lock.acquire()
            if mode == "direct":
                sender = asyncio.create_task(daemon._send_direct(socket, [{"type": "pong"}]))
            else:
                assert daemon._enqueue(socket, "pong", '{"type":"pong"}')
                sender = daemon._client_writer_tasks[socket]
            async with asyncio.timeout(5):
                while not getattr(lock, "_waiters", None):
                    await asyncio.sleep(0)
            clock.advance(0.625)
            sender.cancel()
            with pytest.raises(asyncio.CancelledError):
                await sender
            lock.release()
        close = one(caplog, "close")
        assert close["send_lock_wait_max_ms"] == 625
        assert close["send_call_max_ms"] == 0
        assert close["traffic"]["pong"]["direct_sent"] == 0
        assert close["traffic"]["pong"]["broadcast_sent"] == 0
        assert close["tx_messages"] == 1
        assert_accounting(close)
    asyncio.run(run())


def test_success_and_failure_share_cap_and_suppressed_state_still_updates(daemon, caplog, clock):
    async def run():
        token, owner = await issue_seat(daemon)
        async with connected(daemon) as (socket, _, _):
            for _ in range(4):
                assert (await socket.rpc({"type": "ping", "stream_token": []}))["type"] == "pong"
            assert (await socket.rpc({"type": "ping", "stream_token": token, "from_stream_id": owner}))["type"] == "pong"
            assert (await socket.rpc({"type": "ping", "stream_token": token,
                                      "from_stream_id": "fixture:wrong"}))["type"] == "pong"
            assert len(auth_records(caplog)) == 5
            assert [event["event"] for event in auth_records(caplog)] == ["auth_fail"] * 4 + ["auth_ok"]
        close = one(caplog, "close")
        assert close["auth_state"] == "failed"
        assert close["stream_id"] == owner
        assert close["auth_suppressed_total"] == 1
    asyncio.run(run())


def test_invalid_seat_attempt_remains_visible_when_verified_operator_survives(daemon, caplog):
    async def run():
        async with connected(daemon, ScriptedSocket("198.51.100.20")) as (socket, welcome, _):
            _, hello = signed_hello(daemon, welcome)
            assert (await socket.rpc(hello))["type"] == "ready"
            reply = await socket.rpc({"type": "list_sessions", "stream_token": "synthetic-invalid-seat-token"})
            assert "error_code" not in reply
            assert daemon._operator_authenticated(socket)
        success = one(caplog, "auth_ok")
        failure = one(caplog, "auth_fail")
        assert success["auth_method"] == "operator_v2"
        assert failure["auth_method"] == "seat_token" and failure["reason"] == "expired"
        assert failure["client_metadata_source"] == "verified_credential"
    asyncio.run(run())


@pytest.mark.parametrize("peer,local,binds,family,expected", [
    pytest.param("/tmp/synthetic-private-peer.sock", None, ["127.0.0.1"], socket_module.AF_UNIX, "unix", id="unix-family-never-path"),
    pytest.param(("::ffff:127.0.0.1", 45123), None, ["127.0.0.1"], socket_module.AF_INET6, "loopback", id="mapped-ipv6-loopback"),
    pytest.param(("::1", 45123, 0, 0), None, ["::1"], socket_module.AF_INET6, "loopback", id="ipv6-loopback"),
    pytest.param(("192.0.2.4", 45123), ("192.0.2.2", 7791), ["192.0.2.2"], socket_module.AF_INET, "tailnet", id="tailnet-ipv4-configured"),
    pytest.param(("::ffff:192.0.2.4", 45123), ("192.0.2.2", 7791), ["192.0.2.2"], socket_module.AF_INET6, "tailnet", id="mapped-ipv6-tailnet"),
    pytest.param(("2001:db8::123", 45123, 0, 0), ("2001:db8::456", 7791, 0, 0), ["2001:db8::456"], socket_module.AF_INET6, "tailnet", id="tailnet-ipv6-configured"),
    pytest.param(("192.0.2.4", 45123), ("192.0.2.22", 7791), ["127.0.0.1"], socket_module.AF_INET, "other", id="tailnet-peer-unconfigured-listener"),
    pytest.param(("192.0.2.4", 45123), None, ["192.0.2.2"], socket_module.AF_INET, "other", id="tailnet-peer-no-listener-evidence"),
    pytest.param(("198.51.100.20", 45123), ("127.0.0.1", 7791), ["127.0.0.1"], socket_module.AF_INET, "other", id="other-valid-peer"),
    pytest.param(None, None, ["127.0.0.1"], None, "unknown", id="missing-peer"),
    pytest.param((), None, ["127.0.0.1"], None, "unknown", id="empty-peer"),
    pytest.param(("not-an-ip", 45123), None, ["127.0.0.1"], None, "unknown", id="malformed-peer"),
    pytest.param((3232235777, 45123), None, ["127.0.0.1"], None, "unknown", id="integer-is-not-peer-address"),
    pytest.param((True, 45123), None, ["127.0.0.1"], None, "unknown", id="boolean-is-not-peer-address"),
])
def test_actual_transport_classification_uses_socket_and_configured_listener_only(
        daemon, caplog, peer, local, binds, family, expected):
    daemon._conn_diag_tailnet_networks = tuple(map(ipaddress.ip_network, ("192.0.2.0/24", "2001:db8::/32")))
    async def run():
        socket = ScriptedSocket()
        socket.remote_address = peer
        socket.local_address = local
        socket.transport = SimpleNamespace(get_extra_info=lambda name: {
            "socket": SimpleNamespace(family=family), "sockname": local,
        }.get(name))
        daemon.binds = binds
        async with connected(daemon, socket):
            pass
        connect = one(caplog, "connect")
        close = one(caplog, "close")
        assert connect["transport"] == close["transport"] == expected
        assert connect["tls"] is close["tls"] is False
        # No peer text, socket path or port becomes a schema field or value.
        serialized = json.dumps(records(caplog))
        forbidden_values = ["/tmp/synthetic-private-peer.sock", "remote_address", "sockname"]
        if isinstance(peer, tuple) and peer and isinstance(peer[0], str):
            forbidden_values.append(peer[0])
        for forbidden in forbidden_values:
            assert forbidden not in serialized
    asyncio.run(run())


@pytest.mark.parametrize("tls", [False, True])
def test_tls_fact_is_owned_by_accept_handler_and_ignores_wire_claims(daemon, caplog, tls):
    daemon._conn_diag_tailnet_networks = (ipaddress.ip_network("192.0.2.0/24"),)
    async def run():
        socket = ScriptedSocket("192.0.2.4")
        socket.local_address = ("192.0.2.2", 7791)
        socket.request = SimpleNamespace(headers={"X-Forwarded-Proto": "https" if not tls else "http",
                                                  "X-Forwarded-For": "127.0.0.1"})
        daemon.binds = daemon.dot_tls_binds = ["192.0.2.2"]
        async with connected(daemon, socket, tls=tls) as (socket, _, _):
            reply = await socket.rpc({"type": "hello", "tls": not tls, "transport": "loopback",
                "client": "pentacle", "peer": "127.0.0.1", "subscribe": {"snapshot": False}})
            assert reply["error_code"] == "authentication_required"
        for event in records(caplog):
            assert event["tls"] is tls and event["transport"] == "tailnet"
        one(caplog, "connect")
        one(caplog, "close")
    asyncio.run(run())


@pytest.mark.parametrize("seat,expected", [
    pytest.param("A0._-z", "fixture:A0._-z", id="canonical-punctuation"),
    pytest.param("a" * 120, "fixture:" + "a" * 120, id="canonical-128-chars"),
    pytest.param("a" * 121, None, id="reject-129-chars"),
    pytest.param("private/seat", None, id="reject-path-separator"),
    pytest.param("private:seat", None, id="reject-extra-colon"),
    pytest.param("private seat", None, id="reject-space"),
    pytest.param("private雪", None, id="reject-nonascii"),
    pytest.param("private\nseat", None, id="reject-newline"),
])
def test_store_verified_owner_projection_requires_canonical_bounded_stream(daemon, caplog, seat, expected):
    async def run():
        token, owner = await issue_seat(daemon, seat=seat)
        async with connected(daemon, ScriptedSocket("198.51.100.20")) as (socket, _, _):
            assert (await socket.rpc({"type": "ping", "stream_token": token}))["type"] == "pong"
            assert daemon._client_authenticated_streams[socket] == owner
        accepted = one(caplog, "auth_ok")
        assert accepted["auth_method"] == "seat_token"
        assert accepted["stream_id"] == one(caplog, "close")["stream_id"] == expected
        if expected is None:
            assert json.dumps(owner, ensure_ascii=False) not in json.dumps(records(caplog), ensure_ascii=False)
    asyncio.run(run())


@pytest.mark.parametrize("scope,expected", [
    pytest.param("fixture:assistant", "fixture:assistant", id="canonical-scope"),
    pytest.param("fixture:" + "a" * 120, "fixture:" + "a" * 120, id="scope-128-chars"),
    pytest.param("fixture:" + "a" * 121, None, id="reject-scope-129-chars"),
])
def test_real_verified_scope_projection_is_bounded_and_ignores_wire_scope(daemon, caplog, scope, expected):
    async def run():
        async with connected(daemon, ScriptedSocket("198.51.100.20")) as (socket, welcome, _):
            _, hello = signed_hello(daemon, welcome, client="pentacle-mobile", scope={"stream": scope})
            hello["scope"] = {"stream": "wire:unverified-claim"}
            assert (await socket.rpc(hello))["type"] == "hello"
            assert daemon._connection_trust[socket].scope == {"stream": scope}
        accepted = one(caplog, "auth_ok")
        assert accepted["auth_method"] == "scoped_v2"
        assert accepted["stream_id"] == one(caplog, "close")["stream_id"] == expected
        assert "wire:unverified-claim" not in json.dumps(records(caplog))
    asyncio.run(run())


def test_invalid_operator_proof_remains_observable_after_preliminary_seat_failure(daemon, caplog):
    async def run():
        async with connected(daemon) as (socket, welcome, _):
            _, hello = signed_hello(daemon, welcome)
            hello["stream_token"] = []
            hello["auth_v2"]["proof"] = operator_auth.encode_b64url(b"x" * operator_auth.AUTH_PROOF_BYTES)
            assert (await socket.rpc(hello))["error_code"] == "operator_auth_invalid"
            assert not daemon._operator_authenticated(socket)
        failures = records(caplog, "auth_fail")
        operator_failures = [event for event in failures if event["auth_method"] == "operator_v2"]
        assert len(operator_failures) == 1, "preliminary seat failure hid the real operator-proof rejection"
        assert operator_failures[0]["reason"] == "operator_auth_invalid"
        assert operator_failures[0]["auth_stage"] == "hello"
        assert not records(caplog, "auth_ok")
    asyncio.run(run())


def test_operator_method_precedence_preserves_independently_verified_seat_owner(daemon, caplog):
    async def run():
        token, owner = await issue_seat(daemon)
        async with connected(daemon, ScriptedSocket("198.51.100.20")) as (socket, welcome, _):
            _, hello = signed_hello(daemon, welcome)
            hello.update(stream_token=token, from_stream_id=owner)
            assert (await socket.rpc(hello))["type"] == "ready"
            assert daemon._operator_authenticated(socket)
            assert daemon._client_authenticated_streams[socket] == owner
        accepted = one(caplog, "auth_ok")
        assert accepted["auth_method"] == "operator_v2"
        assert accepted["stream_id"] == one(caplog, "close")["stream_id"] == owner
        assert accepted["client_metadata_source"] == "verified_credential"
    asyncio.run(run())


def test_auth_decision_commits_before_parked_business_handler_and_cannot_restore_stale_state(daemon, caplog):
    async def run():
        token, owner = await issue_seat(daemon)
        entered, release = asyncio.Event(), asyncio.Event()
        business_handler = daemon.handlers["list_sessions"]
        async def parked_handler(message):
            entered.set()
            await release.wait()
            return await business_handler(message)
        daemon.handlers["list_sessions"] = parked_handler
        async with connected(daemon, ScriptedSocket("198.51.100.20")) as (socket, _, _):
            socket.feed({"type": "list_sessions", "request_id": "parked-authenticated-read",
                         "stream_token": token, "from_stream_id": owner})
            await asyncio.wait_for(entered.wait(), 5)
            accepted = records(caplog, "auth_ok")
            assert len(accepted) == 1, "authentication was held open until the unrelated business handler completed"
            assert accepted[0]["auth_method"] == "seat_token" and accepted[0]["stream_id"] == owner
            assert (await socket.rpc({"type": "ping", "stream_token": []}))["type"] == "pong"
            assert one(caplog, "auth_fail")["reason"] == "malformed"
            assert socket not in daemon._client_authenticated_streams
            release.set()
            reply = await socket.receive(request_id="parked-authenticated-read")
            assert "error_code" not in reply
            assert [event["event"] for event in auth_records(caplog)] == ["auth_ok", "auth_fail"]
        assert one(caplog, "close")["auth_state"] == "failed"
    asyncio.run(run())


@pytest.mark.parametrize("parent_still_running", [False, True])
def test_inherited_child_auth_context_cannot_hide_background_revalidation(daemon, caplog, parent_still_running):
    async def run():
        token, owner = await issue_seat(daemon)
        entered, revalidate, release_handler = asyncio.Event(), asyncio.Event(), asyncio.Event()
        children = []
        business_handler = daemon.handlers["list_sessions"]
        async def background_broadcast():
            await revalidate.wait()
            await daemon.broadcast({"type": "host.status", "host": "fixture", "online": True})
        async def spawn_background(message):
            children.append(asyncio.create_task(background_broadcast()))
            entered.set()
            if parent_still_running:
                await release_handler.wait()
            return await business_handler(message)
        daemon.handlers["list_sessions"] = spawn_background
        try:
            async with connected(daemon, ScriptedSocket("198.51.100.20")) as (socket, _, _):
                socket.feed({"type": "list_sessions", "request_id": "background-owner",
                             "stream_token": token, "from_stream_id": owner})
                await asyncio.wait_for(entered.wait(), 5)
                if not parent_still_running:
                    assert "error_code" not in await socket.receive(request_id="background-owner")
                assert one(caplog, "auth_ok")["stream_id"] == owner
                await daemon.store.mark_closed("fixture", "seat", closed_at="2026-10-09T00:00:00Z", pane_status="pane_dead")
                revalidate.set()
                await asyncio.wait_for(asyncio.gather(*children), 5)
                failure = one(caplog, "auth_fail")
                assert failure["reason"] == "expired" and failure["auth_stage"] == "revalidation"
                release_handler.set()
                if parent_still_running:
                    assert "error_code" not in await socket.receive(request_id="background-owner")
                assert [event["event"] for event in auth_records(caplog)] == ["auth_ok", "auth_fail"]
            assert one(caplog, "close")["auth_state"] == "failed"
        finally:
            for child in children:
                if not child.done():
                    child.cancel()
            if children:
                await asyncio.gather(*children, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("configured", [None, "", "   ", "invalid-only-entry"])
def test_tailnet_networks_have_no_default(monkeypatch, caplog, configured):
    if configured is None:
        monkeypatch.delenv("PENTACLE_CONN_DIAG_TAILNET_NETWORKS", raising=False)
    else:
        monkeypatch.setenv("PENTACLE_CONN_DIAG_TAILNET_NETWORKS", configured)
    server = Server(binds=["0.0.0.0", "::"])
    for peer in ("192.0.2.4", "2001:db8::4"):
        ws = ScriptedSocket(peer)
        ws.local_address = ("192.0.2.2", 7791)
        assert server._diag_transport(ws) == "other"
    warnings = [r for r in caplog.records if r.name == LOGGER]
    if configured == "invalid-only-entry":
        assert len(warnings) == 1
        assert warnings[0].getMessage() == "Connection diagnostic tailnet networks: skipped 1 invalid entries"
        assert configured not in caplog.text
    else:
        assert not warnings


def test_tailnet_networks_read_once_skip_invalid_without_logging_text(monkeypatch, caplog):
    entries = ("private-invalid-entry", "192.0.2.0/99", "2001:db8::/129")
    monkeypatch.setenv("PENTACLE_CONN_DIAG_TAILNET_NETWORKS",
                       " 192.0.2.0/24, 198.51.100.0/24, 2001:db8::/32," + ",".join(entries))
    caplog.set_level(logging.INFO)
    server = Server(binds=["0.0.0.0", "::"])
    monkeypatch.setenv("PENTACLE_CONN_DIAG_TAILNET_NETWORKS", "203.0.113.0/24")
    assert tuple(map(str, server._conn_diag_tailnet_networks)) == (
        "192.0.2.0/24", "198.51.100.0/24", "2001:db8::/32")
    for peer, expected in (("192.0.2.0", "tailnet"), ("192.0.2.255", "tailnet"),
                           ("192.0.1.255", "other"), ("192.0.3.0", "other"),
                           ("198.51.100.4", "tailnet"), ("::ffff:192.0.2.4", "tailnet"),
                           ("2001:db8::4", "tailnet"), ("2001:db9::", "other"),
                           ("203.0.113.4", "other")):
        ws = ScriptedSocket(peer)
        ws.local_address = ("192.0.2.2", 7791)
        assert server._diag_transport(ws) == expected
    warnings = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == "Connection diagnostic tailnet networks: skipped 3 invalid entries"
    assert all(entry not in caplog.text for entry in entries)
    assert all(network not in caplog.text for network in ("192.0.2.0/24", "198.51.100.0/24", "2001:db8::/32"))


def test_valid_tailnet_network_configuration_logs_nothing(monkeypatch, caplog):
    monkeypatch.setenv("PENTACLE_CONN_DIAG_TAILNET_NETWORKS", "192.0.2.0/24,2001:db8::/32")
    caplog.set_level(logging.INFO)
    server = Server()
    assert len(server._conn_diag_tailnet_networks) == 2
    assert not [r for r in caplog.records if r.name == LOGGER]
