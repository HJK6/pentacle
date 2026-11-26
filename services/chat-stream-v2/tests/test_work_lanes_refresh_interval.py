"""Minimum interval between work-lane drain passes; latest state wins; a trailing pass always runs."""
import asyncio
import json
import math
import time
from types import SimpleNamespace

import pytest

import store_work_lanes
from _shared.specs_service import SpecsSubsystem
from store import Store
from work_lanes_projection import WorkLanesInventory
from test_assistant_prose_mirror import ASSISTANT
from test_work_lanes import run

NO_SESSIONS = SimpleNamespace(list_open=lambda: [])
# Generous stuck detector; interval waits are observed as events, never timed.
STUCK_S = 30


async def _burst(inv, seconds, gap):
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        inv.refresh()
        await asyncio.sleep(gap)
    await inv._task


def _count_passes(store):
    passes = []
    real = store.reconcile_work_lane_episodes

    async def counted(*args, **kwargs):
        passes.append(time.monotonic())
        return await real(*args, **kwargs)
    store.reconcile_work_lane_episodes = counted
    return passes


def test_burst_route_evaluations_bounded_by_interval(monkeypatch):
    """Measured shape: 17 lanes, 928 resolved routes of ~3 KB each, one inventory change every 20 ms."""
    monkeypatch.setenv("WORK_LANES_REFRESH_MIN_S", "1")
    evaluations = []
    real = store_work_lanes.lead_reported_done_conn
    monkeypatch.setattr(store_work_lanes, "lead_reported_done_conn",
                        lambda conn, lane: (evaluations.append(1), real(conn, lane))[1])

    async def body(env):
        lane_ids = [(await env.adopt(f"stream:l{i}"))["lane"]["lane_id"] for i in range(17)]
        pad = "x" * 2835

        def seed(conn):
            for i in range(928):
                conn.execute(
                    "INSERT INTO v2_assistant_composite_routes(route_id,stream_id,input_identity,payload_digest,"
                    "input_request_id,body,routing_state,delivery_state,dispatch_id,route_json,created_at,updated_at)"
                    " VALUES (?,?,?,?,?,?,'resolved','landed',?,?,?,?)",
                    (f"r{i}", ASSISTANT, f"in{i}", "d", f"req{i}", "b", f"disp{i}",
                     json.dumps({"lane_id": lane_ids[i % 17], "pad": pad}),
                     "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z"))
            conn.commit()
        await env.store.submit(seed)

        async def broadcast(frame):
            pass
        inv = WorkLanesInventory(env.store, NO_SESSIONS, broadcast)
        passes = _count_passes(env.store)
        evaluations.clear()
        await _burst(inv, 2.0, 0.02)
        # 2 s at one pass per second: the leading pass, at most two spaced passes, one trailing pass.
        assert 2 <= len(passes) <= 4, passes
        assert len(evaluations) <= 4 * 2 * 17, len(evaluations)
    run(body)


def _gated_interval(monkeypatch):
    """Module-local clock and sleep: each projection wait is recorded and held until the test releases it."""
    import work_lanes_projection as projection
    real_asyncio, real_time = asyncio, time
    gate = SimpleNamespace(clock=[0.0], waits=asyncio.Queue(), cancelled=[])

    async def interval_sleep(delay):
        released = real_asyncio.get_running_loop().create_future()
        gate.waits.put_nowait((delay, released))
        try:
            await released
        except real_asyncio.CancelledError:
            gate.cancelled.append(delay)
            raise
        gate.clock[0] += delay

    class AsyncProxy:
        sleep = staticmethod(interval_sleep)
        def __getattr__(self, name):
            return getattr(real_asyncio, name)

    class TimeProxy:
        def monotonic(self):
            return gate.clock[0]
        def __getattr__(self, name):
            return getattr(real_time, name)

    monkeypatch.setattr(projection, "time", TimeProxy())
    monkeypatch.setattr(projection, "asyncio", AsyncProxy())
    return gate


async def _settle(gate, task, interval):
    """Return the drain's held interval wait, or None once the drain finishes without one."""
    while True:
        getter = asyncio.ensure_future(gate.waits.get())
        done, _ = await asyncio.wait({getter, task}, timeout=STUCK_S, return_when=asyncio.FIRST_COMPLETED)
        if getter not in done:
            getter.cancel()
            assert task in done, "drain neither finished nor entered its interval wait"
            task.result()  # a drain that raised fails here
            return None
        delay, released = getter.result()
        if delay == interval:
            return released


