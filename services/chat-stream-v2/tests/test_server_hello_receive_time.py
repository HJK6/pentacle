"""Receive-time auth through the real application read/dispatch/send path."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from _shared import operator_auth
import server as server_module
from test_connection_logging import (
    ScriptedSocket, connected, daemon, one, signed_hello,
)


def test_timely_hello_survives_eleven_second_ordering_wait(daemon, monkeypatch):
    async def run():
        async with connected(daemon, ScriptedSocket("192.0.2.20")) as (socket, welcome, _):
            _, hello = signed_hello(daemon, welcome)
            received = asyncio.Event()
            receive_age = []
            original = daemon._diag_rx
            def observed(peer, raw):
                original(peer, raw)
                if json.loads(raw).get("type") == "hello":
                    receive_age.append(time.time() - (welcome["auth"]["operator"]["expires_at"] - 10))
                    received.set()
            monkeypatch.setattr(daemon, "_diag_rx", observed)
            async with daemon._host_stats_order_lock:
                socket.feed(hello)
                await asyncio.wait_for(received.wait(), 1)
                assert receive_age[0] < 1
                await asyncio.sleep(11)
            reply = await socket.receive()
            assert reply["type"] == "ready", reply
            assert daemon._operator_authenticated(socket)
    asyncio.run(run())


@pytest.mark.parametrize("case", ["late", "client_time", "replay", "cross_socket", "wrong", "revoked"])
def test_receive_time_preserves_nonce_and_credential_rejections(daemon, monkeypatch, case):
    async def run():
        async with connected(daemon, ScriptedSocket("192.0.2.20")) as (socket, welcome, _):
            cid, hello = signed_hello(daemon, welcome)
            assert operator_auth.AUTH_NONCE_TTL_SECONDS == 10.0
            if case in {"late", "client_time"}:
                expires = welcome["auth"]["operator"]["expires_at"]
                monkeypatch.setattr(server_module.time, "time", lambda: expires + 1)
                if case == "client_time":
                    hello.update(received_at=expires - 9, _hello_received_at=expires - 9, timestamp=expires - 9)
            elif case == "wrong":
                hello["auth_v2"]["proof"] = operator_auth.encode_b64url(b"x" * operator_auth.AUTH_PROOF_BYTES)
            elif case == "revoked":
                daemon.operator_credential_registry.revoke(cid)
            elif case == "replay":
                assert (await socket.rpc(hello))["type"] == "ready"
            elif case == "cross_socket":
                async with connected(daemon, ScriptedSocket("192.0.2.21")) as (other, _, _):
                    reply = await other.rpc(hello)
                    assert reply["error_code"] == "operator_auth_invalid"
                    assert not daemon._operator_authenticated(other)
                return
            reply = await socket.rpc(hello)
            assert reply["error_code"] == "operator_auth_invalid"
            # The first attempt consumes the challenge, including late or bad proof.
            assert socket not in daemon._operator_challenges
            again = await socket.rpc(hello)
            assert again["error_code"] == "operator_auth_invalid"
    asyncio.run(run())


@pytest.mark.parametrize("delivery", ["handshake", "direct", "broadcast", "before_rejection"])
def test_non_handshake_count_covers_the_whole_connection(daemon, caplog, delivery):
    async def run():
        async with connected(daemon) as (socket, welcome, _):
            if delivery == "direct":
                await daemon._send_direct(socket, [{"type": "counter.fixture"}])
                await socket.receive(kind="counter.fixture")
            elif delivery in {"broadcast", "before_rejection"}:
                await daemon.broadcast({"type": "counter.fixture"})
                await socket.receive(kind="counter.fixture")
            if delivery in {"handshake", "before_rejection"}:
                _, hello = signed_hello(daemon, welcome)
                hello["auth_v2"]["proof"] = operator_auth.encode_b64url(b"x" * operator_auth.AUTH_PROOF_BYTES)
                assert (await socket.rpc(hello))["type"] == "hello.error"
        close = one(caplog, "close")
        assert close["non_handshake_tx"] == (0 if delivery == "handshake" else 1)
    asyncio.run(run())
