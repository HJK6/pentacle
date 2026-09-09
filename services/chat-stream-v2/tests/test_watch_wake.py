"""D2 caller, clock, transaction and lifecycle contracts (fixture frozen first)."""
import asyncio
import json
import hashlib
import time

import pytest

from server import Server
from sessions import Sessions
from store import Store
from watch_wake import WatchWake, registration_payload, run_reconcile_callbacks


def exercise(check):
    async def run():
        store = Store()
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            parent = await sessions.open("hosta", "p", provider="shell")
            child = await sessions.open("hosta", "c", provider="shell", no_watch=True,
                                        parent_stream_id="hosta:p")
            await check(store, sessions, parent, child)
        finally:
            store.stop()
    asyncio.run(run())


def test_wake_atomic_replay_and_owner_lifecycle():
    async def check(store, sessions, parent, child):
        gen = parent["session_generation"]
        payload = {"request_id": "one", "due_at": 110, "note": "resume", "urgent": False}
        first = await store.register_watch_wake("wake", "hosta:p", gen, payload, now=100)
        assert first == await store.register_watch_wake("wake", "hosta:p", gen, payload, now=105)
        with pytest.raises(ValueError, match="request_conflict"):
            await store.register_watch_wake("wake", "hosta:p", gen, {**payload, "note": "other"}, now=105)
        assert await store.evaluate_watch_wake({}, now=109) == 0
        assert await store.evaluate_watch_wake({}, now=110) == 1
        assert await store.evaluate_watch_wake({}, now=500) == 0
        rows = await store.list_watch_wake("wake", "hosta:p", gen)
        assert rows[0]["state"] == "consumed"
        notices = await store.submit(lambda c: [dict(r) for r in c.execute("SELECT * FROM v2_outbound_notices")])
        assert len(notices) == 1 and notices[0]["notice_id"] == rows[0]["notice_id"]
        with pytest.raises(ValueError, match="not_owner"):
            await store.cancel_watch_wake("wake", "hosta:c", child["session_generation"], first["id"])
        await store.update_session("hosta", "p", status="closed")
        assert await store.watch_notice_valid(notices[0]["notice_id"]) is False
    exercise(check)


def test_idle_strict_boundary_quiet_coalescence_and_short_turn():
    async def check(store, sessions, parent, child):
        gen = child["session_generation"]
        await store.register_watch_wake("watch", "hosta:p", parent["session_generation"],
            {"request_id": "watch", "child_stream_id": "hosta:c", "triggers": ["idle", "quiet=15"], "repeat": True}, now=100)
        observation = {"hosta:c": {"session_generation": gen, "working": False}}
        assert await store.evaluate_watch_wake(observation, now=100.0) == 0
        assert await store.evaluate_watch_wake(observation, now=1000) == 1  # quiet is >=
        assert await store.evaluate_watch_wake(observation, now=1001) == 0  # same fact, idle >
        assert await store.evaluate_watch_wake(observation, now=2000) == 0
        observation["hosta:c"].update(genuine_activity_generation=gen, genuine_activity_at=1999)
        assert await store.evaluate_watch_wake(observation, now=2001) == 0
        assert await store.evaluate_watch_wake(observation, now=2899) == 1
        assert await store.evaluate_watch_wake(observation, now=2900) == 0
    exercise(check)


def test_watch_rejects_grandchild_and_retires_on_reparent():
    async def check(store, sessions, parent, child):
        await sessions.open("hosta", "g", provider="shell", parent_stream_id="hosta:c", no_watch=True)
        payload = {"request_id": "watch", "child_stream_id": "hosta:g", "triggers": ["end"], "repeat": False}
        with pytest.raises(ValueError, match="not_direct_child"):
            await store.register_watch_wake("watch", "hosta:p", parent["session_generation"], payload, now=100)
        await store.register_watch_wake("watch", "hosta:p", parent["session_generation"],
                                        {**payload, "child_stream_id": "hosta:c"}, now=100)
        await store.update_session("hosta", "c", parent_stream_id=None)
        rows = await store.list_watch_wake("watch", "hosta:p", parent["session_generation"])
        assert rows[0]["state"] == "cancelled"
    exercise(check)


