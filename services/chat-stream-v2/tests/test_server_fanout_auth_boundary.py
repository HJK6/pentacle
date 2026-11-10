"""D3 application read/fan-out controls; real auth, no injected trust maps."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from websockets.datastructures import Headers

from _shared import operator_auth
from test_connection_logging import ScriptedSocket, connected, daemon, issue_seat, signed_hello


def peer(transport):
    socket = ScriptedSocket("192.0.2.20" if transport == "remote" else "127.0.0.1")
    socket.request = SimpleNamespace(headers=Headers(
        [("Origin", "https://untrusted.example")] if transport == "origin" else []))
    return socket


async def sensitive_frames(daemon):
    await issue_seat(daemon)
    await daemon.store.open_session("fixture", "other", provider="codex", pane_status="pane_alive")
    await daemon.sessions.refresh()
    sessions = daemon.sessions.list_open()
    assert any(row["stream_id"] == "fixture:seat" for row in sessions)
    return [
        {"type": "session.inventory", "sessions": sessions},
        {"type": "chat.event", "event": {"stream_id": "fixture:seat", "text": "private fixture"}},
        {"type": "fanout.fixture", "marker": "must also stop after rejection"},
    ]


async def flush(daemon):
    for _ in range(100):
        await asyncio.sleep(0)
        if all(q.empty() for q in daemon._client_send_queues.values()):
            await asyncio.sleep(0)
            return
    raise AssertionError("writer queues did not drain")


def sent_since(socket, start):
    return [json.loads(raw) for raw in socket.sent[start:]]


@pytest.mark.parametrize("transport", ["remote", "native", "origin"])
@pytest.mark.parametrize("stage", ["no_hello", "delayed_hello", "rejected_proof"])
def test_application_fanout_auth_matrix(daemon, monkeypatch, transport, stage):
    async def run():
        frames = await sensitive_frames(daemon)
        async with connected(daemon, peer(transport)) as (socket, welcome, _):
            received = asyncio.Event()
            original = daemon._diag_rx
            def observed(ws, raw):
                original(ws, raw)
                received.set()
            monkeypatch.setattr(daemon, "_diag_rx", observed)
            _, hello = signed_hello(daemon, welcome)
            if stage == "rejected_proof":
                hello["auth_v2"]["proof"] = operator_auth.encode_b64url(b"x" * operator_auth.AUTH_PROOF_BYTES)
                assert (await socket.rpc(hello))["error_code"] == "operator_auth_invalid"
            if stage == "delayed_hello":
                await daemon._host_stats_order_lock.acquire()
                socket.feed(hello)
                await asyncio.wait_for(received.wait(), 1)
            try:
                start = len(socket.sent)
                for frame in frames:
                    await daemon.broadcast(frame)
                await flush(daemon)
                delivered = sent_since(socket, start)
                eligible = transport == "native" and stage != "rejected_proof"
                if eligible:
                    assert [f["type"] for f in delivered] == [f["type"] for f in frames]
                    assert delivered[0]["sessions"]
                else:
                    assert delivered == [], (transport, stage, delivered)
            finally:
                if stage == "delayed_hello":
                    daemon._host_stats_order_lock.release()
                    assert (await socket.receive(kind="ready"))["type"] == "ready"
            if stage == "rejected_proof":
                retry = await socket.rpc({"type": "hello", "client": "native-bootstrap", "subscribe": {"mode": "rpc"}})
                assert retry["type"] == "hello.error"
                start = len(socket.sent)
                for frame in frames:
                    await daemon.broadcast(frame)
                await flush(daemon)
                assert sent_since(socket, start) == []
    asyncio.run(run())


def test_fresh_native_no_claim_hello_still_gets_bootstrap_ready(daemon):
    async def run():
        async with connected(daemon, peer("native")) as (socket, _, _):
            reply = await socket.rpc({"type": "hello", "client": "native-bootstrap",
                                      "subscribe": {"mode": "rpc", "snapshot": False}})
            assert reply["type"] == "ready"
    asyncio.run(run())


class OrderedPeers(set):
    """Keep real registration/discard but choose both group representatives."""
    def __init__(self, order):
        super().__init__(order)
        self.order = order

    def __iter__(self):
        return (peer for peer in self.order if peer in self)


@pytest.mark.parametrize("rejected_first", [False, True])
def test_mixed_native_group_never_reuses_eligible_projection_for_rejected_peer(daemon, rejected_first):
    async def run():
        frames = await sensitive_frames(daemon)
        async with connected(daemon) as (eligible, _, _), connected(daemon) as (rejected, welcome, _):
            _, hello = signed_hello(daemon, welcome)
            hello["auth_v2"]["proof"] = operator_auth.encode_b64url(b"x" * operator_auth.AUTH_PROOF_BYTES)
            assert (await rejected.rpc(hello))["error_code"] == "operator_auth_invalid"
            daemon._clients = OrderedPeers([rejected, eligible] if rejected_first else [eligible, rejected])
            good_start, bad_start = len(eligible.sent), len(rejected.sent)
            for frame in frames:
                await daemon.broadcast(frame)
            await flush(daemon)
            assert [f["type"] for f in sent_since(eligible, good_start)] == [f["type"] for f in frames]
            assert sent_since(rejected, bad_start) == []
    asyncio.run(run())


@pytest.mark.parametrize("principal", ["operator", "seat", "scoped"])
def test_valid_principals_preserve_fanout_policy(daemon, principal):
    async def run():
        frames = await sensitive_frames(daemon)
        # Scoped local credentials retain their existing single-stream projection;
        # operator and seat prove remote access without the native exception.
        socket = peer("native" if principal == "scoped" else "remote")
        async with connected(daemon, socket) as (socket, welcome, _):
            if principal == "seat":
                hello = {"type": "hello", "client": "agent-orch", "stream_id": "fixture:seat",
                         "stream_token": "synthetic-seat-token", "subscribe": {"mode": "rpc"}}
            else:
                _, hello = signed_hello(daemon, welcome, client="pentacle-mobile" if principal == "scoped" else "pentacle",
                                        scope={"stream": "fixture:seat"} if principal == "scoped" else None)
            reply = await socket.rpc(hello)
            assert reply["type"] in {"ready", "hello"}, reply
            await flush(daemon)
            start = len(socket.sent)
            for frame in frames[:2]:
                await daemon.broadcast(frame)
            await daemon.broadcast({"type": "chat.event", "event": {"stream_id": "fixture:other", "text": "other scope"}})
            await flush(daemon)
            delivered = sent_since(socket, start)
            assert {f["type"] for f in delivered} >= {"session.inventory", "chat.event"}
            if principal == "scoped":
                assert all(row["stream_id"] == "fixture:seat" for f in delivered if f["type"] == "session.inventory" for row in f["sessions"])
                assert all(f["event"]["stream_id"] == "fixture:seat" for f in delivered if f["type"] == "chat.event")
    asyncio.run(run())
