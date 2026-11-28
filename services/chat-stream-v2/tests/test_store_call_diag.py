"""Named Store call timing: slow lines, hourly top list and the daily slow alert."""
from __future__ import annotations

import _thread
import asyncio
from concurrent.futures import Future
import functools
import json
import logging
import re
import time
from types import SimpleNamespace

import pytest

from alerts import Alerts
from error_adapters import ErrorFact
from loop_watchdog import RequestTiming, request_timing
from store import Store
import store_call_diag
import store_host_transfer as host_transfer


def slow_client_op(conn):
    time.sleep(0.08)
    return "client"


def slow_actor_op(conn):
    time.sleep(0.08)
    return "actor"


def slow_job_op(conn):
    time.sleep(0.08)
    return "job"


def fast_op(conn):
    return "fast"


async def finish_prior_store_calls(store):
    # The worker records diagnostics before starting the next queued call.
    assert await asyncio.wait_for(store.submit(fast_op), timeout=1.0) == "fast"


def slow_raising_op(conn):
    time.sleep(0.08)
    raise LookupError("kept")


def slow_payload_op(conn, payload):
    time.sleep(0.08)
    return payload


def lines(caplog, kind):
    return [json.loads(r.getMessage().split(" ", 1)[1]) for r in caplog.records
            if r.getMessage().startswith(kind + " ")]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(store_call_diag, "SLOW_LINE_S", 0.05)
    monkeypatch.setattr(store_call_diag, "ALERT_S", 60.0)
    s = Store(str(tmp_path / "hot.db"))
    s.start()
    yield s
    s.stop()


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_slow_ordinary_actor_and_internal_calls_each_log_one_named_line(store, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="chat_streamd_v2.store")
    monkeypatch.setattr(host_transfer, "require_active", lambda conn, *actor: None)
    timing = RequestTiming()
    token = request_timing.set(timing)
    try:
        assert await store.submit(slow_client_op) == "client"
        assert await store.submit(fast_op) == "fast"
    finally:
        request_timing.reset(token)
    token = host_transfer.actor_context.set(("fixture-stream", "fixture-generation"))
    try:
        assert await store.submit(slow_actor_op) == "actor"
    finally:
        host_transfer.actor_context.reset(token)
    # An internal job runs outside any client request timing.
    assert await asyncio.create_task(store.submit(slow_job_op)) == "job"
    await finish_prior_store_calls(store)
    slow = lines(caplog, "store_call_slow")
    assert [line["name"] for line in slow] == ["slow_client_op", "slow_actor_op", "slow_job_op"]
    for line in slow:
        assert set(line) == {"name", "queue_ms", "execution_ms", "caller", "worker"}
        assert line["worker"] == "writer"
        assert line["execution_ms"] >= 50 and line["queue_ms"] >= 0
    assert timing.calls == 2


@pytest.mark.asyncio
async def test_long_qualname_is_bounded_in_line_and_alert(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="chat_streamd_v2.store")
    monkeypatch.setattr(store_call_diag, "SLOW_LINE_S", 0.05)
    monkeypatch.setattr(store_call_diag, "ALERT_S", 0.05)
    facts = []

    async def sink(fact):
        facts.append(fact)

    def op(conn):
        time.sleep(0.08)
        return 7
    op.__qualname__ = "Very.long<locals>." + "x" * 250
    s = Store(str(tmp_path / "hot.db")); s.start()
    s.call_diag.alert = sink
    try:
        assert await s.submit(op) == 7
        await finish_prior_store_calls(s)
        await settle()
    finally:
        s.stop()
    [line] = lines(caplog, "store_call_slow")
    name = line["name"]
    assert name == ("Very.longlocals." + "x" * 250)[:96] and len(name) == 96
    assert [f.episode_id for f in facts] == [name + ":" + store_call_diag._utc_day()]
    assert len(facts[0].episode_id) <= 128