@pytest.mark.parametrize("payload,code", [
    ({}, "exactly_one_time_required"), ({"in": "1m", "at": "2099-01-01T00:00:00Z"}, "exactly_one_time_required"),
    ({"in": "0s"}, "invalid_duration"), ({"in": "-1h"}, "invalid_duration"),
    ({"in": "1.5h"}, "invalid_duration"), ({"in": "60"}, "invalid_duration"),
    ({"at": "2099-01-01T00:00:00"}, "invalid_timestamp"),
])
def test_invalid_times(payload, code):
    with pytest.raises(ValueError, match=code):
        registration_payload("wake", payload, time.time())


def test_offset_normalization():
    assert registration_payload("wake", {"at": "2099-01-01T01:00:00+01:00"}, 100)["due_at"] == \
        registration_payload("wake", {"at": "2099-01-01T00:00:00Z"}, 100)["due_at"]


def test_auth_identity_generation_and_retry_contract():
    async def check(store, sessions, parent, child):
        token = "private-test-token"
        await store.update_session("hosta", "p", token_hash=hashlib.sha256(token.encode()).hexdigest())
        clock = [100]
        api = WatchWake(store, sessions, clock=lambda: clock[0])
        msg = {"type": "wake.register", "request_id": "auth", "in": "10s", "stream_token": token,
               "_auth_context": {"stream_id": "hosta:p", "token_verified": True}}
        response = await api.handle(msg)
        assert response["ok"] and response["wake"]["due_at"] == 110
        clock[0] = 105
        assert (await api.handle(msg))["wake"]["id"] == response["wake"]["id"]
        past = {**msg, "request_id": "past", "in": None, "at": "1970-01-01T00:01:00Z"}
        assert (await api.handle(past))["error_code"] == "time_not_future"
        absolute = {**msg, "request_id": "absolute", "in": None, "at": "1970-01-01T00:02:00Z"}
        registered = await api.handle(absolute)
        assert registered["ok"]
        clock[0] = 150
        assert (await api.handle(absolute))["wake"]["id"] == registered["wake"]["id"]
        assert (await api.handle({**msg, "from_stream_id": "hosta:c"}))["error_code"] == "caller_mismatch"
        assert (await api.handle({**msg, "_auth_context": {"operator_authenticated": True}}))["error_code"] == "not_authenticated"
        await sessions.open("hosta", "p", provider="shell", token_hash=hashlib.sha256(b"replacement").hexdigest())
        assert (await api.handle(msg))["error_code"] == "stale_generation"
        assert await store.evaluate_watch_wake({}, now=500) == 0
    exercise(check)


def test_dispatch_strips_forged_internal_identity():
    async def run():
        reply = await Server()._dispatch(json.dumps({"type": "wake.list", "request_id": "forged",
            "_auth_context": {"stream_id": "hosta:p", "token_verified": True}}))
        assert reply[0] == {"type": "wake.error", "request_id": "forged", "ok": False, "error_code": "not_authenticated"}
    asyncio.run(run())


@pytest.mark.parametrize("activity", [None, float("nan"), float("inf"), 99999, -1, True])
def test_missing_invalid_future_activity_does_not_shorten_quiet(activity):
    async def check(store, sessions, parent, child):
        await store.register_watch_wake("watch", "hosta:p", parent["session_generation"],
            {"request_id": "quiet", "child_stream_id": "hosta:c", "triggers": ["quiet=1"]}, now=100)
        obs = {"hosta:c": {"session_generation": child["session_generation"], "working": True,
                "genuine_activity_generation": child["session_generation"], "genuine_activity_at": activity}}
        assert await store.evaluate_watch_wake(obs, now=159) == 0
        assert await store.evaluate_watch_wake(obs, now=160) == 1
        assert await store.evaluate_watch_wake(obs, now=500) == 0
    exercise(check)


def test_subpass_working_observation_resets_idle_only():
    async def check(store, sessions, parent, child):
        await store.register_watch_wake("watch", "hosta:p", parent["session_generation"],
            {"request_id": "idle", "child_stream_id": "hosta:c", "triggers": ["idle"], "repeat": True}, now=100)
        obs = {"hosta:c": {"session_generation": child["session_generation"], "working": False}}
        await store.evaluate_watch_wake(obs, now=100)
        obs["hosta:c"]["watch_working_at"] = 999
        assert await store.evaluate_watch_wake(obs, now=1001) == 0
        assert await store.evaluate_watch_wake(obs, now=1901) == 0
        assert await store.evaluate_watch_wake(obs, now=1902) == 1
    exercise(check)


