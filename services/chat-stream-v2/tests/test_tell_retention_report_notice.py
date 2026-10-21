"""RED: tell retention must not strand an unsettled child-report-ready notice.

`Store.put_tell_delivery` prunes `v2_tell_deliveries` to TELL_RETENTION rows but
only protects tells referenced by child-exchange rows and by
notification_answer / error_alert outbound notices.  An unsettled report notice
whose tell row is pruned reads back as `tell_row_missing` (proof_unavailable,
no proof_state), which `OutboundNoticeQueue._retry` treats as proof-pending and
resets to max_attempts-1 forever.
"""

from __future__ import annotations

import asyncio

import pytest

import outbound_notices as notices_module
from comms import Comms
from ledger import child_report_ready_tell_id
from outbound_notices import (
    NOTICE_KIND_NOTIFICATION_ANSWER,
    NOTICE_KIND_REPORT,
    OutboundNoticeQueue,
    ensure_notice_marker,
    notice_needle,
)
from sessions import Sessions, VerbError
from store import TELL_RETENTION, Store
from submission_events import PROOF_TERMINAL_BOUND_S

from test_outbound_notices import (
    _CodexNoticeSpawn,
    _CodexNoticeTmux,
    _CrashAfterPasteSpawn,
    _CrashAfterPasteTmux,
    _DelayedUserProof,
    _PendingComms,
    _config,
    _stored_notice,
)

ROW_MISSING_REASON = "tell_row_missing: no delivery proof row (never persisted or pruned)"
ROW_MISSING_NEXT_ACTION = "inspect the recipient transcript for the notice marker; resend deliberately only if absent"


def test_tell_retention_does_not_strand_unsettled_report_notice() -> None:
    notice_id = child_report_ready_tell_id("report-retention-red")

    async def run() -> tuple[dict, dict | None, int]:
        tmux = _CodexNoticeTmux()
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=tmux, local_host="hosta")
            await sessions.open("hosta", "parent", provider="codex")
            comms = Comms(
                store, sessions, _CodexNoticeSpawn(tmux), submission_proof=_DelayedUserProof(),
            )
            queue = OutboundNoticeQueue(store, comms, config=_config())
            await queue.enqueue(
                kind=NOTICE_KIND_REPORT,
                dedupe_key="report:report-retention-red",
                recipient_stream_id="hosta:parent",
                tell_id=notice_id,
                body="child report ready",
                source_stream_id="hosta:child",
                metadata={"report_id": "report-retention-red"},
            )
            # First attempt pastes and records a proof-pending tell row.
            assert await queue.deliver_now(notice_id) is False
            assert await store.get_tell_delivery(notice_id) is not None

            # Newer unrelated tells push the unsettled notice's row out of the window.
            for i in range(TELL_RETENTION + 100):
                await store.put_tell_delivery(
                    f"newer-tell-{i}",
                    {"payload_digest": "d", "reply": {"delivery_status": "delivered"}, "delivery": None},
                )
            retained = await store.get_tell_delivery(notice_id)

            # Sweep well past the retry bound (max_attempts=3).
            for _ in range(8):
                await queue.deliver_now(notice_id)
            return await _stored_notice(store, notice_id), retained, len(tmux.pastes)
        finally:
            store.stop()

    row, retained, pastes = asyncio.run(run())
    assert pastes == 1
    assert row is not None
    # Either the tell row is protected from pruning, or the notice goes terminal.
    assert retained is not None or row["terminal_at"], (
        f"stranded: tell row pruned and notice never terminal "
        f"(attempts={row['attempts']} last_error={row['last_error']!r})"
    )


async def _flood_tells(store: Store, count: int) -> None:
    for i in range(count):
        await store.put_tell_delivery(
            f"flood-{i}",
            {"payload_digest": "d", "reply": {"delivery_status": "delivered"}, "delivery": None},
        )


async def _put_tell(store: Store, tell_id: str) -> None:
    await store.put_tell_delivery(
        tell_id,
        {"payload_digest": "d", "reply": {"delivery_status": "committed_pending_proof"}, "delivery": None},
    )


