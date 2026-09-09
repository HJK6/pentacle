"""Unit tests for the daemon-side retention cadence (`retention.RetentionJob`).

These drive the job through a running `Store` — the passes must work when they run
as submitted callables on the store's worker thread, which is the only way the
daemon ever executes them. `run_pass()` is the forced trigger, so nothing here
waits out a cadence.

The `sessions` table comes from the store's DDL; the event-tail fixture is
created directly so the test remains self-contained.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import sqlite3
from pathlib import Path

import pytest

from retention import (  # noqa: E402
    DEFAULT_INTERVAL_S,
    PassResult,
    RetentionConfig,
    RetentionJob,
    SCHEDULE_RETENTION_BATCH,
    SCHEDULE_RETENTION_DAYS,
)
from store import Store  # noqa: E402

# The event-tail DDL is kept local so the fixture can run against a fresh store.
# IF NOT EXISTS makes the seed a no-op when the store already owns the table.
EVENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS session_event_tail (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id TEXT NOT NULL,
    session_created_at TEXT NOT NULL DEFAULT '',
    event_key TEXT NOT NULL,
    event_json TEXT NOT NULL,
    event_ts TEXT,
    recorded_at REAL NOT NULL,
    identity TEXT,
    UNIQUE(stream_id, session_created_at, event_key)
);
CREATE INDEX IF NOT EXISTS idx_session_event_tail_stream_event
    ON session_event_tail(stream_id, session_created_at, event_id DESC);
"""

CREATED_AT = "2026-08-01T00:00:00Z"
HOST = "hosta"

    # Large enough that archiving it leaves space a VACUUM visibly reclaims.
PAYLOAD = "x" * 256


def sid(name: str) -> str:
    return f"{HOST}:{name}"


def seed(
    db: Path,
    *,
    open_names: list[str],
    terminal_names: list[str],
    events_per_session: int = 0,
) -> None:
    """Build a fixture DB: sessions through the real store, events directly."""

    async def _rows() -> None:
        store = Store(str(db))
        store.start()
        try:
            for name in open_names:
                await store.open_session(
                    HOST, name, created_at=CREATED_AT, visibility="visible"
                )
            for name in terminal_names:
                await store.open_session(
                    HOST, name, created_at=CREATED_AT, visibility="visible"
                )
                await store.update_session(HOST, name, status="closed", closed_at=CREATED_AT)
        finally:
            store.stop()

    asyncio.run(_rows())

    conn = sqlite3.connect(db)
    try:
        conn.executescript(EVENT_SCHEMA_SQL)
        if events_per_session:
            conn.executemany(
                "INSERT INTO session_event_tail (stream_id, session_created_at, event_key,"
                " event_json, event_ts, recorded_at) VALUES (?,?,?,?,?,?)",
                [
                    (sid(name), CREATED_AT, f"e{i:06d}", PAYLOAD, CREATED_AT, float(i))
                    for name in [*open_names, *terminal_names]
                    for i in range(events_per_session)
                ],
            )
        conn.commit()
    finally:
        conn.close()


def run_pass(db: Path, cfg: RetentionConfig, *, pending: int | None = None) -> PassResult:
    """One forced pass against `db` through a real, running store."""

    async def _go() -> PassResult:
        store = Store(str(db))
        store.start()
        try:
            if pending is not None:
                # Stand in for RPCs queued behind the job on the store thread.
                store.pending = lambda: pending  # type: ignore[method-assign]
            return await RetentionJob(store, cfg).run_pass()
        finally:
            store.stop()

    return asyncio.run(_go())


def counts(db: Path) -> tuple[int, int]:
    """(hot session rows, hot event rows)."""
    conn = sqlite3.connect(db)
    try:
        s = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        e = conn.execute("SELECT COUNT(*) FROM session_event_tail").fetchone()[0]
        return s, e
    finally:
        conn.close()


def archived(db: Path, table: str) -> int:
    archive = db.parent / "sessions_archive.db"
    if not archive.exists():
        return 0
    conn = sqlite3.connect(archive)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def utc_days(days: float) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(days=days)
    ).isoformat(timespec="microseconds").replace("+00:00", "Z")


