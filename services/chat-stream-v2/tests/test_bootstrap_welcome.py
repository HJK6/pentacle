"""A client connecting during adoption must receive a complete first bootstrap."""

import asyncio
import json
import time

import pytest
import websockets

from server import Server
from sessions import Sessions
from store import Store


def test_early_hello_and_broadcast_wait_for_complete_bootstrap():
    async def run():
        store = Store(":memory:")
        store.start()
        server = None
        try:
            await store.open_session("local", "seeded", provider="codex")
            sessions = Sessions(store, local_host="local")
            server = Server(port=0, store=store, sessions=sessions, local_host="local")
            # Also runs against the pre-fix server: its handler ignores this gate.
            server.bootstrap_ready = asyncio.Event()
            server.inventory_ready.clear()
            port = await server.bind()
            async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
                await socket.send(json.dumps({"type": "hello", "client": "bootstrap-test"}))
                await server.broadcast({"type": "session.inventory", "sessions": []})
                receive = asyncio.create_task(socket.recv())
                try:
                    done, _ = await asyncio.wait({receive}, timeout=0.05)
                    assert not done, "an application frame escaped before bootstrap readiness"
                    assert not server._operator_challenges, "nonce was minted before readiness"
                    await sessions.refresh()
                    server.inventory_ready.set()
                    # Inventory alone must not release an unresolved welcome.
                    assert not receive.done()
                    server.runtime_sha = "a" * 40
                    released_at = time.time()
                    server.bootstrap_ready.set()
                    # A competing startup broadcast must not precede welcome.
                    await server.broadcast({"type": "session.inventory", "sessions": []})
                    welcome = json.loads(await asyncio.wait_for(receive, 1))
                    assert welcome["type"] == "welcome"
                    assert welcome["runtime_sha"] == "a" * 40
                    assert welcome["auth"]["operator"]["expires_at"] > released_at + 9
                    async with asyncio.timeout(1):
                        while True:
                            frame = json.loads(await socket.recv())
                            if frame["type"] == "snapshot":
                                break
                    assert [row["stream_id"] for row in frame["sessions"]] == ["local:seeded"]
                finally:
                    receive.cancel()
                    await asyncio.gather(receive, return_exceptions=True)
            # A later connection uses the same normal welcome/snapshot contract.
            async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
                assert json.loads(await socket.recv())["runtime_sha"] == "a" * 40
        finally:
            if server is not None:
                await asyncio.wait_for(server.close(), 2)
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("ending", ["disconnect", "shutdown", "cancel", "ready_and_closed"])
def test_pending_bootstrap_cleanup(ending):
    class Socket:
        remote_address = ("127.0.0.1", 12345)

        def __init__(self):
            self.closed = asyncio.Event()
            self.sent = []

        async def wait_closed(self):
            await self.closed.wait()

        async def send(self, value):
            self.sent.append(json.loads(value))

        async def close(self, **_kwargs):
            self.closed.set()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self.closed.wait()
            raise StopAsyncIteration

    async def run():
        server = Server(port=0)
        server.bootstrap_ready = asyncio.Event()
        if ending == "shutdown":
            port = await server.bind()
            socket = await websockets.connect(f"ws://127.0.0.1:{port}")
            try:
                await asyncio.wait_for(server.close(), 1)
                await asyncio.wait_for(socket.wait_closed(), 1)
                assert not server._client_writer_tasks
                assert not server._connection_diagnostics
            finally:
                await socket.close()
                await asyncio.wait_for(server.close(), 1)
            return
        socket = Socket()
        before = asyncio.all_tasks()
        handler = asyncio.create_task(server._handle_client(socket))
        try:
            for _ in range(4):
                await asyncio.sleep(0)
            if ending == "cancel":
                handler.cancel()
            else:
                socket.closed.set()
                if ending == "ready_and_closed":
                    server.bootstrap_ready.set()
            await asyncio.wait_for(asyncio.gather(handler, return_exceptions=True), 1)
            assert not socket.sent
            assert not server._operator_challenges
            assert not server._clients
            assert not server._client_writer_tasks
            assert not server._connection_diagnostics
            await asyncio.sleep(0)
            assert not (asyncio.all_tasks() - before), "bootstrap helper task leaked"
        finally:
            socket.closed.set()
            handler.cancel()
            await asyncio.gather(handler, return_exceptions=True)
            await asyncio.wait_for(server.close(), 1)
    asyncio.run(run())