async def _enqueue_kind(queue: OutboundNoticeQueue, tell_id: str, kind: str, **extra: object) -> None:
    await queue.enqueue(
        kind=kind,
        dedupe_key=f"{kind}:{tell_id}",
        recipient_stream_id="hosta:parent",
        tell_id=tell_id,
        body="body",
        source_stream_id="hosta:child",
        **extra,
    )


def test_retention_keeps_tells_of_unsettled_notices_of_any_kind_and_prunes_settled() -> None:
    async def run() -> dict[str, bool]:
        store = Store(":memory:")
        store.start()
        try:
            queue = OutboundNoticeQueue(store, _PendingComms(), config=_config())
            kinds = {
                "t-unsettled-report": "report",
                "t-unsettled-reconciler": "reconciler",
                "t-delivered-report": "report",
                "t-terminal-report": "report",
                "t-delivered-answer": NOTICE_KIND_NOTIFICATION_ANSWER,
                "t-none": None,
            }
            for tell_id, kind in kinds.items():
                if kind is not None:
                    extra = {}
                    if kind == NOTICE_KIND_NOTIFICATION_ANSWER:
                        extra["metadata"] = {"producer_session_generation": "g"}
                    await _enqueue_kind(queue, tell_id, kind, **extra)
                await _put_tell(store, tell_id)
            await store.submit(lambda conn: (
                conn.execute("UPDATE v2_outbound_notices SET delivered_at='2026-10-21T00:00:00Z' "
                             "WHERE tell_id IN ('t-delivered-report','t-delivered-answer')"),
                conn.execute("UPDATE v2_outbound_notices SET terminal_at='2026-10-21T00:00:00Z' "
                             "WHERE tell_id='t-terminal-report'"),
                conn.commit(),
            ))
            await _flood_tells(store, TELL_RETENTION + 10)
            return {t: await store.get_tell_delivery(t) is not None for t in kinds}
        finally:
            store.stop()

    kept = asyncio.run(run())
    assert kept == {
        "t-unsettled-report": True,
        "t-unsettled-reconciler": True,
        # Settled notices stay prunable, except the pre-existing answer protection.
        "t-delivered-report": False,
        "t-terminal-report": False,
        "t-delivered-answer": True,
        "t-none": False,
    }


def _comms_without_row(provider: str, *, pane: str = "") -> tuple[Store, Comms, _CrashAfterPasteTmux, Sessions]:
    tmux = _CrashAfterPasteTmux()
    tmux.capture_text = pane
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, tmux=tmux, local_host="hosta")
    return store, Comms(store, sessions, _CrashAfterPasteSpawn(tmux), submission_proof=_DelayedUserProof()), tmux, sessions


def _notice_message(tell_id: str) -> dict:
    return {
        "_outbound_notice_kind": NOTICE_KIND_REPORT,
        "tell_id": tell_id, "stream_id": "hosta:parent", "to_stream_id": "hosta:parent",
        "message": "child report ready", "urgent": False,
    }


def test_missing_row_reply_carries_row_missing_state_without_input() -> None:
    async def run() -> tuple[dict, int]:
        store, comms, tmux, sessions = _comms_without_row("codex")
        try:
            await sessions.open("hosta", "parent", provider="codex", pane_status="pane_alive")
            reply = await comms.deliver_outbound_notice(_notice_message("gone-tell"), check_existing=True)
            return reply, len(tmux.pastes)
        finally:
            store.stop()

    reply, pastes = asyncio.run(run())
    assert pastes == 0
    assert reply["delivery_status"] == "proof_unavailable"
    assert reply["proof_state"] == "row_missing"
    assert reply["reason"] == "tell_row_missing"


def test_missing_row_with_pane_marker_is_recorded_delivered_not_row_missing() -> None:
    tell_id = "pane-proven-tell"

    async def run() -> tuple[dict, dict | None]:
        pane = ensure_notice_marker(tell_id, "child report ready")
        assert notice_needle(tell_id) in pane
        store, comms, tmux, sessions = _comms_without_row("shell", pane=pane)
        try:
            await sessions.open("hosta", "parent", provider="shell", pane_status="pane_alive")
            reply = await comms.deliver_outbound_notice(_notice_message(tell_id), check_existing=True)
            return reply, await store.get_tell_delivery(tell_id)
        finally:
            store.stop()

    reply, stored = asyncio.run(run())
    assert reply["delivery_status"] == "delivered"
    assert reply.get("proof_state") != "row_missing"
    assert stored is not None and stored["delivery"]["delivery_status"] == "delivered"