@pytest.mark.asyncio
async def test_naming_failure_at_admission_runs_call_as_unnamed(store, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="chat_streamd_v2.store")

    def broken(fn):
        raise RuntimeError("naming broke")
    monkeypatch.setattr(store_call_diag, "call_name", broken)
    assert await store.submit(slow_client_op) == "client"
    with pytest.raises(LookupError, match="kept"):
        await store.submit(slow_raising_op)
    await finish_prior_store_calls(store)
    assert [line["name"] for line in lines(caplog, "store_call_slow")] == ["unnamed", "unnamed"]


@pytest.mark.asyncio
async def test_unusable_name_is_unnamed_and_lines_carry_no_payload(store, caplog):
    caplog.set_level(logging.INFO, logger="chat_streamd_v2.store")
    secret = "PRIVATE_PAYLOAD_7f3a"
    assert await store.submit(functools.partial(slow_payload_op, payload=secret)) == secret
    await finish_prior_store_calls(store)
    [line] = lines(caplog, "store_call_slow")
    assert line["name"] == "unnamed"
    assert re.fullmatch(r"[A-Za-z0-9._:-]{1,96}", line["caller"])
    assert secret not in caplog.text and "partial" not in caplog.text


def test_call_name_is_static_and_reduced():
    class Callable:
        def __call__(self, conn):
            return None
    assert store_call_diag.call_name(lambda conn: None).endswith("test_call_name_is_static_and_reduced.locals.lambda")
    assert store_call_diag.call_name(Callable()) == "unnamed"
    assert store_call_diag.call_name(Callable().__call__) == (
        "test_call_name_is_static_and_reduced.locals.Callable.__call__")


def test_hourly_line_group_totals(caplog):
    caplog.set_level(logging.INFO, logger="chat_streamd_v2.store")
    diag = store_call_diag.StoreCallDiag()
    diag.record("_WorkLanesStoreMixin.get_work_lane.locals.read", False, None, 0, 0, 0.125)
    diag.record("_WorkLanesStoreMixin.work_lane_snapshot.locals.read", False, None, 1, 1, 1.25)
    diag.record("AdvisorSessions.tick.locals.read", False, None, 2, 2, 2.5)
    for name in (
        "Store.consent_expire.locals._op", "Store.consent_push_due.locals._op",
        "QueuedSendNow._tick.locals.pending", "WorkLaneEpisodesStoreMixin.reconcile_work_lane_episodes.locals.evaluate",
        "Store.resolve_closed_awaiters.locals._op", "ErrorAlerts.advisory_snapshot",
        "ErrorAlerts.notice_rows.locals.read", "Store.fetch_session_event_page.locals._op", "unrelated",
    ):
        diag.record(name, False, None, 3, 3, 3.125)
    diag.record("unrelated", False, None, 3601, 3601, 3601)
    [line] = lines(caplog, "store_call_top")
    assert line["groups"] == {
        "lane_reads": {"calls": 2, "execution_ms": 375.0},
        "rulings_loop": {"calls": 0, "execution_ms": 0.0},
        "advisor_checks": {"calls": 1, "execution_ms": 500.0},
        "alert_notice_reads": {"calls": 1, "execution_ms": 125.0},
        "alert_snapshot": {"calls": 1, "execution_ms": 125.0},
        "consent_timers": {"calls": 2, "execution_ms": 250.0},
        "queued_send_now": {"calls": 1, "execution_ms": 125.0},
        "lane_episodes": {"calls": 1, "execution_ms": 125.0},
        "closed_awaiters": {"calls": 1, "execution_ms": 125.0},
        "history_pages": {"calls": 1, "execution_ms": 125.0},
        "proof_reads": {"calls": 0, "execution_ms": 0.0},
    }
    assert line["feature_total"] == {"calls": 10, "execution_ms": 1750.0}
    assert line["all_calls"] == {"calls": 12, "execution_ms": 2000.0}


