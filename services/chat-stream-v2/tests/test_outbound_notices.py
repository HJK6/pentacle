"""Focused durability coverage for the single daemon outbound-notice queue."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from comms import Comms
from ledger import Ledger, child_report_ready_tell_id
from outbound_notices import (
    OutboundNoticeConfig,
    OutboundNoticeConflict,
    OutboundNoticeQueue,
    NOTICE_KIND_RECONCILER,
    NOTICE_KIND_REPORT,
    ensure_notice_marker,
)
from reconciler import SessionReconciler
from sessions import Sessions
from store import Store
from submission_events import EventProof, EventWatermark, PROOF_FAST_WAIT_S


def _config(**overrides: object) -> OutboundNoticeConfig:
    values = {
        "lease_s": 0.1,
        "max_attempts": 3,
    }
    values.update(overrides)
    return OutboundNoticeConfig(**values)


def test_from_env_keeps_only_load_bearing_runtime_controls(monkeypatch) -> None:
    prefix = "PENTACLE_" + "OUTBOUND_NOTICE_"
    for suffix, value in {
        "INTERVAL_S": "0.05",
        "MAX_PER_PASS": "1",
        "LEASE_S": "12.5",
        "BACKOFF_BASE_S": "0.01",
        "BACKOFF_MAX_S": "0.02",
        "MAX_ATTEMPTS": "4",
        "FIRST_DELAY_S": "9",
    }.items():
        monkeypatch.setenv(prefix + suffix, value)

    assert OutboundNoticeConfig.from_env() == OutboundNoticeConfig(
        lease_s=12.5,
        max_attempts=4,
    )


async def _enqueue(queue: OutboundNoticeQueue, *, notice_id: str = "notice-1", kind: str = "test") -> dict:
    return await queue.enqueue(
        kind=kind,
        dedupe_key=f"{kind}:{notice_id}",
        recipient_stream_id="hosta:parent",
        tell_id=notice_id,
        body="durable body",
        source_stream_id="hosta:child",
        metadata={"notice_id": notice_id},
    )


async def _stored_notice(store: Store, notice_id: str) -> dict | None:
    """Test-only inspection of the durable queue; it is not a runtime API."""
    return await store.submit(lambda conn: (
        dict(row) if (row := conn.execute(
            "SELECT * FROM v2_outbound_notices WHERE notice_id=?", (notice_id,),
        ).fetchone()) is not None else None
    ))


class _RecordingComms:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[tuple[dict, bool]] = []

    async def deliver_outbound_notice(self, message: dict, *, check_existing: bool = False) -> dict:
        self.calls.append((dict(message), check_existing))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary transport failure")
        return {
            "type": "tell.ok",
            "delivery_status": "delivered",
            "submission_confirmed": True,
            "delivery_ack_at": "2026-08-12T00:00:00Z",
        }


class _PendingComms:
    def __init__(self, proof_state: str = "pending") -> None:
        self.proof_state = proof_state

    async def deliver_outbound_notice(self, message: dict, *, check_existing: bool = False) -> dict:
        return {
            "type": "tell.ok",
            "tell_id": message["tell_id"],
            "to_stream_id": message["to_stream_id"],
            "delivery_status": "proof_pending",
            "submission_confirmed": False,
            "proof_state": self.proof_state,
        }


class _KeySequenceTmux:
    """Busy-pane double for the existing injection seam's key sequence."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.turn_interrupted = False

    async def capture(self, name: str) -> str:
        return "busy command remains active"

    async def paste(self, name: str, text: str) -> None:
        self.events.append("paste+Enter")

    async def run(self, *args: str, **kwargs: object) -> tuple[int, str]:
        if args[:1] == ("send-keys",):
            key = str(args[-1])
            self.events.append(key)
            if key == "Escape":
                self.turn_interrupted = True
        return 0, ""


