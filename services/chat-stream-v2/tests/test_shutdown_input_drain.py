"""A tell or send in flight at shutdown finishes before the Store stops.

Journey (release L0, probe 6): a tell commits its pre-input record, SIGTERM
starts the daemon's graceful shutdown, and the connection closes. The tell
must still paste once and record its outcome, and an input that arrives after
the stop began is refused retryably, with nothing recorded, so the same id
delivers once after boot. Real Server, websocket, Comms and Store, and the real
`main.shutdown`; only the pane transport is synthetic.
"""
import asyncio
import hashlib
import json
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

import main as daemon_main
from comms import Comms
from server import Server, WS_MAX_SIZE
from sessions import Sessions
from shutdown_budget import ShutdownBudget
from spawnctl import SpawnCtl
from store import Store, STREAM_TOKEN_HASH_VERSION
from test_dead_connection_auth_binding import SyntheticProvider, until

TOKEN = "synthetic-shutdown-token"
SENDER, TARGET = "fixture:sender", "fixture:seat"


class _Stopped:
    async def stop(self, **_kwargs):
        return None


async def _daemon(path):
    store = Store(str(path)); store.start()
    provider = SyntheticProvider()
    sessions = Sessions(store, tmux=provider, local_host="fixture")
    spawnctl = SpawnCtl(store, sessions, tmux=provider)
    comms = Comms(store, sessions, spawnctl, attachment_root=path.parent / "attachments")
    daemon = Server(store=store, sessions=sessions, spawnctl=spawnctl, comms=comms,
                    local_host="fixture", seat_operator_authority=False)
    listener = await serve(daemon._handle_client, "127.0.0.1", 0, max_size=WS_MAX_SIZE,
                           ping_interval=None, close_timeout=1)
    daemon._ws_server = listener
    return store, provider, sessions, spawnctl, comms, daemon, listener


async def _seed(store, sessions):
    await sessions.open("fixture", "sender", provider="claude", pane_status="pane_alive")
    await sessions.open("fixture", "seat", provider="claude", pane_status="pane_alive", visibility="visible")
    await store.grant_stream_token("fixture", "sender", hashlib.sha256(TOKEN.encode()).hexdigest(),
                                   STREAM_TOKEN_HASH_VERSION)


async def _socket(listener):
    address = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
    socket = await connect(address, ping_interval=None, close_timeout=1, proxy=None)
    assert json.loads(await asyncio.wait_for(socket.recv(), 3))["type"] == "welcome"
    await socket.send(json.dumps({"type": "hello", "request_id": "hello", "client": "agent-orch",
                                  "from_stream_id": SENDER, "stream_token": TOKEN,
                                  "subscribe": {"mode": "rpc", "snapshot": False}}))
    async with asyncio.timeout(3):
        while json.loads(await socket.recv()).get("type") != "ready":
            pass
    return socket


def _tell(tell_id):
    return {"type": "tell", "request_id": tell_id, "tell_id": tell_id, "stream_id": TARGET,
            "message": "synthetic work at shutdown", "from_stream_id": SENDER, "stream_token": TOKEN}


def _shutdown(daemon, spawnctl, store):
    stopped = _Stopped()
    return daemon_main.shutdown(ShutdownBudget(10.0), server=daemon, spawnctl=spawnctl, tasks=[],
                                composites=[], lane_rulings=None, notify=stopped, assets=stopped,
                                lifecycle=stopped, store=store)


