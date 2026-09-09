"""Every projection-changing Sessions mutation publishes inventory immediately."""

from __future__ import annotations

import asyncio
import sqlite3

from inventory import InventoryEmitter
from sessions import Sessions
from store import SESSIONS_DDL, Store


HOST = "testhost"


async def _registry() -> tuple[Store, Sessions, list[dict]]:
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, local_host=HOST)
    frames: list[dict] = []

    async def broadcast(frame: dict) -> None:
        frames.append(frame)

    emitter = InventoryEmitter(sessions, broadcast, min_interval_s=60)
    sessions.set_inventory_emitter(emitter)
    return store, sessions, frames


async def _prime(sessions: Sessions) -> None:
    sessions._inventory_emitter.prime()  # type: ignore[union-attr]


async def _one_immediate_frame(sessions: Sessions, frames: list[dict]) -> None:
    await asyncio.sleep(0)
    assert len(frames) == 1
    assert frames[0]["type"] == "session.inventory"
    assert await sessions._inventory_emitter.emit_if_changed() is False  # type: ignore[union-attr]
    assert len(frames) == 1


def test_mark_closed_locked_emits_inventory_once_immediately() -> None:
    async def run() -> None:
        store, sessions, frames = await _registry()
        try:
            row = await sessions.open(HOST, "close-me", visibility="visible")
            await _prime(sessions)

            async def terminate(_name: str):
                return "ok", "", {"reap_status": "reaped", "survivors": []}

            sessions._terminate_pane = terminate  # type: ignore[method-assign]
            result = await sessions.close(HOST, "close-me")
            assert result["session"]["status"] == "closed"
            assert row["stream_id"] not in {s["stream_id"] for s in frames[0].get("sessions", [])}
            await _one_immediate_frame(sessions, frames)
        finally:
            store.stop()

    asyncio.run(run())


def test_mark_reconciled_dead_emits_inventory_immediately() -> None:
    async def run() -> None:
        store, sessions, frames = await _registry()
        try:
            row = await sessions.open(HOST, "dead", visibility="visible")
            await _prime(sessions)
            await sessions.mark_reconciled_dead(
                HOST, "dead", presumed_dead_at="2026-09-02T00:00:00Z",
                closed_at="2026-09-02T00:00:01Z", expected_generation=row["session_generation"],
            )
            await _one_immediate_frame(sessions, frames)
        finally:
            store.stop()

    asyncio.run(run())


def test_rename_emits_inventory_immediately() -> None:
    async def run() -> None:
        store, sessions, frames = await _registry()
        try:
            await sessions.open(HOST, "rename", visibility="visible")
            await _prime(sessions)
            await sessions.rename(HOST, "rename", "new title")
            assert frames[0]["sessions"][0]["display_name"] == "new title"
            await _one_immediate_frame(sessions, frames)
        finally:
            store.stop()

    asyncio.run(run())


def test_legacy_sessions_gain_a_nullable_title_without_a_backfill(tmp_path) -> None:
    db = tmp_path / "legacy-sessions.db"
    legacy_ddl = SESSIONS_DDL.replace("    title TEXT,\n", "")
    with sqlite3.connect(db) as conn:
        conn.executescript(legacy_ddl)
        conn.execute(
            "INSERT INTO sessions (host, session_name, visibility, created_at) VALUES (?, ?, ?, ?)",
            (HOST, "legacy", "visible", "2026-09-03T00:00:00Z"),
        )

    async def run() -> None:
        store = Store(str(db))
        store.start()
        try:
            columns = await store.submit(
                lambda conn: {str(column[1]) for column in conn.execute("PRAGMA table_info(sessions)")}
            )
            row = await store.fetch_session(HOST, "legacy")
            sessions = Sessions(store, local_host=HOST)
            await sessions.refresh()
            (listed,) = sessions.list_open()
            assert "title" in columns
            assert row is not None and row["title"] is None
            assert listed["title"] == "New Chat - Testhost"
            assert listed["display_name"] == "New Chat - Testhost"
        finally:
            store.stop()

    asyncio.run(run())


def test_titles_persist_across_refresh_and_clear_on_closed_name_reopen() -> None:
    async def run() -> None:
        store, sessions, _frames = await _registry()
        try:
            await sessions.open(HOST, "durable", visibility="visible", title="opened title")
            assert (await store.fetch_session(HOST, "durable"))["title"] == "opened title"

            await sessions.rename(HOST, "durable", "renamed title")
            assert (await store.fetch_session(HOST, "durable"))["title"] == "renamed title"

            restored = Sessions(store, local_host=HOST)
            await restored.refresh()
            assert restored.get(f"{HOST}:durable")["title"] == "renamed title"

            await store.update_session(HOST, "durable", status="closed")
            successor = Sessions(store, local_host=HOST)
            reopened = await successor.open(HOST, "durable", visibility="visible")
            assert reopened["title"] is None
            assert (await store.fetch_session(HOST, "durable"))["title"] is None
        finally:
            store.stop()

    asyncio.run(run())


def test_set_visibility_emits_inventory_immediately() -> None:
    async def run() -> None:
        store, sessions, frames = await _registry()
        try:
            await sessions.open(HOST, "visibility", visibility="visible")
            await _prime(sessions)
            await sessions.set_visibility(HOST, "visibility", "hidden")
            assert frames[0]["sessions"][0]["visibility"] == "hidden"
            await _one_immediate_frame(sessions, frames)
        finally:
            store.stop()

    asyncio.run(run())


def test_reparent_emits_inventory_immediately() -> None:
    async def run() -> None:
        store, sessions, frames = await _registry()
        try:
            await sessions.open(HOST, "old", visibility="visible")
            await sessions.open(HOST, "new", visibility="visible")
            await sessions.open(HOST, "child", visibility="visible", parent_stream_id=f"{HOST}:old")
            await _prime(sessions)
            await sessions.reparent(
                HOST, "child", f"{HOST}:new",
                auth_context={"token_verified": True, "stream_id": f"{HOST}:old"},
            )
            child = next(row for row in frames[0]["sessions"] if row["stream_id"] == f"{HOST}:child")
            assert child["parent_stream_id"] == f"{HOST}:new"
            await _one_immediate_frame(sessions, frames)
        finally:
            store.stop()

    asyncio.run(run())


def test_reparent_children_handoff_emits_inventory_immediately() -> None:
    async def run() -> None:
        store, sessions, frames = await _registry()
        try:
            await sessions.open(HOST, "old", visibility="visible")
            await sessions.open(HOST, "new", visibility="visible")
            await sessions.open(HOST, "child", visibility="visible", parent_stream_id=f"{HOST}:old")
            await _prime(sessions)
            assert await sessions.reparent_children(f"{HOST}:old", f"{HOST}:new") == 1
            assert len(frames) == 1
            await _one_immediate_frame(sessions, frames)
            assert await sessions.reparent_children(f"{HOST}:old", f"{HOST}:new") == 0
            assert len(frames) == 1
        finally:
            store.stop()

    asyncio.run(run())