def test_trailing_pass_publishes_latest_state_only(monkeypatch):
    monkeypatch.setenv("WORK_LANES_REFRESH_MIN_S", "0.3")
    gate = _gated_interval(monkeypatch)

    async def body(env):
        lane_id = (await env.adopt("stream:t", state="paused", lead=False, owner="operator"))["lane"]["lane_id"]

        async def retitle(title):
            await env.store.submit(lambda c: (c.execute(
                "UPDATE v2_assistant_composite_lanes SET title=? WHERE lane_id=?", (title, lane_id)), c.commit()))
        frames = []

        async def broadcast(frame):
            frames.append(frame)
        inv = WorkLanesInventory(env.store, NO_SESSIONS, broadcast)
        inv.refresh()
        assert await _settle(gate, inv._task, 0.3) is None
        assert [f["lanes"][0]["title"] for f in frames] == ["Lane stream:t"]
        await retitle("second")
        inv.refresh()
        # The drain is held in its interval wait until both writes have landed.
        released = await _settle(gate, inv._task, 0.3)
        assert released is not None
        await retitle("third")
        inv.refresh()
        assert [f["lanes"][0]["title"] for f in frames] == ["Lane stream:t"]
        released.set_result(None)
        await asyncio.wait_for(inv._task, STUCK_S)
        # "second" was superseded inside the interval; the trailing pass published the latest state.
        assert [f["lanes"][0]["title"] for f in frames] == ["Lane stream:t", "third"]
    run(body)


def test_first_pass_is_immediate_and_next_waits_for_interval(monkeypatch):
    monkeypatch.setenv("WORK_LANES_REFRESH_MIN_S", "0.5")
    gate = _gated_interval(monkeypatch)

    async def body(env):
        async def broadcast(frame):
            pass
        inv = WorkLanesInventory(env.store, NO_SESSIONS, broadcast)
        passes = _count_passes(env.store)
        inv.refresh()
        assert await _settle(gate, inv._task, 0.5) is None
        assert len(passes) == 1
        first_pass = inv._last_pass
        inv.refresh()
        released = await _settle(gate, inv._task, 0.5)
        assert released is not None and len(passes) == 1
        released.set_result(None)
        await asyncio.wait_for(inv._task, STUCK_S)
        assert len(passes) == 2 and inv._last_pass - first_pass >= 0.5
    run(body)


def test_stop_cancels_a_pending_wait(monkeypatch):
    monkeypatch.setenv("WORK_LANES_REFRESH_MIN_S", "30")
    gate = _gated_interval(monkeypatch)

    async def body(env):
        async def broadcast(frame):
            pass
        inv = WorkLanesInventory(env.store, NO_SESSIONS, broadcast)
        passes = _count_passes(env.store)
        inv.start()
        periodic = inv._periodic
        assert await _settle(gate, inv._task, 30) is None
        assert len(passes) == 1
        inv.refresh()
        released = await _settle(gate, inv._task, 30)
        assert released is not None
        await asyncio.wait_for(inv.stop(), STUCK_S)
        # The drain's own held interval wait was cancelled; the periodic task
        # (in its 60 s sweep wait or shielding the drain) was cancelled too.
        assert released.cancelled() and gate.cancelled.count(30) == 1
        assert set(gate.cancelled) <= {30, 60.0}
        assert periodic.cancelled()
        assert len(passes) == 1
    run(body)


def test_core_ready_start_is_not_delayed_by_interval(tmp_path, monkeypatch):
    """Composes with the deferred boot scan: pre-ready refreshes stay idle, start() scans at once."""
    from test_work_index_persistence import write_item
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(tmp_path))
    write_item(tmp_path)
    monkeypatch.setenv("WORK_LANES_REFRESH_MIN_S", "30")
    gate = _gated_interval(monkeypatch)

    async def go():
        store = Store(":memory:")
        store.start()
        specs = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None, debounce_s=0)
        scans = []
        original = specs.scan_work_observations
        specs.scan_work_observations = lambda: (scans.append(1), original())[1]

        async def broadcast(frame):
            pass
        inv = WorkLanesInventory(store, NO_SESSIONS, broadcast, specs=specs)
        try:
            for _ in range(5):
                inv.refresh()
            await asyncio.sleep(0.05)
            assert not scans
            inv.start()
            # The core-ready drain scans without entering the 30 s interval wait.
            assert await _settle(gate, inv._task, 30) is None
            assert len(scans) == 1
            assert (await store.work_index_status())["available"] is True
        finally:
            await inv.stop()
            store.stop()
    asyncio.run(go())