def test_missing_row_without_pane_marker_on_scanned_provider_is_row_missing() -> None:
    async def run() -> dict:
        store, comms, tmux, sessions = _comms_without_row("shell", pane="unrelated pane text")
        try:
            await sessions.open("hosta", "parent", provider="shell", pane_status="pane_alive")
            return await comms.deliver_outbound_notice(_notice_message("absent-tell"), check_existing=True)
        finally:
            store.stop()

    reply = asyncio.run(run())
    assert reply["proof_state"] == "row_missing"
    assert reply["delivery_status"] == "proof_unavailable"


@pytest.mark.parametrize(
    ("age_s", "terminal"),
    [(PROOF_TERMINAL_BOUND_S - 1.0, False), (PROOF_TERMINAL_BOUND_S + 1.0, True)],
)
def test_row_missing_terminalizes_for_open_recipient_only_after_age_floor(
    monkeypatch: pytest.MonkeyPatch, age_s: float, terminal: bool,
) -> None:
    async def run() -> dict:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            await sessions.open("hosta", "parent", provider="codex")
            now = 2_000_000_000.0
            monkeypatch.setattr(notices_module.time, "time", lambda: now)
            queue = OutboundNoticeQueue(store, _PendingComms("row_missing"), config=_config())
            await queue.enqueue(
                kind=NOTICE_KIND_REPORT,
                dedupe_key="report:row-missing",
                recipient_stream_id="hosta:parent",
                tell_id="row-missing",
                body="body",
                source_stream_id="hosta:child",
                created_at=notices_module._iso_from_epoch(now - age_s),
            )
            assert await queue.deliver_now("row-missing") is False
            row = await _stored_notice(store, "row-missing")
            assert row is not None
            return row
        finally:
            store.stop()

    row = asyncio.run(run())
    assert bool(row["terminal_at"]) is terminal
    if terminal:
        assert row["terminal_reason"] == ROW_MISSING_REASON
        assert row["next_action"] == ROW_MISSING_NEXT_ACTION
    else:
        assert row["last_error"] == "submission_proof_pending"


def test_never_persisted_attempt_goes_terminal_on_row_missing_without_paste(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attempt 1 fails before paste (no tell row); attempt 2 reads row_missing."""

    async def run() -> tuple[dict, dict, int]:
        tmux = _CodexNoticeTmux()
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=tmux, local_host="hosta")
            await sessions.open("hosta", "parent", provider="codex")
            comms = Comms(
                store, sessions, _CodexNoticeSpawn(tmux), submission_proof=_DelayedUserProof(),
            )
            real_tell = comms.tell
            calls = {"n": 0}

            async def flaky_tell(msg: dict) -> dict:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise VerbError("transport_unavailable", "pane busy before paste")
                return await real_tell(msg)

            comms.tell = flaky_tell  # type: ignore[method-assign]
            now = 2_000_000_000.0
            monkeypatch.setattr(notices_module.time, "time", lambda: now)
            queue = OutboundNoticeQueue(store, comms, config=_config())
            await queue.enqueue(
                kind=NOTICE_KIND_REPORT,
                dedupe_key="report:never-persisted",
                recipient_stream_id="hosta:parent",
                tell_id="never-persisted",
                body="body",
                source_stream_id="hosta:child",
                created_at=notices_module._iso_from_epoch(now - PROOF_TERMINAL_BOUND_S - 5.0),
            )
            assert await queue.deliver_now("never-persisted") is False
            first = await _stored_notice(store, "never-persisted")
            assert first is not None and first["attempts"] == 1 and not first["terminal_at"]
            assert await store.get_tell_delivery("never-persisted") is None
            assert await queue.deliver_now("never-persisted") is False
            second = await _stored_notice(store, "never-persisted")
            assert second is not None
            return first, second, len(tmux.pastes)
        finally:
            store.stop()

    first, second, pastes = asyncio.run(run())
    assert pastes == 0
    assert second["terminal_reason"] == ROW_MISSING_REASON
    assert second["next_action"] == ROW_MISSING_NEXT_ACTION