def test_pending_inactivity_cancelled_but_end_delivers_after_close():
    async def check(store, sessions, parent, child):
        await store.register_watch_wake("watch", "hosta:p", parent["session_generation"],
            {"request_id": "end", "child_stream_id": "hosta:c", "triggers": ["quiet=1", "end"]}, now=100)
        assert await store.evaluate_watch_wake({}, now=160) == 1
        await sessions.mark_closed("hosta", "c", reason="D2 lifecycle contract")
        rows = await store.submit(lambda c: [dict(r) for r in c.execute("SELECT * FROM v2_outbound_notices ORDER BY rowid")])
        assert len(rows) == 2
        assert rows[0]["terminal_reason"] == "watch_lifecycle_retired"
        assert await store.watch_notice_valid(rows[1]["notice_id"])
        await sessions.open("hosta", "c", provider="shell", no_watch=True)
        assert await store.watch_notice_valid(rows[1]["notice_id"])
    exercise(check)


@pytest.mark.parametrize("terminal_status", ["done", "aborted", "error", "death"])
@pytest.mark.parametrize("opt_out", [False, True])
def test_terminal_notice_survives_child_recovery(tmp_path, terminal_status, opt_out):
    from types import SimpleNamespace
    from tests.test_report_variants import _provenance_state, _provenance_report
    from tests.test_outbound_notices import _RecordingComms, _stored_notice
    from outbound_notices import OutboundNoticeQueue
    from reconciler import SessionReconciler

    async def run():
        store, sessions, ledger, _ = await _provenance_state(tmp_path)
        try:
            await sessions.open("alpha", "parent", provider="shell")
            await store.update_session("alpha", "worker", no_watch=opt_out,
                                       parent_stream_id="alpha:parent")
            await sessions.refresh()
            child = await store.fetch_session("alpha", "worker")
            # The first delivery is deferred; use the real ledger/reconciler producer.
            if terminal_status == "death":
                await SessionReconciler(sessions, None, outbound=ledger.outbound)._surface(
                    child, {"episode_id": "death"}, "tmux_absent")
            else:
                await ledger.ingest({**_provenance_report("terminal"), "status": terminal_status,
                                     "reason": "recovery regression"})
            notice = await store.submit(lambda c: dict(c.execute(
                "SELECT * FROM v2_outbound_notices").fetchone()))
            assert notice["kind"] == ("reconciler" if terminal_status == "death" else "report")
            assert not notice["delivered_at"] and not notice["terminal_at"]
            sessions.tmux = SimpleNamespace(session_state=lambda name: "alive",
                                            pane_identity=lambda name: {"pane_pid": "999"})
            await sessions.mark_reconciled_dead("alpha", "worker",
                expected_generation=child["session_generation"],
                presumed_dead_at="2026-09-08T18:00:00Z", closed_at="2026-09-08T18:00:01Z")
            assert (await sessions.restore_reconciled("alpha", "worker"))["restored"]
            restored = await store.fetch_session("alpha", "worker")
            assert restored["session_generation"] != child["session_generation"]
            transport = _RecordingComms()
            queue = OutboundNoticeQueue(store, transport)
            service = WatchWake(store, sessions, queue)
            assert await service.delivery_guard(notice) is None
            await store.evaluate_watch_wake({})
            pending = await _stored_notice(store, notice["notice_id"])
            assert not pending["terminal_at"]
            assert pending["body"] == notice["body"] and pending["tell_id"] == notice["tell_id"]
            assert await queue.deliver_now(notice["notice_id"])
            await queue.deliver_now(notice["notice_id"])
            assert len(transport.calls) == 1
            assert transport.calls[0][0]["to_stream_id"] == "alpha:parent"
            assert (await _stored_notice(store, notice["notice_id"]))["delivered_at"]
            # The historical fact still cannot reach a replacement recipient.
            await sessions.mark_closed("alpha", "parent", reason="recipient replacement control")
            await sessions.open("alpha", "parent", provider="shell")
            assert not await store.watch_notice_valid(notice["notice_id"])
        finally:
            store.stop()
    asyncio.run(run())


