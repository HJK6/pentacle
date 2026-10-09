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
