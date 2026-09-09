"""notification.list default cap and TTL expiry sweep.

Two guarantees this pins:

  * `notification.list` bounds a bare call (no `limit`) to
    `DEFAULT_NOTIFICATION_LIST_LIMIT`, so no caller can pull the full >1MiB
    row set by omitting params — while every EXPLICIT limit (large for a full
    listing, `0` for empty, plus explicit `states`) is honoured verbatim, and
    a default state filter is NOT imposed (mobile omits `states` on purpose).

  * the expiry sweep expires due non-question rows and PRESERVES open
    question rows (they carry no `expires_at`), broadcasting each expired card
    and returning the count.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from notify import DEFAULT_NOTIFICATION_LIST_LIMIT, Notify  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


async def _notify(tmp_path: Path, *, broadcast=None) -> Notify:
    n = Notify(db_path=str(tmp_path / "notifications.db"), broadcast=broadcast)
    await n.start()
    return n


async def _seed_notifications(n: Notify, count: int) -> None:
    """`count` open, non-question notifications (each a distinct dedup_key)."""
    for i in range(count):
        await n._db.call(
            "create_notification",
            producer="test-seed",
            title=f"n{i}",
            body="x",
            dedup_key=f"seed-{i}",
        )


# -- notification.list default cap ----------------------------------------

def test_bare_list_is_capped_at_default(tmp_path: Path) -> None:
    async def go() -> None:
        n = await _notify(tmp_path)
        try:
            await _seed_notifications(n, DEFAULT_NOTIFICATION_LIST_LIMIT + 25)
            reply = await n.notification({"type": "notification.list", "request_id": "r1"})
        finally:
            await n.stop()
        assert reply["type"] == "notification.list.ok"
        assert len(reply["notifications"]) == DEFAULT_NOTIFICATION_LIST_LIMIT

    _run(go())


def test_explicit_large_limit_lists_everything(tmp_path: Path) -> None:
    async def go() -> None:
        n = await _notify(tmp_path)
        total = DEFAULT_NOTIFICATION_LIST_LIMIT + 25
        try:
            await _seed_notifications(n, total)
            reply = await n.notification(
                {"type": "notification.list", "request_id": "r1", "limit": 100000}
            )
        finally:
            await n.stop()
        assert len(reply["notifications"]) == total

    _run(go())


def test_explicit_zero_limit_is_empty(tmp_path: Path) -> None:
    async def go() -> None:
        n = await _notify(tmp_path)
        try:
            await _seed_notifications(n, 5)
            reply = await n.notification(
                {"type": "notification.list", "request_id": "r1", "limit": 0}
            )
        finally:
            await n.stop()
        # limit=0 is explicit "empty" (SQLite LIMIT 0), NOT the omitted default.
        assert reply["notifications"] == []

    _run(go())


def test_states_are_not_defaulted_bare_call_sees_terminal_rows(tmp_path: Path) -> None:
    async def go() -> None:
        n = await _notify(tmp_path)
        try:
            rec = await n._db.call(
                "create_notification", producer="test-seed", title="t", dedup_key="d1"
            )
            await n._db.call("resolve_notification", rec["notification_id"],
                             action_kind="resolved", by="operator")
            reply = await n.notification({"type": "notification.list", "request_id": "r1"})
        finally:
            await n.stop()
        # A bare call must still surface the resolved (terminal) row: no default
        # state filter is imposed. Mobile relies on this to render terminal cards.
        states = {r["state"] for r in reply["notifications"]}
        assert "resolved" in states

    _run(go())


def test_explicit_states_filter_still_honoured(tmp_path: Path) -> None:
    async def go() -> None:
        n = await _notify(tmp_path)
        try:
            open_rec = await n._db.call(
                "create_notification", producer="test-seed", title="open", dedup_key="d-open"
            )
            done = await n._db.call(
                "create_notification", producer="test-seed", title="done", dedup_key="d-done"
            )
            await n._db.call("resolve_notification", done["notification_id"],
                             action_kind="resolved", by="operator")
            reply = await n.notification(
                {"type": "notification.list", "request_id": "r1", "states": ["open"]}
            )
        finally:
            await n.stop()
        ids = {r["notification_id"] for r in reply["notifications"]}
        assert open_rec["notification_id"] in ids
        assert done["notification_id"] not in ids

    _run(go())


# -- TTL expiry sweep ------------------------------------------------------

def test_expiry_expires_due_nonquestion_and_preserves_questions(tmp_path: Path) -> None:
    async def go() -> None:
        broadcasts: list[dict] = []

        async def _bcast(frame: dict) -> None:
            broadcasts.append(frame)

        n = await _notify(tmp_path, broadcast=_bcast)
        try:
            # A non-question row whose TTL already elapsed (created in the past).
            due = await n._db.call(
                "create_notification", producer="test-seed", title="due",
                dedup_key="d-due", ttl_seconds=60, now="2020-01-01T00:00:00Z",
            )
            # An open agent question — the operator ask protocol; no expires_at,
            # so the sweep must never touch it.
            question = await n._db.call(
                "create_agent_question",
                envelope={
                    "schema_version": 1, "question_id": "q-keep",
                    "title": "keep me", "body": "b", "dedup_key": "q-keep",
                    "response_mode": "ack", "options": [{"label": "OK", "value": "ack"}],
                },
                actions=[{
                    "kind": "ack", "action_id": "a0",
                    "value": {"schema_version": 1, "question_id": "q-keep", "answer": "ack"},
                }],
            )
            expired = await n.expire_once()

            due_after = await n._db.call("get_notification", due["notification_id"])
            q_after = await n._db.call(
                "get_notification", question["notification_id"]
            )
        finally:
            await n.stop()

        assert expired == 1
        assert due_after["state"] == "expired"
        # Question survives, still open.
        assert q_after["state"] == "open"
        # The expired card was broadcast so UIs drop it.
        assert any(
            f.get("type") == "notification"
            and (f.get("notification") or {}).get("notification_id") == due["notification_id"]
            and (f.get("notification") or {}).get("state") == "expired"
            for f in broadcasts
        )

    _run(go())


def test_expiry_returns_zero_when_nothing_due(tmp_path: Path) -> None:
    async def go() -> None:
        n = await _notify(tmp_path)
        try:
            # Fresh row: TTL default is 7 days out, so it is not due.
            await n._db.call(
                "create_notification", producer="test-seed", title="fresh", dedup_key="fresh"
            )
            assert await n.expire_once() == 0
        finally:
            await n.stop()

    _run(go())


def test_expiry_post_processing_cap_bounds_broadcast(tmp_path: Path) -> None:
    async def go() -> None:
        broadcasts: list[dict] = []

        async def _bcast(frame: dict) -> None:
            broadcasts.append(frame)

        n = await _notify(tmp_path, broadcast=_bcast)
        try:
            for i in range(5):
                await n._db.call(
                    "create_notification", producer="test-seed", title=f"due{i}",
                    dedup_key=f"due-{i}", ttl_seconds=60, now="2020-01-01T00:00:00Z",
                )
            # All 5 are due and expire, but post-processing (broadcast) is capped.
            expired = await n.expire_once(cap=2)
        finally:
            await n.stop()
        assert expired == 5
        notif_frames = [f for f in broadcasts if f.get("type") == "notification"]
        assert len(notif_frames) == 2

    _run(go())
