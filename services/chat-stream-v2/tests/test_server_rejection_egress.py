"""Correction 2: rejection at the direct-send and retained-trust boundaries."""
from __future__ import annotations

import asyncio
import json

import pytest

from _shared import operator_auth
from server import _EncodedFrame
from test_connection_logging import connected, daemon, issue_seat, records, signed_hello
from test_server_fanout_auth_boundary import peer, sensitive_frames, sent_since


def bad_hello(daemon, welcome):
    _, hello = signed_hello(daemon, welcome)
    hello["auth_v2"]["proof"] = operator_auth.encode_b64url(b"x" * operator_auth.AUTH_PROOF_BYTES)
    return hello


def test_direct_inventory_waiting_for_send_lock_cannot_land_after_rejection(daemon, monkeypatch):
    async def run():
        await sensitive_frames(daemon)
        async with connected(daemon, peer("native")) as (socket, welcome, _):
            pending, rejected = asyncio.Event(), asyncio.Event()
            original_send = daemon._send_direct
            original_failed = daemon._diag_auth_failure

            async def observed_send(target, frames, **kwargs):
                if isinstance(frames, list) and any(f.get("type") == "list_sessions.ok" for f in frames):
                    pending.set()
                return await original_send(target, frames, **kwargs)

            def observed_failed(target, *args, **kwargs):
                original_failed(target, *args, **kwargs)
                if target is socket and target in daemon._operator_rejected:
                    rejected.set()

            monkeypatch.setattr(daemon, "_send_direct", observed_send)
            monkeypatch.setattr(daemon, "_diag_auth_failure", observed_failed)
            lock = daemon._client_send_locks[socket]
            await lock.acquire()
            try:
                socket.feed({"type": "list_sessions", "request_id": "inventory-before-rejection"})
                await asyncio.wait_for(pending.wait(), 1)
                socket.feed(bad_hello(daemon, welcome))
                await asyncio.wait_for(rejected.wait(), 1)
                start = len(socket.sent)
            finally:
                lock.release()
            await socket.receive(kind="hello.error")
            delivered = sent_since(socket, start)
            assert not any(f.get("active") for f in delivered), delivered
    asyncio.run(run())


@pytest.mark.parametrize("transport", ["remote", "native", "origin"])
def test_previously_authenticated_rejected_socket_cannot_read_inventory(daemon, transport):
    async def run():
        await sensitive_frames(daemon)
        async with connected(daemon, peer(transport)) as (socket, welcome, _):
            _, hello = signed_hello(daemon, welcome)
            assert (await socket.rpc(hello))["type"] == "ready"
            assert (await socket.rpc(hello))["error_code"] == "operator_auth_invalid"
            assert socket in daemon._operator_rejected
            result = await socket.rpc({"type": "list_sessions"})
            assert not result.get("active"), result
            assert result.get("error_code") == "authentication_required", result
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["batch", "async", "history"])
def test_rejection_between_direct_frames_stops_payload_and_closes_iterator(daemon, monkeypatch, mode):
    async def run():
        async with connected(daemon) as (socket, welcome, _):
            first_sent, resume, rejected, closed = (asyncio.Event() for _ in range(4))
            original_send, original_failed = socket.send, daemon._diag_auth_failure
            kind = "request_stream_events.chunk" if mode == "history" else "fetch_blob.chunk"
            first = {"type": kind, "request_id": "stream-race", "data": "permitted-before-rejection"}
            second = {"type": kind, "request_id": "stream-race", "data": "must-never-send"}

            async def gated_send(payload):
                await original_send(payload)
                if json.loads(payload).get("data") == first["data"]:
                    first_sent.set()
                    await resume.wait()

            def observed_failure(target, *args, **kwargs):
                original_failed(target, *args, **kwargs)
                if target is socket and target in daemon._operator_rejected:
                    rejected.set()

            async def source():
                try:
                    yield _EncodedFrame(kind, json.dumps(first))
                    await asyncio.sleep(0)
                    yield _EncodedFrame(kind, json.dumps(second))
                    raise AssertionError("sensitive iterator was drained after terminal refusal")
                finally:
                    closed.set()

            monkeypatch.setattr(socket, "send", gated_send)
            monkeypatch.setattr(daemon, "_diag_auth_failure", observed_failure)
            frames = [first, second] if mode == "batch" else source()
            sending = asyncio.create_task(daemon._send_direct(socket, frames, interleave_history=mode == "history"))
            try:
                await asyncio.wait_for(first_sent.wait(), 1)
                socket.feed(bad_hello(daemon, welcome))
                await asyncio.wait_for(rejected.wait(), 1)
            finally:
                resume.set()
            assert await sending is False
            await socket.receive(kind="hello.error")
            delivered = sent_since(socket, 1)
            assert [f.get("data") for f in delivered if "data" in f] == [first["data"]]
            refusals = [f for f in delivered if f.get("request_id") == "stream-race" and f.get("error_code")]
            assert refusals == [{"type": kind.rsplit(".", 1)[0] + ".error", "request_id": "stream-race", "error_code": "authentication_required"}]
            assert daemon._connection_diagnostics[socket].non_handshake_tx == 2
            if mode != "batch":
                assert closed.is_set()
    asyncio.run(run())


