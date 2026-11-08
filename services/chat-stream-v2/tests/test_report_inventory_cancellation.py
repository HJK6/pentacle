"""The public report caller must finish its durable roster projection.

Reports use the existing generation-matched v2_agent_report_state projection;
restart and replay must never guess state from a report timestamp ordering.
"""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from test_session_inventory_cancellation import HOST, SOURCE, client, daemon, public_row, rpc
from test_cancellation_continuation_controls import disconnect, observe_request


def report(sid, report_id, status):
    return {"from_stream_id": sid, "report_id": report_id, "msg_id": 0,
            "status": status, "summary": "Cancellation fixture", "findings": [],
            "reason": "fixture", "next_action": "continue"}


def agent(parent, sid):
    return next(row for row in parent["agents"] if row["stream_id"] == sid)


@pytest.mark.parametrize("status,expected", [("done", "done"), ("error", "blocked"), ("progress", "idle")])
@pytest.mark.parametrize("boundary", ["returned", "first_commit"])
def test_report_disconnect_after_commit_projects_once_and_survives_replay_restart(tmp_path, monkeypatch, status, expected, boundary):
    async def run():
        database = tmp_path / "sessions.db"
        sid = HOST + ":v2-target"
        reached, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        commit_release = threading.Event()
        committed = []
        requests, writes, projections = [], [], []
        async with daemon(database, seed_host=HOST) as (store, sessions, server, _):
            # A progress report must clear an earlier terminal projection.
            prior_status = "done" if status == "progress" else "progress"
            async with client(server, sid) as socket:
                reply = await rpc(socket, "report", "prior-report", **report(sid, "prior", prior_status))
                assert reply["type"] == "report.ok", reply
            old_agent = agent(await public_row(server, SOURCE), sid)
            assert old_agent["state"] != expected
            original_put, original_apply, original_serve = store.put_report, sessions.apply_report_state, server._serve
            original_submit = store.submit
            loop = asyncio.get_running_loop()

            class CommitBarrier:
                """Pause only after the real SQLite transaction has committed."""
                def __init__(self, conn):
                    self.conn = conn

                def __getattr__(self, name):
                    return getattr(self.conn, name)

                def pause(self, had_transaction):
                    if had_transaction and not self.conn.in_transaction and not committed:
                        committed.append(True)
                        loop.call_soon_threadsafe(reached.set)
                        assert commit_release.wait(5)

                def commit(self):
                    had_transaction = self.conn.in_transaction
                    result = self.conn.commit()
                    self.pause(had_transaction)
                    return result

                def __enter__(self):
                    self.conn.__enter__()
                    return self

                def __exit__(self, kind, value, traceback):
                    had_transaction = self.conn.in_transaction
                    result = self.conn.__exit__(kind, value, traceback)
                    if kind is None:
                        self.pause(had_transaction)
                    return result

            async def submit_with_commit_barrier(callback, **kwargs):
                return await original_submit(lambda conn: callback(CommitBarrier(conn)), **kwargs)

            async def commit_then_pause(fields, **kwargs):
                row = await original_put(fields, **kwargs)
                if fields["report_id"] == "cancelled-report" and row.get("_report_inserted"):
                    writes.append(row)
                    if boundary == "returned":
                        reached.set()
                        await release.wait()
                return row

            async def project(row):
                await original_apply(row)
                projections.append(row["report_id"])

            async def serve(socket, raw):
                if json.loads(raw).get("request_id") != "cancel-report":
                    return await original_serve(socket, raw)
                requests.append(asyncio.current_task())
                try:
                    return await original_serve(socket, raw)
                finally:
                    finished.set()

            monkeypatch.setattr(store, "put_report", commit_then_pause)
            monkeypatch.setattr(sessions, "apply_report_state", project)
            monkeypatch.setattr(server, "_serve", serve)
            if boundary == "first_commit":
                monkeypatch.setattr(store, "submit", submit_with_commit_barrier)
            payload = report(sid, "cancelled-report", status)
            try:
                async with client(server, sid) as submitter:
                    await submitter.send(json.dumps({"type": "report", "request_id": "cancel-report", **payload}))
                    await asyncio.wait_for(reached.wait(), 5)
                    await submitter.close()
                    async with asyncio.timeout(5):
                        while not requests[0].cancelling():
                            await asyncio.sleep(0)
                    release.set()
                    commit_release.set()
                    await asyncio.wait_for(finished.wait(), 5)
                    assert requests[0].cancelled(), "a disconnected report caller must retain uncertainty"
                saved = await store.get_report("cancelled-report")
                assert saved["status"] == status
                state = await store.child_report_state(sid)
                assert state["generation"] == saved["session_generation"]
                assert state["status"] == status
                assert bool(committed) == (boundary == "first_commit")
                # Replaying inserted=False cannot hide a missing first projection.
                async with client(server, sid) as retry:
                    reply = await rpc(retry, "report", "retry-report", **payload)
                    assert reply["type"] == "report.ok", reply
                    assert reply["ledger_row_id"] == saved["ledger_row_id"]
                live = agent(await public_row(server, SOURCE), sid)
                assert live["state"] == expected, "durable report missing from public parent roster after retry"
                assert live["since"] == state["ts"]
                assert len(writes) == 1 and projections == ["cancelled-report"]
            finally:
                release.set()
                commit_release.set()
        async with daemon(database) as (store, _, restarted, _):
            state = await store.child_report_state(sid)
            restored = agent(await public_row(restarted, SOURCE), sid)
            assert restored["state"] == expected and restored["since"] == state["ts"]
            # A later metadata update must preserve the persisted report state.
            async with client(restarted) as socket:
                assert (await rpc(socket, "rename", "later-metadata", stream_id=sid,
                                  display_name="after restart"))["type"] == "rename.ok"
            assert agent(await public_row(restarted, SOURCE), sid)["state"] == expected
    asyncio.run(run())


def test_report_notice_work_remains_cancellable_after_projection(tmp_path, monkeypatch):
    async def run():
        async with daemon(tmp_path / "sessions.db", seed_host=HOST) as (store, _, server, _):
            reached, release = asyncio.Event(), asyncio.Event()
            requests, finished = observe_request(monkeypatch, server)
            notice_cancelled = []
            async def paused_notice(row):
                reached.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    notice_cancelled.append(row["report_id"])
                    raise
            monkeypatch.setattr(server.ledger, "_announce_child_report_ready", paused_notice)
            # SOURCE is authenticated by disconnect(); give it a parent so the
            # public parent roster is the external projection oracle.
            await store.update_session(HOST, "v2-source", parent_stream_id=HOST + ":v2-successor")
            await server.sessions.refresh()
            try:
                await disconnect(server, {"type": "report", **report(SOURCE, "notice-cancel", "done")},
                                 reached, requests, finished)
                assert notice_cancelled == ["notice-cancel"], "unrelated notice work survived caller cancellation"
                parent = await public_row(server, HOST + ":v2-successor")
                assert agent(parent, SOURCE)["state"] == "done"
                assert (await store.get_report("notice-cancel"))["status"] == "done"
            finally:
                release.set()
    asyncio.run(run())
