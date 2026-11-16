"""No-code controls for durable reads and detached spawn admission."""
import asyncio
import json
import os

from notify import Notify
from spawnctl import SpawnCtl
from test_session_inventory_cancellation import HOST, client, daemon, public_row, receive, rpc
from test_spawn_error_no_phantom_row import BootReadyTmux
from test_window_schedule_contract import RouteProbeSpecs
from work_lane_link import link_specs


def observe_request(monkeypatch, server):
    requests, finished = [], asyncio.Event()
    original = server._serve
    async def serve(socket, raw):
        if json.loads(raw).get("request_id") != "disconnect-control":
            return await original(socket, raw)
        requests.append(asyncio.current_task())
        try:
            return await original(socket, raw)
        finally:
            finished.set()
    monkeypatch.setattr(server, "_serve", serve)
    return requests, finished


async def disconnect(server, payload, reached, requests, finished):
    async with client(server) as socket:
        await socket.send(json.dumps({"request_id": "disconnect-control", **payload}))
        barrier = asyncio.create_task(reached.wait())
        reply = asyncio.create_task(receive(socket, lambda f: f.get("request_id") == "disconnect-control"))
        try:
            done, _ = await asyncio.wait((barrier, reply), timeout=5, return_when=asyncio.FIRST_COMPLETED)
            assert barrier in done, f"request completed before the commit barrier: {reply.result() if reply.done() else 'no response'}"
        finally:
            for task in (barrier, reply):
                if not task.done():
                    task.cancel()
            await asyncio.gather(barrier, reply, return_exceptions=True)
        await socket.close()
        await asyncio.wait_for(finished.wait(), 5)
        assert requests[0].cancelled(), "the actual Server request was not cancelled"


def test_notification_commit_without_broadcast_is_readable_and_deduplicated_after_restart(tmp_path, monkeypatch):
    async def run():
        database = tmp_path / "notifications.db"
        async with daemon(tmp_path / "sessions.db", seed_host=HOST) as (_, sessions, server, _):
            notify = Notify(str(database), sessions=sessions, broadcast=server.broadcast)
            await notify.start()
            server.handlers.update(notify.wire_handlers())
            reached, release = asyncio.Event(), asyncio.Event()
            requests, finished = observe_request(monkeypatch, server)
            original = notify._db.call
            saved, broadcasts = [], []
            async def call(method, *args, **kwargs):
                result = await original(method, *args, **kwargs)
                if method == "create_notification":
                    saved.append(result)
                    reached.set()
                    await release.wait()
                return result
            async def broadcast(frame):
                broadcasts.append(frame)
                await server.broadcast(frame)
            monkeypatch.setattr(notify._db, "call", call)
            monkeypatch.setattr(notify, "_broadcast", broadcast)
            payload = {"type": "notification.create", "producer": "cancel-fixture",
                       "dedup_key": "saved-once", "title": "Durable fixture", "severity": "info"}
            try:
                await disconnect(server, payload, reached, requests, finished)
                assert broadcasts == [], "barrier did not precede broadcast continuation"
                async with client(server) as reader:
                    listed = await rpc(reader, "notification.list", "read-saved")
                    assert [r["notification_id"] for r in listed["notifications"]] == [saved[0]["notification_id"]]
                await notify.stop()
                notify = Notify(str(database), sessions=sessions, broadcast=server.broadcast)
                await notify.start()
                server.handlers.update(notify.wire_handlers())
                async with client(server) as reader:
                    listed = await rpc(reader, "notification.list", "read-restarted")
                    assert [r["notification_id"] for r in listed["notifications"]] == [saved[0]["notification_id"]]
                    retried = await rpc(reader, "notification.create", "retry-create",
                                        **{k: v for k, v in payload.items() if k != "type"})
                    assert retried["notification"]["notification_id"] == saved[0]["notification_id"]
                    assert len((await rpc(reader, "notification.list", "read-retry"))["notifications"]) == 1
            finally:
                release.set()
                await notify.stop()
    asyncio.run(run())


def test_spawn_open_commit_survives_actual_request_disconnect_and_restart(tmp_path, monkeypatch):
    class RecordingPane(BootReadyTmux):
        def __init__(self):
            super().__init__()
            self.creates, self.pastes = 0, 0
        async def new_session(self, *args, **kwargs):
            self.creates += 1
            await super().new_session(*args, **kwargs)
        async def pane_pid(self, name):
            return ""  # No host PID exists or is inspected for this counterpart.
        async def paste(self, *args):
            self.pastes += 1

    async def run():
        database = tmp_path / "sessions.db"
        reached, release = asyncio.Event(), asyncio.Event()
        writes, signals = [], []
        def no_signal(*args, **kwargs):
            signals.append(args)
            raise AssertionError("spawn fixture must never signal a host process")
        monkeypatch.setattr(os, "kill", no_signal)
        async with daemon(database, seed_host=HOST) as (store, sessions, server, _):
            pane = RecordingPane()
            spec = "spec_example__cancel_continuation"
            await link_specs(store, spec)
            ctl = SpawnCtl(store, sessions, tmux=pane, specs=RouteProbeSpecs())
            server.spawnctl = ctl
            original = store.open_session
            async def commit_then_pause(host, name, **fields):
                row = await original(host, name, **fields)
                if name == "v2-created":
                    writes.append(row)
                    reached.set()
                    await release.wait()
                return row
            monkeypatch.setattr(store, "open_session", commit_then_pause)
            requests, finished = observe_request(monkeypatch, server)
            try:
                await disconnect(server, {"type": "spawn", "host": HOST, "session_name": "v2-created",
                    "command": "fixture-never-executed", "objective": "Durable admission control", "spec_id": spec},
                    reached, requests, finished)
                assert len(ctl._background_spawns) == 1
                assert not next(iter(ctl._background_spawns)).cancelled()
                release.set()
                results = await asyncio.wait_for(asyncio.gather(*tuple(ctl._background_spawns)), 5)
                assert results[0]["type"] == "spawn.ok", results
                row = await public_row(server, HOST + ":v2-created")
                assert row["session_generation"] == writes[0]["session_generation"]
                assert len(writes) == pane.creates == 1 and pane.pastes == 0
            finally:
                release.set()
                await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
        async with daemon(database) as (store, _, restarted, _):
            durable = await store.fetch_session(HOST, "v2-created")
            row = await public_row(restarted, HOST + ":v2-created")
            assert row["status"] == durable["status"] == "open"
            assert row["session_generation"] == writes[0]["session_generation"]
        assert signals == []
    asyncio.run(run())