class _KeySequenceSpawn:
    def __init__(self, tmux: _KeySequenceTmux) -> None:
        self.tmux = tmux

    async def _await_marker(self, *args: object, **kwargs: object) -> bool:
        return True


@pytest.mark.parametrize(
    ("kind", "expected", "interrupts_busy_turn"),
    [
        (NOTICE_KIND_REPORT, ["paste+Enter"], False),
        (NOTICE_KIND_RECONCILER, ["paste+Enter"], False),
        ("watch", ["paste+Enter"], False),
        ("wake", ["paste+Enter"], False),
        ("wake_urgent", ["Escape", "paste+Enter"], True),
    ],
    ids=["report-is-ordinary", "reconciler-is-passive", "watch-is-passive", "wake-is-passive", "urgent-wake"],
)
def test_notice_kind_selects_existing_key_sequence(
    kind: str, expected: list[str], interrupts_busy_turn: bool,
) -> None:
    async def run() -> tuple[list[str], bool]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _KeySequenceTmux()
            sessions = Sessions(store, tmux=tmux, local_host="hosta")
            await sessions.open("hosta", "parent", provider="stub")
            comms = Comms(store, sessions, _KeySequenceSpawn(tmux))
            queue = OutboundNoticeQueue(store, comms, config=_config())
            notice_id = f"notice-{kind}"
            await _enqueue(queue, notice_id=notice_id, kind=kind)
            delivered = await queue.deliver_now(notice_id)
            row = await _stored_notice(store, notice_id)
            assert delivered is True, row.get("last_error") if row else row
            assert tmux.events.count("paste+Enter") == 1
            return tmux.events, tmux.turn_interrupted
        finally:
            store.stop()

    events, was_interrupted = asyncio.run(run())
    assert events == expected
    assert was_interrupted is interrupts_busy_turn


def test_enqueue_dedupe_is_payload_bound_and_durable() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            comms = _RecordingComms()
            queue = OutboundNoticeQueue(store, comms, config=_config())
            first = await _enqueue(queue)
            assert first["created"] is True
            stored = await _stored_notice(store, "notice-1")
            assert stored is not None
            assert "[pentacle-notice:notice-1]" in stored["body"]

            duplicate = await _enqueue(queue)
            assert duplicate["created"] is False
            with pytest.raises(OutboundNoticeConflict):
                await queue.enqueue(
                    kind="test",
                    dedupe_key="test:notice-1",
                    recipient_stream_id="hosta:parent",
                    tell_id="notice-1",
                    body="different payload",
                    source_stream_id="hosta:child",
                )
        finally:
            store.stop()

    asyncio.run(run())


def test_lifecycle_cleanup_leaves_unrelated_notices_alone() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            await store.open_session("hosta", "child", provider="shell")
            queue = OutboundNoticeQueue(store, _RecordingComms(), config=_config())
            await _enqueue(queue, notice_id="report-notice", kind="report")
            await store.mark_closed(
                "hosta",
                "child",
                closed_at="2026-08-09T00:00:00Z",
                pane_status="pane_dead",
            )
            report = await _stored_notice(store, "report-notice")
            assert report is not None and report["terminal_at"] is None
        finally:
            store.stop()

    asyncio.run(run())


def test_claim_lease_allows_one_owner_then_recovers_after_expiry() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            queue = OutboundNoticeQueue(store, _RecordingComms(), config=_config())
            await _enqueue(queue)
            first, second = await asyncio.gather(
                store.claim_outbound_notice("notice-1", owner="owner-a", lease_s=0.1),
                store.claim_outbound_notice("notice-1", owner="owner-b", lease_s=0.1),
            )
            assert (first is None) != (second is None)
            await asyncio.sleep(0.13)
            recovered = await store.claim_outbound_notice(
                "notice-1", owner="owner-b", lease_s=0.1
            )
            assert recovered is not None
            assert recovered["attempts"] == 2
        finally:
            store.stop()

    asyncio.run(run())


