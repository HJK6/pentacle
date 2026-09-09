"""Reconciler self-close backlog sweep — closes finished hidden seats the
report-time self-close never reaped.

Contract: spec-self-close. Two predicates,
both requiring a terminal report of the row's current generation:
  A. flag set (retry for a report-time close that failed/raced);
  B. parent closed/absent (nobody can send more work; the flag is moot).
"""

from __future__ import annotations

import asyncio

from ledger import Ledger
from reconciler import ReconcileConfig, SessionReconciler
from sessions import Sessions
from store import Store


class _NoHosts:
    """Minimal hosts: presence is disabled, so only local_host is read."""

    local_host = "hosta"
    peers: dict = {}


def _install_idle_capture(sessions: Sessions, working_streams: object = ()) -> None:
    """Stub the close-time capture probe (no tmux in tests): every seat reads
    `idle` unless named in `working_streams`, which read `working=True`. The
    sweep's `defer_if_working` gate closes an idle seat and defers a working one,
    so the harness must supply the same overlay the real probe would stamp."""
    working = {str(s) for s in working_streams}

    async def _probe(host: str, name: str, row: object) -> str:
        sid = f"{host}:{name}"
        overlay = {"capture_liveness": "idle", "working": sid in working, "working_label": ""}
        if isinstance(row, dict):
            row.update(overlay)
        sessions.apply_live(sid, **overlay)
        return "idle"

    sessions._probe_capture_liveness = _probe  # type: ignore[method-assign]


def _build(
    store: Store, *, working_streams: object = ()
) -> tuple[Sessions, SessionReconciler]:
    sessions = Sessions(store, tmux=None, local_host="hosta")
    _install_idle_capture(sessions, working_streams)
    reconciler = SessionReconciler(
        sessions, _NoHosts(), config=ReconcileConfig(interval_s=60, threshold_checks=2)
    )
    return sessions, reconciler


async def _seed_terminal_report(sessions: Sessions, store: Store, stream_id: str) -> None:
    """File a terminal report WITHOUT closing (ingest, not report), leaving the
    seat open — exactly the fall-through the sweep must reap."""
    await Ledger(store, sessions=sessions).ingest(
        {
            "from_stream_id": stream_id,
            "status": "done",
            "summary": "finished",
            "findings": [],
            "next_action": "done",
        }
    )


def test_sweep_closes_flagged_hidden_seat_with_terminal_report() -> None:
    """Predicate A: flag set + terminal report → closed even with a live parent."""

    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions, reconciler = _build(store)
            await store.open_session("hosta", "leader-a", visibility="hidden")
            await store.open_session(
                "hosta", "worker-a", parent_stream_id="hosta:leader-a",
                visibility="hidden", self_close_on_completion=True,
            )
            await sessions.refresh()
            await _seed_terminal_report(sessions, store, "hosta:worker-a")
            assert (await store.fetch_session("hosta", "worker-a"))["status"] == "open"

            counters = await reconciler.reconcile_once()

            assert counters["self_close_swept"] == 1
            assert (await store.fetch_session("hosta", "worker-a"))["status"] == "closed"
            # The live parent (no report) is untouched.
            assert (await store.fetch_session("hosta", "leader-a"))["status"] == "open"
        finally:
            store.stop()

    asyncio.run(go())


def test_sweep_closes_hidden_seat_with_report_when_parent_closed() -> None:
    """Predicate B: terminal report + parent closed → closed even with flag off."""

    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions, reconciler = _build(store)
            await store.open_session("hosta", "leader-b", visibility="hidden")
            await store.open_session(
                "hosta", "worker-b", parent_stream_id="hosta:leader-b",
                visibility="hidden", self_close_on_completion=False,
            )
            await sessions.refresh()
            await _seed_terminal_report(sessions, store, "hosta:worker-b")
            await sessions.close("hosta", "leader-b", "test")
            assert (await store.fetch_session("hosta", "leader-b"))["status"] == "closed"

            counters = await reconciler.reconcile_once()

            assert counters["self_close_swept"] == 1
            assert (await store.fetch_session("hosta", "worker-b"))["status"] == "closed"
        finally:
            store.stop()

    asyncio.run(go())