@pytest.mark.parametrize("interleave", [False, True])
def test_rejection_during_direct_iterator_await_is_rechecked(daemon, monkeypatch, interleave):
    async def run():
        async with connected(daemon) as (socket, welcome, _):
            fetching, resume, rejected, closed = (asyncio.Event() for _ in range(4))
            failed = daemon._diag_auth_failure
            def observed_failure(target, *args, **kwargs):
                failed(target, *args, **kwargs)
                if target is socket and target in daemon._operator_rejected:
                    rejected.set()
            monkeypatch.setattr(daemon, "_diag_auth_failure", observed_failure)
            async def source():
                try:
                    fetching.set()
                    await resume.wait()
                    yield {"type": "fetch_blob.chunk", "request_id": "await-race", "data": "withheld"}
                    raise AssertionError("must stop after terminal refusal")
                finally:
                    closed.set()
            sending = asyncio.create_task(daemon._send_direct(socket, source(), interleave_history=interleave))
            try:
                await asyncio.wait_for(fetching.wait(), 1)
                socket.feed(bad_hello(daemon, welcome))
                await asyncio.wait_for(rejected.wait(), 1)
            finally:
                resume.set()
            assert await sending is False
            reply = await socket.receive(request_id="await-race")
            assert reply == {"type": "fetch_blob.error", "request_id": "await-race", "error_code": "authentication_required"}
            assert closed.is_set()
            assert daemon._connection_diagnostics[socket].non_handshake_tx == 1
    asyncio.run(run())


def test_rejection_overrides_auth_context_computed_before_wait(daemon, monkeypatch):
    async def run():
        await sensitive_frames(daemon)
        async with connected(daemon, peer("remote")) as (socket, welcome, _):
            _, hello = signed_hello(daemon, welcome)
            assert (await socket.rpc(hello))["type"] == "ready"
            computed, resume = asyncio.Event(), asyncio.Event()
            original = daemon._auth_context
            async def waiting_context(target, msg):
                context = await original(target, msg)
                if msg.get("request_id") == "cached-auth":
                    assert context["operator_authenticated"]
                    computed.set()
                    await resume.wait()
                return context
            monkeypatch.setattr(daemon, "_auth_context", waiting_context)
            socket.feed({"type": "list_sessions", "request_id": "cached-auth"})
            try:
                await asyncio.wait_for(computed.wait(), 1)
                assert (await socket.rpc(hello))["error_code"] == "operator_auth_invalid"
            finally:
                resume.set()
            reply = await socket.receive(request_id="cached-auth")
            assert reply == {"type": "list_sessions.error", "request_id": "cached-auth", "error_code": "authentication_required"}
            assert not daemon._operator_authenticated(socket)
            assert socket not in daemon._connection_trust
    asyncio.run(run())


def test_auth_context_cannot_return_operator_trust_after_its_own_await(daemon, monkeypatch):
    import threading
    import server as server_module
    async def run():
        async with connected(daemon) as (socket, welcome, _):
            _, hello = signed_hello(daemon, welcome)
            assert (await socket.rpc(hello))["type"] == "ready"
            entered, resume = threading.Event(), threading.Event()
            def verify(_token):
                entered.set()
                assert resume.wait(2)
                return True
            monkeypatch.setattr(server_module.local_admin, "verify", verify)
            pending = asyncio.create_task(daemon._auth_context(socket, {"type": "list_sessions", "local_admin_token": "synthetic-admin"}))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                assert (await socket.rpc(hello))["error_code"] == "operator_auth_invalid"
            finally:
                resume.set()
            context = await pending
            assert not context["operator_authenticated"]
            assert not context["operator_trusted"]
            assert context["operator_principal"] == context["credential_id"] == ""
    asyncio.run(run())


