"""Report waivers retire generations, without weakening authority or liveness."""
import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from sessions import VerbError
from store import Store
from test_lifecycle_authority import (
    HOST, OPERATOR, _cancel_after_commit, _close, _manager, _pause_after, scenario,
)
from test_offline_operator_close import build
from test_approved_close_retry import ROOT, TARGET, HOST as LANE_HOST, journey


@pytest.mark.parametrize("report", ["none", "progress", "prior", "done", "error", "aborted"])
def test_report_waiver_is_exact_audited_and_durable(report, tmp_path):
    state = {}
    path = str(tmp_path / "sessions.db")

    async def check(env):
        manager = await _manager(env)
        target = await env.open("target", provider="codex")
        if report != "none":
            await env.report("target", status="done" if report == "prior" else report,
                             generation="old" if report == "prior" else None)
        before = await env.store.submit(lambda c: c.execute("SELECT count(*) FROM v2_reports").fetchone()[0])
        msg = _close(manager, "target", reason="Retire an abandoned acceptance generation")
        assert (await env.server._on_close(msg))["type"] == "close.ok"
        rows = [r for r in await env.audit() if r["action"].startswith("manager_close")]
        waived = report in {"none", "progress", "prior"}
        assert [(r["action"], r["result"]) for r in rows] == (
            [("manager_close_report_waived", "admitted")] if waived else []
        ) + [("manager_close", "admitted"), ("manager_close", "applied")]
        for r in rows:
            assert (r["actor_kind"], r["actor_identity"], r["actor_generation"], r["target_generation"],
                    r["reason"], r["request_id"], r["old_revision"], r["new_revision"]) == (
                "manager", manager["stream_id"], manager["session_generation"], target["session_generation"],
                msg["reason"], msg["request_id"], 1, 1)
        assert await env.store.submit(lambda c: c.execute("SELECT count(*) FROM v2_reports").fetchone()[0]) == before
        state["rows"] = rows

    scenario(check, path=path)
    store = Store(path)
    store.start()
    try:
        async def read():
            return list(reversed(await store.lifecycle_authority_audit_rows(limit=50)))
        assert [r for r in asyncio.run(read()) if r["action"].startswith("manager_close")] == state["rows"]
    finally:
        store.stop()


@pytest.mark.parametrize("action", ["manager_close_report_waived", "manager_close"])
@pytest.mark.parametrize("remote", [False, True])
def test_pre_effect_audit_failure_preserves_live_and_offline_targets(action, remote):
    async def check(env):
        manager = await _manager(env)
        if remote:
            from test_offline_operator_close import Peer
            env.sessions.hosts = Peer()
            target_host = "remote-peer"
            await env.store.open_session(target_host, "target", offline_since_ts="2026-09-27T00:00:00Z")
        else:
            target_host = HOST
            await env.open("target")
        real = env.store.lifecycle_authority_audit

        async def fail(**fields):
            if fields["action"] == action and fields["result"] == "admitted":
                raise sqlite3.OperationalError("fixture audit failure")
            return await real(**fields)

        env.store.lifecycle_authority_audit = fail
        msg = _close(manager, "target", stream_id=target_host + ":target")
        with pytest.raises(sqlite3.OperationalError, match="fixture audit failure"):
            await env.server._on_close(msg)
        assert (await env.store.fetch_session(target_host, "target"))["status"] == "open"
        assert env.tmux.killed == []
        assert await env.store.get_deferred_reap(target_host + ":target") is None
    scenario(check)


def test_unreported_cancelled_close_finishes_waiver_and_outcome():
    async def check(env):
        manager = await _manager(env)
        await env.open("target")
        committed, resume = _pause_after(env, "mark_closed")
        task = asyncio.create_task(env.server._on_close(_close(manager, "target")))
        await _cancel_after_commit(task, committed, resume)
        rows = [r for r in await env.audit() if r["action"].startswith("manager_close")]
        assert [(r["action"], r["result"]) for r in rows] == [
            ("manager_close_report_waived", "admitted"), ("manager_close", "admitted"),
            ("manager_close", "applied")]
        assert (await env.store.fetch_session(HOST, "target"))["status"] == "closed"
    scenario(check)