def seed_schedule_row(
    conn: sqlite3.Connection,
    schedule_id: str,
    *,
    state: str,
    terminal_days: float | None,
    prompt_blob_id: str | None = None,
    with_dispatch: bool = True,
    receipt_retain_days: float = -1,
) -> None:
    request_id = f"request-{schedule_id}"
    created = utc_days(-90)
    terminal_at = utc_days(terminal_days) if terminal_days is not None else None
    conn.execute(
        "INSERT INTO v2_schedules ("
        "schedule_id,request_id,owner_stream_id,owner_spec_ids_json,owner_spec_provenance_json,"
        "target_host,requested_provider,requested_model,requested_effort,resolved_provider,"
        "resolved_model,resolved_effort,fires_at_utc,state,generation,prompt_sha256,prompt_blob_id,"
        "created_at,updated_at,terminal_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            schedule_id, request_id, "hosta:owner", "[]", "[]", "hosta", "codex",
            "model-b", "high", "codex", "model-b", "high",
            utc_days(60), state, 1, prompt_blob_id, prompt_blob_id,
            created, terminal_at or created, terminal_at,
        ),
    )
    if with_dispatch:
        conn.execute(
            "INSERT INTO v2_schedule_dispatches ("
            "schedule_id,generation,spawn_key,phase,spawn_request_id,prepared_at,evidence_json) "
            "VALUES (?,1,?,'spawn_delivered',?,?, '{}')",
            (schedule_id, f"schedule:{schedule_id}:1", f"schedule-spawn:{schedule_id}:1", created),
        )
    conn.execute(
        "INSERT INTO v2_operation_receipts ("
        "receipt_id,request_id,phase,surface,verb,actor_kind,actor_id,canonical_payload_sha256,"
        "target_id,measured_state_json,result_json,measured_at,retain_until) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"receipt-{schedule_id}", request_id, "row_committed", "schedule",
            "schedule.insert", "seat", "hosta:owner", "0" * 64, schedule_id,
            "{}", "{}", created, utc_days(receipt_retain_days),
        ),
    )


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "sessions.db"
    seed(
        path,
        open_names=["open-1", "open-2"],
        terminal_names=["closed-1", "closed-2", "closed-3"],
        events_per_session=10,
    )
    return path


def cfg(**kw) -> RetentionConfig:
    base = dict(tail_keep=5, batch_size=500, max_rows_per_pass=10_000)
    base.update(kw)
    return RetentionConfig(**base)


# --------------------------------------------------------------------------- #
# the passes, through the store
# --------------------------------------------------------------------------- #


def test_pass_archives_terminal_rows_and_their_events(db: Path) -> None:
    result = run_pass(db, cfg())

    assert result.sessions_moved == 3
    # 3 terminal sessions x 10 events move wholesale...
    assert result.events_terminal_moved == 30
    # ...and each of the 2 open sessions is trimmed from 10 to tail_keep=5.
    assert result.events_tail_moved == 10

    hot_sessions, hot_events = counts(db)
    assert hot_sessions == 2
    assert hot_events == 10


def test_pass_migrates_legacy_additive_sessions_archive(db: Path) -> None:
    """A legacy archive gains every current additive session column."""
    source = sqlite3.connect(db)
    try:
        ddl = source.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone()[0]
        source_columns = [row[1] for row in source.execute("PRAGMA table_info(sessions)")]
    finally:
        source.close()
    legacy_ddl = ddl.replace(" title TEXT,\n", "")
    archive = sqlite3.connect(db.parent / "sessions_archive.db")
    try:
        archive.execute(legacy_ddl)
        archive.commit()
    finally:
        archive.close()

    result = run_pass(db, cfg())

    assert result.sessions_moved == 3
    archive = sqlite3.connect(db.parent / "sessions_archive.db")
    try:
        assert [row[1] for row in archive.execute("PRAGMA table_info(sessions)")] == source_columns
    finally:
        archive.close()
    assert archived(db, "sessions") == 3
    assert archived(db, "session_event_tail") == 40


def test_pass_migrates_sessions_archive_missing_additive_columns(db: Path) -> None:
    """The scratch archive may omit newer nullable lifecycle fields."""
    source = sqlite3.connect(db)
    try:
        ddl = source.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone()[0]
        source_info = list(source.execute("PRAGMA table_info(sessions)"))
        source_columns = [row[1] for row in source_info]
    finally:
        source.close()

    missing_columns = [row for row in source_info if row[1] in {"close_kind", "bootstrap_state", "title"}]
    assert len(missing_columns) == 3
    # Match the tail columns wherever the DDL happens to wrap: ALTER-added
    # columns are appended inline, so the leading whitespace is not stable.
    tail_definition = ", ".join(
        f"{row[1]} {row[2]}" for row in missing_columns
    ) + ","
    assert tail_definition in ddl
    legacy_ddl = ddl.replace(tail_definition, "", 1)
    assert "no_watch INTEGER NOT NULL DEFAULT 0," in legacy_ddl
    legacy_ddl = legacy_ddl.replace("no_watch INTEGER NOT NULL DEFAULT 0,", "", 1)

    archive = sqlite3.connect(db.parent / "sessions_archive.db")
    try:
        archive.execute(legacy_ddl)
        archive.commit()
        assert len(list(archive.execute("PRAGMA table_info(sessions)"))) == len(source_columns) - 4
    finally:
        archive.close()

    result = run_pass(db, cfg())

    assert result.sessions_moved == 3
    archive = sqlite3.connect(db.parent / "sessions_archive.db")
    try:
        assert [row[1] for row in archive.execute("PRAGMA table_info(sessions)")] == source_columns
        tail_columns = [row[1] for row in missing_columns]
        tail_select = ", ".join(f'"{column}"' for column in tail_columns)
        assert archive.execute(f"SELECT {tail_select} FROM sessions").fetchall() == [
            (None, None, None),
        ] * 3
    finally:
        archive.close()
    assert archived(db, "sessions") == 3


