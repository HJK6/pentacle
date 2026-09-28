"""Durable child admission and lifecycle race regressions."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json

import pytest

from server import Server
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from store import Store
from test_lifecycle_authority import IdleTmux

HOST = "fixture-node"
SOURCE = HOST + ":source"


class Panes(IdleTmux):
    async def new_session(self, name, command, cwd=None, env=None):
        self.live.add(name)

    async def capture(self, name):
        return "READY\n⏵⏵ bypass permissions\n❯"


@asynccontextmanager
async def environment():
    store = Store(":memory:")
    store.start()
    tmux = Panes()
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    ctl = SpawnCtl(store, sessions, tmux=tmux)
    server = Server(store=store, sessions=sessions, spawnctl=ctl, local_host=HOST)
    async def opened(name, host=HOST, **fields):
        tmux.live.add(name)
        return await sessions.open(host, name, **fields)
    try:
        await opened("source", provider="claude", effective_model="claude-sonnet-5", effective_effort="high")
        yield store, sessions, ctl, server, tmux, opened
    finally:
        pending = list(ctl._background_spawns)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        store.stop()


def close_message(actor=SOURCE, **fields):
    return {"host": HOST, "session_name": "source", "request_id": "close-source",
            "operator_confirm": True,
            "_auth_context": {"token_verified": True, "stream_id": actor}, **fields}


@pytest.mark.parametrize("caller", ["self", "parent", "operator"])
@pytest.mark.parametrize("child_host", [HOST, "fixture-offline-peer"])
def test_close_refuses_child_before_kill(caller, child_host):
    async def run():
        async with environment() as (store, sessions, ctl, server, tmux, opened):
            await opened("owner")
            await store.update_session(HOST, "source", parent_stream_id=HOST + ":owner")
            await opened("child", host=child_host, parent_stream_id=SOURCE, offline_since_ts=1)
            msg = close_message(HOST + ":owner" if caller == "parent" else SOURCE)
            if caller == "operator":
                msg["_auth_context"] = {"operator_authenticated": True}
            with pytest.raises(VerbError) as err:
                await server._on_close(msg)
            assert err.value.code == "close_live_children"
            assert err.value.extra["live_children"][0]["stream_id"] == child_host + ":child"
            assert tmux.killed == [] and "source" in tmux.live
            assert (await store.fetch_session(HOST, "source"))["status"] == "open"
    asyncio.run(run())


def test_close_refuses_admitted_pending_child():
    async def run():
        async with environment() as (store, sessions, ctl, server, tmux, opened):
            assert await store.reserve_stream_id(HOST, "pending", ttl_s=60, request_id="child-request")
            assert await store.record_spawn_intent(HOST, "pending",
                {"open_fields": {"parent_stream_id": SOURCE}}, request_id="child-request")
            with pytest.raises(VerbError) as err:
                await server._on_close(close_message())
            assert err.value.code == "close_pending_spawn"
            assert err.value.extra["pending_spawns"][0]["request_id"] == "child-request"
            assert tmux.killed == []
    asyncio.run(run())


@pytest.mark.parametrize("disposition", ["closed", "reparented"])
def test_close_succeeds_after_explicit_child_disposition(disposition):
    async def run():
        async with environment() as (store, sessions, ctl, server, tmux, opened):
            await opened("child", parent_stream_id=SOURCE)
            if disposition == "closed":
                await sessions.close(HOST, "child", operator_confirm=True)
            else:
                await opened("successor")
                await sessions.reparent(HOST, "child", HOST + ":successor",
                    auth_context={"token_verified": True, "stream_id": SOURCE})
            first = await server._on_close(close_message())
            second = await server._on_close(close_message())
            assert first["type"] == "close.ok" and second["type"] == "close.already_closed"
            assert tmux.killed.count("source") == 1
    asyncio.run(run())


def test_close_preserves_unauthorized_and_stale_generation_fences():
    async def run():
        async with environment() as (store, sessions, ctl, server, tmux, opened):
            old = await store.fetch_session(HOST, "source")
            await sessions.close(HOST, "source", operator_confirm=True)
            await opened("source")
            await opened("child", parent_stream_id=SOURCE)
            with pytest.raises(VerbError, match="close requires"):
                await server._on_close(close_message(HOST + ":stranger"))
            result = await sessions.close(HOST, "source", expected_generation=sessions._row_generation(old))
            assert result["stale_generation"] and "source" in tmux.live
    asyncio.run(run())


def test_spawn_after_close_refuses_before_creating_pane(monkeypatch):
    async def run():
        async with environment() as (store, sessions, ctl, server, tmux, opened):
            reached, release = asyncio.Event(), asyncio.Event()
            original = ctl._prepare_brief
            async def pause(*args, **kw):
                reached.set()
                await release.wait()
                return await original(*args, **kw)
            monkeypatch.setattr(ctl, "_prepare_brief", pause)
            task = asyncio.create_task(ctl.spawn({"command": "run", "session_name": "child",
                "request_id": "spawn-child", "parent_stream_id": SOURCE, "objective": "Child race"}, HOST))
            await reached.wait()
            assert (await server._on_close(close_message()))["type"] == "close.ok"
            release.set()
            with pytest.raises(VerbError) as err:
                await task
            assert err.value.code == "spawn_parent_closed"
            assert "child" not in tmux.live
    asyncio.run(run())


def test_close_after_child_admission_refuses_pending_spawn(monkeypatch):
    async def run():
        async with environment() as (store, sessions, ctl, server, tmux, opened):
            reached, release = asyncio.Event(), asyncio.Event()
            original = ctl._spawn_fenced
            async def pause(*args, **kw):
                reached.set()
                await release.wait()
                return await original(*args, **kw)
            monkeypatch.setattr(ctl, "_spawn_fenced", pause)
            task = asyncio.create_task(ctl.spawn({"command": "run", "session_name": "child",
                "request_id": "spawn-child", "parent_stream_id": SOURCE, "objective": "Child race"}, HOST))
            await reached.wait()
            with pytest.raises(VerbError) as err:
                await server._on_close(close_message())
            assert err.value.code == "close_pending_spawn" and "source" in tmux.live
            release.set()
            assert (await task)["type"] == "spawn.ok"
    asyncio.run(run())


def test_reparent_waits_for_close_then_refuses_closed_target(monkeypatch):
    async def run():
        async with environment() as (store, sessions, ctl, server, tmux, opened):
            await opened("owner")
            await opened("child", parent_stream_id=HOST + ":owner")
            reached, release = asyncio.Event(), asyncio.Event()
            original = tmux.kill_session
            async def pause(name):
                reached.set()
                await release.wait()
                await original(name)
            monkeypatch.setattr(tmux, "kill_session", pause)
            closing = asyncio.create_task(server._on_close(close_message()))
            await reached.wait()
            moving = asyncio.create_task(sessions.reparent(HOST, "child", SOURCE,
                auth_context={"token_verified": True, "stream_id": HOST + ":owner"}))
            await asyncio.sleep(0)
            assert not moving.done()
            release.set()
            assert (await closing)["type"] == "close.ok"
            with pytest.raises(VerbError) as err:
                await moving
            assert err.value.code == "reparent_target_closed"
            assert (await store.fetch_session(HOST, "child"))["parent_stream_id"] == HOST + ":owner"
    asyncio.run(run())


def test_close_after_reparent_refuses_new_child():
    async def run():
        async with environment() as (store, sessions, ctl, server, tmux, opened):
            await opened("owner")
            await opened("child", parent_stream_id=HOST + ":owner")
            await sessions.reparent(HOST, "child", SOURCE,
                auth_context={"token_verified": True, "stream_id": HOST + ":owner"})
            with pytest.raises(VerbError) as err:
                await server._on_close(close_message())
            assert err.value.code == "close_live_children" and tmux.killed == []
    asyncio.run(run())


def test_confirmed_process_death_records_truth_despite_open_child():
    async def run():
        async with environment() as (store, sessions, ctl, server, tmux, opened):
            await opened("child", parent_stream_id=SOURCE)
            await tmux.kill_session("source")
            await sessions.mark_closed(HOST, "source", reason="confirmed fixture death")
            assert (await store.fetch_session(HOST, "source"))["status"] == "closed"
            assert (await store.fetch_session(HOST, "child"))["status"] == "open"
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["children", "partial_children", "prompts", "source_close"])
def test_failed_handoff_preserves_one_successor_and_replays_disposition(tmp_path, monkeypatch, stage):
    from notify import Notify
    from test_question_contract_d3 import _ask
    async def run():
        async with environment() as (store, sessions, ctl, server, tmux, opened):
            await opened("child", parent_stream_id=SOURCE)
            if stage == "partial_children":
                await opened("child-remaining", parent_stream_id=SOURCE)
            notify = Notify(str(tmp_path / "notifications.db"), sessions=sessions)
            await notify.start()
            ctl.consent_notify = notify
            try:
                assert (await notify.prompt(_ask("q-partial", producer=SOURCE)))["type"] == "prompt.ask.ok"
                async def fail_children(source, successor):
                    if stage == "partial_children":
                        await sessions.reparent(HOST, "child", successor,
                            auth_context={"token_verified": True, "stream_id": SOURCE})
                    raise RuntimeError("injected child transfer failure")
                async def fail_prompts(*args):
                    raise RuntimeError("injected prompt transfer failure")
                async def fail_close(*args, **kw):
                    return {"failed": True, "reason": "fixture termination unconfirmed"}
                if stage in {"children", "partial_children"}:
                    monkeypatch.setattr(sessions, "reparent_children", fail_children)
                elif stage == "prompts":
                    monkeypatch.setattr(notify, "transfer_questions_for_handoff", fail_prompts)
                else:
                    monkeypatch.setattr(sessions, "_close_locked", fail_close)
                msg = {"command": "run", "session_name": "successor", "objective": "Continue fixture",
                       "request_id": "handoff-first", "idempotency_key": "one-successor",
                       "handoff": True, "handoff_from_stream_id": SOURCE}
                first = await ctl.spawn(msg, HOST)
                disposition = first["handoff"]
                assert disposition["state"] == "incomplete"
                assert disposition["stage"] == ("children" if stage == "partial_children" else stage)
                assert disposition["next_action"]
                assert (await store.fetch_session(HOST, "source"))["status"] == "open"
                assert (await store.fetch_session(HOST, "successor"))["status"] == "open"
                assert tmux.killed == []
                child = await store.fetch_session(HOST, "child")
                assert child["parent_stream_id"] == (SOURCE if stage == "children" else HOST + ":successor")
                q = await notify._db.call("get_agent_question", "q-partial")
                assert q["state"] == "open"
                assert q["producer_stream_id"] == (HOST + ":successor" if stage == "source_close" else SOURCE)
                async def must_not_repeat(*args):
                    raise AssertionError("same-key replay repeated post-steps")
                monkeypatch.setattr(ctl, "_finish_handoff", must_not_repeat)
                second = await ctl.spawn({**msg, "request_id": "handoff-replay"}, HOST)
                assert second["replayed"] and second["stream_id"] == first["stream_id"]
                assert second["handoff"] == disposition
                status = await ctl.spawn_status({"target": "one-successor"}, HOST)
                assert status["outcomes"][0]["handoff"] == disposition
                awaited = await ctl.await_spawn({"spawn_request_id": "handoff-first"})
                assert awaited["state"] == "ready" and awaited["handoff"] == disposition
                if stage == "partial_children":
                    assert (await store.fetch_session(HOST, "child-remaining"))["parent_stream_id"] == SOURCE
                assert len([name for name in tmux.live if name == "successor"]) == 1
            finally:
                await notify.stop()
    asyncio.run(run())


def test_unconfirmed_handoff_receipt_replays_pending_without_repeating(monkeypatch):
    async def run():
        async with environment() as (store, sessions, ctl, server, tmux, opened):
            async def crash(*args):
                raise RuntimeError("injected receipt persistence failure")
            monkeypatch.setattr(store, "record_handoff_outcome", crash)
            msg = {"command": "run", "session_name": "successor", "objective": "Continue fixture",
                   "request_id": "pending-handoff", "handoff": True, "handoff_from_stream_id": SOURCE}
            first = await ctl.spawn(msg, HOST)
            assert first["handoff"]["state"] == "indeterminate"
            assert (await store.fetch_session(HOST, "source"))["status"] == "closed"
            replay = await ctl.spawn(msg, HOST)
            assert replay["replayed"] and replay["handoff"]["state"] == "pending"
            assert replay["stream_id"] == first["stream_id"] and tmux.killed == ["source"]
    asyncio.run(run())


@pytest.mark.parametrize("release", ["approved", "deadline", "disabled"])
def test_ruling_child_refusal_is_terminal_and_requires_fresh_close(monkeypatch, release):
    from assistant_composite import AssistantComposite, AssistantCompositeConfig
    import time
    monkeypatch.delenv("PENTACLE_RULING_SLA_S", raising=False)
    async def run():
        async with environment() as (store, sessions, ctl, server, tmux, opened):
            source = await store.fetch_session(HOST, "source")
            advisor = await opened("advisor", provider="codex", pane_status="pane_alive")
            lane = await opened("lead", role="lead", parent_stream_id=SOURCE, pane_status="pane_alive")
            await opened("qa-child", parent_stream_id=lane["stream_id"])
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": HOST + ":assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": SOURCE,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": source["session_generation"],
                "PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID": advisor["stream_id"],
            })
            server.assistant_composite = AssistantComposite(store, config=config)
            await server.lane_rulings._record_ownership({
                "requester_stream_id": SOURCE, "requester_generation": source["session_generation"],
                "ruling_request_id": "fixture-admitted-lead"}, lane["stream_id"], lane["session_generation"])
            def report(conn):
                conn.execute("INSERT INTO v2_reports (report_id,from_stream_id,session_generation,status,summary,created_at,ingested_at) "
                             "VALUES (?,?,?,'done','Fixture accepted','2026-09-28T00:00:00Z','2026-09-28T00:00:00Z')",
                             ("fixture-terminal-report", lane["stream_id"], lane["session_generation"]))
                conn.commit()
            await store.submit(report)
            auth = {"token_verified": True, "stream_id": SOURCE,
                    "session_generation": source["session_generation"]}
            msg = {"type": "close", "host": HOST, "session_name": "lead",
                   "request_id": "close-ruled-lead", "_auth_context": auth}
            first = await server._on_close(msg)
            assert first["type"] == "close.pending_ruling"
            rid = first["ruling_request_id"]
            pending = await server.lane_rulings._fetch(rid)
            assert server.lane_rulings.sla_s == 600
            assert 599 < pending["deadline"] - time.time() <= 600
            if release == "approved":
                await server._on_assistant_ruling({"ruling_request_id": rid, "ruling": "approve",
                    "request_id": "approve-child-guard", "_auth_context": {
                        "token_verified": True, "stream_id": advisor["stream_id"],
                        "session_generation": advisor["session_generation"]}})
            elif release == "deadline":
                await store.submit(lambda conn: (conn.execute(
                    "UPDATE v2_assistant_lane_rulings SET deadline=0 WHERE ruling_request_id=?", (rid,)), conn.commit()))
                await server.lane_rulings.tick()
            else:
                await server._on_assistant_authority({"action": "set", "value": "disabled", "_auth_context": auth})
            refused = await server.lane_rulings._fetch(rid)
            assert refused["state"] == ("approved_but_not_closed" if release == "approved" else "release_blocked")
            outcome = json.loads(refused["outcome_json"])
            assert outcome["error_code"] == "close_live_children"
            assert outcome["live_children"][0]["stream_id"] == HOST + ":qa-child"
            assert tmux.killed == [] and "lead" in tmux.live
            notice = await store.submit(lambda conn: conn.execute(
                "SELECT body FROM v2_outbound_notices WHERE notice_id=?", ("assistant-lane-ruling-result:" + rid,)).fetchone())
            assert notice and "close_live_children" in notice[0]
            await sessions.close(HOST, "qa-child", operator_confirm=True)
            await server.lane_rulings.tick()
            assert "lead" in tmux.live and (await server.lane_rulings._fetch(rid))["state"] == refused["state"]
            fresh = await server._on_close({**msg, "request_id": "fresh-after-child-disposition"})
            if release != "disabled":
                assert fresh["type"] == "close.pending_ruling" and fresh["ruling_request_id"] != rid
                await server._on_assistant_ruling({"ruling_request_id": fresh["ruling_request_id"], "ruling": "approve",
                    "request_id": "approve-fresh-close", "_auth_context": {
                        "token_verified": True, "stream_id": advisor["stream_id"],
                        "session_generation": advisor["session_generation"]}})
            else:
                assert fresh["type"] == "close.ok"
            assert (await store.fetch_session(HOST, "lead"))["status"] == "closed"
            assert tmux.killed.count("lead") == 1
    asyncio.run(run())