@pytest.mark.parametrize("state", ["gone", "unreachable", "exception", "alive"])
def test_missing_local_pane_requires_positive_gone_and_rechecks_live_capture(state):
    async def check(env):
        manager = await _manager(env)
        await env.open("target", offline_since_ts="2026-09-27T00:00:00Z",
                       presumed_dead_at="2026-09-27T01:00:00Z")
        env.tmux.live.discard("target")
        env.tmux.session_state = AsyncMock(return_value=state)
        if state == "exception":
            env.tmux.session_state.side_effect = VerbError("tmux_timeout", "fixture transport")
        env.sessions.apply_live(HOST + ":target", capture_liveness="idle", working=False)
        env.tmux.working.add("target")
        result = await env.server._on_close(_close(manager, "target", force=True, operator_override=True))
        row = await env.store.fetch_session(HOST, "target")
        if state == "gone":
            assert result["type"] == "close.ok" and result["reap_status"] == "unknown"
            assert row["status"] == "closed"
        else:
            assert result["type"] == "close.failed" and row["status"] == "open"
        assert env.tmux.killed == []
    scenario(check)


@pytest.mark.parametrize("case", ["offline", "gone", "idle", "busy", "unknown", "mid_close_loss"])
def test_remote_manager_close_keeps_intent_distinct_from_death(case, monkeypatch):
    monkeypatch.setattr("sessions.REMOTE_CLOSE_RETRY_S", 0)
    async def run():
        store, peer, sessions, server, target = await build()
        try:
            manager = await store.open_session("local-peer", "manager", provider="codex", role="lead",
                                               pane_status="pane_alive", bootstrap_state="ready")
            await store.lifecycle_authority_mutate(
                {"action": "designate", "target_stream_id": "local-peer:manager",
                 "target_generation": manager["session_generation"], "expected_revision": 0,
                 "reason": "Fixture manager designation", "request_id": "fixture-grant"},
                {**OPERATOR, "_consent_id": "fixture-consent"}, sessions.assistant.role)
            peer.online = case != "offline"
            peer.alive = case != "gone"
            peer.capture_checked = AsyncMock(return_value=(case != "unknown",
                "Working (esc to interrupt)\n" if case == "busy" else "ready\n"))
            if case == "mid_close_loss":
                peer.session_state = AsyncMock(side_effect=["alive"] + ["unreachable"] * 3)
            msg = {"stream_id": "remote-peer:v2-offline", "reason": "Retire unreachable acceptance target",
                   "request_id": "manager-remote-close", "_auth_context": {
                       "token_verified": True, "stream_id": "local-peer:manager",
                       "session_generation": manager["session_generation"]}}
            reply = await server._on_close(msg)
            row = await store.fetch_session("remote-peer", "v2-offline")
            deferred = await store.get_deferred_reap("remote-peer:v2-offline")
            if case == "offline":
                assert reply["type"] == "close.ok" and reply["reap_status"] == "deferred_host_offline"
                assert row["status"] == "closed" and row["pane_status"] == "unknown"
                assert deferred["generation"] == target["session_generation"] and deferred["done_at"] is None
                assert await store.get_session_reap("remote-peer:v2-offline") is None
                audit = await store.latest_close_audit("remote-peer:v2-offline")
                assert audit["actor_kind"] == "manager" and audit["closed_by"] == "local-peer:manager"
                peer.capture_checked.assert_not_awaited()
            elif case in {"gone", "idle"}:
                assert reply["type"] == "close.ok" and reply["reap_status"] == "unknown"
                assert row["status"] == "closed" and deferred is None
                if case == "gone":
                    peer.capture_checked.assert_not_awaited()
            else:
                assert reply["type"] == "close.failed" and row["status"] == "open" and deferred is None
            assert peer.kills == (1 if case == "idle" else 0)
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("remote", [False, True])
def test_unreported_offline_target_with_child_or_spawn_refuses_before_waiver(pending, remote):
    async def check(env):
        manager = await _manager(env)
        target_host = HOST
        if remote:
            from test_offline_operator_close import Peer
            env.sessions.hosts = Peer()
            target_host = "remote-peer"
            await env.store.open_session(target_host, "target", offline_since_ts="2026-09-27T00:00:00Z")
        else:
            await env.open("target", offline_since_ts="2026-09-27T00:00:00Z")
        sid = target_host + ":target"
        if pending:
            assert await env.store.reserve_stream_id(target_host, "pending", ttl_s=60, request_id="spawn", nonce="n")
            assert await env.store.record_spawn_intent(target_host, "pending", {
                "open_fields": {"parent_stream_id": sid}}, request_id="spawn", nonce="n")
        else:
            await env.store.open_session(target_host, "child", parent_stream_id=sid,
                                         offline_since_ts="2026-09-27T00:00:00Z")
        with pytest.raises(VerbError) as error:
            await env.server._on_close(_close(manager, "target", stream_id=sid))
        assert error.value.code == ("close_pending_spawn" if pending else "close_live_children")
        assert not any(r["action"] == "manager_close_report_waived" for r in await env.audit())
        assert (await env.store.fetch_session(target_host, "target"))["status"] == "open"
        assert await env.store.get_deferred_reap(sid) is None
        assert env.tmux.killed == []
    scenario(check)