def test_store_failure_rolls_back_consumption_and_notice(monkeypatch):
    import store_watch_wake
    async def check(store, sessions, parent, child):
        await store.register_watch_wake("wake", "hosta:p", parent["session_generation"],
            {"request_id": "crash", "due_at": 110}, now=100)
        original = store_watch_wake._consume
        def fail(*args):
            original(*args)
            raise RuntimeError("after consumption before commit")
        monkeypatch.setattr(store_watch_wake, "_consume", fail)
        with pytest.raises(RuntimeError, match="before commit"):
            await store.evaluate_watch_wake({}, now=110)
        rows = await store.list_watch_wake("wake", "hosta:p", parent["session_generation"])
        assert rows[0]["state"] == "active"
        assert await store.submit(lambda c: c.execute("SELECT COUNT(*) FROM v2_outbound_notices").fetchone()[0]) == 0
        monkeypatch.setattr(store_watch_wake, "_consume", original)
        assert await store.evaluate_watch_wake({}, now=120) == 1
    exercise(check)


def test_callbacks_continue_after_failure():
    seen = []
    async def fail():
        seen.append("pin")
        raise RuntimeError("pin failed")
    async def questions():
        seen.append("questions")
    async def watches():
        seen.append("watches")
    asyncio.run(run_reconcile_callbacks(fail, questions, watches))
    assert seen == ["pin", "questions", "watches"]


@pytest.mark.parametrize("terminal_status", ["done", "aborted"])
def test_accepted_reports_coalesce_and_progress_rearms_blocker(tmp_path, terminal_status):
    from tests.test_report_variants import _provenance_state, _provenance_report
    from reconciler import SessionReconciler
    async def run():
        store, sessions, ledger, frames = await _provenance_state(tmp_path)
        try:
            parent = await sessions.open("alpha", "parent", provider="shell")
            await store.update_session("alpha", "worker", parent_stream_id="alpha:parent")
            await sessions.refresh()
            watches = await store.list_watch_wake("watch", "alpha:parent", parent["session_generation"])
            assert len(watches) == 1
            msg = {**_provenance_report("error-1"), "status": "error", "reason": "blocked fixture"}
            first = await ledger.ingest(msg)
            await ledger.ingest(msg)
            watches = await store.list_watch_wake("watch", "alpha:parent", parent["session_generation"])
            assert set(watches[0]["consumed"]) == {"blocker"}
            await ledger.ingest({**_provenance_report("progress"), "status": "progress"})
            watches = await store.list_watch_wake("watch", "alpha:parent", parent["session_generation"])
            assert "blocker" not in watches[0]["consumed"]
            await ledger.ingest({**_provenance_report("error-2"), "status": "error", "reason": "blocked fixture"})
            done = await ledger.ingest({**_provenance_report("done"), "status": terminal_status,
                                       "reason": "D2 terminal fixture"})
            ended = (await store.list_watch_wake("watch", "alpha:parent", parent["session_generation"]))[0]
            assert ended["state"] == "consumed"
            assert ended["retired_reason"] == "terminal_end"
            observation = {"alpha:worker": {"session_generation": done["session_generation"], "working": False}}
            assert await store.evaluate_watch_wake(observation, now=time.time()) == 0
            assert await store.evaluate_watch_wake(observation, now=time.time() + 901) == 0
            await store.register_watch_wake("watch", "alpha:parent", parent["session_generation"],
                {"request_id": "late", "child_stream_id": "alpha:worker", "triggers": ["end"]})
            await ledger.ingest(_provenance_report("another-done"))
            late = (await store.list_watch_wake("watch", "alpha:parent", parent["session_generation"]))[-1]
            assert late["consumed"] == {}  # the generation's terminal fact predates registration
            assert await store.submit(lambda c: c.execute("SELECT COUNT(*) FROM v2_watch_facts WHERE kind='end'").fetchone()[0]) == 1
            await sessions.mark_closed("alpha", "worker", reason="D2 reported close contract")
            await SessionReconciler(sessions, None, outbound=ledger.outbound)._surface(
                await store.fetch_session("alpha", "worker"), {"episode_id": "death"}, "tmux_absent")
            rows = await store.submit(lambda c: [dict(r) for r in c.execute("SELECT * FROM v2_outbound_notices")])
            assert len(rows) == 3  # two new errors, one terminal generation
            watches = await store.list_watch_wake("watch", "alpha:parent", parent["session_generation"])
            assert set(watches[0]["consumed"]) == {"blocker", "end"}
            assert len(frames) == 3
            assert first["report_id"] != done["report_id"]
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("trigger", ["idle", "quiet=15"])
def test_watch_without_end_survives_report_until_physical_close(tmp_path, trigger):
    from tests.test_report_variants import _provenance_state, _provenance_report
    async def run():
        store, sessions, ledger, _ = await _provenance_state(tmp_path)
        try:
            parent = await sessions.open("alpha", "parent", provider="shell")
            await store.update_session("alpha", "worker", parent_stream_id="alpha:parent", no_watch=True)
            await sessions.refresh()
            watch = await store.register_watch_wake("watch", "alpha:parent", parent["session_generation"],
                {"request_id": "endless", "child_stream_id": "alpha:worker", "triggers": [trigger]})
            await ledger.ingest(_provenance_report("done"))
            rows = await store.list_watch_wake("watch", "alpha:parent", parent["session_generation"])
            assert next(row for row in rows if row["id"] == watch["id"])["state"] == "active"
            await sessions.mark_closed("alpha", "worker", reason="D2 physical close contract")
            rows = await store.list_watch_wake("watch", "alpha:parent", parent["session_generation"])
            assert next(row for row in rows if row["id"] == watch["id"])["state"] == "cancelled"
        finally:
            store.stop()
    asyncio.run(run())