@pytest.mark.asyncio
async def test_snapshot_and_other_notice_reads_are_named_separately(store, caplog, monkeypatch):
    from error_alerts import ErrorAlerts

    caplog.set_level(logging.INFO, logger="chat_streamd_v2.store")
    monkeypatch.setattr(store_call_diag, "SLOW_LINE_S", -1)
    alerts = ErrorAlerts.__new__(ErrorAlerts)
    alerts.store = store

    async def empty_facts():
        return []

    alerts._error_rows = empty_facts
    await alerts._notice_snapshot()
    await alerts.notice_rows(notice_ids=())
    await finish_prior_store_calls(store)
    names = [line["name"] for line in lines(caplog, "store_call_slow")]
    assert names.count("ErrorAlerts.advisory_snapshot") == 1
    assert names.count("ErrorAlerts.notice_rows.locals.read") == 1


@pytest.mark.parametrize("worker", ["writer", "advisory", "history"])
def test_store_hourly_bucket_excludes_post_boundary_completion(caplog, worker):
    caplog.set_level(logging.INFO, logger="chat_streamd_v2.store")
    label = "store_call_top" if worker == "writer" else "store_read_top"
    options = {} if worker == "writer" else {"worker": worker}
    diag = store_call_diag.StoreCallDiag(summary_label=label, flush_send=False, **options)
    diag.record("old", True, None, 999.0, 999.5, 1000.0)
    assert not lines(caplog, label)
    diag.record("new", True, None, 4598.0, 4599.0, 4600.25)
    [old] = lines(caplog, label)
    assert [op["name"] for op in old["top"]] == ["old"]
    assert old["worker"] == worker
    diag.record("next", True, None, 8200.0, 8200.0, 8200.0)
    new = lines(caplog, label)[1]
    assert [op["name"] for op in new["top"]] == ["new"]
    assert [line["worker"] for line in lines(caplog, "store_call_slow")] == [worker, worker]


def test_hourly_line_lists_top_five_by_total_execution(caplog):
    caplog.set_level(logging.INFO, logger="chat_streamd_v2.store")
    diag = store_call_diag.StoreCallDiag()
    now = 1000.0
    diag.record("warmup", False, None, now, now, now)
    mix = {"a": (3, 0.01), "b": (1, 0.2), "c": (40, 0.001), "d": (2, 0.05), "e": (5, 0.03), "f": (1, 0.002)}
    for name, (calls, each) in mix.items():
        for _ in range(calls):
            now += 1
            diag.record(name, False, None, now, now, now + each)
    assert not lines(caplog, "store_call_top")
    diag.record("fast", False, None, 1000.0 + 3600, 1000.0 + 3600, 1000.0 + 3600)
    [top] = lines(caplog, "store_call_top")
    assert [(op["name"], op["calls"]) for op in top["top"]] == [("b", 1), ("e", 5), ("d", 2), ("c", 40), ("a", 3)]
    assert [op["execution_ms"] for op in top["top"]] == sorted((op["execution_ms"] for op in top["top"]), reverse=True)
    assert top["window_s"] == 3600.0 and top["instrumentation_failures"] == 0
    # Totals reset each hour.
    diag.record("g", False, None, 1000.0 + 7200, 1000.0 + 7200, 1000.0 + 7200.5)
    assert [op["name"] for op in lines(caplog, "store_call_top")[1]["top"]] == ["fast"]


@pytest.mark.asyncio
async def test_ten_slow_calls_of_one_operation_schedule_one_alert_per_day():
    day = ["2026-11-11"]
    diag = store_call_diag.StoreCallDiag(today=lambda: day[0])
    facts = []

    async def sink(fact):
        facts.append(fact)
    diag.alert = sink
    loop = asyncio.get_running_loop()
    for _ in range(10):
        diag.record("Store.op_one", False, loop, 0.0, 0.0, 3.0)
    diag.record("Store.op_two", False, loop, 0.0, 0.0, 3.0)
    diag.record("Store.op_three", False, loop, 0.0, 0.0, 1.0)  # slow line, below the alert bound
    await settle()
    assert [(f.family, f.code, f.episode_id) for f in facts] == [
        ("daemon_runtime.v1", "store_call_slow", "Store.op_one:2026-11-11"),
        ("daemon_runtime.v1", "store_call_slow", "Store.op_two:2026-11-11")]
    day[0] = "2026-11-12"
    diag.record("Store.op_one", False, loop, 0.0, 0.0, 3.0)
    await settle()
    assert facts[-1].episode_id == "Store.op_one:2026-11-12" and len(facts) == 3