async def lane_manager(env, *, direct_parent=False):
    manager = await env.auth("primary")
    await env.store.lifecycle_authority_mutate(
        {"action": "designate", "target_stream_id": ROOT,
         "target_generation": manager["session_generation"], "expected_revision": 0,
         "reason": "Fixture lane manager", "request_id": "fixture-manager"},
        {**OPERATOR, "_consent_id": "fixture-consent"}, env.sessions.assistant.role)
    await env.store.submit(lambda c: c.execute("DELETE FROM v2_reports"))
    if not direct_parent:
        await env.store.update_session(LANE_HOST, "lane", parent_stream_id=None)
    env.tmux.text = "ready\n"
    return manager


@pytest.mark.parametrize("direct_parent", [False, True])
def test_no_report_lane_requires_ruler_then_audited_manager_admission(direct_parent):
    async def run():
        async with journey() as env:
            manager = await lane_manager(env, direct_parent=direct_parent)
            first = await env.request()
            assert first["type"] == "close.pending_ruling" and env.tmux.kills == 0
            row = await env.row()
            intent = json.loads(row["intent_json"])
            assert "_ruling_report" not in intent
            assert intent["_ruling_report_waiver"]["manager_generation"] == manager["session_generation"]
            assert intent["_ruling_report_waiver"]["reason"] == intent["reason"]
            audit = await env.store.submit(lambda c: [dict(r) for r in c.execute(
                "SELECT * FROM v2_assistant_lane_ruling_audit WHERE event='report_prerequisite_waived'")])
            assert len(audit) == 1 and audit[0]["detail"] == intent["reason"]
            assert audit[0]["actor_stream_id"] == manager["stream_id"]
            assert 599 < row["deadline"] - row["created_at"] <= 600
            assert (await env.request())["ruling_request_id"] == first["ruling_request_id"]
            assert (await env.approve())["state"] == "done"
            assert env.tmux.kills == 1
            assert (await env.request())["type"] == "close.ok" and env.tmux.kills == 1
            rows = list(reversed(await env.store.lifecycle_authority_audit_rows(limit=50)))
            assert [(r["action"], r["result"]) for r in rows if r["action"].startswith("manager_close")] == [
                ("manager_close_report_waived", "admitted"), ("manager_close", "admitted"),
                ("manager_close", "applied")]
            assert await env.store.find_report(TARGET, statuses={"done", "error", "aborted"},
                                              session_generation=row["target_generation"]) is None
    asyncio.run(run())