def test_unfinished_notice_survives_store_restart_and_sweep(tmp_path) -> None:
    async def run() -> None:
        db = str(tmp_path / "outbound.db")
        store = Store(db)
        store.start()
        queue = OutboundNoticeQueue(store, _RecordingComms(), config=_config())
        await _enqueue(queue)
        assert await store.claim_outbound_notice(
            "notice-1", owner="crashed-owner", lease_s=0.1
        ) is not None
        await asyncio.sleep(0.13)
        store.stop()

        comms = _RecordingComms()
        restarted = Store(db)
        restarted.start()
        try:
            queue = OutboundNoticeQueue(restarted, comms, config=_config())
            assert await queue.drain_once() == 1
            assert len(comms.calls) == 1
            row = await _stored_notice(restarted, "notice-1")
            assert row is not None and row["delivered_at"] and row["attempts"] == 2
        finally:
            restarted.stop()

    asyncio.run(run())


class _CrashAfterPasteTmux:
    def __init__(self) -> None:
        self.capture_text = ""
        self.pastes: list[str] = []

    async def run(self, *args: str, **kwargs: object) -> tuple[int, str]:
        return 0, ""

    async def capture(self, name: str) -> str:
        return self.capture_text

    async def paste(self, name: str, text: str) -> None:
        self.pastes.append(text)
        self.capture_text += "\n" + text


class _CrashAfterPasteSpawn:
    def __init__(self, tmux: _CrashAfterPasteTmux) -> None:
        self.tmux = tmux

    async def _await_marker(self, *args: object, **kwargs: object) -> bool:
        raise RuntimeError("crash after paste")


class _CodexNoticeTmux:
    def __init__(self) -> None:
        self.screen = "OpenAI Codex\n─────────\n› \n  gpt-5-codex high"
        self.pastes: list[str] = []
        self.enter_only = 0

    async def capture(self, name: str) -> str:
        return self.screen

    async def paste(self, name: str, text: str) -> None:
        self.pastes.append(text)
        self.screen = (
            "OpenAI Codex\n─────────\n"
            f"› [Pasted Content {len(text)} chars]\n  gpt-5-codex high"
        )

    async def send_enter(self, name: str) -> None:
        self.enter_only += 1
        # The initial bounded tell attempt intentionally remains editable. The
        # recovery sweep below changes only the observed pane state, proving it
        # performs no second input operation.

    async def run(self, *args: str, **kwargs: object) -> tuple[int, str]:
        return 0, ""


class _CodexNoticeSpawn:
    def __init__(self, tmux: _CodexNoticeTmux) -> None:
        self.tmux = tmux

    async def _await_marker(self, *args: object, **kwargs: object) -> bool:
        return True


class _DelayedUserProof:
    def __init__(self) -> None:
        self.landed = False
        self.wait_timeouts: list[float] = []

    async def watermark(self, stream_id: str) -> EventWatermark:
        return EventWatermark(stream_id, 253987980, "reachable")

    async def wait(self, stream_id: str, *, expected_text: str,
                   watermark: EventWatermark, timeout_s: float) -> EventProof:
        self.wait_timeouts.append(timeout_s)
        return await self.lookup(
            stream_id, expected_text=expected_text, watermark=watermark,
        )

    async def lookup(self, stream_id: str, *, expected_text: str,
                     watermark: EventWatermark) -> EventProof:
        if not self.landed:
            return EventProof("pending", stream_id, watermark.daemon_seq)
        return EventProof(
            "proven", stream_id, watermark.daemon_seq,
            event_id=254000000, event_ts="2026-08-27T03:43:05.494Z",
        )


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("kind", ["watch", "wake", "wake_urgent"])
def test_d2_busy_provider_queue_requires_proof_and_only_urgent_interrupts(provider, kind):
    async def run():
        store = Store()
        store.start()
        tmux = _CodexNoticeTmux()
        keys = []
        original_run = tmux.run
        async def record_keys(*args, **kwargs):
            if args[:1] == ("send-keys",):
                keys.append(args[-1])
            return await original_run(*args, **kwargs)
        tmux.run = record_keys
        try:
            sessions = Sessions(store, tmux=tmux, local_host="hosta")
            await sessions.open("hosta", "parent", provider=provider)
            sessions.apply_live("hosta:parent", working=True)
            proof = _DelayedUserProof()
            comms = Comms(store, sessions, _CodexNoticeSpawn(tmux), submission_proof=proof)
            queue = OutboundNoticeQueue(store, comms, config=_config())
            await _enqueue(queue, kind=kind)
            assert not await queue.deliver_now("notice-1")
            assert keys.count("Escape") == int(kind == "wake_urgent")
            assert len(tmux.pastes) == 1
            assert (await store.get_tell_delivery("notice-1"))["delivery"]["delivery_status"] == "committed_pending_proof"
            assert not await queue.deliver_now("notice-1")
            proof.landed = True
            assert await queue.deliver_now("notice-1")
            assert len(tmux.pastes) == 1
            assert keys.count("Escape") == int(kind == "wake_urgent")
        finally:
            store.stop()
    asyncio.run(run())