def test_tell_with_a_committed_pre_input_record_finishes_during_shutdown(tmp_path):
    async def run():
        store, provider, sessions, spawnctl, comms, daemon, listener = await _daemon(tmp_path / "drain.db")
        try:
            await _seed(store, sessions)
            prepared, resume = asyncio.Event(), asyncio.Event()
            original = comms._prepare_tell_input

            async def paused(*args, **kwargs):
                await original(*args, **kwargs)  # the pre-input record is committed
                prepared.set()
                await resume.wait()
            comms._prepare_tell_input = paused
            socket = await _socket(listener)
            await socket.send(json.dumps(_tell("in-flight")))
            await asyncio.wait_for(prepared.wait(), 5)
            stopping = asyncio.ensure_future(_shutdown(daemon, spawnctl, store))
            await asyncio.sleep(0.05)  # shutdown has begun while the tell waits to paste
            resume.set()
            await asyncio.wait_for(stopping, 15)
        finally:
            listener.close()
            store.stop(timeout=3)
        reopened = Store(str(tmp_path / "drain.db")); reopened.start()
        try:
            row = await reopened.get_tell_delivery("in-flight")
            assert (row["reply"]["delivery_status"], row["delivery"]["submission_attempts"]) == ("delivered", 1), row["reply"]
            assert len(provider.pastes) == 1 and provider.pastes[0][1].endswith("synthetic work at shutdown")
        finally:
            reopened.stop(timeout=3)
    asyncio.run(run())


def _late(verb, request_id):
    if verb == "tell":
        return _tell(request_id), "tell.ok"
    return ({"type": "send", "request_id": request_id, "stream_id": TARGET, "text": "synthetic work at shutdown",
             "optimistic_id": request_id, "from_stream_id": SENDER, "stream_token": TOKEN}, "send.result")


async def _reply(socket, request_id):
    async with asyncio.timeout(5):
        while True:
            frame = json.loads(await socket.recv())
            if frame.get("request_id") == request_id:
                return frame


async def _recorded(store, request_id):
    tell = await store.get_tell_delivery(request_id)
    sends = await store.submit(lambda conn: conn.execute(
        "SELECT count(*) FROM v2_send_receipts WHERE request_id=?", (request_id,)).fetchone()[0])
    return tell, sends



@pytest.mark.parametrize("verb", ["tell", "send"])
def test_input_arriving_during_the_stop_is_refused_then_delivers_once_after_boot(tmp_path, verb):
    async def run():
        path = tmp_path / "refused.db"
        request, ok = _late(verb, "late-" + verb)
        store, provider, sessions, spawnctl, comms, daemon, listener = await _daemon(path)
        try:
            await _seed(store, sessions)
            prepared, resume = asyncio.Event(), asyncio.Event()
            original = comms._prepare_tell_input

            async def paused(*args, **kwargs):  # an admitted tell holds the drain open
                await original(*args, **kwargs)
                prepared.set()
                await resume.wait()
            comms._prepare_tell_input = paused
            socket = await _socket(listener)
            await socket.send(json.dumps(_tell("holding")))
            await asyncio.wait_for(prepared.wait(), 5)
            stopping = asyncio.ensure_future(_shutdown(daemon, spawnctl, store))
            await until(lambda: daemon._input_stopping)
            await socket.send(json.dumps(request))
            refused = await _reply(socket, request["request_id"])
            assert refused["type"] == f"{verb}.error" and refused["error_code"] == "daemon_stopping", refused
            assert refused["retryable"] is True and refused["action_committed"] is False, refused
            assert await _recorded(store, request["request_id"]) == (None, 0)
            resume.set()
            await asyncio.wait_for(stopping, 15)
        finally:
            listener.close()
            store.stop(timeout=3)
        assert [text.endswith("synthetic work at shutdown") for _name, text in provider.pastes] == [True]

        store, provider, sessions, spawnctl, comms, daemon, listener = await _daemon(path)
        try:
            await sessions.refresh()
            socket = await _socket(listener)
            await socket.send(json.dumps(request))
            delivered = await _reply(socket, request["request_id"])
            assert delivered["type"] == ok and delivered.get("submission_confirmed") is True, delivered
            await socket.send(json.dumps(request))
            replay = await _reply(socket, request["request_id"])
            assert replay["type"] == ok, replay
            assert len(provider.pastes) == 1, provider.pastes
        finally:
            listener.close()
            store.stop(timeout=3)
    asyncio.run(run())
