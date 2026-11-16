"""Adoption deadline race: a successful awaited pane-liveness read that crosses
the adoption deadline must not turn a saved pre-paste watermark into a terminal
failure.

Real Store/Sessions/SpawnCtl after an owned SIGKILL at the pre-paste boundary
(the same harness as test_restart_input_residuals). Only the fake pane's
`has_session` is delayed, once, after the transcript probe has run, so the
deadline is first observed after the saved-watermark unknown guard evaluated
false.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_restart_input_residuals as residuals  # noqa: E402
from test_restart_input_residuals import BRIEF, HOST, KEY, NAME, FakePane  # noqa: E402

import spawnctl  # noqa: E402
from sessions import Sessions  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from store import Store  # noqa: E402

DEADLINE_S = 1.0


class DeadlineCrossingPane(FakePane):
    """Answers `has_session` truthfully, but the first call after the armed
    transcript probe returns only once the adoption deadline has passed."""

    def __init__(self, root):
        super().__init__(root)
        self.armed = False
        self.crossings = []

    async def has_session(self, name):
        alive = await super().has_session(name)
        if self.armed:
            self.armed = False
            started = time.monotonic()
            await asyncio.sleep(DEADLINE_S + 0.25)
            self.crossings.append({"alive": alive, "delayed_s": time.monotonic() - started})
        return alive


async def reconcile_across_deadline(root):
    store = Store(str(root / "sessions.db")); store.start()
    original_tree = spawnctl.process_tree
    original_deadline = spawnctl.ADOPTION_BOOT_DEADLINE_S
    try:
        pane = DeadlineCrossingPane(root)
        sessions = Sessions(store, tmux=pane, local_host=HOST)
        await sessions.refresh()
        ctl = SpawnCtl(store, sessions, tmux=pane, owner_instance_id="new-instance")

        async def process_tree(pid): return [pid]
        async def no_transcript(pids): return []
        spawnctl.process_tree = process_tree
        ctl._open_transcripts = no_transcript
        spawnctl.ADOPTION_BOOT_DEADLINE_S = DEADLINE_S

        probe = ctl._bounded_adoption_transcript_status
        statuses = []

        async def armed_probe(*args, **kwargs):
            status = await probe(*args, **kwargs)
            statuses.append(status)
            pane.armed = status == "no_transcript"
            return status
        ctl._bounded_adoption_transcript_status = armed_probe

        before = await store.reservations(include_expired=True)
        reconciled = await ctl.reconcile_spawn_intents()
        return {
            "before": before,
            "reconciled": reconciled,
            "statuses": statuses,
            "crossings": pane.crossings,
            "outcome": await store.get_spawn_outcome(HOST, NAME),
            "awaited": await ctl.await_spawn({"spawn_request_id": residuals.REQUEST}),
            "reservations_after": await store.reservations(include_expired=True),
            "pane_after": pane.state(),
        }
    finally:
        spawnctl.process_tree = original_tree
        spawnctl.ADOPTION_BOOT_DEADLINE_S = original_deadline
        store.stop()


@pytest.fixture(scope="module")
def crossed(tmp_path_factory):
    root = tmp_path_factory.mktemp("adoption-deadline-race")
    proc = subprocess.run(
        [sys.executable, residuals.__file__, "--child", str(root), "watermark"],
        capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == -signal.SIGKILL, (proc.returncode, proc.stderr)
    record = asyncio.run(reconcile_across_deadline(root))
    (root / "receipt-deadline-race.json").write_text(json.dumps(record, indent=2, default=str) + "\n")
    return record


def test_boundary_is_actually_crossed(crossed):
    payload = json.loads(crossed["before"][0]["payload"])
    assert payload["delivery_receipt"]["proof_watermark_state"] == "reachable"
    assert crossed["statuses"] and crossed["statuses"][-1] == "no_transcript"
    assert crossed["crossings"] and crossed["crossings"][0]["alive"] is True
    assert crossed["crossings"][0]["delayed_s"] >= DEADLINE_S
    assert crossed["pane_after"]["alive"] is True
    assert crossed["pane_after"]["pastes"] == []


def test_saved_watermark_stays_unknown_after_awaited_liveness_crosses_deadline(crossed):
    outcome = crossed["outcome"]
    assert outcome["state"] == "indeterminate", outcome
    assert outcome["reason"].startswith("spawn_delivery_unknown"), outcome
    assert crossed["reservations_after"], "retain the same operation for proof-only recovery"
    assert crossed["awaited"]["prompt_submission_status"] == "unknown"
    assert crossed["awaited"]["do_not_respawn"] is True
