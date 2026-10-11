"""Boot freshness and lifecycle controls use the existing sole Store writer."""
import asyncio
import json
import threading
from types import SimpleNamespace

from _shared.specs_service import SpecsSubsystem
from store import Store
from work_lanes_projection import WorkLanesInventory


def test_preready_refresh_coalesces_without_starting_scan(tmp_path, monkeypatch):
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path))
    async def run():
        store = Store(':memory:'); store.start()
        specs = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None, debounce_s=0)
        calls = []
        original = specs.scan_work_observations
        specs.scan_work_observations = lambda: (calls.append(1), original())[1]
        async def broadcast(frame):
            pass
        inv = WorkLanesInventory(store, SimpleNamespace(list_open=lambda: []), broadcast, specs=specs)
        try:
            for _ in range(20):
                inv.refresh()
            await asyncio.sleep(.05)
            assert not calls, 'an adoption callback bypassed core readiness'
            inv.start()
            await inv._task
            assert len(calls) == 1
        finally:
            await inv.stop(); store.stop()
    asyncio.run(run())


def test_pending_marker_preserves_records_timestamps_and_history(tmp_path, monkeypatch):
    from test_work_index_persistence import write_item
    from test_work_lanes import run
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path))
    write_item(tmp_path, True)
    async def body(env):
        lane = (await env.adopt(state='paused', lead=False, owner='operator', members=['spec_demo__span']))['lane']
        specs = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None, debounce_s=0)
        await env.store.reconcile_work_observations(specs.scan_work_observations(), settle_s=0, sweep=True)
        before = await env.store.get_work_lane(lane['lane_id'])
        meta = await env.store.work_index_status()
        await env.store.mark_work_index_pending()
        after = await env.store.get_work_lane(lane['lane_id'])
        pending = await env.store.work_index_status()
        assert before['members'] == after['members'] and before['events'] == after['events']
        assert before['lane'] == after['lane']
        assert pending == dict(meta, available=False, error='work_index_unavailable')
        async def broadcast(frame):
            pass
        inv = WorkLanesInventory(env.store, SimpleNamespace(list_open=lambda: []), broadcast)
        for frame in [await inv.current(), await inv.list()]:
            assert frame['work_index']['available'] is False
            assert frame['lanes'][0]['completion_pending'] is None
        await env.store.reconcile_work_observations(specs.scan_work_observations(), settle_s=0, sweep=True)
        fresh = await env.store.get_work_lane(lane['lane_id'])
        assert fresh['members'][0]['obs_rev'] == before['members'][0]['obs_rev']
        assert fresh['events'] == before['events']
    run(body)


def test_late_scan_cannot_apply_to_replaced_store(tmp_path, monkeypatch):
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path))
    async def run():
        store = Store(':memory:'); store.start()
        replacement = Store(':memory:'); replacement.start()
        entered = threading.Event(); release = threading.Event()
        specs = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None, debounce_s=0)
        original = specs.scan_work_observations
        def held():
            entered.set(); release.wait(3); return original()
        specs.scan_work_observations = held
        async def broadcast(frame):
            pass
        inv = WorkLanesInventory(store, SimpleNamespace(list_open=lambda: []), broadcast, specs=specs)
        try:
            task = asyncio.create_task(inv.reconcile_index())
            assert await asyncio.to_thread(entered.wait, 2)
            inv.store = replacement
            release.set(); await task
            assert (await replacement.work_index_status())['snapshot_at'] is None
            assert (await store.work_index_status())['snapshot_at'] is None
        finally:
            release.set(); await inv.stop(); store.stop(); replacement.stop()
    asyncio.run(run())


def test_stop_while_scanning_discards_late_result(tmp_path, monkeypatch):
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path))
    async def run():
        store = Store(':memory:'); store.start()
        entered, release = threading.Event(), threading.Event()
        specs = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None, debounce_s=0)
        original = specs.scan_work_observations
        def held():
            entered.set(); release.wait(3); return original()
        specs.scan_work_observations = held
        specs.start = lambda: None
        async def broadcast(frame):
            pass
        inv = WorkLanesInventory(store, SimpleNamespace(list_open=lambda: []), broadcast, specs=specs)
        try:
            inv.start()
            assert await asyncio.to_thread(entered.wait, 2)
            await inv.stop()
            release.set(); await asyncio.sleep(.05)
            assert (await store.work_index_status())['snapshot_at'] is None
            inv.refresh(); await asyncio.sleep(.02)
            assert inv._task is None
        finally:
            release.set(); await inv.stop(); store.stop()
    asyncio.run(run())


def test_watcher_attach_off_loop_and_event_during_scan_is_reconciled(tmp_path, monkeypatch):
    from test_work_index_persistence import write_item
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path))
    folder = write_item(tmp_path)
    async def run():
        store = Store(':memory:'); store.start()
        entered, release, attaching, attach_release = (threading.Event() for _ in range(4))
        specs = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None, debounce_s=0)
        original = specs.scan_work_observations
        scans = []
        def held():
            result = original(); scans.append(result)
            if len(scans) == 1:
                entered.set(); release.wait(3)
            return result
        def attach():
            attaching.set(); attach_release.wait(3)
        specs.scan_work_observations = held
        specs.start = attach
        async def broadcast(frame):
            pass
        inv = WorkLanesInventory(store, SimpleNamespace(list_open=lambda: []), broadcast, specs=specs, settle_s=0)
        try:
            inv.start()
            assert await asyncio.to_thread(attaching.wait, 1)
            assert await asyncio.to_thread(entered.wait, 1)
            # The loop and sole Store are usable while attachment and scan wait.
            assert (await store.work_index_status())['available'] is False
            spec = folder/'spec.md'; spec.write_text(spec.read_text().replace('[ ]','[x]'))
            specs.changed_callback(['spec_demo__span'])
            await asyncio.sleep(0)
            release.set(); attach_release.set(); await inv._task
            assert len(scans) == 2
            records = await store.submit(lambda conn: json.loads(conn.execute('SELECT record_json FROM v2_work_item_observations').fetchone()[0]))
            assert records['member']['ac_checked'] == 1, (records, scans)
        finally:
            release.set(); attach_release.set(); await inv.stop(); store.stop()
    asyncio.run(run())


def test_without_watcher_periodic_poll_recovers_failed_scan(tmp_path, monkeypatch):
    from test_work_index_persistence import write_item
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path))
    write_item(tmp_path)
    async def run():
        store = Store(':memory:'); store.start()
        specs = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None, debounce_s=0)
        specs.start = lambda: None  # Watcher-disabled polling is still complete.
        original = specs.scan_work_observations
        calls = []
        def fail_once():
            calls.append(1)
            if len(calls) == 1:
                raise OSError('synthetic first-scan failure')
            return original()
        specs.scan_work_observations = fail_once
        async def broadcast(frame):
            pass
        inv = WorkLanesInventory(store, SimpleNamespace(list_open=lambda: []), broadcast, specs=specs, sweep_interval_s=.1)
        try:
            inv.start(interval_s=.05)
            await inv._task
            assert len(calls) == 1 and not (await store.work_index_status())['available']
            async with asyncio.timeout(1):
                while not (await store.work_index_status())['available']:
                    await asyncio.sleep(.01)
            assert len(calls) == 2
        finally:
            await inv.stop(); store.stop()
    asyncio.run(run())