@pytest.mark.parametrize("change", ["revoke", "replace", "child", "pending", "busy", "unknown", "target", "requester"])
def test_pending_report_waiver_rechecks_every_effect_fence(change):
    async def run():
        async with journey() as env:
            await lane_manager(env, direct_parent=True)
            await env.request()
            if change == "revoke":
                await env.store.lifecycle_authority_mutate(
                    {"action": "revoke", "expected_revision": 1, "reason": "Stop fixture close", "request_id": "revoke"},
                    {**OPERATOR, "_consent_id": "fixture-consent"}, env.sessions.assistant.role)
            elif change == "replace":
                replacement = await env.store.open_session(LANE_HOST, "replacement", role="lead",
                    pane_status="pane_alive", bootstrap_state="ready")
                await env.store.lifecycle_authority_mutate(
                    {"action": "designate", "expected_revision": 1, "reason": "Replace fixture manager",
                     "request_id": "replace", "target_stream_id": LANE_HOST + ":replacement",
                     "target_generation": replacement["session_generation"]},
                    {**OPERATOR, "_consent_id": "fixture-consent"}, env.sessions.assistant.role)
            elif change == "child":
                await env.store.open_session(LANE_HOST, "child", parent_stream_id=TARGET)
            elif change == "pending":
                assert await env.store.reserve_stream_id(LANE_HOST, "pending", ttl_s=60, request_id="spawn", nonce="n")
                assert await env.store.record_spawn_intent(LANE_HOST, "pending", {
                    "open_fields": {"parent_stream_id": TARGET}}, request_id="spawn", nonce="n")
            elif change == "busy":
                env.tmux.text = "Working (esc to interrupt)\n"
            elif change == "unknown":
                env.tmux.capture_ok = False
            else:
                await env.store.open_session(LANE_HOST, "lane" if change == "target" else "primary",
                                             session_generation="replacement")
            if change == "target":
                with pytest.raises(VerbError, match="target_generation_changed"):
                    await env.approve()
            else:
                await env.approve()
            assert env.tmux.kills == 0
            assert (await env.row())["state"] != "done"
            assert (await env.store.fetch_session(LANE_HOST, "lane"))["status"] == "open"
    asyncio.run(run())


@pytest.mark.parametrize("ruling", ["deny", "revise", "timeout", "unavailable"])
def test_no_report_waiver_preserves_external_ruling_dispositions(ruling):
    async def run():
        async with journey() as env:
            await lane_manager(env)
            if ruling == "unavailable":
                await env.store.mark_closed(LANE_HOST, "advisor", closed_at="2026-09-28T00:00:00Z",
                                            pane_status="pane_dead")
            result = await env.request()
            if ruling == "timeout":
                await env.store.submit(lambda c: c.execute(
                    "UPDATE v2_assistant_lane_rulings SET deadline=0 WHERE ruling_request_id=?", (env.rid,)))
                await env.rulings.tick()
            elif ruling in {"deny", "revise"}:
                await env.server._on_assistant_ruling({"ruling_request_id": env.rid, "request_id": "decision",
                    "ruling": ruling, "reason": "Fixture disposition", "_auth_context": await env.auth("advisor")})
            row = await env.row()
            assert row["state"] == {"deny": "denied", "revise": "revised"}.get(ruling, "done")
            assert env.tmux.kills == (0 if ruling in {"deny", "revise"} else 1)
            if ruling in {"timeout", "unavailable"}:
                assert row["mirror_sent"] == 1 and row["ruling"] is None
                assert row["reason"] == ("deadline" if ruling == "timeout" else "authority_unavailable")
    asyncio.run(run())


