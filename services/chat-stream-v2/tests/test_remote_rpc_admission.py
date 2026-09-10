"""Network admission must precede side effects and private inventory reads."""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from _shared.operator_auth import ConnectionTrust
from server import Server
from store import STREAM_TOKEN_HASH_VERSION


class Peer:
    def __init__(self, address="192.0.2.10"):
        self.remote_address = (address, 54321) if address is not None else None


@pytest.mark.parametrize("verb", ["spawn", "send", "tell", "spawn_freeze", "spawn_unfreeze", "grant_token", "list_sessions", "request_stream_events"])
def test_remote_unauthenticated_rpc_never_reaches_handler(verb):
    async def run():
        daemon = Server()
        calls = []
        async def handler(msg):
            calls.append(msg)
            return {"type": f"{verb}.ok"}
        daemon.handlers[verb] = handler
        reply = await daemon._dispatch(json.dumps({
            "type": verb, "request_id": "denied", "client": "pentacle",
            "from_stream_id": "local:claimed", "_auth_context": {"operator_authenticated": True},
        }), websocket=Peer())
        assert reply == [{"type": f"{verb}.error", "error_code": "authentication_required", "request_id": "denied"}]
        assert calls == []
    asyncio.run(run())


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_actual_loopback_keeps_local_cli_bootstrap(address):
    async def run():
        daemon = Server()
        async def handler(msg):
            return {"type": "grant_token.ok"}
        daemon.handlers["grant_token"] = handler
        assert (await daemon._dispatch('{"type":"grant_token"}', websocket=Peer(address)))[0]["type"] == "grant_token.ok"
    asyncio.run(run())


def test_unknown_peer_is_not_local_bootstrap():
    async def run():
        daemon = Server()
        reply = await daemon._dispatch('{"type":"spawn_freeze"}', websocket=Peer(None))
        assert reply[0]["error_code"] == "authentication_required"
    asyncio.run(run())


def test_remote_hello_without_proof_has_no_snapshot_or_subscription():
    async def run():
        daemon = Server()
        peer = Peer()
        reply = await daemon._dispatch('{"type":"hello","client":"pentacle"}', websocket=peer)
        assert reply == [{"type": "hello.error", "error_code": "authentication_required"}]
        assert peer not in daemon._clients
    asyncio.run(run())


def test_authenticated_operator_can_invoke_protected_rpc():
    async def run():
        daemon = Server()
        peer = Peer()
        daemon._connection_trust[peer] = ConnectionTrust(
            transport="v2", client_kind="pentacle", operator_trusted=True,
            credential_id="00000000-0000-4000-8000-000000000001",
        )
        async def handler(msg):
            assert msg["_auth_context"]["operator_authenticated"]
            return {"type": "spawn_freeze.ok"}
        daemon.handlers["spawn_freeze"] = handler
        assert (await daemon._dispatch('{"type":"spawn_freeze"}', websocket=peer))[0]["type"] == "spawn_freeze.ok"
    asyncio.run(run())


def test_bound_seat_token_is_revalidated_and_cannot_grant_admin_authority():
    async def run():
        token = "synthetic-seat-token"
        state = {"stream_id": "local:seat", "status": "open", "token_hash_version": STREAM_TOKEN_HASH_VERSION}
        async def token_state(digest):
            assert digest == hashlib.sha256(token.encode()).hexdigest()
            return dict(state) if state else None
        daemon = Server(store=SimpleNamespace(stream_token_state=token_state))
        peer = Peer()
        async def handler(msg):
            assert msg["_auth_context"]["stream_id"] == "local:seat"
            return {"type": "send.ok"}
        daemon.handlers["send"] = handler
        daemon.handlers["grant_token"] = handler
        first = {"type": "send", "from_stream_id": "local:seat", "stream_token": token}
        assert (await daemon._dispatch(json.dumps(first), websocket=peer))[0]["type"] == "send.ok"
        assert (await daemon._dispatch('{"type":"send"}', websocket=peer))[0]["type"] == "send.ok"
        assert (await daemon._dispatch('{"type":"grant_token"}', websocket=peer))[0]["error_code"] == "operator_auth_required"
        assert (await daemon._dispatch('{"type":"spawn_freeze"}', websocket=peer))[0]["error_code"] == "operator_auth_required"
        assert (await daemon._dispatch('{"type":"send","stream_token":"wrong"}', websocket=peer))[0]["error_code"] == "authentication_required"
        state.clear()
        assert (await daemon._dispatch('{"type":"send"}', websocket=peer))[0]["error_code"] == "authentication_required"
    asyncio.run(run())