def test_interval_configuration(monkeypatch):
    monkeypatch.delenv("WORK_LANES_REFRESH_MIN_S", raising=False)
    assert WorkLanesInventory(None, NO_SESSIONS, None).refresh_min_s == 2.0
    assert WorkLanesInventory(None, NO_SESSIONS, None, refresh_min_s=0).refresh_min_s == 0.0
    monkeypatch.setenv("WORK_LANES_REFRESH_MIN_S", "0.75")
    assert WorkLanesInventory(None, NO_SESSIONS, None).refresh_min_s == 0.75
    for value in ("nan", "inf", "-1"):
        monkeypatch.setenv("WORK_LANES_REFRESH_MIN_S", value)
        with pytest.raises(ValueError, match="work_lanes_refresh_interval_invalid"):
            WorkLanesInventory(None, NO_SESSIONS, None)
    for value in (math.nan, math.inf, -0.5):
        with pytest.raises(ValueError, match="work_lanes_refresh_interval_invalid"):
            WorkLanesInventory(None, NO_SESSIONS, None, refresh_min_s=value)


def test_zero_interval_keeps_back_to_back_passes(monkeypatch):
    monkeypatch.setenv("WORK_LANES_REFRESH_MIN_S", "0")

    async def body(env):
        async def broadcast(frame):
            pass
        inv = WorkLanesInventory(env.store, NO_SESSIONS, broadcast)
        passes = _count_passes(env.store)
        inv.refresh()
        await inv._task
        inv.refresh()
        await inv._task
        assert len(passes) == 2
    run(body)


def test_work_index_sweep_defaults_and_override(monkeypatch):
    monkeypatch.delenv("WORK_INDEX_SWEEP_S", raising=False)
    monkeypatch.delenv("WORK_INDEX_SETTLE_S", raising=False)
    inv = WorkLanesInventory(None, NO_SESSIONS, None)
    assert inv.sweep_interval_s == 60
    assert inv.settle_s == 300
    monkeypatch.setenv("WORK_INDEX_SWEEP_S", "123")
    assert WorkLanesInventory(None, NO_SESSIONS, None).sweep_interval_s == 123
    assert WorkLanesInventory(None, NO_SESSIONS, None, sweep_interval_s=7).sweep_interval_s == 7


def test_periodic_sweep_observes_edit_without_skipping_next_tick(tmp_path, monkeypatch):
    """A nonzero scan duration must not turn a 60-second interval into 120."""
    from test_work_index_persistence import write_item
    import work_lanes_projection as projection
    spec_path = write_item(tmp_path) / "spec.md"
    spec_id = "spec_demo__span"
    clock = [0.0]
    deadlines, observations = [], []
    ticks = asyncio.Queue()
    sleeping = asyncio.Event()
    real_asyncio = asyncio

    async def periodic_sleep(delay):
        deadlines.append(clock[0] + delay)
        sleeping.set()
        await ticks.get()

    class AsyncProxy:
        sleep = staticmethod(periodic_sleep)
        def __getattr__(self, name):
            return getattr(real_asyncio, name)

    monkeypatch.setattr(projection, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(projection, "asyncio", AsyncProxy())
    monkeypatch.delenv("WORK_INDEX_SWEEP_S", raising=False)
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(tmp_path))
    specs = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None, debounce_s=0)
    original = specs.scan_work_observations

    def scan():
        result = original()
        clock[0] += 0.2  # Representative real-tree warm sweep duration.
        return result

    specs.scan_work_observations = scan
    specs.start = lambda: None  # Production polling mode without watcher callbacks.

    async def reconcile(result, **kwargs):
        observations.append(result["candidates"][spec_id][0]["facts"]["ac_checked"])

    async def noop(*args, **kwargs):
        pass

    async def go():
        store = SimpleNamespace(reconcile_work_observations=reconcile, reconcile_work_lane_episodes=noop)
        inv = WorkLanesInventory(store, NO_SESSIONS, noop, specs=specs)
        inv.emit_if_changed = noop
        try:
            inv.start()
            await inv._task
            await real_asyncio.wait_for(sleeping.wait(), 2)
            before = observations[-1]
            replacement = spec_path.with_suffix(".tmp")
            replacement.write_text(spec_path.read_text().replace("[ ]", "[x]"))
            replacement.replace(spec_path)
            clock[0] = deadlines[0]
            ticks.put_nowait(None)
            await real_asyncio.sleep(0.01)
            await inv._task
            assert observations[-1] == before + 1
            assert clock[0] <= 61
        finally:
            await inv.stop()
    asyncio.run(go())