def test_sweep_closes_hidden_seat_with_report_when_parent_absent() -> None:
    """Predicate B, absent branch: parent row never existed → closed."""

    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions, reconciler = _build(store)
            await store.open_session(
                "hosta", "worker-orphan", parent_stream_id="hosta:ghost-leader",
                visibility="hidden", self_close_on_completion=False,
            )
            await sessions.refresh()
            await _seed_terminal_report(sessions, store, "hosta:worker-orphan")

            counters = await reconciler.reconcile_once()

            assert counters["self_close_swept"] == 1
            assert (await store.fetch_session("hosta", "worker-orphan"))["status"] == "closed"
        finally:
            store.stop()

    asyncio.run(go())


def test_sweep_spares_reported_seat_with_live_parent_and_no_flag() -> None:
    """Neither predicate: flag off + live parent → left open despite a report."""

    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions, reconciler = _build(store)
            await store.open_session("hosta", "leader-c", visibility="hidden")
            await store.open_session(
                "hosta", "worker-c", parent_stream_id="hosta:leader-c",
                visibility="hidden", self_close_on_completion=False,
            )
            await sessions.refresh()
            await _seed_terminal_report(sessions, store, "hosta:worker-c")

            counters = await reconciler.reconcile_once()

            assert counters["self_close_swept"] == 0
            assert (await store.fetch_session("hosta", "worker-c"))["status"] == "open"
        finally:
            store.stop()

    asyncio.run(go())


def test_sweep_spares_seat_without_terminal_report() -> None:
    """Both predicates require a report: parent closed but no report → left open."""

    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions, reconciler = _build(store)
            await store.open_session("hosta", "leader-d", visibility="hidden")
            await store.open_session(
                "hosta", "worker-d", parent_stream_id="hosta:leader-d",
                visibility="hidden", self_close_on_completion=True,
            )
            await sessions.refresh()
            await sessions.close("hosta", "leader-d", "test")

            counters = await reconciler.reconcile_once()

            assert counters["self_close_swept"] == 0
            assert (await store.fetch_session("hosta", "worker-d"))["status"] == "open"
        finally:
            store.stop()

    asyncio.run(go())


def test_sweep_ignores_stale_generation_report() -> None:
    """Fail closed: a report from a prior generation does not authorize a close."""

    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions, reconciler = _build(store)
            await store.open_session(
                "hosta", "worker-e", parent_stream_id="hosta:ghost",
                visibility="hidden", self_close_on_completion=True,
            )
            await sessions.refresh()
            await _seed_terminal_report(sessions, store, "hosta:worker-e")
            # Advance the seat to a new generation; the report stays on the old one.
            await store.submit(
                lambda conn: conn.execute(
                    "UPDATE v2_session_generations SET generation=? "
                    "WHERE host=? AND session_name=?",
                    ("stale-bumped-generation", "hosta", "worker-e"),
                )
            )

            counters = await reconciler.reconcile_once()

            assert counters["self_close_swept"] == 0
            assert (await store.fetch_session("hosta", "worker-e"))["status"] == "open"
        finally:
            store.stop()

    asyncio.run(go())


def test_sweep_revalidates_against_current_row_not_snapshot() -> None:
    """Contract case/2: a stale snapshot claiming flag/parent-gone must not
    authorize a close when the authoritative current row does not match."""

    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions, reconciler = _build(store)
            await store.open_session("hosta", "leader-g", visibility="hidden")  # live
            await store.open_session(
                "hosta", "worker-g", parent_stream_id="hosta:leader-g",
                visibility="hidden", self_close_on_completion=False,
            )
            await sessions.refresh()
            await _seed_terminal_report(sessions, store, "hosta:worker-g")
            # A snapshot that LIES (claims the flag) — but the current DB row has
            # flag off and a live parent, so neither predicate actually holds.
            stale = {
                "host": "hosta", "session_name": "worker-g", "visibility": "hidden",
                "self_close_on_completion": True,
            }
            counters = {"self_close_swept": 0, "closed": 0}
            await reconciler._sweep_self_close_backlog([stale], counters)

            assert counters["self_close_swept"] == 0
            assert (await store.fetch_session("hosta", "worker-g"))["status"] == "open"
        finally:
            store.stop()

    asyncio.run(go())