def test_open_sessions_and_their_newest_events_are_untouched(db: Path) -> None:
    run_pass(db, cfg())

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT session_name, status FROM sessions ORDER BY session_name"
        ).fetchall()
        assert rows == [("open-1", "open"), ("open-2", "open")]
        # The tail cap keeps the NEWEST rows: events 5..9 of each open session.
        kept = conn.execute(
            "SELECT event_key FROM session_event_tail WHERE stream_id=? ORDER BY event_id",
            (sid("open-1"),),
        ).fetchall()
        assert [k for (k,) in kept] == [f"e{i:06d}" for i in range(5, 10)]
    finally:
        conn.close()


def test_a_pass_without_work_changes_nothing(db: Path) -> None:
    run_pass(db, cfg())
    second = run_pass(db, cfg())

    assert second.sessions_moved == 0
    assert second.events_moved == 0
    assert counts(db) == (2, 10)


# --------------------------------------------------------------------------- #
# per-pass cap (loop rule 2)
# --------------------------------------------------------------------------- #


def test_cap_leaves_the_remainder_for_the_next_pass(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    seed(path, open_names=["open-1"], terminal_names=[f"closed-{i}" for i in range(12)])

    # Cap of 5 with a batch of 2: the pass must stop mid-table, not drain it.
    first = run_pass(path, cfg(max_rows_per_pass=5, batch_size=2))
    assert first.sessions_moved == 5
    assert first.capped is True
    hot, _ = counts(path)
    assert hot == 8  # 1 open + 7 terminal still waiting

    second = run_pass(path, cfg(max_rows_per_pass=5, batch_size=2))
    assert second.sessions_moved == 5
    assert counts(path)[0] == 3

    third = run_pass(path, cfg(max_rows_per_pass=5, batch_size=2))
    assert third.sessions_moved == 2
    assert third.capped is False
    assert counts(path)[0] == 1  # only the open session remains
    assert archived(path, "sessions") == 12


def test_cap_is_shared_across_the_sessions_and_events_passes(db: Path) -> None:
    result = run_pass(db, cfg(max_rows_per_pass=4, batch_size=2))
    # 3 session rows spend 3 of the 4-row budget, leaving 1 for events.
    assert result.sessions_moved == 3
    assert result.events_moved == 1
    assert result.capped is True


# --------------------------------------------------------------------------- #
# VACUUM gating
# --------------------------------------------------------------------------- #


def test_vacuum_is_skipped_while_requests_are_queued(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    seed(path, open_names=["open-1"], terminal_names=["closed-1"], events_per_session=2000)
    before = path.stat().st_size

    # Threshold of 1 byte makes the reclaim test pass, so the ONLY thing that
    # can stop the VACUUM is the queue check.
    result = run_pass(path, cfg(vacuum_min_bytes=1), pending=1)

    assert result.vacuumed is False
    assert result.vacuum_skip_reason == "requests_queued"
    assert result.reclaimable_bytes > 0
    assert path.stat().st_size >= before  # not rewritten, so not shrunk


def test_vacuum_runs_when_idle_and_over_threshold(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    seed(path, open_names=["open-1"], terminal_names=["closed-1"], events_per_session=2000)
    before = path.stat().st_size

    result = run_pass(path, cfg(vacuum_min_bytes=1), pending=0)

    assert result.vacuumed is True
    assert path.stat().st_size < before


def test_vacuum_is_skipped_when_there_is_little_to_reclaim(db: Path) -> None:
    # The default 64 MiB threshold dwarfs this fixture's freelist.
    result = run_pass(db, cfg(), pending=0)

    assert result.vacuumed is False
    assert result.vacuum_skip_reason == "below_threshold"


# --------------------------------------------------------------------------- #
# guards
# --------------------------------------------------------------------------- #


def test_in_memory_store_is_a_noop() -> None:
    async def _go() -> PassResult:
        store = Store(":memory:")
        store.start()
        try:
            return await RetentionJob(store, cfg()).run_pass()
        finally:
            store.stop()

    result = asyncio.run(_go())
    assert result.skipped == "in_memory_db"
    assert result.sessions_moved == 0


def test_a_db_without_an_event_table_still_archives_sessions(tmp_path: Path) -> None:
    """A fresh v2 DB has no v1 `session_event_tail`; that is a skip, not a crash."""
    path = tmp_path / "sessions.db"

    async def _rows() -> None:
        store = Store(str(path))
        store.start()
        try:
            await store.open_session(HOST, "closed-1", created_at=CREATED_AT, visibility="visible")
            await store.update_session(HOST, "closed-1", status="closed")
        finally:
            store.stop()

    asyncio.run(_rows())

    result = run_pass(path, cfg())
    assert result.sessions_moved == 1
    assert result.events_moved == 0


def test_schedule_retention_locked_numerics_order_counters_and_no_blob_delete(
    tmp_path: Path,
) -> None:
    assert DEFAULT_INTERVAL_S == 6 * 3600
    assert SCHEDULE_RETENTION_DAYS == 30
    assert SCHEDULE_RETENTION_BATCH == 500
    path = tmp_path / "sessions.db"
    store = Store(str(path))
    store.start()
    store.stop()
    blob_id = "a" * 64
    blob_root = tmp_path / "shared-blobs"
    blob_path = blob_root / blob_id[:2] / blob_id
    blob_path.parent.mkdir(parents=True)
    blob_bytes = b"shared content belongs to each blob fixture"
    blob_path.write_bytes(blob_bytes)
    with sqlite3.connect(path) as conn:
        seed_schedule_row(
            conn, "sched-old", state="fired", terminal_days=-31,
            prompt_blob_id=blob_id,
        )
        seed_schedule_row(
            conn, "sched-grace", state="failed", terminal_days=-29,
        )
        seed_schedule_row(
            conn, "sched-far-pending", state="pending", terminal_days=None,
            with_dispatch=False,
        )

    traces: list[str] = []

    async def purge() -> PassResult:
        running = Store(str(path))
        running.start()
        try:
            def install_trace(conn: sqlite3.Connection) -> None:
                conn.set_trace_callback(traces.append)
            await running.submit(install_trace)
            return await RetentionJob(
                running,
                cfg(blob_root=blob_root),
            ).run_pass()
        finally:
            running.stop()

    result = asyncio.run(purge())
    assert result.schedule_dispatches_purged == 1
    assert result.schedules_purged == 1
    assert result.schedule_receipts_purged == 1
    assert result.schedule_blobs_unreclaimed == 1
    assert result.schedule_blob_bytes_unreclaimed == len(blob_bytes)
    delete_statements = [
        statement for statement in traces
        if statement.lstrip().upper().startswith("DELETE FROM V2_")
    ]
    delete_tables = [statement.split()[2].lower() for statement in delete_statements]
    assert delete_tables.index("v2_schedule_dispatches") < delete_tables.index("v2_schedules")
    assert delete_tables.index("v2_schedules") < delete_tables.index("v2_operation_receipts")
    with sqlite3.connect(path) as conn:
        schedules = {
            row[0] for row in conn.execute("SELECT schedule_id FROM v2_schedules")
        }
        receipts = {
            row[0] for row in conn.execute("SELECT receipt_id FROM v2_operation_receipts")
        }
    assert schedules == {"sched-grace", "sched-far-pending"}
    assert receipts == {"receipt-sched-grace", "receipt-sched-far-pending"}
    assert blob_path.read_bytes() == blob_bytes


def test_schedule_retention_caps_each_class_at_500(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    store = Store(str(path))
    store.start()
    store.stop()
    with sqlite3.connect(path) as conn:
        for index in range(501):
            seed_schedule_row(
                conn, f"sched-cap-{index:03d}", state="cancelled", terminal_days=-31,
            )

    first = run_pass(path, cfg())
    assert (
        first.schedule_dispatches_purged,
        first.schedules_purged,
        first.schedule_receipts_purged,
    ) == (500, 500, 500)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM v2_schedule_dispatches").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM v2_schedules").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM v2_operation_receipts").fetchone()[0] == 1
    second = run_pass(path, cfg())
    assert (
        second.schedule_dispatches_purged,
        second.schedules_purged,
        second.schedule_receipts_purged,
    ) == (1, 1, 1)


def test_expired_schedule_receipt_is_pinned_while_target_row_exists(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    store = Store(str(path))
    store.start()
    store.stop()
    with sqlite3.connect(path) as conn:
        seed_schedule_row(
            conn,
            "sched-more-than-30d-out",
            state="pending",
            terminal_days=None,
            with_dispatch=False,
            receipt_retain_days=-1,
        )
    result = run_pass(path, cfg())
    assert result.schedule_receipts_purged == 0
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM v2_operation_receipts "
            "WHERE target_id='sched-more-than-30d-out'"
        ).fetchone()[0] == 1
