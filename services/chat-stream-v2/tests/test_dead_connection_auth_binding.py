"""A departed socket cannot reacquire connection state from a late token lookup."""
import asyncio
import hashlib
import json
import threading

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from _shared.operator_auth import OperatorCredentialRegistry
from comms import Comms
from server import Server, WS_MAX_SIZE
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store, STREAM_TOKEN_HASH_VERSION


class Socket:
    remote_address = ("127.0.0.1", 12345)

    async def send(self, frame):
        pass


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.001)


@pytest.mark.parametrize("when", ["during_lookup", "before_lookup", "live"])
@pytest.mark.parametrize("dot", [False, True])
def test_token_lookup_retains_request_context_without_dead_socket_binding(tmp_path, when, dot):
    async def run():
        store = Store(str(tmp_path / "state.db"))
        store.start()
        daemon = Server(store=store, dot_principal_stream_ids=["fixture:seat"] if dot else [])
        socket = Socket()
        released = threading.Event()
        entered = threading.Event()
        blocked = lookup = None
        writer = None
        try:
            await store.open_session("fixture", "seat")
            token = "synthetic-lifetime-token"
            await store.grant_stream_token("fixture", "seat", hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
            daemon._register_client(socket)
            writer = daemon._client_writer_tasks[socket]
            msg = {"type": "send", "stream_token": token, "from_stream_id": "fixture:seat"}
            assert (await daemon._auth_context(socket, msg))["token_verified"]
            assert daemon._client_authenticated_streams[socket] == "fixture:seat"
            if when == "before_lookup":
                daemon._unregister_client(socket)

            def barrier(conn):
                entered.set()
                assert released.wait(3)

            blocked = asyncio.create_task(store.submit(barrier))
            await until(entered.is_set)
            lookup = asyncio.create_task(daemon._auth_context(socket, msg))
            await until(lambda: store._queue.qsize() == 1)
            if when == "during_lookup":
                daemon._unregister_client(socket)
            released.set()
            context = await lookup
            await blocked
            assert context["token_verified"] is True
            assert context["stream_id"] == "fixture:seat"
            assert context["dot_principal"] is dot
            if when == "live":
                assert daemon._client_authenticated_streams[socket] == "fixture:seat"
                assert socket in daemon._client_token_hashes
                assert (socket in daemon._client_dot_connections) is dot
            else:
                assert {
                    "seat": socket in daemon._client_authenticated_streams,
                    "token": socket in daemon._client_token_hashes,
                    "dot": socket in daemon._client_dot_connections,
                } == {"seat": False, "token": False, "dot": False}
        finally:
            released.set()
            if lookup or blocked:
                await asyncio.gather(*(task for task in (lookup, blocked) if task), return_exceptions=True)
            daemon._unregister_client(socket)
            if writer:
                await asyncio.gather(writer, return_exceptions=True)
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("dot", [False, True])
def test_standalone_auth_helper_keeps_unmanaged_binding(tmp_path, dot):
    """Adapters which never register a socket still get the original binding."""
    async def run():
        store = Store(str(tmp_path / "standalone.db"))
        store.start()
        daemon = Server(store=store, dot_principal_stream_ids=["fixture:seat"] if dot else [])
        socket = Socket()
        token = "synthetic-standalone-token"
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        try:
            await store.open_session("fixture", "seat")
            await store.grant_stream_token("fixture", "seat", token_hash, STREAM_TOKEN_HASH_VERSION)
            for msg in (
                {"type": "send", "stream_token": token, "from_stream_id": "fixture:seat"},
                {"type": "send"},
            ):
                context = await daemon._auth_context(socket, msg)
                assert context["token_verified"] is True
                assert context["stream_id"] == "fixture:seat"
                assert context["dot_principal"] is dot
                assert daemon._client_authenticated_streams == {socket: "fixture:seat"}
                assert daemon._client_token_hashes == {socket: token_hash}
                assert (socket in daemon._client_dot_connections) is dot
                assert not hasattr(socket, "_pentacle_registration_live")
        finally:
            daemon._unregister_client(socket)
            assert not hasattr(socket, "_pentacle_registration_live")
            assert store.stop(timeout=3)
    asyncio.run(run())


class SyntheticProvider:
    """Only the external provider boundary is replaced; send/Store are real."""
    def __init__(self):
        self.screens = {}
        self.pastes = []
        self.enters = 0

    async def capture(self, name):
        return self.screens.get(name, "❯ \n")

    async def paste(self, name, text):
        self.pastes.append((name, text))
        self.screens[name] = self.screens.get(name, "❯ \n").rstrip() + "\n⏺ " + text + "\n❯ \n"

    async def send_enter(self, name):
        self.enters += 1

    async def run(self, *args, **kwargs):
        raise AssertionError(f"Unexpected external provider operation: {args!r}")


@pytest.mark.parametrize("explicit_token", [False, True])
def test_repeated_disconnected_sends_land_once_without_stale_bindings(tmp_path, explicit_token):
    """Three disconnect/replay cycles reuse one real daemon and Store worker."""
    async def run():
        store = Store(str(tmp_path / "reconnect.db"))
        store.start()
        provider = SyntheticProvider()
        sessions = Sessions(store, tmux=provider, local_host="fixture")
        spawnctl = SpawnCtl(store, sessions, tmux=provider)
        comms = Comms(store, sessions, spawnctl, attachment_root=tmp_path / "attachments")
        daemon = Server(store=store, sessions=sessions, spawnctl=spawnctl, comms=comms,
                        local_host="fixture", seat_operator_authority=False)
        daemon.operator_credential_registry = OperatorCredentialRegistry(tmp_path / "operator-credentials.json")
        token = "synthetic-reconnect-token"
        stream = "fixture:seat"
        sockets = []
        blockers = []
        releases = []
        listener = None
        owned_before = asyncio.all_tasks()
        counts = []

        async def reply(socket, kind, request_id):
            async with asyncio.timeout(3):
                while True:
                    frame = json.loads(await socket.recv())
                    if frame.get("request_id") == request_id:
                        assert frame["type"] == kind, frame
                        return frame

        async def authenticate(label):
            socket = await connect(address, ping_interval=None, close_timeout=1, proxy=None)
            sockets.append(socket)
            assert json.loads(await asyncio.wait_for(socket.recv(), 3))["type"] == "welcome"
            await socket.send(json.dumps({
                "type": "hello", "request_id": label, "client": "agent-orch",
                "from_stream_id": stream, "stream_token": token,
                "subscribe": {"mode": "rpc", "snapshot": False},
            }))
            await reply(socket, "ready", label)
            return socket

        async def receipts(request_id):
            return await store.submit(lambda conn: [dict(row) for row in conn.execute(
                "SELECT request_id, receipt_id, state, delivery, submission_confirmed, reason "
                "FROM v2_send_receipts WHERE request_id = ? ORDER BY rowid", (request_id,),
            )])

        def binding_counts():
            return {name: len(getattr(daemon, name)) for name in (
                "_client_authenticated_streams", "_client_token_hashes", "_client_dot_connections",
            )}

        try:
            await sessions.open("fixture", "seat", provider="claude", pane_status="pane_alive", visibility="visible")
            await store.grant_stream_token("fixture", "seat", hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
            listener = await serve(daemon._handle_client, "127.0.0.1", 0,
                                   max_size=WS_MAX_SIZE, ping_interval=None, close_timeout=1)
            address = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
            for cycle in range(3):
                socket = await authenticate(f"hello-{cycle}")
                (managed_socket,) = daemon._clients
                assert daemon._client_authenticated_streams[managed_socket] == stream
                assert managed_socket in daemon._client_token_hashes
                release, entered = threading.Event(), threading.Event()
                releases.append(release)

                def barrier(conn, release=release, entered=entered):
                    entered.set()
                    assert release.wait(10), "Store barrier was not released"

                blocker = asyncio.create_task(store.submit(barrier))
                blockers.append(blocker)
                await until(entered.is_set)
                request_id = f"disconnected-send-{cycle}"
                request = {"type": "send", "request_id": request_id, "stream_id": stream,
                           "text": f"Synthetic disconnected send {cycle}", "optimistic_id": f"opt-{cycle}"}
                if explicit_token:
                    request.update(stream_token=token, from_stream_id=stream)
                await socket.send(json.dumps(request))
                await until(lambda: store._queue.qsize() == 1 and len(daemon._detached_send_tasks) == 1)
                await socket.close()
                await until(lambda: not daemon._clients and daemon._admission_connections == 0)
                assert managed_socket not in daemon._client_authenticated_streams
                assert managed_socket not in daemon._client_token_hashes
                assert len(daemon._detached_send_tasks) == 1
                release.set()
                await asyncio.wait_for(blocker, 3)
                async with asyncio.timeout(10):
                    while daemon._detached_send_tasks:
                        await asyncio.sleep(.001)
                rows = await receipts(request_id)
                assert [row["state"] for row in rows] == ["accepted", "landed"], rows
                assert [row["delivery"] for row in rows] == ["accepted", "landed"], rows
                assert [row["submission_confirmed"] for row in rows] == [0, 1], rows
                assert len({row["receipt_id"] for row in rows}) == 1
                assert len(provider.pastes) == cycle + 1
                counts.append(binding_counts())
                enters_before_replay = provider.enters

                # Replaying the admitted request over a fresh authenticated
                # connection must return its durable result without another paste.
                reconnected = await authenticate(f"reconnect-{cycle}")
                await reconnected.send(json.dumps(request))
                result = await reply(reconnected, "send.result", request_id)
                assert result["delivery"] == "landed", result
                assert result["coalesced"] is True
                assert result["do_not_resubmit"] is True
                assert result["receipt_id"] == rows[0]["receipt_id"]
                await reconnected.send(json.dumps({
                    "type": "send.receipt.get", "request_id": request_id, "to_stream_id": stream,
                }))
                queried = await reply(reconnected, "send.receipt.get.ok", request_id)
                assert queried["found"] is True
                assert len(queried["receipts"]) == 1
                assert queried["receipts"][0]["state"] == "landed"
                # Existing replay semantics append one mirrored landed row,
                # without a second accepted claim or a second delivery.
                replay_rows = await receipts(request_id)
                assert replay_rows[:2] == rows
                assert len(replay_rows) == 3
                assert replay_rows[2]["state"] == replay_rows[2]["delivery"] == "landed"
                assert replay_rows[2]["submission_confirmed"] == 1
                assert replay_rows[2]["reason"] == f"coalesced_replay:{request_id}"
                assert queried["receipts"][0]["receipt_id"] == replay_rows[2]["receipt_id"]
                assert len(provider.pastes) == cycle + 1
                assert provider.enters == enters_before_replay
                await reconnected.close()
                await until(lambda: not daemon._clients and daemon._admission_connections == 0)
                counts.append(binding_counts())

            assert counts == [{
                "_client_authenticated_streams": 0,
                "_client_token_hashes": 0,
                "_client_dot_connections": 0,
            }] * 6
            assert len({text for _, text in provider.pastes}) == 3
            assert not daemon._client_send_queues
            assert not daemon._client_writer_tasks
            assert not daemon._connection_diagnostics
        finally:
            for release in releases:
                release.set()
            for socket in sockets:
                await asyncio.wait_for(socket.close(), 3)
            if listener:
                listener.close()
                await asyncio.wait_for(listener.wait_closed(), 3)
            owned = asyncio.all_tasks() - owned_before
            for task in owned:
                task.cancel()
            await asyncio.wait_for(asyncio.gather(*owned, *blockers, return_exceptions=True), 3)
            worker = store._thread
            assert store.stop(timeout=3)
            assert not worker.is_alive()
            assert not daemon._detached_send_tasks
            assert not daemon._clients
            assert all(socket.close_code is not None for socket in sockets)
            assert listener is None or not listener.is_serving()
    asyncio.run(run())
