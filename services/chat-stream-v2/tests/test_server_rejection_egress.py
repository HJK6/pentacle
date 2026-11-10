"""Correction 2: rejection at the direct-send and retained-trust boundaries."""
from __future__ import annotations

import asyncio

import pytest

from _shared import operator_auth
from test_connection_logging import connected, daemon, signed_hello
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
