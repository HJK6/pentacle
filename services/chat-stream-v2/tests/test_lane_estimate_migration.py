"""CHECK widening retains every receipt and remains atomic/idempotent."""
from dataclasses import asdict
import json
import sqlite3

import pytest

from store_work_lane_episodes import DDL, LaneEpisodeFact, ensure_work_lane_episodes_schema

LEGACY = DDL.replace(",'estimate_missing'", "").replace(",'lead_lost'", "")


def rows(conn):
    return list(conn.execute("SELECT * FROM v2_work_lane_episodes ORDER BY rowid"))


def insert(conn, kind, seq=1, *, cleared=None, emitted=None, legacy=False):
    identity = f"wl-demo:{kind}:{seq}"
    payload = asdict(LaneEpisodeFact("wl-demo", kind, identity, "2026-10-09T00:00:00Z"))
    if legacy:
        payload.pop("condition")
        payload.pop("principal")
    conn.execute("INSERT INTO v2_work_lane_episodes VALUES(?,?,?,?,?,?,?,?)",
                 (identity, "wl-demo", kind, seq, payload["opened_at"], cleared, emitted,
                  json.dumps(payload, sort_keys=True)))


def test_legacy_upgrade_reopen_indexes_and_fact_defaults(tmp_path):
    path = tmp_path / "episodes.db"
    with sqlite3.connect(path) as conn:
        conn.execute(LEGACY)
        conn.execute("CREATE UNIQUE INDEX idx_v2_work_lane_episodes_open ON v2_work_lane_episodes(lane_id,kind) WHERE cleared_at IS NULL")
        conn.execute("CREATE INDEX custom_episode_ref ON v2_work_lane_episodes(emitted_ref)")
        insert(conn, "completed", cleared="2026-10-10", emitted="proved:old", legacy=True)
        insert(conn, "completed", seq=2, legacy=True)
        insert(conn, "stale", emitted="proved:stale")
        before = rows(conn)
        ensure_work_lane_episodes_schema(conn)
        assert rows(conn) == before
        fact = LaneEpisodeFact(**json.loads(before[0][-1]))
        assert fact.principal == "daemon:work-lanes" and fact.condition == "active"
        insert(conn, "estimate_missing")
        after = rows(conn)
    with sqlite3.connect(path) as conn:
        ensure_work_lane_episodes_schema(conn)
        ensure_work_lane_episodes_schema(conn)
        assert rows(conn) == after
        assert {r[1] for r in conn.execute("PRAGMA index_list(v2_work_lane_episodes)")} >= {
            "idx_v2_work_lane_episodes_open", "custom_episode_ref"}
        with pytest.raises(sqlite3.IntegrityError):
            insert(conn, "estimate_missing", seq=2)
        conn.rollback()
        assert rows(conn) == after


def test_migration_failure_restores_table_and_rows_atomically():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(LEGACY)
        conn.execute("CREATE INDEX custom_episode_ref ON v2_work_lane_episodes(emitted_ref)")
        insert(conn, "stale")
        conn.commit()
        before = rows(conn)
        original = conn.execute("SELECT sql FROM sqlite_master WHERE name='v2_work_lane_episodes'").fetchone()[0]
        # Fail late, after copy/drop/rename, while restoring explicit indexes.
        def authorize(action, one, two, db, source):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_INDEX and one == "custom_episode_ref" else sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        with pytest.raises(sqlite3.DatabaseError):
            ensure_work_lane_episodes_schema(conn)
        conn.set_authorizer(None)
        assert rows(conn) == before
        assert conn.execute("SELECT sql FROM sqlite_master WHERE name='v2_work_lane_episodes'").fetchone()[0] == original
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='v2_work_lane_episodes_replacement'").fetchone() is None
        ensure_work_lane_episodes_schema(conn)
        assert rows(conn) == before
    finally:
        conn.close()