@pytest.mark.asyncio
async def test_alert_reaches_error_alerts_emit_through_alerts_error():
    emitted = []

    class Sink:
        async def emit(self, fact, *, principal=None):
            emitted.append((fact, principal))
            return "notification"
    alerts = Alerts()
    alerts.sink = Sink()
    diag = store_call_diag.StoreCallDiag(today=lambda: "2026-11-11")
    diag.alert = alerts.error
    diag.record("Store.op", False, asyncio.get_running_loop(), 0.0, 0.5, 3.0)
    await settle()
    assert emitted == [(ErrorFact("daemon_runtime.v1", "store_call_slow", "Store.op:2026-11-11"), None)]


@pytest.mark.asyncio
async def test_raising_alert_sink_never_changes_results_or_futures(tmp_path, monkeypatch):
    monkeypatch.setattr(store_call_diag, "SLOW_LINE_S", 0.05)
    monkeypatch.setattr(store_call_diag, "ALERT_S", 0.05)
    calls = []

    async def sink(fact):
        calls.append(fact)
        raise RuntimeError("sink down")
    s = Store(str(tmp_path / "hot.db")); s.start()
    s.call_diag.alert = sink
    try:
        assert await asyncio.wait_for(s.submit(slow_client_op), 5) == "client"
        with pytest.raises(LookupError, match="kept"):
            await asyncio.wait_for(s.submit(slow_raising_op), 5)
        await finish_prior_store_calls(s)
        await settle()
        assert len(calls) == 2 and s.call_diag.failures == 2
    finally:
        s.stop()


@pytest.mark.asyncio
async def test_instrumentation_failure_is_counted_and_dropped(store, monkeypatch):
    class BrokenJson:
        @staticmethod
        def dumps(*args, **kwargs):
            raise RuntimeError("line broke")
    monkeypatch.setattr(store_call_diag, "json", BrokenJson)
    assert await asyncio.wait_for(store.submit(slow_client_op), 5) == "client"
    with pytest.raises(LookupError, match="kept"):
        await asyncio.wait_for(store.submit(slow_raising_op), 5)
    await finish_prior_store_calls(store)
    assert store.call_diag.failures == 2


@pytest.mark.asyncio
async def test_alert_path_store_calls_are_logged_but_never_alert(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="chat_streamd_v2.store")
    monkeypatch.setattr(store_call_diag, "SLOW_LINE_S", 0.05)
    monkeypatch.setattr(store_call_diag, "ALERT_S", 0.05)
    s = Store(str(tmp_path / "hot.db")); s.start()
    facts, done = [], asyncio.Event()

    async def sink(fact):
        facts.append(fact)
        await s.submit(slow_job_op)  # the alert path's own slow store call
        done.set()
    s.call_diag.alert = sink
    try:
        assert await s.submit(slow_client_op) == "client"
        await asyncio.wait_for(done.wait(), 5)
        await settle()
    finally:
        s.stop()
    assert [f.episode_id.split(":")[0] for f in facts] == ["slow_client_op"]
    assert [line["name"] for line in lines(caplog, "store_call_slow")] == ["slow_client_op", "slow_job_op"]


class _CallerMixin:
    async def slow_via_mixin(self):
        return await self.slow_direct()


class _CallerStore(_CallerMixin, Store):
    async def slow_direct(self):
        return await self.submit(slow_client_op)


