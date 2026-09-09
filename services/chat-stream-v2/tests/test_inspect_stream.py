"""Focused contract tests for the v2 read-only inspect path."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ledger import Ledger  # noqa: E402
from server import RECENT_LIMIT, Server  # noqa: E402
from sessions import Sessions  # noqa: E402
from store import Store  # noqa: E402


STREAM_ID = "alpha:inspect"


async def _state() -> tuple[Store, Sessions, Server, Ledger]:
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, local_host="alpha")
    await sessions.open("alpha", "inspect", visibility="visible", role="worker")
    ledger = Ledger(store, sessions=sessions)
    return store, sessions, Server(store=store, sessions=sessions), ledger


async def _event(store: Store, index: int, *, limit: int = 600) -> None:
    await store.append_session_event(
        STREAM_ID,
        {
            "stream_id": STREAM_ID,
            "provider": "claude",
            "kind": "ASSIST_TEXT",
            "text": f"event-{index}",
            "timestamp": f"2026-08-08T00:00:{index % 60:02d}Z",
            "raw": {"jsonl_record_uuid": f"inspect-{index}", "jsonl_event_index": 0},
        },
        identity=f"inspect-{index}",
        limit=limit,
    )


async def _report(ledger: Ledger, report_id: str, msg_id: int, *, status: str = "done") -> None:
    payload = {
        "report_id": report_id,
        "from_stream_id": STREAM_ID,
        "msg_id": msg_id,
        "status": status,
        "summary": report_id,
    }
    if status == "done":
        payload.update({"findings": [], "next_action": "inspect"})
    await ledger.ingest(payload)


def test_inspect_report_exact_and_terminal_fallback() -> None:
    async def _go() -> None:
        store, sessions, server, ledger = await _state()
        try:
            await _report(ledger, "report-old", 3)
            await _report(ledger, "report-new", 8)
            await _report(ledger, "progress-newest", 9, status="progress")

            exact = await server._on_inspect_stream({
                "stream_id": STREAM_ID, "msg_id": 3, "event_tail": 0,
                "from_stream_id": "alpha:untrusted-claim",
            })
            fallback = await server._on_inspect_stream({
                "stream_id": STREAM_ID, "msg_id": 99, "event_tail": 0,
            })
            latest = await server._on_inspect_stream({
                "stream_id": STREAM_ID, "event_tail": 0,
            })
            by_report_id = await server._on_inspect_stream({
                "stream_id": STREAM_ID, "report_id": "report-old", "event_tail": 0,
            })
            missing_report_id = await server._on_inspect_stream({
                "stream_id": STREAM_ID, "report_id": "not-present", "event_tail": 0,
            })

            assert exact["stream_id"] == STREAM_ID
            assert exact["existing_report"]["report_id"] == "report-old"
            assert fallback["existing_report"]["report_id"] == "report-new"
            assert latest["existing_report"]["report_id"] == "report-new"
            assert by_report_id["existing_report"]["report_id"] == "report-old"
            assert missing_report_id["existing_report"] is None
            assert exact["recent_events"] == []
            assert exact["send_frames"]["status"] == "not_implemented"
        finally:
            store.stop()

    asyncio.run(_go())


def test_inspect_report_id_is_scoped_to_the_requested_stream() -> None:
    async def _go() -> None:
        store, sessions, server, ledger = await _state()
        try:
            await sessions.open("beta", "other", visibility="visible", role="worker")
            await ledger.ingest({
                "report_id": "other-stream-report",
                "from_stream_id": "beta:other",
                "msg_id": 1,
                "status": "done",
                "summary": "other",
                "findings": [],
                "next_action": "inspect",
            })

            reply = await server._on_inspect_stream({
                "stream_id": STREAM_ID,
                "report_id": "other-stream-report",
                "event_tail": 0,
            })

            assert reply["existing_report"] is None
        finally:
            store.stop()

    asyncio.run(_go())


def test_inspect_event_tail_is_oldest_first_and_bounded() -> None:
    async def _go() -> None:
        store, _sessions, server, _ledger = await _state()
        try:
            for index in range(RECENT_LIMIT + 5):
                await _event(store, index)

            reply = await server._on_inspect_stream({
                "stream_id": STREAM_ID, "event_tail": RECENT_LIMIT + 100,
            })
            events = reply["recent_events"]
            assert len(events) == RECENT_LIMIT
            assert [event["text"] for event in events[:2]] == ["event-5", "event-6"]
            assert [event["text"] for event in events[-2:]] == [
                f"event-{RECENT_LIMIT + 3}", f"event-{RECENT_LIMIT + 4}",
            ]
            assert [event["daemon_seq"] for event in events] == sorted(
                event["daemon_seq"] for event in events
            )
            assert (await server._on_inspect_stream({
                "stream_id": STREAM_ID, "event_tail": 0,
            }))["recent_events"] == []
        finally:
            store.stop()

    asyncio.run(_go())


def test_inspect_rejects_unbounded_or_malformed_read_arguments() -> None:
    async def _go() -> None:
        store, _sessions, server, _ledger = await _state()
        try:
            for payload in (
                {"type": "inspect_stream", "request_id": "bad-msg", "stream_id": STREAM_ID, "msg_id": True},
                {"type": "inspect_stream", "request_id": "bad-tail", "stream_id": STREAM_ID, "event_tail": "all"},
                {"type": "inspect_stream", "request_id": "bad-report", "stream_id": STREAM_ID, "report_id": ""},
            ):
                (reply,) = await server._dispatch(json.dumps(payload))
                assert reply["type"] == "inspect_stream.error"
                assert reply["error_code"] == "invalid_request"
                assert reply["request_id"] == payload["request_id"]
        finally:
            store.stop()

    asyncio.run(_go())


def test_inspect_keeps_durable_history_after_close() -> None:
    async def _go() -> None:
        store, sessions, server, ledger = await _state()
        try:
            await _event(store, 1)
            await _report(ledger, "closed-report", 4)
            await sessions.mark_closed("alpha", "inspect", reason="test")

            reply = await server._on_inspect_stream({
                "stream_id": STREAM_ID, "msg_id": 4, "event_tail": 1,
            })
            assert reply["session"]["status"] == "closed"
            assert reply["existing_report"]["report_id"] == "closed-report"
            assert [event["text"] for event in reply["recent_events"]] == ["event-1"]
        finally:
            store.stop()

    asyncio.run(_go())