@pytest.mark.parametrize("case", ["ordinary", "unverified", "default_reason", "missing_request"])
def test_no_report_lane_rejects_forged_marker_and_missing_authority(case):
    async def run():
        async with journey() as env:
            if case != "ordinary":
                await lane_manager(env)
            else:
                await env.store.submit(lambda c: c.execute("DELETE FROM v2_reports"))
            auth = await env.auth("primary")
            if case == "unverified":
                auth["token_verified"] = False
            msg = {"host": LANE_HOST, "session_name": "lane", "request_id": "test-request",
                   "reason": "Explicit fixture close", "_auth_context": auth,
                   "_ruling_report_waiver": {"manager_stream_id": ROOT}, "_ruling_report": {"report_id": "forged"}}
            if case == "default_reason":
                msg["reason"] = "manual"
            elif case == "missing_request":
                msg.pop("request_id")
            with pytest.raises(VerbError):
                await env.server._on_close(msg)
            assert env.tmux.kills == 0
            assert await env.store.submit(lambda c: c.execute("SELECT count(*) FROM v2_assistant_lane_rulings").fetchone()[0]) == 0
    asyncio.run(run())


def test_no_report_intent_survives_restart_and_report_arrival(tmp_path):
    from test_approved_close_retry import Journey
    async def run():
        async with journey(tmp_path / "sessions.db") as env:
            await lane_manager(env)
            await env.request()
            original = await env.row()
            await env.report()
            assert (await env.request())["ruling_request_id"] == original["ruling_request_id"]
            restarted = Journey(env.store, env.tmux, env.config)
            restarted.rid = env.rid
            try:
                assert (await restarted.approve())["state"] == "done"
                assert env.tmux.kills == 1
                row = await restarted.row()
                assert row["intent_json"] == original["intent_json"]
                assert row["intent_digest"] == original["intent_digest"]
                rows = await env.store.lifecycle_authority_audit_rows(limit=50)
                assert not any(r["action"] == "manager_close_report_waived" for r in rows)
            finally:
                await restarted.rulings.stop()
    asyncio.run(run())


@pytest.mark.parametrize("binding", ["disabled", "", ROOT])
def test_no_report_manager_keeps_existing_non_external_binding_paths(binding):
    async def run():
        async with journey() as env:
            manager = await lane_manager(env)
            await env.store.put("assistant.authority.stream_id", binding)
            reply = await env.server._on_close({"host": LANE_HOST, "session_name": "lane",
                "request_id": "close", "reason": "Retire disposable lane", "_auth_context": manager})
            assert reply["type"] == "close.ok" and env.tmux.kills == 1
            assert await env.store.submit(lambda c: c.execute("SELECT count(*) FROM v2_assistant_lane_rulings").fetchone()[0]) == 0
    asyncio.run(run())


def test_operator_lane_close_without_report_does_not_use_manager_waiver():
    async def run():
        async with journey() as env:
            await env.store.submit(lambda c: c.execute("DELETE FROM v2_reports"))
            env.tmux.text = "ready\n"
            reply = await env.server._on_close({"host": LANE_HOST, "session_name": "lane",
                "request_id": "operator-close", "reason": "Fixture operator retirement", "_auth_context": OPERATOR})
            assert reply["type"] == "close.ok" and env.tmux.kills == 1
            assert not any(r["action"].startswith("manager_close")
                           for r in await env.store.lifecycle_authority_audit_rows(limit=50))
    asyncio.run(run())


@pytest.mark.parametrize("direct_parent", [False, True])
@pytest.mark.parametrize("reported", [False, True])
def test_lane_rejects_stale_generation_before_reserving_ruling(direct_parent, reported):
    async def run():
        async with journey() as env:
            manager = await lane_manager(env, direct_parent=direct_parent)
            if reported:
                await env.report()
            with pytest.raises(VerbError) as error:
                await env.server._on_close({"host": LANE_HOST, "session_name": "lane",
                    "request_id": "stale-lane", "reason": "Retire exact acceptance generation",
                    "expected_generation": "prior-generation", "_auth_context": manager})
            assert error.value.code == "lifecycle_generation_mismatch"
            assert env.tmux.kills == 0
            assert (await env.store.fetch_session(LANE_HOST, "lane"))["status"] == "open"
            assert await env.store.submit(lambda c: c.execute("SELECT count(*) FROM v2_assistant_lane_rulings").fetchone()[0]) == 0
    asyncio.run(run())


