"""The fleet-floor Store boot owns creation, not historical repair."""

from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import sqlite3

from store import SCHEMA_VERSION, Store  # noqa: E402


STORE_TOKENS = (
    "_migrate_legacy_outbound_notices",
    "SCHEDULE_COLUMNS",
    "_normalize_object_sql",
    "_canonical_table_sql",
    "_schedule_schema_healthy",
    "def objects(",
    "have_receipts",
    "ALTER TABLE v2_send_receipts",
    "SAVEPOINT v2_schedule_schema",
    "ROLLBACK TO v2_schedule_schema",
    "rebuild_receipts",
    "preserved_receipts",
    "DROP TABLE v2_operation_receipts",
    "v2_coordination_windows",
    "schedule_columns",
    "ALTER TABLE v2_schedules",
    "UPDATE v2_schedules SET created_by_stream_id",
    "ALTER TABLE v2_stream_reservations",
    "have_out",
    "ALTER TABLE v2_spawn_outcomes",
    "spawn_idempotency",
    "spawn_attempts",
    "send_idempotency",
    "report_idempotency",
    "v2_push_tokens",
    "v2_routing_integrity_rollout",
    "missing_generations",
    "WHERE g.host IS NULL",
    "INSERT OR IGNORE INTO v2_session_generations",
    "have_reports",
    "ALTER TABLE v2_reports",
    "have_sessions",
    "ALTER TABLE sessions",
    'v2_handoff_" "model_change_overrides',
    "v2_lifecycle_audit",
    "v2_daemon_lifecycle",
    "v2_session_lifecycle_history",
    "v2_reconciler_episodes",
)


def _table_counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as conn:
        tables = [
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            for name in tables
        }


def _generations(path: Path) -> list[tuple[str, str, str]]:
    with sqlite3.connect(path) as conn:
        return [
            tuple(str(value) for value in row)
            for row in conn.execute(
                "SELECT host,session_name,generation FROM v2_session_generations "
                "ORDER BY host,session_name"
            )
        ]


def _version(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def test_below_floor_ladder_is_absent_and_readiness_stays_live() -> None:
    store_source = Path(__file__).parents[1].joinpath("store.py").read_text()
    routing_source = Path(__file__).parents[1].joinpath("store_routing.py").read_text()

    # The current D1 feature adds one receipt field; the retired historical ladder remains forbidden.
    store_source = store_source.replace("ALTER TABLE v2_reports ADD COLUMN exchange_json TEXT", "D1 additive receipt")
    # D1b adds provenance above the retained floor; historical migrations stay forbidden.
    store_source = store_source.replace("ALTER TABLE sessions ADD COLUMN objective_source TEXT", "D1b additive provenance")
    # D1b provenance also lands on v2_schedules so a fired schedule keeps its derived source.
    store_source = store_source.replace("ALTER TABLE v2_schedules ADD COLUMN objective_source TEXT", "D1b additive schedule provenance")
    # D2 introduces two additive fields above the existing fleet floor.
    for statement in (
        "ALTER TABLE sessions ADD COLUMN no_watch INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE v2_schedules ADD COLUMN no_watch INTEGER NOT NULL DEFAULT 0",
    ):
        assert store_source.count(statement) == 1
        store_source = store_source.replace(statement, "")
    # Current observer binding is one additive lifecycle field above the floor.
    statement = "ALTER TABLE sessions ADD COLUMN observer_binding TEXT"
    assert store_source.count(statement) == 1
    store_source = store_source.replace(statement, "")
    # Usage accounting adds one report snapshot above the retained floor.
    statement = "ALTER TABLE v2_reports ADD COLUMN usage_snapshot TEXT"
    assert store_source.count(statement) == 1
    store_source = store_source.replace(statement, "")
    for token in STORE_TOKENS:
        assert token not in store_source, token
    assert "_migrate_legacy_outbound_notices" not in routing_source

    main_source = Path(__file__).parents[1].joinpath("main.py").read_text()
    assert (
        'if store.schedule_schema_health == "ok":\n'
        "        window_schedule.mark_store_ready()"
    ) in main_source


def test_fresh_store_reaches_schema_floor_and_schedule_readiness(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    store = Store(str(path))
    store.start()
    try:
        assert store.schedule_schema_health == "ok"
    finally:
        store.stop()

    assert _version(path) == SCHEMA_VERSION
    assert {"sessions", "v2_session_generations", "v2_schedules"} <= set(_table_counts(path))


def test_floor_copy_stamps_version_without_mutating_rows(tmp_path: Path) -> None:
    source = tmp_path / "floor-source.db"
    target = tmp_path / "floor-copy.db"

    async def seed() -> None:
        store = Store(str(source))
        store.start()
        try:
            await store.open_session(
                "hosta", "floor", provider="codex", created_at="2026-09-03T00:00:00Z",
            )
            await store.put("floor-key", "floor-value")
        finally:
            store.stop()

    asyncio.run(seed())
    with sqlite3.connect(source) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copy2(source, target)
    with sqlite3.connect(target) as conn:
        conn.execute("PRAGMA user_version = 0")

    before_counts = _table_counts(target)
    before_generations = _generations(target)
    assert _version(target) == 0

    store = Store(str(target))
    store.start()
    try:
        assert store.schedule_schema_health == "ok"
    finally:
        store.stop()

    assert _version(target) == SCHEMA_VERSION
    assert _table_counts(target) == before_counts
    assert _generations(target) == before_generations
