"""Existing Store persistence: durable last-good facts, root loss, and atomic events."""
import asyncio
import json

import pytest

from store import Store
from test_work_lanes import run
from _shared.specs_service import SpecsSubsystem


def write_item(root, checked=False):
    folder = root / "work" / "completed" / "demo__span"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "spec.md").write_text(
        f"---\nid: spec_demo__span\ntitle: Span\n---\n## Acceptance Criteria\n- [{'x' if checked else ' '}] Inspect.\n")
    (folder / "summary.md").write_text("**Status** — Ready for inspection.\n")
    return folder


def test_restart_absent_root_and_restoration(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    folder = write_item(root, True)
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(root))
    path = str(tmp_path / "fixture.db")
    lane_id = None
    async def seed(env):
        nonlocal lane_id
        lane_id = (await env.adopt(state="paused", lead=False, owner="operator", members=["spec_demo__span"]))["lane"]["lane_id"]
        catalog = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None, debounce_s=0)
        await env.store.reconcile_work_observations(catalog.scan_work_observations(), settle_s=0, sweep=True)
    run(seed, path)
    root.rename(tmp_path / "offline-memory")
    async def restart():
        store = Store(path)
        store.start()
        try:
            catalog = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None, debounce_s=0)
            await store.reconcile_work_observations(catalog.scan_work_observations(), settle_s=0, sweep=True)
            meta = await store.work_index_status()
            assert meta["available"] is False and meta["snapshot_at"] is not None
            shown = await store.get_work_lane(lane_id)
            assert shown["members"][0]["ac_checked"] == 1 and shown["members"][0]["terminal"] == "completed"
            assert shown["members"][0]["observation"]["quality"] == "stale"
            (tmp_path / "offline-memory").rename(root)
            await store.reconcile_work_observations(catalog.scan_work_observations(), settle_s=0, sweep=True)
            assert (await store.work_index_status())["available"] is True
            shown = await store.get_work_lane(lane_id)
            assert shown["members"][0]["obs_rev"] == 1 and shown["members"][0]["observation"]["quality"] == "fresh"
            assert not [e for e in shown["events"] if e["operation"] == "item_change"]
        finally:
            store.stop()
    asyncio.run(restart())


def test_snapshot_and_history_rollback_atomically(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    write_item(root)
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(root))
    async def body(env):
        lid = (await env.adopt(state="paused", lead=False, owner="operator", members=["spec_demo__span"]))["lane"]["lane_id"]
        catalog = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None, debounce_s=0)
        await env.store.reconcile_work_observations(catalog.scan_work_observations(), settle_s=0, sweep=True)
        write_item(root, True)
        def fail():
            raise RuntimeError("synthetic transaction fault")
        env.store._work_index_fault = fail
        with pytest.raises(RuntimeError, match="synthetic transaction fault"):
            await env.store.reconcile_work_observations(catalog.scan_work_observations(), settle_s=0, sweep=True)
        shown = await env.store.get_work_lane(lid)
        assert shown["members"][0]["obs_rev"] == 1 and shown["members"][0]["ac_checked"] == 0
        assert not [e for e in shown["events"] if e["operation"] == "item_change"]
        env.store._work_index_fault = None
        await env.store.reconcile_work_observations(catalog.scan_work_observations(), settle_s=0, sweep=True)
        shown = await env.store.get_work_lane(lid)
        assert shown["members"][0]["obs_rev"] == 2
        assert len([e for e in shown["events"] if e["operation"] == "item_change"]) == 1
    run(body)


@pytest.mark.parametrize("copy_first", [False, True])
def test_move_orders_settle_duplicates_and_missing_without_done(tmp_path, monkeypatch, copy_first):
    import shutil
    import time
    root = tmp_path / "memory"
    folder = write_item(root, True)
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(root))
    async def body(env):
        lid = (await env.adopt(state="paused", lead=False, owner="operator", members=["spec_demo__span"]))["lane"]["lane_id"]
        catalog = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None, debounce_s=0)
        now = time.time() + 1
        async def scan(at):
            await env.store.reconcile_work_observations(catalog.scan_work_observations(now=at), settle_s=10, sweep=True)
            return (await env.store.get_work_lane(lid))["members"][0]
        assert (await scan(now))["terminal"] == "completed"
        target = root / "work" / "in_progress" / "different-folder"
        target.parent.mkdir(parents=True)
        if copy_first:
            shutil.copytree(folder, target)
        else:
            folder.rename(tmp_path / "in-transit")
        stale = await scan(now + 1)
        assert stale["observation"]["quality"] == "stale" and stale["terminal"] == "completed"
        settled = await scan(now + 12)
        assert settled["status"] == ("ambiguous" if copy_first else "missing")
        assert settled["terminal"] is None and settled["obs_rev"] == 1
        if copy_first:
            (folder / "spec.md").unlink()
            (folder / "summary.md").unlink()
            folder.rmdir()
        else:
            (tmp_path / "in-transit").rename(target)
        restored = await scan(now + 13)
        assert restored["status"] == "in_progress" and restored["observation"]["quality"] == "fresh"
        assert restored["obs_rev"] == 2
        spec = target / "spec.md"
        original = spec.read_text()
        spec.write_text("---\nid: [broken\n---\n")
        error = await scan(now + 14)
        assert error["status"] == "in_progress" and error["observation"]["quality"] == "error"
        assert error["obs_rev"] == 2 and error["observation"]["observed_at"] == restored["observation"]["observed_at"]
        spec.write_text(original)
        assert (await scan(now + 15))["obs_rev"] == 2
        events = (await env.store.get_work_lane(lid))["events"]
        assert len([e for e in events if e["operation"] == "item_change"]) == 1
    run(body)


def test_derived_and_lead_loss_writes_do_not_refresh_work_activity(tmp_path, monkeypatch):
    from work_lane_progress import lane_progress
    root = tmp_path / "memory"
    write_item(root, True)
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(root))
    async def body(env):
        from test_work_lanes import FD
        lead = await env.seat("activity-lead", role="lead", parent_stream_id=FD)
        lid = (await env.adopt(lead=lead, members=["spec_demo__span"]))["lane"]["lane_id"]
        catalog = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None, debounce_s=0)
        await env.store.reconcile_work_observations(catalog.scan_work_observations(), settle_s=0, sweep=True)
        before = lane_progress((await env.store.work_lane_rows())[0])["freshness_at"]
        await env.store.mark_closed("host-a", "activity-lead", closed_at="2026-01-01T00:00:00Z", pane_status="pane_dead")
        await env.store.reconcile_work_lanes()
        await env.store.reconcile_work_observations(catalog.scan_work_observations(), settle_s=0, sweep=True)
        row = (await env.store.work_lane_rows())[0]
        assert row["work_state"] == "paused"
        assert lane_progress(row)["freshness_at"] == before
    run(body)