def test_rejection_during_actor_validation_prevents_handler_invocation(daemon, monkeypatch):
    async def run():
        token, stream_id = await issue_seat(daemon)
        async with connected(daemon) as (socket, welcome, _):
            entered, resume = asyncio.Event(), asyncio.Event()
            original_require = daemon.store.require_current_actor
            calls = []
            async def parked_require(*args, **kwargs):
                entered.set()
                await resume.wait()
                return await original_require(*args, **kwargs)
            original_handler = daemon.handlers["list_sessions"]
            async def observed_handler(msg):
                calls.append(msg)
                return await original_handler(msg)
            monkeypatch.setattr(daemon.store, "require_current_actor", parked_require)
            daemon.handlers["list_sessions"] = observed_handler
            socket.feed({"type": "list_sessions", "request_id": "actor-race", "from_stream_id": stream_id, "stream_token": token})
            try:
                await asyncio.wait_for(entered.wait(), 1)
                assert (await socket.rpc(bad_hello(daemon, welcome)))["error_code"] == "operator_auth_invalid"
            finally:
                resume.set()
            reply = await socket.receive(request_id="actor-race")
            assert reply["error_code"] == "authentication_required"
            assert calls == []
    asyncio.run(run())


def test_rejection_preserves_error_control_and_strips_application_payload(daemon):
    async def run():
        async with connected(daemon, peer("remote")) as (socket, welcome, _):
            _, hello = signed_hello(daemon, welcome)
            assert (await socket.rpc(hello))["type"] == "ready"
            assert (await socket.rpc(hello))["error_code"] == "operator_auth_invalid"
            assert (await socket.rpc({"type": "list_sessions"}))["error_code"] == "authentication_required"
            assert (await socket.rpc({"type": "ping"}))["type"] == "pong"
            await daemon._send_direct(socket, json.dumps({"type": "list_sessions.error", "request_id": "error", "error_code": "internal_error", "error": "private exception", "active": ["private row"]}))
            assert await socket.receive(request_id="error") == {"type": "list_sessions.error", "request_id": "error", "error_code": "internal_error"}
    asyncio.run(run())


def test_rejected_operator_cannot_reprove_but_same_credential_works_on_fresh_socket(daemon, caplog):
    async def run():
        await sensitive_frames(daemon)
        async with connected(daemon, peer("remote")) as (socket, welcome, _):
            cid, envelope = daemon.operator_credential_registry.issue("pentacle")
            identity = operator_auth.decode_envelope(envelope)
            def proof_for(challenge):
                return {"type": "hello", "client": "pentacle", "subscribe": {"mode": "rpc", "snapshot": False}, "auth_v2": {"scheme": operator_auth.AUTH_SCHEME, "credential_id": cid, "proof": operator_auth.make_proof(identity["proof_key"], challenge["auth"]["operator"]["nonce"], cid, "pentacle")}}
            hello = proof_for(welcome)
            assert (await socket.rpc(hello))["type"] == "ready"
            assert (await socket.rpc({"type": "list_sessions"}))["active"]
            count = daemon._connection_diagnostics[socket].non_handshake_tx
            assert (await socket.rpc(hello))["error_code"] == "operator_auth_invalid"
            assert (await socket.rpc(hello))["error_code"] == "operator_auth_invalid"
            assert (await socket.rpc({"type": "hello", "client": "native-bootstrap"}))["error_code"] == "operator_auth_invalid"
            assert not daemon._operator_authenticated(socket)
            assert daemon._connection_diagnostics[socket].non_handshake_tx == count
            async with connected(daemon, peer("remote")) as (fresh, fresh_welcome, _):
                assert (await fresh.rpc(proof_for(fresh_welcome)))["type"] == "ready"
                assert (await fresh.rpc({"type": "list_sessions"}))["active"]
        from test_deploy_script import deploy_mod
        rows = records(caplog)
        close = rows[-1]
        assert close["event"] == "close" and close["non_handshake_tx"] >= count
        assert any(row["event"] == "auth_ok" and row["conn_id"] == close["conn_id"] for row in rows)
        close = {**close, "auth_state": "failed", "close_code": 1011, "close_reason": "keepalive_ping_timeout"}
        result = deploy_mod.classify_slow_consumer_lines(["conn_diag " + json.dumps(row) for row in [*rows[:-1], close]])
        assert result["failure_count"] == 1 and result["excluded_auth_failure_count"] == 0
    asyncio.run(run())
