"""Contract coverage for `v2_spawn_outcomes` coverage and semantics.

Defect 1 (COVERAGE): a spawn whose request is interrupted by a bare
`BaseException` (e.g. `asyncio.CancelledError`) AFTER admission skips the
`except Exception` outcome write entirely -- the session row exists but
`v2_spawn_outcomes` has no row for it at all, unboundedly. Reproduced here by
monkeypatching the boot-readiness poll to raise `CancelledError` after
`Sessions.open()` has already run.

Defect 2 (SEMANTICS): `state='failed'` with a `boot_not_ready` reason means
the readiness poll timed out, not that the seat failed to boot -- admission
already happened. A consumer must be able to tell this apart from a genuine
boot/registration failure without parsing `reason` prose.
"""

from __future__ import annotations

import tmux_transport

import asyncio
import sqlite3

import pytest

import spawnctl as spawnctl_mod  # noqa: E402
from sessions import Sessions  # noqa: E402
from sessions import VerbError  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from store import Store  # noqa: E402

HOST = "localhost"


class BootingTmux:
    """A live pane that never renders a ready marker (boot never confirms)."""

    def __init__(self) -> None:
        self.alive = False
        self.pastes: list[str] = []

    async def new_session(
        self, _name: str, _command: str, cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.alive = True

    async def has_session(self, _name: str) -> bool:
        return self.alive

    async def session_state(self, _name: str) -> str:
        return "alive" if self.alive else "gone"

    async def capture(self, _name: str) -> str:
        return "still booting, no marker yet"

    async def pane_pid(self, _name: str) -> str:
        return "4242"

    async def kill_session(self, _name: str) -> None:
        self.alive = False

    async def paste(self, _name: str, text: str) -> None:
        self.pastes.append(text)

    async def send_enter(self, _name: str) -> None:
        return None

    async def run(self, *args: str, **_kwargs: object) -> tuple[int, str]:
        return 0, ""


def test_admitted_cancelled_spawn_gets_an_outcome_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """An admitted session with a cancelled spawn request
    has ZERO `v2_spawn_outcomes` rows -- invisible to any spawn-forensics
    audit still receives an outcome row."""

    async def cancelled_marker_wait(*_a: object, **_k: object) -> bool:
        # Simulate the RPC caller's request being cancelled while this daemon
        # is mid-poll for boot readiness -- AFTER `Sessions.open()` already
        # admitted the session (see `_spawn_fenced`: admission precedes the
        # readiness poll).
        raise asyncio.CancelledError()

    monkeypatch.setattr(spawnctl_mod.SpawnCtl, "_await_marker", cancelled_marker_wait)

    async def run() -> tuple[dict | None, dict | None]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = BootingTmux()
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            with pytest.raises(asyncio.CancelledError):
                await ctl.spawn(
                    {"objective": "Exercise the existing spawn contract", "command": "provider", "session_name": "cancelled-mid-boot",
                     "request_id": "r-cancelled"},
                    HOST,
                )
            row = await store.fetch_session(HOST, "cancelled-mid-boot")
            outcome = await store.get_spawn_outcome(HOST, "cancelled-mid-boot")
            return row, outcome
        finally:
            store.stop()

    row, outcome = asyncio.run(run())

    # The session was admitted (this is what makes the missing row a coverage
    # gap rather than an unremarkable no-op).
    assert row is not None and row["status"] == "open"
    # This is the AC1 assertion: an admitted session must not have a
    # An admitted session must have an outcome row.
    assert outcome is not None, (
        "admitted session 'cancelled-mid-boot' has NO v2_spawn_outcomes row "
        "-- invisible to spawn-forensics auditing (Defect 1)"
    )
    assert outcome["state"] not in ("delivered", "failed")
    assert "cancelled" in (outcome["reason"] or "")


def test_admitted_sessions_gain_outcome_after_interrupted_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: an interrupted admitted spawn still has an outcome row."""

    # Keep the proof direct: inspect the fixture database rather than adding a
    # special-purpose Store surface.
    assert not hasattr(Store, "admitted_sessions_missing_outcome")

    async def cancelled_marker_wait(*_a: object, **_k: object) -> bool:
        raise asyncio.CancelledError()

    monkeypatch.setattr(spawnctl_mod.SpawnCtl, "_await_marker", cancelled_marker_wait)

    async def run() -> list[str]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = BootingTmux()
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            with pytest.raises(asyncio.CancelledError):
                await ctl.spawn(
                    {"objective": "Exercise the existing spawn contract", "command": "provider", "session_name": "probe-target",
                     "request_id": "r-probe"},
                    HOST,
                )

            def _missing(conn: sqlite3.Connection) -> list[str]:
                return [
                    str(row[0])
                    for row in conn.execute(
                        "SELECT s.session_name FROM sessions AS s "
                        "LEFT JOIN v2_spawn_outcomes AS o "
                        "ON o.host=s.host AND o.session_name=s.session_name "
                        "WHERE s.host=? AND o.session_name IS NULL",
                        (HOST,),
                    ).fetchall()
                ]

            return await store.submit(_missing)
        finally:
            store.stop()

    missing = asyncio.run(run())
    assert missing == []


def test_readiness_timeout_is_typed_not_a_kill_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal (non-cancelled) `boot_not_ready` timeout must set the
    typed `readiness_timed_out` flag so a consumer never has to string-match
    `reason` to tell a readiness timeout apart from a real boot failure."""
    monkeypatch.setattr(spawnctl_mod, "BOOT_READY_HARD_DEADLINE_S", 0.01)
    monkeypatch.setattr(tmux_transport, "POLL_INTERVAL_S", 0.001)

    async def run() -> dict | None:
        store = Store(":memory:")
        store.start()
        try:
            tmux = BootingTmux()  # never renders the marker -> timeout
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            with pytest.raises(VerbError) as excinfo:
                await ctl.spawn(
                    {"objective": "Exercise the existing spawn contract", "command": "provider", "session_name": "never-ready",
                     "request_id": "r-never-ready"},
                    HOST,
                )
            assert excinfo.value.code == "boot_not_ready"
            return await store.get_spawn_outcome(HOST, "never-ready")
        finally:
            store.stop()

    outcome = asyncio.run(run())
    assert outcome is not None and outcome["state"] == "failed"
    assert outcome["readiness_timed_out"] == 1
