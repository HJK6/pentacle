"""Truthful prompt-delivery receipt on the failure path. A prompt-bearing spawn
that fails before staging (the live-pane
collision) must record a durable failed receipt — never a missing receipt, never
`not_requested`. An empty-string prompt stays `not_requested`.

The failure handler must attach a durable receipt for a pre-brief collision,
while an empty-string prompt remains `not_requested`.
"""

from __future__ import annotations

import asyncio

import pytest

import spawnctl as spawnctl_mod  # noqa: E402
from sessions import Sessions, VerbError  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from store import Store  # noqa: E402

HOST = "localhost"


class CollidingTmux:
    """A live pane already holds every name: the spawn hits the `has_session`
    collision (`stream_id_unavailable`) that is raised BEFORE `_prepare_brief`."""

    async def has_session(self, _name: str) -> bool:
        return True

    async def new_session(self, name: str, _command: str, cwd=None, env=None) -> None:  # pragma: no cover
        raise AssertionError("collision must be raised before any pane is created")

    async def capture(self, _name: str) -> str:  # pragma: no cover
        return "READY"

    async def kill_session(self, _name: str) -> None:  # pragma: no cover
        return None

    async def session_state(self, _name: str) -> str:  # pragma: no cover
        return "alive"


def _run(msg: dict):
    async def go():
        tmux = CollidingTmux()
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            err = None
            try:
                await ctl.spawn(msg, HOST)
            except VerbError as exc:
                err = exc
            outcome = await store.get_spawn_outcome(HOST, msg["session_name"])
            row = sessions.get(f"{HOST}:{msg['session_name']}")
            return err, outcome, row
        finally:
            store.stop()

    return asyncio.run(go())


def test_prompt_bearing_pre_brief_failure_records_failed_receipt() -> None:
    err, outcome, row = _run(
        {"objective": "Exercise the existing spawn contract", "command": "stub", "session_name": "pre", "request_id": "rr", "prompt": "do the thing"}
    )
    assert isinstance(err, VerbError) and err.code == "stream_id_unavailable"
    assert outcome is not None and outcome["state"] == "failed"
    receipt = outcome.get("delivery_receipt")
    # The prompt WAS requested, so the durable outcome must carry a truthful
    # failed receipt — not a missing one, and not `not_requested`.
    assert isinstance(receipt, dict), "prompt-bearing failure must persist a receipt"
    assert receipt.get("state") == "failed"
    assert receipt.get("state") != "not_requested"
    # No briefless OPEN row is ever created (row opens only on confirmed delivery).
    assert row is None


def test_empty_prompt_stays_not_requested() -> None:
    err, outcome, _row = _run(
        {"objective": "Exercise the existing spawn contract", "command": "stub", "session_name": "pre", "request_id": "rr", "prompt": ""}
    )
    assert isinstance(err, VerbError) and err.code == "stream_id_unavailable"
    assert outcome is not None and outcome["state"] == "failed"
    # An empty prompt is not a request: the failed outcome carries no prompt
    # receipt (nothing to be truthful ABOUT), and never claims delivery.
    receipt = outcome.get("delivery_receipt")
    assert receipt is None or receipt.get("state") in (None, "not_requested")