def test_wire_hello_authentication_finishes_before_back_to_back_rpc(monkeypatch):
    from store import Store
    from websockets.asyncio.client import connect

    async def run():
        store = Store(":memory:")
        store.start()
        token = "synthetic-wire-seat-token"
        await store.open_session("local", "seat")
        await store.grant_stream_token("local", "seat", hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
        daemon = Server(port=0, store=store)
        # Treat the test transport as remote; address classification itself is
        # covered above. Authentication, real sockets and store remain real.
        monkeypatch.setattr(daemon, "_is_loopback_client", lambda peer: False)
        original = store.stream_token_state
        async def delayed_lookup(digest):
            await asyncio.sleep(.03)
            return await original(digest)
        monkeypatch.setattr(store, "stream_token_state", delayed_lookup)
        calls = []
        async def handler(msg):
            calls.append(msg["_auth_context"]["stream_id"])
            return {"type": "send.ok"}
        daemon.handlers["send"] = handler
        await daemon.bind()
        port = daemon._ws_server.sockets[0].getsockname()[1]
        try:
            async with connect(f"ws://127.0.0.1:{port}") as ws:
                assert json.loads(await ws.recv())["type"] == "welcome"
                await ws.send(json.dumps({"type": "send", "request_id": "unauth"}))
                assert json.loads(await ws.recv())["error_code"] == "authentication_required"
                assert calls == []
                assert not daemon._host_stats_clients
                actual_peer = next(iter(daemon._clients))
                assert daemon._frame_for_client(actual_peer, "session.inventory", {"sessions": []}) is None
                await ws.send(json.dumps({
                    "type": "hello", "client": "agent-orch", "from_stream_id": "local:seat",
                    "stream_token": token, "subscribe": {"mode": "rpc", "snapshot": False},
                }))
                await ws.send(json.dumps({"type": "send", "request_id": "after-hello"}))
                frames = []
                while not any(frame.get("request_id") == "after-hello" for frame in frames):
                    frames.append(json.loads(await asyncio.wait_for(ws.recv(), 2)))
                assert frames[0]["type"] == "ready"
                assert frames[-1]["type"] == "send.ok"
                assert calls == ["local:seat"]
        finally:
            await daemon.close()
            store.stop()
    asyncio.run(run())


def test_system_producer_rpc_does_not_gain_inventory_subscription(monkeypatch):
    monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN", "synthetic-system-token")
    async def run():
        daemon = Server()
        peer = Peer()
        hello = {"type": "hello", "from_stream_id": "service:scheduler",
                 "stream_token": "synthetic-system-token", "subscribe": {"mode": "rpc", "snapshot": False}}
        frames = await daemon._dispatch(json.dumps(hello), websocket=peer)
        assert frames == [{"type": "ready", "snapshot": False, "events_mode": "full"}]
        assert peer not in daemon._clients
        hello["subscribe"] = {"all": True}
        assert (await daemon._dispatch(json.dumps(hello), websocket=Peer()))[0]["error_code"] == "authentication_required"
        async def admin(msg):
            assert msg["_auth_context"]["service_authenticated"]
            return {"type": "spawn_freeze.ok"}
        daemon.handlers["spawn_freeze"] = admin
        request = {"type": "spawn_freeze", "from_stream_id": "service:scheduler", "stream_token": "synthetic-system-token"}
        assert (await daemon._dispatch(json.dumps(request), websocket=peer))[0]["type"] == "spawn_freeze.ok"
    asyncio.run(run())
