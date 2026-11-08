"""Real Store/socket cancellation at the session metadata commit boundary.

The Store and Server are subjects. Only the pane transport is a recording
counterpart; no provider, process signal, or deployed daemon is involved.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os

import pytest
import websockets

from inventory import InventoryEmitter
from ledger import Ledger
from routing_integrity import RoutingIntegrity
from server import Server
from sessions import Sessions
from store import Store, STREAM_TOKEN_HASH_VERSION

HOST = "cancel-fixture"
SOURCE = HOST + ":v2-source"
TOKEN = "disposable-cancellation-fixture-token"
CASES = {
    "rename": ({"display_name": "committed title"}, "title", "committed title"),
    "set_visibility": ({"visibility": "hidden"}, "visibility", "hidden"),
    "status_card": ({"goal": "committed goal"}, "status_card", None),
    "role.set": ({"role": "qa"}, "role", "qa"),
    "reparent": ({"new_parent_stream_id": HOST + ":v2-successor"},
                 "parent_stream_id", HOST + ":v2-successor"),
}


class NoPaneActions:
    def __init__(self):
        self.actions = []

    def __getattr__(self, name):
        async def prohibited(*args, **kwargs):
            self.actions.append((name, args))
            raise AssertionError(f"metadata mutation attempted pane action: {name}")
        return prohibited


@asynccontextmanager
async def daemon(path, *, seed_host=None):
    store = Store(str(path))
    store.start()
    panes = NoPaneActions()
    sessions = Sessions(store, tmux=panes, local_host=HOST)
    server = Server(host="127.0.0.1", port=0, store=store,
                    sessions=sessions, local_host=HOST)
    emitter = InventoryEmitter(sessions, server.broadcast)
    sessions.set_inventory_emitter(emitter)
    try:
        if seed_host is not None:
            for name in ("v2-source", "v2-successor"):
                await store.open_session(HOST, name, visibility="visible", provider="shell")
            await store.open_session(seed_host, "v2-target", visibility="visible",
                                     provider="codex", requested_model="gpt-6.1-sol", effective_model="gpt-6.1-sol",
                                     requested_effort="high", effective_effort="high",
                                     role="worker", title="initial title",
                                     parent_stream_id=SOURCE)
            await store.grant_stream_token(HOST, "v2-source",
                hashlib.sha256(TOKEN.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
            await store.grant_stream_token(seed_host, "v2-target",
                hashlib.sha256((TOKEN + seed_host + ":v2-target").encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
        await sessions.refresh()
        server.ledger = Ledger(store, sessions=sessions, broadcast=server.broadcast,
                               routing_integrity=RoutingIntegrity(store, sessions))
        emitter.prime()
        await server.bind()
        yield store, sessions, server, panes
    finally:
        await server.close()
        assert panes.actions == []
        store.stop()


async def receive(socket, predicate):
    async with asyncio.timeout(5):
        while True:
            frame = json.loads(await socket.recv())
            if predicate(frame):
                return frame


@asynccontextmanager
async def client(server, actor=SOURCE):
    async with websockets.connect(f"ws://127.0.0.1:{server.port}") as socket:
        assert json.loads(await socket.recv())["type"] == "welcome"
        await socket.send(json.dumps({"type": "hello", "client": "cancel-fixture",
            "from_stream_id": actor, "stream_token": TOKEN if actor == SOURCE else TOKEN + actor,
            "subscribe": {"include_subagents": True}}))
        await receive(socket, lambda f: f["type"] in {"snapshot", "hello.error"})
        yield socket


async def rpc(socket, verb, request_id, **fields):
    await socket.send(json.dumps({"type": verb, "request_id": request_id, **fields}))
    return await receive(socket, lambda f: f.get("request_id") == request_id)


async def public_row(server, sid):
    async with client(server) as socket:
        reply = await rpc(socket, "list_sessions", "read-inventory")
        assert reply["type"] == "list_sessions.ok", reply
        return next(row for row in reply["active"] if row["stream_id"] == sid)


def assert_projection(row, durable, verb):
    _, field, expected = CASES[verb]
    assert row["session_generation"] == durable["session_generation"]
    assert row["status"] == durable["status"] == "open"
    assert row.get(field) == durable.get(field), "committed metadata missing from public inventory"
    if verb == "status_card":
        assert row[field]["goal"] == "committed goal"
    else:
        assert row[field] == expected


@pytest.mark.parametrize("target_host", [HOST, "independent-peer"])
@pytest.mark.parametrize("verb", CASES)
def test_disconnect_after_commit_converges_before_and_after_restart(tmp_path, monkeypatch, verb, target_host):
    async def run():
        database = tmp_path / "sessions.db"
        sid = target_host + ":v2-target"
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        request = []
        writes, cached, signals = [], [], []

        def no_signal(*args, **kwargs):
            signals.append(args)
            raise AssertionError("session metadata must not signal a process")
        monkeypatch.setattr(os, "kill", no_signal)
        async with daemon(database, seed_host=target_host) as (store, sessions, server, _):
            generation = sessions.get(sid)["session_generation"]
            original_update, original_cache, original_serve = store.update_session, sessions._cache, server._serve

            async def commit_then_pause(host, name, **fields):
                row = await original_update(host, name, **fields)
                if host == target_host and name == "v2-target":
                    writes.append(row)
                    entered.set()
                    await release.wait()
                return row

            def cache(row, **fields):
                result = original_cache(row, **fields)
                if row and row["stream_id"] == sid:
                    cached.append(result)
                return result

            async def serve(socket, raw):
                if json.loads(raw).get("request_id") != "cancel-mutation":
                    return await original_serve(socket, raw)
                request.append(asyncio.current_task())
                try:
                    return await original_serve(socket, raw)
                finally:
                    finished.set()

            monkeypatch.setattr(store, "update_session", commit_then_pause)
            monkeypatch.setattr(sessions, "_cache", cache)
            monkeypatch.setattr(server, "_serve", serve)
            try:
                async with client(server) as observer, client(server) as submitter:
                    await submitter.send(json.dumps({"type": verb, "request_id": "cancel-mutation",
                        "stream_id": sid, **CASES[verb][0]}))
                    await asyncio.wait_for(entered.wait(), 5)
                    committed = await store.fetch_session(target_host, "v2-target")
                    assert committed["session_generation"] == generation
                    await submitter.close()
                    # Observe cancellation from Server's real connection teardown.
                    async with asyncio.timeout(5):
                        while not request[0].cancelling():
                            await asyncio.sleep(0)
                    release.set()
                    await asyncio.wait_for(finished.wait(), 5)
                    assert request[0].cancelled(), "the disconnected caller must retain uncertainty"
                    durable = await store.fetch_session(target_host, "v2-target")
                    assert len(writes) == 1, "disconnect repeated the durable mutation"
                    live = await public_row(server, sid)
                    assert_projection(live, durable, verb)
                    assert len(cached) == 1, "cache continuation must finish exactly once"
                    pushed = await receive(observer, lambda f: f["type"] == "session.inventory")
                    assert_projection(next(r for r in pushed["sessions"] if r["stream_id"] == sid), durable, verb)
                    if verb == "role.set":
                        assert (await store.fetch_role_source(target_host, "v2-target"))["role_source"] == "role_set"
            finally:
                release.set()
        # A new Store, Sessions and bound Server prove actual persistence/rebuild,
        # without inheriting an in-memory cache or the barrier wrappers.
        async with daemon(database) as (reopened, _, restarted, _):
            durable = await reopened.fetch_session(target_host, "v2-target")
            assert durable["session_generation"] == generation
            assert_projection(await public_row(restarted, sid), durable, verb)
        assert signals == []
    asyncio.run(run())


@pytest.mark.parametrize("verb", CASES)
@pytest.mark.parametrize("timing", ["before_admission", "after_continuation", "reply_then_retry"])
def test_mutation_cancellation_controls_and_persisted_restart(tmp_path, monkeypatch, verb, timing):
    async def run():
        database = tmp_path / "sessions.db"
        sid = HOST + ":v2-target"
        reached, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        request, writes = [], []
        async with daemon(database, seed_host=HOST) as (store, sessions, server, _):
            before = await store.fetch_session(HOST, "v2-target")
            original_serve, original_update = server._serve, store.update_session

            async def serve(socket, raw):
                if json.loads(raw).get("request_id") != "timing-control":
                    return await original_serve(socket, raw)
                request.append(asyncio.current_task())
                try:
                    return await original_serve(socket, raw)
                finally:
                    finished.set()

            async def update(*args, **kwargs):
                row = await original_update(*args, **kwargs)
                writes.append(row)
                return row

            monkeypatch.setattr(server, "_serve", serve)
            monkeypatch.setattr(store, "update_session", update)
            if timing == "before_admission":
                resolve = sessions.resolve
                async def pause_resolve(msg):
                    result = await resolve(msg)
                    reached.set()
                    await release.wait()
                    return result
                monkeypatch.setattr(sessions, "resolve", pause_resolve)
            elif timing == "after_continuation":
                emit = sessions._inventory_emitter.emit_if_changed
                async def pause_emit(**kwargs):
                    result = await emit(**kwargs)
                    reached.set()
                    await release.wait()
                    return result
                monkeypatch.setattr(sessions._inventory_emitter, "emit_if_changed", pause_emit)
            try:
                async with client(server) as submitter:
                    payload = {"stream_id": sid, **CASES[verb][0]}
                    if timing == "reply_then_retry":
                        reply = await rpc(submitter, verb, "timing-control", **payload)
                        assert reply["type"] == verb + ".ok", reply
                        replay = await rpc(submitter, verb, "explicit-retry", **payload)
                        # Reparenting transfers authorization too: the old parent
                        # cannot mutate the child again after a completed move.
                        assert replay["type"] == verb + (".error" if verb == "reparent" else ".ok"), replay
                        assert len(writes) == (1 if verb == "reparent" else 2)
                    else:
                        await submitter.send(json.dumps({"type": verb, "request_id": "timing-control", **payload}))
                        await asyncio.wait_for(reached.wait(), 5)
                        await submitter.close()
                        async with asyncio.timeout(5):
                            while not request[0].cancelling():
                                await asyncio.sleep(0)
                        release.set()
                        await asyncio.wait_for(finished.wait(), 5)
                        assert request[0].cancelled()
                        assert len(writes) == (0 if timing == "before_admission" else 1)
                durable = await store.fetch_session(HOST, "v2-target")
                live = await public_row(server, sid)
                if timing == "before_admission":
                    assert durable == before, "cancellation before admission wrote phantom metadata"
                    _, field, _ = CASES[verb]
                    assert live.get(field) == before.get(field)
                else:
                    assert_projection(live, durable, verb)
                    assert not (durable.get("status_card") or {}).get("updates"), "goal retry created duplicate updates"
            finally:
                release.set()
        async with daemon(database) as (reopened, _, restarted, _):
            row = await public_row(restarted, sid)
            durable = await reopened.fetch_session(HOST, "v2-target")
            if timing == "before_admission":
                _, field, _ = CASES[verb]
                assert row.get(field) == before.get(field)
            else:
                assert_projection(row, durable, verb)
    asyncio.run(run())
