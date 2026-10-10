"""Spawn binding and session spec attach/detach never walk the work tree on the
event loop or the Store thread, and resolve from one snapshot per operation.

Regression for a daemon stall: each spec-bound spawn and each spec attach or
detach walked the whole work tree, repeatedly, on the event loop.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from _shared.specs_service import SpecsSubsystem
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store
from uiverbs import UIVerbs


def _spec(root: Path, status: str, folder: str, declared: str | None) -> None:
    path = root / "work" / status / folder
    path.mkdir(parents=True)
    front = f"id: {declared}\n" if declared else ""
    (path / "spec.md").write_text(f"---\n{front}title: T\ntype: spec\nstatus: {status}\n---\n\nGoal.\n",
                                  encoding="utf-8")


def _service(root: Path) -> SpecsSubsystem:
    return SpecsSubsystem(memory_root=root, session_summaries=lambda: [],
                          changed_callback=lambda ids: None, debounce_s=0)


def test_snapshot_answers_equal_direct_calls(tmp_path) -> None:
    _spec(tmp_path, "in_progress", "pentacle__alpha", "spec_pentacle__alpha")
    _spec(tmp_path, "ready_for_dev", "pentacle-web__beta", "spec_pentacle_web__beta")
    _spec(tmp_path, "in_progress", "pentacle__dup_one", "spec_pentacle__dup")
    _spec(tmp_path, "completed", "pentacle__dup_two", "spec_pentacle__dup")
    _spec(tmp_path, "in_progress", "pentacle__undeclared", None)
    _spec(tmp_path, "unlisted_bucket", "pentacle__hidden", "spec_pentacle__hidden")
    service = _service(tmp_path)
    snapshot = service.spec_resolution_snapshot()
    for value in ["spec_pentacle__alpha", "pentacle__alpha", "pentacle-web__beta", "pentacle_web__beta",
                  "spec_pentacle_web__beta", "spec_pentacle__dup", "pentacle__undeclared",
                  "spec_pentacle__hidden", "pentacle__hidden", "spec_pentacle__missing", "", None]:
        assert snapshot.resolve_for_spawn(value) == service.resolve_for_spawn(value), value
        assert snapshot.canonical_spec_identity(value) == service.canonical_spec_identity(value), value
        assert snapshot.resolution_for(value) == service.resolution_for(value), value


def test_spawn_binding_and_attach_detach_walk_off_loop_once(tmp_path) -> None:
    attached = [f"pentacle__held_{i}" for i in range(3)]
    for folder in attached + ["pentacle__wanted", "pentacle__extra"]:
        _spec(tmp_path, "in_progress", folder, f"spec_{folder}")
    for i in range(4000):
        _spec(tmp_path, ("in_progress", "ready_for_dev", "completed")[i % 3],
              f"pentacle__filler_{i:04d}", f"spec_pentacle__filler_{i:04d}")
    service = _service(tmp_path)

    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, local_host="alpha")
            await sessions.open("alpha", "lead", role="lead", spec_id=f"spec_{attached[0]}",
                                spec_ids=[f"spec_{s}" for s in attached])
            ctl = SpawnCtl(store, sessions, tmux=object(), specs=service)
            verbs = UIVerbs(store, sessions, ctl, specs=service)
            walks: list[tuple[str, bool, bool]] = []
            real = service._scan_folders_by_id
            label = ["none"]

            def scan(*a, **k):
                current = threading.current_thread()
                walks.append((label[0], current is threading.main_thread(), current is store._thread))
                return real(*a, **k)
            service._scan_folders_by_id = scan

            label[0] = "spawn"
            binding = await ctl._resolve_spec_binding(
                {"spec_ids": ["pentacle__wanted", "spec_pentacle__extra"]}, "alpha", "child")
            assert binding["spec_ids"] == ["spec_pentacle__wanted", "spec_pentacle__extra"]
            label[0] = "attach"
            attach = await verbs.session_spec_update({"action": "attach", "host": "alpha",
                                                      "session_name": "lead", "spec_id": "pentacle__wanted"})
            assert "spec_pentacle__wanted" in attach["session"]["spec_ids"]
            label[0] = "detach"
            detach = await verbs.session_spec_update({"action": "detach", "host": "alpha",
                                                      "session_name": "lead", "spec_id": "spec_pentacle__wanted"})
            assert "spec_pentacle__wanted" not in detach["session"]["spec_ids"]

            blocked = [w for w in walks if w[1] or w[2]]
            assert not blocked, blocked
            for op in ("spawn", "attach", "detach"):
                # One catalog walk plus one fresh tree walk per operation.
                assert 1 <= sum(1 for w in walks if w[0] == op) <= 2, walks
        finally:
            store.stop()

    asyncio.run(run())