def test_retry_scans_recipient_marker_before_reinjecting_after_crash() -> None:
    async def run() -> None:
        tmux = _CrashAfterPasteTmux()
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=tmux, local_host="hosta")
            await sessions.open("hosta", "parent", provider="shell", pane_status="pane_alive")
            comms = Comms(store, sessions, _CrashAfterPasteSpawn(tmux))
            queue = OutboundNoticeQueue(store, comms, config=_config())
            await _enqueue(queue)

            assert await queue.deliver_now("notice-1") is False
            failed = await _stored_notice(store, "notice-1")
            assert failed is not None and failed["attempts"] == 1
            assert len(tmux.pastes) == 1

            assert await queue.deliver_now("notice-1") is True
            completed = await _stored_notice(store, "notice-1")
            assert completed is not None and completed["delivered_at"]
            assert len(tmux.pastes) == 1
        finally:
            store.stop()

    asyncio.run(run())


def test_codex_notice_sweep_does_not_promote_from_pane_chrome_without_user_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import comms as comms_module

    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_POLL_S", 0.001)

    async def run() -> tuple[dict, dict, _CodexNoticeTmux, Store]:
        tmux = _CodexNoticeTmux()
        store = Store(":memory:")
        store.start()
        sessions = Sessions(store, tmux=tmux, local_host="hosta")
        await sessions.open("hosta", "parent", provider="codex")
        proof = _DelayedUserProof()
        comms = Comms(
            store, sessions, _CodexNoticeSpawn(tmux), submission_proof=proof,
        )
        queue = OutboundNoticeQueue(store, comms, config=_config())
        await _enqueue(queue)

        assert await queue.deliver_now("notice-1") is False
        pending_tell = await store.get_tell_delivery("notice-1")
        assert pending_tell is not None
        assert pending_tell["delivery"]["delivery_status"] == "committed_pending_proof"
        assert pending_tell["reply"]["reason"] == "submission_proof_pending"
        assert len(tmux.pastes) == 1
        assert tmux.enter_only == 0
        assert proof.wait_timeouts == [PROOF_FAST_WAIT_S]

        body = str((await _stored_notice(store, "notice-1"))["body"])
        tmux.screen = (
            "OpenAI Codex\n"
            "Messages to be submitted after next tool call\n"
            f"↳ {body}\n"
            "• scope readback: services/chat-stream-v2\n"
            "› "
        )
        assert await queue.deliver_now("notice-1") is False
        pending = await store.get_tell_delivery("notice-1")
        queued = await _stored_notice(store, "notice-1")
        assert pending is not None and queued is not None
        return pending, queued, tmux, store

    pending, queued, tmux, store = asyncio.run(run())
    try:
        assert pending["delivery"]["delivery_status"] == "committed_pending_proof"
        assert pending["reply"]["submission_confirmed"] is False
        assert queued["delivered_at"] is None
        assert len(tmux.pastes) == 1
        assert tmux.enter_only == 0
    finally:
        store.stop()