@pytest.mark.parametrize("stored", ["prior-generation", None])
@pytest.mark.parametrize("release", ["approve", "timeout"])
@pytest.mark.parametrize("reported", [False, True])
def test_legacy_lane_intent_preserves_generation_on_restart(stored, release, reported, tmp_path):
    from assistant_lane_rulings import _canonical, _digest
    from test_approved_close_retry import Journey
    async def run():
        async with journey(tmp_path / "sessions.db") as env:
            await lane_manager(env)
            if reported:
                await env.report()
            create = env.rulings._create

            async def legacy_create(**fields):
                intent = fields["intent"]
                if stored is None:
                    intent.pop("expected_generation", None)
                else:
                    intent["expected_generation"] = stored
                return await create(**fields)

            # Model the old writer before reservation/notice, keeping the
            # durable digest and outbound notice consistent with that writer.
            env.rulings._create = legacy_create
            await env.request()
            env.rulings._create = create
            original = await env.row()
            intent = json.loads(original["intent_json"])
            restarted = Journey(env.store, env.tmux, env.config)
            restarted.rid = env.rid
            try:
                if stored is None:
                    assert (await restarted.request())["ruling_request_id"] == env.rid
                if release == "approve":
                    await restarted.approve()
                else:
                    await env.store.submit(lambda c: c.execute(
                        "UPDATE v2_assistant_lane_rulings SET deadline=0 WHERE ruling_request_id=?", (env.rid,)))
                    await restarted.rulings.tick()
                row = await restarted.row()
                assert row["intent_json"] == _canonical(intent)
                assert row["intent_digest"] == _digest(intent)
                if stored is None:
                    assert row["state"] == "done" and env.tmux.kills == 1
                else:
                    assert row["state"] in {"approved_but_not_closed", "release_blocked"}
                    assert json.loads(row["outcome_json"])["error"] == "target_generation_changed"
                    assert env.tmux.kills == 0
                    assert (await env.store.fetch_session(LANE_HOST, "lane"))["status"] == "open"
            finally:
                await restarted.rulings.stop()
    asyncio.run(run())


def test_lane_omitted_generation_is_frozen_and_explicit_retry_matches():
    async def run():
        async with journey() as env:
            manager = await lane_manager(env)
            await env.request()
            row = await env.row()
            assert json.loads(row["intent_json"])["expected_generation"] == row["target_generation"]
            reply = await env.server._on_close({"type": "close", "host": LANE_HOST, "session_name": "lane",
                "request_id": "close-lane", "reason": "accepted lane finished", "defer_if_working": True,
                "expected_generation": row["target_generation"], "_auth_context": manager})
            await env.rulings.stop()
            assert reply["ruling_request_id"] == env.rid
            assert (await env.row())["intent_json"] == row["intent_json"]
    asyncio.run(run())


def test_manager_generation_change_before_ruling_does_not_rebind_intent():
    async def run():
        async with journey() as env:
            manager = await lane_manager(env)
            fetch = env.store.fetch_session
            reads = 0

            async def changing_fetch(host, name):
                nonlocal reads
                if (host, name) == (LANE_HOST, "lane"):
                    reads += 1
                    if reads == 2:
                        await env.store.open_session(host, name, session_generation="replacement")
                return await fetch(host, name)

            env.store.fetch_session = changing_fetch
            with pytest.raises(VerbError) as error:
                await env.server._on_close({"host": LANE_HOST, "session_name": "lane",
                    "request_id": "generation-race", "reason": "Retire exact acceptance generation",
                    "_auth_context": manager})
            assert error.value.code == "lifecycle_generation_mismatch"
            assert env.tmux.kills == 0
            assert await env.store.submit(lambda c: c.execute("SELECT count(*) FROM v2_assistant_lane_rulings").fetchone()[0]) == 0
    asyncio.run(run())
