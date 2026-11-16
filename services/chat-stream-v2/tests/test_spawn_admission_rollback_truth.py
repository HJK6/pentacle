"""Spawn-admission rollback truth — store-level contract tests.

These tests ensure that rollback cannot rewrite a completed session and that
the durable record keeps rollback distinct from an ordinary session close:

  AC1 — a stale/duplicate admission retry landing on an ALREADY-closed row is
        refused by the CAS AND made audible (WARN), not silently swallowed.
  AC5 — a spawn-admission rollback close is typed `spawn_rollback`, queryably
        distinct from a normal `session_close` on the session row.
"""

from __future__ import annotations

import asyncio
import logging

from store import Store  # noqa: E402

HOST = "hosta"


def _run(coro):
    return asyncio.run(coro)


# ----------------------------- AC1 -----------------------------------------

def test_ac1_stale_close_against_already_closed_row_is_audible(caplog) -> None:
    """A stale spawn-admission retry re-closing an already-closed row is refused
    (row byte-unchanged) AND emits one WARN naming the already-closed drop.

    A stale close is a no-op, but it must remain observable in the warning log
    so it is distinguishable from a benign generation-mismatch no-op."""
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            name = "v2-ac1"
            await store.open_session(HOST, name, session_generation="G1", pane_status="pane_alive")
            # Legitimate live close (a completed session).
            await store.mark_closed(
                HOST, name, closed_at="2026-08-19T09:30:00Z", pane_status="pane_dead",
                expected_generation="G1", close_kind="session_close",
                reason="self_close_on_completion",
            )
            before = await store.fetch_session(HOST, name)

            with caplog.at_level(logging.WARNING, logger="chat_streamd_v2.store"):
                # The stale spawn-admission retry: same generation, boot_not_ready.
                ret = await store.mark_closed(
                    HOST, name, closed_at="2026-08-19T09:45:00Z", pane_status="pane_dead",
                    expected_generation="G1", close_kind="spawn_rollback",
                    reason="boot_not_ready: TUI not ready",
                )
            after = await store.fetch_session(HOST, name)

            assert ret is None, "stale close of an already-closed row must be a no-op"
            # The legitimate close is NOT falsified.
            assert after["closed_at"] == before["closed_at"] == "2026-08-19T09:30:00Z"
            assert after["close_kind"] == "session_close", "close_kind must not be overwritten"
            audible = [
                r for r in caplog.records
                if "already closed" in r.getMessage() and name in r.getMessage()
            ]
            assert audible, "the dropped stale close must emit an audible WARN"
            assert "spawn_rollback" in audible[0].getMessage(), "WARN names the dropped kind"
        finally:
            store.stop()

    _run(go())


# ----------------------------- AC5 -----------------------------------------

def test_ac5_rollback_close_is_typed_distinctly(caplog) -> None:
    """A spawn-admission rollback close is typed `spawn_rollback`, queryably
    distinct from a live `session_close` on the live session row.

    The durable schema preserves the rollback origin, so a rollback close is
    distinguishable from a normal session death."""
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            # A live session that completes and closes normally.
            await store.open_session(HOST, "v2-live", session_generation="L1", pane_status="pane_alive")
            await store.mark_closed(
                HOST, "v2-live", closed_at="2026-08-19T11:00:00Z", pane_status="pane_dead",
                expected_generation="L1", close_kind="session_close",
            )
            # A failed-boot spawn rolled back by spawn-admission.
            await store.open_session(HOST, "v2-roll", session_generation="R1", pane_status="pane_alive")
            await store.mark_closed(
                HOST, "v2-roll", closed_at="2026-08-19T11:00:05Z", pane_status="pane_dead",
                expected_generation="R1", close_kind="spawn_rollback",
            )

            live = await store.fetch_session(HOST, "v2-live")
            roll = await store.fetch_session(HOST, "v2-roll")
            assert live["close_kind"] == "session_close"
            assert roll["close_kind"] == "spawn_rollback"
            assert live["close_kind"] != roll["close_kind"], "the two populations must partition"

            # A reopened row starts a fresh live generation with no close class.
            await store.open_session(HOST, "v2-roll", session_generation="R2", pane_status="pane_alive")
            reopened = await store.fetch_session(HOST, "v2-roll")
            assert reopened["close_kind"] is None, "a reopened (open) row must carry no close class"
        finally:
            store.stop()

    _run(go())