def test_row18075_delayed_user_event_promotes_same_key_without_second_paste() -> None:
    async def run() -> tuple[dict, dict, _CodexNoticeTmux, Store]:
        tmux = _CodexNoticeTmux()
        proof = _DelayedUserProof()
        store = Store(":memory:")
        store.start()
        sessions = Sessions(store, tmux=tmux, local_host="hosta")
        await sessions.open("hosta", "parent", provider="codex")
        comms = Comms(
            store,
            sessions,
            _CodexNoticeSpawn(tmux),
            submission_proof=proof,
        )
        queue = OutboundNoticeQueue(store, comms, config=_config())
        await _enqueue(queue, notice_id="notice-18075")

        assert await queue.deliver_now("notice-18075") is False
        pending = await store.get_tell_delivery("notice-18075")
        assert pending is not None
        assert pending["delivery"]["delivery_status"] == "committed_pending_proof"
        assert pending["delivery"]["proof_watermark"] == 253987980
        assert len(tmux.pastes) == 1
        assert tmux.enter_only == 0
        assert proof.wait_timeouts == [PROOF_FAST_WAIT_S]

        proof.landed = True
        assert await queue.deliver_now("notice-18075") is True
        promoted = await store.get_tell_delivery("notice-18075")
        completed = await _stored_notice(store, "notice-18075")
        assert promoted is not None and completed is not None
        return promoted, completed, tmux, store

    promoted, completed, tmux, store = asyncio.run(run())
    try:
        assert promoted["ledger_row_id"] == promoted["delivery"]["ledger_row_id"]
        assert promoted["delivery"]["delivery_status"] == "delivered"
        assert promoted["delivery"]["proof_event_id"] == 254000000
        assert promoted["delivery"]["proof_event_at"] == "2026-08-27T03:43:05.494Z"
        assert completed["delivered_at"]
        assert len(tmux.pastes) == 1
        assert tmux.enter_only == 0
    finally:
        store.stop()


