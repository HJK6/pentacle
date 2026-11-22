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
    # Status freshness adds exactly one report field above the retained floor.
    statement = "ALTER TABLE v2_reports ADD COLUMN status_freshness TEXT"
    assert store_source.count(statement) == 1
    store_source = store_source.replace(statement, "")
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
    # Named accounts add exactly one nullable binding above the retained floor.
    statement = "ALTER TABLE sessions ADD COLUMN account_binding TEXT"
    assert store_source.count(statement) == 1
    store_source = store_source.replace(statement, "")
    # Lane work assignment adds one nullable session field above the floor.
    statement = "ALTER TABLE sessions ADD COLUMN assigned_spec_id TEXT"
    assert store_source.count(statement) == 1
    store_source = store_source.replace(statement, "")
    # Usage accounting adds one report snapshot above the retained floor.
    statement = "ALTER TABLE v2_reports ADD COLUMN usage_snapshot TEXT"
    assert store_source.count(statement) == 1
    store_source = store_source.replace(statement, "")
    # Voice-input lane adds one additive receipt field (meta.voice={duration_s})
    # above the retained floor; historical receipt migrations stay forbidden.
    statement = "ALTER TABLE v2_send_receipts ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}'"
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
    assert {
        "sessions", "v2_session_generations", "v2_schedules", "v2_recurring_schedules",
    } <= set(_table_counts(path))


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


def test_pre_account_database_opens_additively_and_reopens_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "pre-account.db"

    async def seed() -> None:
        store = Store(str(path))
        store.start()
        try:
            await store.open_session("hosta", "existing", provider="codex",
                                     created_at="2026-09-03T00:00:00Z", title="Keep this session")
            await store.put("existing-key", "existing-value")
        finally:
            store.stop()

    asyncio.run(seed())
    # Synthetic retained-floor database with only this feature's schema absent.
    # No production DB, credentials or historical repair ladder is involved.
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE sessions DROP COLUMN account_binding")
        account_tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'v2_account_*'")]
        assert set(account_tables) == {
            "v2_account_bindings", "v2_account_defaults", "v2_account_session_bindings",
            "v2_account_observations", "v2_account_failures", "v2_account_recovery",
            "v2_account_health_revalidations",
        }
        for table in account_tables:
            conn.execute(f'DROP TABLE "{table}"')
        columns = [row[1] for row in conn.execute("PRAGMA table_info(sessions)")]
        assert "account_binding" not in columns
        original_row = conn.execute("SELECT * FROM sessions").fetchone()
    original_counts = _table_counts(path)
    original_generations = _generations(path)
    original_version = _version(path)

    async def reopen() -> None:
        store = Store(str(path))
        store.start()
        try:
            assert store.schedule_schema_health == "ok"
            row = await store.fetch_session("hosta", "existing")
            assert row["account_binding"] is None
            assert row["title"] == "Keep this session"
            assert await store.get("existing-key") == "existing-value"
        finally:
            store.stop()

    for _ in range(2):
        asyncio.run(reopen())
        with sqlite3.connect(path) as conn:
            fields = {row[1]: row for row in conn.execute("PRAGMA table_info(sessions)")}
            assert set(fields) == set(columns) | {"account_binding"}
            # TEXT, nullable, no default: legacy rows retain unknown binding.
            assert fields["account_binding"][2:5] == ("TEXT", 0, None)
            projection = ",".join(f'"{column}"' for column in columns)
            assert conn.execute(f"SELECT {projection} FROM sessions").fetchone() == original_row
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        counts = _table_counts(path)
        assert {table: counts[table] for table in original_counts} == original_counts
        assert set(counts) - set(original_counts) == set(account_tables)
        assert all(counts[table] == 0 for table in account_tables)
        assert _generations(path) == original_generations
        assert _version(path) == original_version == SCHEMA_VERSION