def test_default_reparent_gets_fresh_baseline_and_rollback_does_not_reinstall():
    async def check(store, sessions, parent, child):
        await store.update_session("hosta", "c", no_watch=False)
        initial = await store.list_watch_wake("watch", "hosta:p", parent["session_generation"])
        await store.update_session("hosta", "c", parent_stream_id=None)
        await store.update_session("hosta", "c", parent_stream_id="hosta:p")
        watches = await store.list_watch_wake("watch", "hosta:p", parent["session_generation"])
        assert len(watches) == 2 and watches[-1]["id"] != initial[0]["id"]
        assert watches[0]["state"] == "cancelled"
        await store.cancel_watch_wake_for_rollback()
        await store.evaluate_watch_wake({})
        watches = await store.list_watch_wake("watch", "hosta:p", parent["session_generation"])
        assert len(watches) == 2 and all(w["state"] == "cancelled" for w in watches)
    exercise(check)


@pytest.mark.parametrize("opt_out", [True, False])
def test_reconciler_restore_preserves_default_watch_choice(opt_out):
    from types import SimpleNamespace
    async def check(store, sessions, parent, child):
        await store.update_session("hosta", "c", no_watch=opt_out)
        await sessions.refresh()
        sessions.tmux = SimpleNamespace(session_state=lambda name: "alive",
                                        pane_identity=lambda name: {"pane_pid": "999"})
        await sessions.mark_reconciled_dead("hosta", "c", expected_generation=child["session_generation"],
            presumed_dead_at="2026-09-08T15:00:00Z", closed_at="2026-09-08T15:00:01Z")
        restored = await sessions.restore_reconciled("hosta", "c")
        assert restored["restored"] is True
        row = await store.fetch_session("hosta", "c")
        assert row["session_generation"] != child["session_generation"]
        assert row["no_watch"] is opt_out
        await store.evaluate_watch_wake({})  # recovery/adoption replay cannot add another default
        watches = await store.list_watch_wake("watch", "hosta:p", parent["session_generation"])
        active = [watch for watch in watches if watch["state"] == "active"]
        assert len(active) == (0 if opt_out else 1)
        assert all(watch["child_generation"] == row["session_generation"] for watch in active)
    exercise(check)


def test_cancel_request_identity_is_bound_to_target_and_verb():
    async def check(store, sessions, parent, child):
        gen = parent["session_generation"]
        one = await store.register_watch_wake("wake", "hosta:p", gen, {"request_id": "one", "due_at": 110}, now=100)
        two = await store.register_watch_wake("wake", "hosta:p", gen, {"request_id": "two", "due_at": 110}, now=100)
        for _ in range(2):
            assert await store.cancel_watch_wake("wake", "hosta:p", gen, one["id"], request_id="cancel") == {"id": one["id"]}
        with pytest.raises(ValueError, match="request_conflict"):
            await store.cancel_watch_wake("wake", "hosta:p", gen, two["id"], request_id="cancel")
        with pytest.raises(ValueError, match="request_conflict"):
            await store.cancel_watch_wake("wake", "hosta:p", gen, two["id"], request_id="one")
        with pytest.raises(ValueError, match="request_conflict"):
            await store.register_watch_wake("wake", "hosta:p", gen, {"request_id": "cancel", "due_at": 110}, now=100)
        assert await store.evaluate_watch_wake({}, now=110) == 1
    exercise(check)