def test_sweep_spares_seat_that_became_visible() -> None:
    """Contract case: a hidden→visible transition before close must spare the seat;
    the sweep re-checks visibility on the authoritative current row."""

    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions, reconciler = _build(store)
            # Current DB row is VISIBLE; the snapshot lies that it is hidden.
            await store.open_session(
                "hosta", "worker-v", parent_stream_id="hosta:ghost",
                visibility="default", self_close_on_completion=True,
            )
            await sessions.refresh()
            await _seed_terminal_report(sessions, store, "hosta:worker-v")
            stale = {
                "host": "hosta", "session_name": "worker-v", "visibility": "hidden",
                "self_close_on_completion": True,
            }
            counters = {"self_close_swept": 0, "closed": 0}
            await reconciler._sweep_self_close_backlog([stale], counters)

            assert counters["self_close_swept"] == 0
            assert (await store.fetch_session("hosta", "worker-v"))["status"] == "open"
        finally:
            store.stop()

    asyncio.run(go())


def test_generation_fenced_close_refuses_mismatch() -> None:
    """Contract case: sessions.close(expected_generation=...) never closes a row
    whose current generation differs — the fence the sweep relies on."""

    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            await store.open_session("hosta", "worker-h", visibility="hidden")
            await sessions.refresh()

            result = await sessions.close(
                "hosta", "worker-h", "test", expected_generation="a-different-generation",
            )

            assert not result.get("failed")
            # The mismatched generation is refused as stale; the row stays open.
            assert (await store.fetch_session("hosta", "worker-h"))["status"] == "open"
        finally:
            store.stop()

    asyncio.run(go())


def test_sweep_spares_visible_seat() -> None:
    """The sweep is hidden-only: a visible seat with a report + closed parent stays."""

    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions, reconciler = _build(store)
            await store.open_session("hosta", "leader-f", visibility="hidden")
            await store.open_session(
                "hosta", "worker-f", parent_stream_id="hosta:leader-f",
                visibility="default", self_close_on_completion=True,
            )
            await sessions.refresh()
            await _seed_terminal_report(sessions, store, "hosta:worker-f")
            await sessions.close("hosta", "leader-f", "test")

            counters = await reconciler.reconcile_once()

            assert counters["self_close_swept"] == 0
            assert (await store.fetch_session("hosta", "worker-f"))["status"] == "open"
        finally:
            store.stop()

    asyncio.run(go())


def test_sweep_counts_only_real_closes() -> None:
    """Contract case: a close result that did NOT actually close (stale generation /
    already closed / visibility-fenced — `failed` is False but no `closed`
    flag) must not be counted as swept. Before the fix the sweep counted every
    non-`failed` result, overstating its counters and log line."""

    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions, reconciler = _build(store)
            # A genuine candidate: hidden, flagged, parent absent, with a
            # terminal report of the current generation — passes every prefilter
            # up to the close call.
            await store.open_session(
                "hosta", "worker-nc", parent_stream_id="hosta:ghost",
                visibility="hidden", self_close_on_completion=True,
            )
            await sessions.refresh()
            await _seed_terminal_report(sessions, store, "hosta:worker-nc")

            async def _non_closing(*_a: object, **_k: object) -> dict:
                # A refused/stale close: honest, not failed, but nothing closed.
                return {"failed": False, "already_closed": True, "stale_generation": True}

            sessions.close = _non_closing  # type: ignore[method-assign]
            snapshot = {
                "host": "hosta", "session_name": "worker-nc", "visibility": "hidden",
                "self_close_on_completion": True,
            }
            counters = {"self_close_swept": 0, "closed": 0}
            await reconciler._sweep_self_close_backlog([snapshot], counters)

            assert counters["self_close_swept"] == 0
            assert counters["closed"] == 0
        finally:
            store.stop()

    asyncio.run(go())


def test_real_close_result_marks_closed() -> None:
    """Contract behind the close result: a genuine close carries `closed: True`, which is
    the signal the sweep now counts on."""

    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            await store.open_session("hosta", "worker-r", visibility="hidden")
            await sessions.refresh()

            result = await sessions.close("hosta", "worker-r", "test")

            assert result.get("closed") is True
            assert (await store.fetch_session("hosta", "worker-r"))["status"] == "closed"
        finally:
            store.stop()

    asyncio.run(go())


