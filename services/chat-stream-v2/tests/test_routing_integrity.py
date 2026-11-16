"""R25 alert-only regressions for daemon-v2 routing integrity."""

from __future__ import annotations

import asyncio
import sqlite3

from routing_integrity import RoutingIntegrity
from server import Server
from store import Store


HOST = "hosta"
NAME = "codex-routing"



def test_unpark_is_retired_without_token_verification() -> None:
    assert not hasattr(RoutingIntegrity, "unpark")
    assert not Server._token_auth_requested({"type": "unpark"})


def test_fresh_routing_episode_schema_has_no_park_or_ack_columns_or_api(tmp_path) -> None:
    async def run() -> None:
        store = Store(str(tmp_path / "schema.db"))
        store.start()
        try:
            columns = await store.submit(
                lambda conn: {row[1] for row in conn.execute("PRAGMA table_info(v2_routing_integrity)")}
            )
            assert {
                "parked", "parked_by", "acknowledged_at", "acknowledged_by",
                "acknowledgement_reason",
            }.isdisjoint(columns)
            assert not hasattr(store, "acknowledge_routing_integrity")
        finally:
            store.stop()

    asyncio.run(run())


def test_historical_routing_ack_columns_remain_readable_without_startup_migration(tmp_path) -> None:
    async def run() -> None:
        database = tmp_path / "historical-routing.db"
        original = Store(str(database))
        original.start()
        original.stop()
        with sqlite3.connect(database) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(v2_routing_integrity)")}
            for column in ("acknowledged_by", "acknowledgement_reason"):
                if column not in columns:
                    conn.execute(f"ALTER TABLE v2_routing_integrity ADD COLUMN {column} TEXT")

        reopened = Store(str(database))
        reopened.start()
        try:
            columns = await reopened.submit(
                lambda conn: {row[1] for row in conn.execute("PRAGMA table_info(v2_routing_integrity)")}
            )
            assert {"acknowledged_by", "acknowledgement_reason"}.issubset(columns)
            assert await reopened.routing_integrity_episode(f"{HOST}:{NAME}") is None
        finally:
            reopened.stop()

    asyncio.run(run())
