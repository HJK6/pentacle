"""Lifecycle acceptance on an isolated socket, durable stores and real panes."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json

import websockets

from notify import Notify
from server import Server
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store, STREAM_TOKEN_HASH_VERSION
from tmux_transport import Tmux
from test_question_contract_d3 import _ask

HOST = "fixture-lifecycle"
SOURCE = HOST + ":source"
COMMAND = "echo READY; echo '⏵⏵ bypass permissions'; echo '❯'; exec sleep 600"


@asynccontextmanager
async def daemon(tmp_path):
    store = Store(str(tmp_path / "sessions.db"))
    store.start()
    tmux = Tmux()
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    ctl = SpawnCtl(store, sessions, tmux=tmux)
    notify = Notify(str(tmp_path / "notifications.db"), sessions=sessions)
    ctl.consent_notify = notify
    server = Server(host="127.0.0.1", port=0, store=store, sessions=sessions,
                    spawnctl=ctl, local_host=HOST)
    server.notify = notify
    server.handlers.update(notify.wire_handlers())
    await notify.start()
    async def closed(sid, *, session_generation=None, reason=""):
        await notify.expire_questions_for_closed_producer(sid, generation=session_generation)
    sessions.set_awaiter_resolver(closed)
    try:
        await tmux.new_session("source", COMMAND)
        await sessions.open(HOST, "source", provider="claude",
                            effective_model="claude-sonnet-5", effective_effort="high")
        token = "fixture-source-token"
        await store.grant_stream_token(HOST, "source", hashlib.sha256(token.encode()).hexdigest(),
                                       STREAM_TOKEN_HASH_VERSION)
        port = await server.bind()
        async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
            assert json.loads(await socket.recv())["type"] == "welcome"
            await socket.send(json.dumps({"type": "hello", "client": "lifecycle-smoke",
                                          "stream_token": token, "from_stream_id": SOURCE}))
            frames = [json.loads(await socket.recv()) for _ in range(2)]
            assert any(frame["type"] == "snapshot" for frame in frames)
            async def send_request(connection, request):
                await connection.send(json.dumps(request))
                async with asyncio.timeout(10):
                    while True:
                        frame = json.loads(await connection.recv())
                        if frame.get("request_id") == request["request_id"]:
                            return frame
            async def rpc(payload):
                request = {"from_stream_id": SOURCE, "stream_token": token, **payload}
                if request["from_stream_id"] == SOURCE:
                    return await send_request(socket, request)
                async with websockets.connect(f"ws://127.0.0.1:{port}") as successor_socket:
                    assert json.loads(await successor_socket.recv())["type"] == "welcome"
                    await successor_socket.send(json.dumps({"type": "hello", "client": "lifecycle-smoke",
                        "stream_token": request["stream_token"], "from_stream_id": request["from_stream_id"]}))
                    frames = [json.loads(await successor_socket.recv()) for _ in range(2)]
                    assert any(frame["type"] == "snapshot" for frame in frames)
                    return await send_request(successor_socket, request)
            yield store, sessions, ctl, notify, rpc
    finally:
        await server.close()
        pending = list(ctl._background_spawns)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # Every pane belongs to the fixture socket. Read cleanup back even on RED.
        await tmux.run("kill-server")
        rc, _ = await tmux.run("list-sessions")
        assert rc != 0, "owned tmux server survived cleanup"
        await notify.stop()
        store.stop()


def spawn_payload(name, **fields):
    return {"type": "spawn", "host": HOST, "session_name": name, "command": COMMAND,
            "objective": "Disposable lifecycle acceptance", "request_id": "spawn-" + name,
            "idempotency_key": "spawn-" + name, **fields}


def test_parent_close_refuses_open_child(tmp_path):
    async def run():
        async with daemon(tmp_path) as (store, sessions, ctl, notify, rpc):
            child = await rpc(spawn_payload("child", parent_stream_id=SOURCE))
            assert child["type"] == "spawn.ok", child
            reply = await rpc({"type": "close", "host": HOST, "session_name": "source",
                               "request_id": "close-source", "operator_confirm": True})
            assert reply.get("error_code") == "close_live_children", reply
            assert (await store.fetch_session(HOST, "source"))["status"] == "open"
            assert await sessions.tmux.has_session("source")
            assert (await store.fetch_session(HOST, "child"))["parent_stream_id"] == SOURCE
    asyncio.run(run())


def test_handoff_reparent_failure_preserves_source(tmp_path, monkeypatch):
    async def run():
        async with daemon(tmp_path) as (store, sessions, ctl, notify, rpc):
            child = await rpc(spawn_payload("child", parent_stream_id=SOURCE))
            assert child["type"] == "spawn.ok", child
            async def broken(*args):
                raise RuntimeError("injected child transfer failure")
            monkeypatch.setattr(sessions, "reparent_children", broken)
            reply = await rpc(spawn_payload("successor", handoff=True, handoff_from_stream_id=SOURCE))
            assert reply["handoff"]["state"] == "incomplete" and reply["handoff"]["stage"] == "children"
            assert (await store.fetch_session(HOST, "source"))["status"] == "open", reply
            assert await sessions.tmux.has_session("source")
            assert (await store.fetch_session(HOST, "child"))["parent_stream_id"] == SOURCE
    asyncio.run(run())


def test_handoff_preserves_open_producer_prompt(tmp_path):
    async def run():
        async with daemon(tmp_path) as (store, sessions, ctl, notify, rpc):
            ask = _ask("q-continuity", producer=SOURCE)
            ask.pop("_auth_context")
            before = await rpc(ask)
            assert before["type"] == "prompt.ask.ok", before
            original = await notify._db.call("get_agent_question", "q-continuity")
            reply = await rpc(spawn_payload("successor", handoff=True, handoff_from_stream_id=SOURCE))
            assert reply["type"] == "spawn.ok", reply
            assert reply["handoff"]["state"] == "complete"
            question = await notify._db.call("get_agent_question", "q-continuity")
            assert question["state"] == "open", question
            assert question["producer_stream_id"] == HOST + ":successor"
            successor = await store.fetch_session(HOST, "successor")
            assert question["producer_session_generation"] == successor["session_generation"]
            assert question["notification_id"] == original["notification_id"]
            assert question["created_at"] == original["created_at"]
            assert (await store.fetch_session(HOST, "source"))["status"] == "closed"
            card = await notify._db.call("get_notification", question["notification_id"])
            assert card["state"] == "open" and card["answer_to_stream_id"] == HOST + ":successor"
            # A fresh successor connection uses the existing verified relay.
            token = "fixture-successor-token"
            await store.grant_stream_token(HOST, "successor", hashlib.sha256(token.encode()).hexdigest(),
                                           STREAM_TOKEN_HASH_VERSION)
            answered = await rpc({"type": "prompt.answer", "request_id": "successor-answer",
                                  "from_stream_id": HOST + ":successor", "stream_token": token,
                                  "question_id": "q-continuity", "selections": ["yes"]})
            assert answered["type"] == "prompt.answer.ok", answered
            saved = await notify._db.call("get_agent_question", "q-continuity")
            assert saved["state"] == "answered" and saved["answer"]["selections"] == ["yes"]
    asyncio.run(run())
