"""Named Store call timing: slow lines, hourly top list and the daily slow alert."""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import time

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
    slow = lines(caplog, "store_call_slow")
    assert [line["name"] for line in slow] == ["slow_client_op", "slow_actor_op", "slow_job_op"]
    for line in slow:
        assert set(line) == {"name", "queue_ms", "execution_ms"}
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
        await settle()
    finally:
        s.stop()
    name = lines(caplog, "store_call_slow")[0]["name"]
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
    assert [line["name"] for line in lines(caplog, "store_call_slow")] == ["unnamed", "unnamed"]


@pytest.mark.asyncio
async def test_unusable_name_is_unnamed_and_lines_carry_no_payload(store, caplog):
    caplog.set_level(logging.INFO, logger="chat_streamd_v2.store")
    secret = "PRIVATE_PAYLOAD_7f3a"
    assert await store.submit(functools.partial(slow_payload_op, payload=secret)) == secret
    assert [line["name"] for line in lines(caplog, "store_call_slow")] == ["unnamed"]
    assert secret not in caplog.text and "partial" not in caplog.text


def test_call_name_is_static_and_reduced():
    class Callable:
        def __call__(self, conn):
            return None
    assert store_call_diag.call_name(lambda conn: None).endswith("test_call_name_is_static_and_reduced.locals.lambda")
    assert store_call_diag.call_name(Callable()) == "unnamed"
    assert store_call_diag.call_name(Callable().__call__) == (
        "test_call_name_is_static_and_reduced.locals.Callable.__call__")


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
    assert [op["name"] for op in lines(caplog, "store_call_top")[1]["top"]] == ["g"]


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
        await settle()
        assert len(calls) == 2 and s.call_diag.failures == 2
        assert await asyncio.wait_for(s.submit(fast_op), 5) == "fast"
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