def test_store_activity_and_owed_indexes_on_existing_database(tmp_path, monkeypatch, request, capsys):
    import json
    from types import SimpleNamespace

    from assistant_owed import OwedItems
    from store_routing import _insert_outbound_notice_conn

    path = tmp_path / "store.db"
    store = Store(str(path))
    store.start()
    store.stop()

    indexes = {"idx_sessions_open", "idx_session_event_tail_activity", "idx_v2_outbound_delivered_owed"}
    stream = "fixture:seat"
    birth = "2025-01-01T00:00:00Z"
    winner = "2099-01-01T07:00:00-05:00"
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        receipt = {"sqlite_version": sqlite3.sqlite_version,
                   "sqlite_source_id": conn.execute("SELECT sqlite_source_id()").fetchone()[0]}
        for name, value in receipt.items():
            request.node.user_properties.append((name, value))
        with capsys.disabled():
            print("STORE_SQLITE_RUNTIME " + json.dumps(receipt, sort_keys=True), flush=True)

        # Model a pre-index writer on the existing Store schema. A bare julianday
        # expression index would reject the clock-relative row during the next boot.
        for name in sorted(indexes):
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        conn.execute("""INSERT INTO sessions(host,session_name,provider,visibility,status,created_at)
                        VALUES('fixture','seat','codex','hidden','open',?)""", (birth,))
        for stamp in ("now", winner):
            conn.execute("""INSERT INTO session_event_tail
                            (stream_id,session_created_at,event_key,event_json,event_ts,recorded_at)
                            VALUES(?,?,?,'{"kind":"TOOL"}',?,1)""", (stream, birth, stamp, stamp))
        conn.execute("""INSERT INTO v2_assistant_owed
                        (owed_id,stream_id,label,source_kind,source_id,source_ids,state,
                         actor_stream_id,actor_generation,created_at,source_at)
                        VALUES('episode',?,'test','message','message','[]','closed',?,'generation',?,?)""",
                     (stream, stream, birth, birth))
        _insert_outbound_notice_conn(conn, notice_id="notice", kind="owed_due", dedupe_key="notice",
                                     recipient_stream_id=stream, tell_id="notice", body="test",
                                     source_stream_id=stream, episode_id="episode", created_at=birth)

    # Capacity state is unrelated to activity projection and lives in kv.
    monkeypatch.setattr("store_capacity_retry.load", lambda conn, sid: None)
    owed = OwedItems(SimpleNamespace(store=store, config=SimpleNamespace(stream_id=stream, name="fixture"),
                                     _env_binding=dict))

    async def migrate_and_write():
        existing = await store.submit(lambda c: {row[0] for row in c.execute(
            "SELECT name FROM sqlite_schema WHERE type='index'")})
        assert indexes <= existing, f"Store boot omitted indexes: {sorted(indexes - existing)}"
        assert await store.claim_outbound_notice("notice", owner="fixture-owner", lease_s=60, force=True)
        assert await store.complete_outbound_notice("notice", owner="fixture-owner")

        statements = []
        await store.submit(lambda c: c.set_trace_callback(statements.append))
        try:
            seat, = await store.list_open_sessions_with_event_summary()
            assert (seat["stream_id"], seat["last_event_at"]) == (stream, winner)
            await owed.tick()
        finally:
            await store.submit(lambda c: c.set_trace_callback(None))

        inventory, = (sql for sql in statements if "AS _bootstrap_event_seen" in sql)
        repair, = (sql for sql in statements if "UPDATE v2_assistant_owed" in sql and "min(delivered_at)" in sql)
        for sql, expected in ((inventory, ("idx_sessions_open", "idx_session_event_tail_activity")),
                              (repair, ("idx_v2_outbound_delivered_owed",))):
            plan = await store.submit(lambda c: [row[3] for row in c.execute("EXPLAIN QUERY PLAN " + sql)])
            for name in expected:
                assert any(name in line.split() for line in plan), (name, plan)
        assert await store.append_session_event(stream, {"kind": "TOOL", "timestamp": "2100-01-01"},
                                                identity="later", limit=500)

    store.start()
    try:
        asyncio.run(migrate_and_write())
    finally:
        store.stop()
    store.start()
    try:
        seat, = asyncio.run(store.list_open_sessions_with_event_summary())
        assert seat["last_event_at"] == "2100-01-01"
        with sqlite3.connect(path) as conn:
            ack, delivered = conn.execute("""SELECT o.due_notice_at,n.delivered_at FROM v2_assistant_owed o
                                            JOIN v2_outbound_notices n ON o.owed_id=n.episode_id
                                            WHERE o.stream_id=n.source_stream_id AND n.notice_id='notice'""").fetchone()
            assert delivered and ack == delivered
    finally:
        store.stop()