def test_failed_default_install_rolls_back_admission(monkeypatch):
    import store as store_module
    async def check(store, sessions, parent, child):
        def fail(*args):
            raise RuntimeError("injected installation failure")
        monkeypatch.setattr(store_module, "install_default_conn", fail)
        with pytest.raises(RuntimeError, match="installation"):
            await sessions.open("hosta", "new", provider="shell", parent_stream_id="hosta:p")
        assert await store.fetch_session("hosta", "new") is None
    exercise(check)


def test_delivery_and_replacement_serialize_on_owner_lifecycle():
    from outbound_notices import OutboundNoticeQueue
    async def check(store, sessions, parent, child):
        entered, release = asyncio.Event(), asyncio.Event()
        class Transport:
            async def deliver_outbound_notice(self, msg, **kwargs):
                entered.set()
                await release.wait()
                return {"delivery_status": "delivered", "submission_confirmed": True}
        queue = OutboundNoticeQueue(store, Transport())
        WatchWake(store, sessions, queue)
        await store.register_watch_wake("wake", "hosta:p", parent["session_generation"],
            {"request_id": "race", "due_at": 110}, now=100)
        await store.evaluate_watch_wake({}, now=110)
        delivery = asyncio.create_task(queue.drain_once(force=True))
        await entered.wait()
        close = asyncio.create_task(store.mark_closed("hosta", "p", closed_at="2026-09-08T00:00:00Z", pane_status="pane_dead"))
        await asyncio.sleep(0)
        assert not close.done()
        release.set()
        assert await delivery == 1
        await close
        replacement = await sessions.open("hosta", "p", provider="shell")
        assert replacement["session_generation"] != parent["session_generation"]
        assert await queue.drain_once(force=True) == 0
    exercise(check)


def test_enqueue_timestamp_includes_store_queue_delay():
    from datetime import datetime
    async def check(store, sessions, parent, child):
        started = time.time()
        await store.register_watch_wake("wake", "hosta:p", parent["session_generation"],
            {"request_id": "slow", "due_at": started + 0.01}, now=started)
        released = []
        def slow_pass(conn):
            time.sleep(0.04)
            released.append(time.time())
        blocker = asyncio.create_task(store.submit(slow_pass))
        await asyncio.sleep(0)
        assert await store.evaluate_watch_wake({}, now=started + 0.02) == 1
        await blocker
        row = await store.submit(lambda c: dict(c.execute("SELECT * FROM v2_outbound_notices").fetchone()))
        assert json.loads(row["metadata"])["trigger_at"] == started + 0.01
        assert datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).timestamp() >= released[0]
    exercise(check)


def test_confirmed_watcher_death_retires_work_immediately():
    async def check(store, sessions, parent, child):
        await store.register_watch_wake("wake", "hosta:p", parent["session_generation"],
            {"request_id": "death", "due_at": 110}, now=100)
        await store.evaluate_watch_wake({}, now=110)
        await store.mark_reconciled_dead("hosta", "p", expected_generation=parent["session_generation"],
            presumed_dead_at="2026-09-08T00:00:00Z", closed_at="2026-09-08T00:00:01Z")
        rows = await store.submit(lambda c: [dict(r) for r in c.execute("SELECT * FROM v2_outbound_notices")])
        assert rows[0]["terminal_at"]
        states = await store.submit(lambda c: [r[0] for r in c.execute("SELECT state FROM v2_watch_wake")])
        assert all(state != "active" for state in states)
    exercise(check)


@pytest.mark.parametrize("verb", ["wake.register", "wake.list", "wake.cancel",
                                  "watch.register", "watch.list", "watch.cancel"])
def test_watch_wake_wire_is_registered(verb):
    async def run():
        server = Server()
        reply = await server._dispatch(json.dumps({"type": verb, "request_id": "red"}))
        assert reply[0].get("error_code") != "unsupported_in_v2"
        assert reply[0]["request_id"] == "red"
    asyncio.run(run())


def test_default_watch_installed_on_admitted_generation():
    async def run():
        store = Store()
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            parent = await sessions.open("hosta", "d2-parent", provider="shell")
            child = await sessions.open("hosta", "d2-child", provider="shell",
                                        parent_stream_id="hosta:d2-parent")
            rows = await store.list_watch_wake("watch", "hosta:d2-parent",
                                                parent["session_generation"])
            assert len(rows) == 1
            assert rows[0]["child_generation"] == child["session_generation"]
            assert rows[0]["triggers"] == ["end", "blocker", "idle"]
        finally:
            store.stop()
    asyncio.run(run())