def test_close_requires_hidden_refuses_now_visible_row() -> None:
    """Contract case: a hidden→visible flip between the sweep's pre-lock recheck and the
    close is refused. `requires_hidden` re-reads visibility on the authoritative
    row INSIDE the lifecycle lock, so `set_visibility` cannot outrun it — the
    now-visible seat is spared (never killed), carrying no `closed` flag."""

    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            await store.open_session("hosta", "worker-vf", visibility="hidden")
            await sessions.refresh()
            generation = Sessions._row_generation(
                await store.fetch_session("hosta", "worker-vf")
            )
            # The flip the fence must catch (generation is unchanged by it).
            await sessions.set_visibility("hosta", "worker-vf", "visible")

            result = await sessions.close(
                "hosta", "worker-vf", "self_close_backlog_sweep",
                expected_generation=generation, requires_hidden=True,
            )

            assert not result.get("closed")
            assert not result.get("failed")
            assert result.get("visibility_changed") is True
            assert (await store.fetch_session("hosta", "worker-vf"))["status"] == "open"
        finally:
            store.stop()

    asyncio.run(go())


async def _seed_terminal_rejection(store: Store, host: str, name: str) -> None:
    """Record a durable terminal-report REJECTION of the seat's CURRENT
    generation — the persisted residue of a schema-rejected report (exit 2).
    No valid `v2_reports` row is created; only the rejection persists."""
    generation = Sessions._row_generation(await store.fetch_session(host, name))
    await store.record_report_rejection(
        f"{host}:{name}", session_generation=generation, status="done",
        reason="schema rejected",
    )


def test_sweep_spares_flagged_seat_with_only_a_rejection() -> None:
    """A flagged hidden seat whose terminal report
    was schema-REJECTED (rejection row of this generation, NO valid report) must
    must not be reaped. A rejection is not a done report, while a later valid
    report still closes the seat."""

    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions, reconciler = _build(store)
            await store.open_session("hosta", "leader-rej", visibility="hidden")
            await store.open_session(
                "hosta", "worker-rej", parent_stream_id="hosta:leader-rej",
                visibility="hidden", self_close_on_completion=True,
            )
            await sessions.refresh()
            await _seed_terminal_rejection(store, "hosta", "worker-rej")

            counters = await reconciler.reconcile_once()

            # A rejection alone must not reap the seat.
            assert counters["self_close_swept"] == 0
            assert (await store.fetch_session("hosta", "worker-rej"))["status"] == "open"

            # No immortal seat: a subsequent VALID terminal report does close it.
            await _seed_terminal_report(sessions, store, "hosta:worker-rej")
            counters = await reconciler.reconcile_once()
            assert counters["self_close_swept"] == 1
            assert (await store.fetch_session("hosta", "worker-rej"))["status"] == "closed"
        finally:
            store.stop()

    asyncio.run(go())


def test_sweep_defers_working_seat_with_terminal_report() -> None:
    """A flagged seat with a valid terminal report but a pane still working
    must not be reaped until the pane becomes idle."""

    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions, reconciler = _build(store, working_streams={"hosta:worker-busy"})
            await store.open_session("hosta", "leader-busy", visibility="hidden")
            await store.open_session(
                "hosta", "worker-busy", parent_stream_id="hosta:leader-busy",
                visibility="hidden", self_close_on_completion=True,
            )
            await sessions.refresh()
            await _seed_terminal_report(sessions, store, "hosta:worker-busy")

            counters = await reconciler.reconcile_once()

            # Regression case: the busy pane is reaped mid-review.
            assert counters["self_close_swept"] == 0
            assert (await store.fetch_session("hosta", "worker-busy"))["status"] == "open"

            # Once idle, the same seat closes on its report (no immortal seat).
            _install_idle_capture(sessions, working_streams=())
            counters = await reconciler.reconcile_once()
            assert counters["self_close_swept"] == 1
            assert (await store.fetch_session("hosta", "worker-busy"))["status"] == "closed"
        finally:
            store.stop()

    asyncio.run(go())


if __name__ == "__main__":  # pragma: no cover
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