def test_retry_budget_records_terminal_reason_and_next_action() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            comms = _RecordingComms(failures=4)
            queue = OutboundNoticeQueue(store, comms, config=_config(max_attempts=2))
            await _enqueue(queue)
            assert await queue.deliver_now("notice-1") is False
            assert await queue.deliver_now("notice-1") is False
            row = await _stored_notice(store, "notice-1")
            assert row is not None
            assert row["terminal_at"]
            assert row["terminal_reason"].startswith("retry_budget_exhausted:")
            assert row["next_action"] == "inspect transport and replay deliberately"
        finally:
            store.stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("recipient_status", "age_s", "terminal"),
    [
        ("open", 60.0, False),
        ("closed", 10.494, False),
        ("closed", 20.001, True),
    ],
)
def test_proof_pending_terminal_requires_bound_and_closed_recipient(
    recipient_status: str,
    age_s: float,
    terminal: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import outbound_notices as notices_module

    async def run() -> dict:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            opened = await sessions.open("hosta", "parent", provider="codex")
            if recipient_status == "closed":
                await store.mark_closed(
                    "hosta",
                    "parent",
                    closed_at="2026-08-27T03:43:00Z",
                    pane_status="pane_dead",
                    expected_generation=str(opened["session_generation"]),
                )
            now = 2_000_000_000.0
            monkeypatch.setattr(notices_module.time, "time", lambda: now)
            queue = OutboundNoticeQueue(
                store,
                _PendingComms(),
                config=_config(max_attempts=1),
            )
            await queue.enqueue(
                kind=NOTICE_KIND_REPORT,
                dedupe_key="report:terminal-proof",
                recipient_stream_id="hosta:parent",
                tell_id="terminal-proof",
                body="pending body",
                source_stream_id="hosta:child",
                created_at=notices_module._iso_from_epoch(now - age_s),
            )
            assert await queue.deliver_now("terminal-proof") is False
            row = await _stored_notice(store, "terminal-proof")
            assert row is not None
            return row
        finally:
            store.stop()

    row = asyncio.run(run())
    assert bool(row["terminal_at"]) is terminal


def test_proof_pending_closed_and_aged_stays_pending_when_current_lookup_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import outbound_notices as notices_module

    async def run() -> dict:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            opened = await sessions.open("hosta", "parent", provider="codex")
            await store.mark_closed(
                "hosta", "parent", closed_at="2026-08-27T03:43:00Z",
                pane_status="pane_dead",
                expected_generation=str(opened["session_generation"]),
            )
            now = 2_000_000_000.0
            monkeypatch.setattr(notices_module.time, "time", lambda: now)
            queue = OutboundNoticeQueue(
                store, _PendingComms("unreachable"), config=_config(max_attempts=1),
            )
            await queue.enqueue(
                kind=NOTICE_KIND_REPORT,
                dedupe_key="report:unreachable-proof",
                recipient_stream_id="hosta:parent",
                tell_id="unreachable-proof",
                body="pending body",
                source_stream_id="hosta:child",
                created_at=notices_module._iso_from_epoch(now - 60.0),
            )
            assert await queue.deliver_now("unreachable-proof") is False
            row = await _stored_notice(store, "unreachable-proof")
            assert row is not None
            return row
        finally:
            store.stop()

    row = asyncio.run(run())
    assert row["terminal_at"] is None
    assert row["last_error"] == "submission_proof_pending"


def test_report_parent_notice_is_enqueue_before_delivery_and_deduped() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            await sessions.open("hosta", "parent", provider="shell")
            await sessions.open(
                "hosta", "child", provider="shell", parent_stream_id="hosta:parent"
            )
            comms = _RecordingComms()
            queue = OutboundNoticeQueue(store, comms, config=_config())
            ledger = Ledger(store, sessions=sessions, comms=comms, outbound=queue)
            row = {
                "report_id": "report-42",
                "ledger_row_id": 42,
                "from_stream_id": "hosta:child",
                "to_stream_id": "hosta:parent",
                "msg_id": 7,
                "status": "done",
                "summary": "finished",
            }
            first = await ledger._announce_child_report_ready(row)
            notice_id = child_report_ready_tell_id("report-42")
            assert await _stored_notice(store, notice_id) is not None
            assert first["error_code"] == "notice_receipt_missing"
            replay = await ledger._announce_child_report_ready(row)
            assert replay["error_code"] == "notice_receipt_missing"
            assert len(comms.calls) == 1
        finally:
            store.stop()

    asyncio.run(run())


def test_reconciler_parent_notice_retries_on_transient_transport_failure() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            comms = _RecordingComms(failures=1)
            queue = OutboundNoticeQueue(store, comms, config=_config())
            reconciler = SessionReconciler(
                sessions,
                None,
                comms=comms,
                outbound=queue,
            )
            await reconciler._surface(
                {
                    "host": "hosta",
                    "session_name": "child",
                    "visibility": "child",
                    "parent_stream_id": "hosta:parent",
                },
                {"episode_start_ts": "2026-08-09T00:00:00Z"},
                "tmux_absent",
            )
            row = await _stored_notice(store,
                "reconciler:c1:hosta:child:2026-08-09T00:00:00Z"
            )
            assert row is not None and row["delivered_at"] is None
            assert await queue.drain_once(force=True) == 1
            assert len(comms.calls) == 2
            row = await _stored_notice(store,
                "reconciler:c1:hosta:child:2026-08-09T00:00:00Z"
            )
            assert row is not None and row["delivered_at"]
        finally:
            store.stop()

    asyncio.run(run())
