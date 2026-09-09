"""Durable C5 awaiter resolution and restart/re-drive coverage."""

from __future__ import annotations

import asyncio

from ledger import Ledger
from sessions import Sessions
from store import Store


HOST = "hosta"


async def _state(path: str = ":memory:") -> tuple[Store, Sessions, Ledger]:
    store = Store(path)
    store.start()
    sessions = Sessions(store, local_host=HOST)
    ledger = Ledger(store, sessions=sessions)
    return store, sessions, ledger


async def _open_child(sessions: Sessions, name: str = "worker") -> dict:
    return await sessions.open(
        HOST,
        name,
        role="worker",
        visibility="hidden",
        parent_stream_id=f"{HOST}:parent",
    )


async def _report(ledger: Ledger, stream_id: str, *, msg_id: int, report_id: str) -> dict:
    return await ledger.ingest({
        "report_id": report_id,
        "from_stream_id": stream_id,
        "msg_id": msg_id,
        "status": "done",
        "summary": "completed",
        "findings": [],
        "next_action": "none",
    })


async def _wait_for_registered(store: Store, stream_id: str, msg_id: int) -> dict:
    for _ in range(100):
        row = await store.get_awaiter_result(stream_id, msg_id)
        if row is not None:
            return row
        await asyncio.sleep(0)
    raise AssertionError("await row was not registered")


def test_confirmed_close_resolves_parked_awaiter_and_persists_result() -> None:
    async def go() -> None:
        store, sessions, ledger = await _state()
        try:
            child = await _open_child(sessions)
            stream_id = child["stream_id"]
            waiter = asyncio.create_task(ledger.await_report({
                "stream_id": stream_id, "msg_id": 7, "timeout": 5,
            }))
            await _wait_for_registered(store, stream_id, 7)

            await sessions.mark_closed(HOST, "worker", reason="confirmed-dead")

            reply = await asyncio.wait_for(waiter, timeout=1)
            assert reply["type"] == "await_report.closed_without_report"
            assert reply["result_kind"] == "closed_without_report"
            durable = await store.get_awaiter_result(stream_id, 7)
            assert durable["outcome"] == "closed_without_report"
            assert durable["resolved_at"]
        finally:
            store.stop()

    asyncio.run(go())


def test_report_ingest_settles_parked_awaiter_as_report() -> None:
    async def go() -> None:
        store, sessions, ledger = await _state()
        try:
            child = await _open_child(sessions, "reported")
            stream_id = child["stream_id"]
            waiter = asyncio.create_task(ledger.await_report({
                "stream_id": stream_id, "msg_id": 8, "timeout": 5,
            }))
            await _wait_for_registered(store, stream_id, 8)

            await _report(ledger, stream_id, msg_id=8, report_id="parked-report")

            reply = await asyncio.wait_for(waiter, timeout=1)
            assert reply["type"] == "await_report.ok"
            assert reply["result_kind"] == "report"
            assert reply["source"] == "broadcast"
            assert reply["report_id"] == "parked-report"
        finally:
            store.stop()

    asyncio.run(go())


def test_reconciler_dead_close_resolves_without_report() -> None:
    async def go() -> None:
        store, sessions, ledger = await _state()
        try:
            child = await _open_child(sessions, "reaped")
            stream_id = child["stream_id"]
            await store.register_awaiter(stream_id, 3)

            await sessions.mark_reconciled_dead(
                HOST,
                "reaped",
                expected_generation=child["session_generation"],
                presumed_dead_at="2026-08-09T00:00:00Z",
                closed_at="2026-08-09T00:00:01Z",
            )

            result = await store.get_awaiter_result(stream_id, 3)
            assert result["outcome"] == "closed_without_report"
            assert (await ledger.sweep_awaited_unreported())["checked"] == 0
        finally:
            store.stop()

    asyncio.run(go())


def test_startup_sweep_re_drives_row_left_by_crash_and_is_idempotent(tmp_path) -> None:
    async def go() -> None:
        db = str(tmp_path / "sessions.db")
        store1, sessions1, _ledger1 = await _state(db)
        child = await _open_child(sessions1, "crashed")
        stream_id = child["stream_id"]
        await store1.register_awaiter(stream_id, 9)
        # Simulate a process dying after the close transaction but before the
        # old Ledger callback could run.
        await store1.mark_closed(
            HOST,
            "crashed",
            closed_at="2026-08-09T00:00:02Z",
            pane_status="pane_dead",
            expected_generation=child["session_generation"],
        )
        store1.stop()

        store2, sessions2, ledger2 = await _state(db)
        try:
            assert sessions2 is not None
            first = await ledger2.sweep_awaited_unreported()
            assert first == {
                "checked": 1,
                "resolved": 1,
                "reports": 0,
                "closed_without_report": 1,
            }
            second = await ledger2.sweep_awaited_unreported()
            assert second["checked"] == 0
            reply = await ledger2.await_report({"stream_id": stream_id, "msg_id": 9})
            assert reply["type"] == "await_report.closed_without_report"
            assert (await store2.get_awaiter_result(stream_id, 9))["outcome"] == "closed_without_report"
        finally:
            store2.stop()

    asyncio.run(go())


def test_live_worker_is_excluded_from_sweep_and_timeout_is_classified() -> None:
    async def go() -> None:
        store, sessions, ledger = await _state()
        try:
            child = await _open_child(sessions, "live")
            stream_id = child["stream_id"]
            timeout = await ledger.await_report({
                "stream_id": stream_id, "msg_id": 12, "timeout": 0.01,
            })
            assert timeout["type"] == "await_report.timeout"
            assert timeout["result_kind"] == "timeout"
            assert (await store.get_awaiter_result(stream_id, 12))["outcome"] == "pending"
            sweep = await ledger.sweep_awaited_unreported()
            assert sweep["checked"] == 0
            assert (await store.get_awaiter_result(stream_id, 12))["outcome"] == "pending"
        finally:
            store.stop()

    asyncio.run(go())


def test_real_report_wins_after_close_and_keeps_one_durable_result() -> None:
    async def go() -> None:
        store, sessions, ledger = await _state()
        try:
            child = await _open_child(sessions, "race")
            stream_id = child["stream_id"]
            await store.register_awaiter(stream_id, 15)
            await sessions.mark_closed(HOST, "race", reason="close-race")
            assert (await store.get_awaiter_result(stream_id, 15))["outcome"] == "closed_without_report"

            await _report(ledger, stream_id, msg_id=15, report_id="race-report")

            durable = await store.get_awaiter_result(stream_id, 15)
            assert durable["outcome"] == "report"
            assert durable["report"]["report_id"] == "race-report"
            reply = await ledger.await_report({"stream_id": stream_id, "msg_id": 15})
            assert reply["type"] == "await_report.ok"
            assert reply["result_kind"] == "report"
            assert reply["report_id"] == "race-report"
        finally:
            store.stop()

    asyncio.run(go())