@pytest.fixture
def caller_store(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(store_call_diag, "SLOW_LINE_S", 0.05)
    caplog.set_level(logging.INFO, logger="chat_streamd_v2.store")
    s = _CallerStore(str(tmp_path / "caller.db"))
    s.start()
    try:
        yield s
    finally:
        s.stop()


async def _logged_slow_calls(store, caplog):
    await finish_prior_store_calls(store)
    return lines(caplog, "store_call_slow")


@pytest.mark.asyncio
async def test_caller_direct_await(caller_store, caplog):
    assert await caller_store.slow_direct() == "client"
    [line] = await _logged_slow_calls(caller_store, caplog)
    assert line["caller"] == f"{__name__}.test_caller_direct_await"


@pytest.mark.asyncio
async def test_caller_skips_store_and_mixin(caller_store, caplog):
    assert await caller_store.slow_via_mixin() == "client"
    [line] = await _logged_slow_calls(caller_store, caplog)
    assert line["caller"] == f"{__name__}.test_caller_skips_store_and_mixin"


@pytest.mark.asyncio
async def test_caller_wait_for(caller_store, caplog):
    assert await asyncio.wait_for(caller_store.slow_direct(), 5) == "client"
    [line] = await _logged_slow_calls(caller_store, caplog)
    assert line["caller"] == f"{__name__}.test_caller_wait_for"


def test_caller_bare_create_task(caller_store, caplog):
    # The pytest-asyncio runner is itself an outside frame. Enter asyncio
    # directly from a C thread bootstrap to exercise a truly bare task.
    completed = Future()

    async def exercise():
        try:
            result = await asyncio.create_task(caller_store.slow_direct())
            slow = await _logged_slow_calls(caller_store, caplog)
        except BaseException as exc:
            completed.set_exception(exc)
        else:
            completed.set_result((result, slow))

    _thread.start_new_thread(asyncio.run, (exercise(),))
    result, [line] = completed.result(timeout=5)
    assert result == "client"
    assert line["caller"] == "unknown"


@pytest.mark.asyncio
async def test_caller_capture_failure_preserves_result_and_exception(caller_store, caplog, monkeypatch):
    def broken(classes):
        raise RuntimeError("capture broke")

    monkeypatch.setattr(store_call_diag, "capture_caller", broken)
    assert await caller_store.slow_direct() == "client"
    with pytest.raises(LookupError, match="kept"):
        await caller_store.submit(slow_raising_op)
    slow = await _logged_slow_calls(caller_store, caplog)
    assert [line["caller"] for line in slow] == ["unknown", "unknown"]


@pytest.mark.asyncio
async def test_caller_sanitizes_and_truncates(caller_store, caplog):
    async def café_abcdefghijklmnopqrstuvwxyz_abcdefghijklmnopqrstuvwxyz_abcdefghijklmnopqrstuvwxyz():
        return await caller_store.slow_direct()

    assert await café_abcdefghijklmnopqrstuvwxyz_abcdefghijklmnopqrstuvwxyz_abcdefghijklmnopqrstuvwxyz() == "client"
    [line] = await _logged_slow_calls(caller_store, caplog)
    expected = f"{__name__}.test_caller_sanitizes_and_truncates.locals.caf_" + "_".join(
        ["abcdefghijklmnopqrstuvwxyz"] * 3
    )
    assert line["caller"] == expected[:96]
    assert len(line["caller"]) == 96


def test_caller_does_not_read_frame_thirteen(monkeypatch):
    reads = []

    class Frame:
        f_code = SimpleNamespace(co_qualname="Store.submit")
        f_globals = {"__name__": "store"}

        @property
        def f_back(self):
            reads.append(None)
            return self

    monkeypatch.setattr(store_call_diag, "sys", SimpleNamespace(_getframe=lambda depth: Frame()))
    assert store_call_diag.capture_caller(frozenset({"Store"})) == "unknown"
    assert len(reads) == 11


def test_caller_does_not_swallow_base_exception(monkeypatch):
    def interrupt(depth):
        raise KeyboardInterrupt()

    monkeypatch.setattr(store_call_diag, "sys", SimpleNamespace(_getframe=interrupt))
    with pytest.raises(KeyboardInterrupt):
        store_call_diag.capture_caller(frozenset({"Store"}))
