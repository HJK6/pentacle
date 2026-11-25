"""Session and event archival: archive commit -> verify -> revalidated hot delete.

Crash boundaries with real process exit live in `tests/archive_crash/`. These
controls cover what a crash alone cannot show: the hot delete's guards,
identity collisions, termination over retained rows and session name reuse.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import retention as r
from store import LINEAGE_CACHE_VERSION_DDL
from test_retention import CREATED_AT, EVENT_SCHEMA_SQL, HOST, cfg, run_pass, seed, sid


def rows(path: Path, table: str, where: str = "1", args=()) -> list[dict]:
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE {where}", args)]


def write(path: Path, sql: str, args=()) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute(sql, args)
        db.commit()


def archive_of(db: Path) -> Path:
    return r.default_archive_path(db)


def at(stage_name, table, action):
    def checkpoint(stage, actual, _rows):
        if (stage, actual) == (stage_name, table):
            action()
    return checkpoint


@pytest.fixture()
def terminal(tmp_path: Path) -> Path:
    """One retired session with two events; nothing else is eligible."""
    path = tmp_path / "sessions.db"
    seed(path, open_names=[], terminal_names=["closed-1"], events_per_session=2)
    return path


def preseed_archive(db: Path, event: dict) -> None:
    with closing(sqlite3.connect(archive_of(db))) as archive:
        archive.executescript(EVENT_SCHEMA_SQL)
        archive.execute(f"INSERT INTO session_event_tail ({','.join(event)}) "
                        f"VALUES ({','.join('?' for _ in event)})", list(event.values()))
        archive.commit()


# --------------------------------------------------------------------------- #
# the hot delete's guards
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("table", [r.EVENT_TABLE, "sessions"])
def test_hot_row_changed_after_the_copy_is_not_deleted(terminal, monkeypatch, table):
    if table == r.EVENT_TABLE:
        change = "UPDATE session_event_tail SET event_json='changed' WHERE event_key='e000000'"
        where = "event_key='e000000'"
    else:
        change = "UPDATE sessions SET objective='changed' WHERE session_name='closed-1'"
        where = "session_name='closed-1'"
    original = rows(terminal, table, where)
    monkeypatch.setattr(r, "history_checkpoint", at("archive_committed", table, lambda: write(terminal, change)))
    run_pass(terminal, cfg())
    hot = rows(terminal, table, where)
    assert len(hot) == 1 and hot != original, "the changed hot row was deleted"
    assert rows(archive_of(terminal), table, where) == original


def test_event_that_stops_being_eligible_after_the_copy_is_not_deleted(terminal, monkeypatch):
    # The stream is reopened under the same name after the archive copy, so its
    # two events are now an open session's tail (2 <= tail_keep).
    reopen = ("INSERT INTO sessions(host,session_name,status,visibility,created_at) "
              f"VALUES('{HOST}','closed-1','open','visible','{CREATED_AT}')")
    monkeypatch.setattr(r, "history_checkpoint", at("archive_committed", r.EVENT_TABLE,
                                                    lambda: write(terminal, reopen)))
    result = run_pass(terminal, cfg())
    assert result.events_moved == 0
    assert len(rows(terminal, r.EVENT_TABLE)) == 2
    assert len(rows(archive_of(terminal), r.EVENT_TABLE)) == 2


def test_session_that_stops_being_eligible_after_the_copy_is_not_deleted(terminal, monkeypatch):
    # A live child now descends from the retired row, so lineage keeps it hot.
    child = ("INSERT INTO sessions(host,session_name,status,visibility,created_at,parent_stream_id) "
             f"VALUES('{HOST}','child','open','visible','{CREATED_AT}','{sid('closed-1')}')")
    monkeypatch.setattr(r, "history_checkpoint", at("archive_committed", "sessions",
                                                    lambda: write(terminal, child)))
    result = run_pass(terminal, cfg())
    assert result.sessions_moved == 0
    assert len(rows(terminal, "sessions", "session_name='closed-1'")) == 1
    assert len(rows(archive_of(terminal), "sessions", "session_name='closed-1'")) == 1


# --------------------------------------------------------------------------- #
# events keep their identity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("collision", ["event_id", "event_key", "identity"])
def test_conflict_on_the_last_of_two_rows(terminal, collision):
    first, last = rows(terminal, r.EVENT_TABLE, "1 ORDER BY event_id")
    other = dict(last, event_id=last["event_id"] + 100, event_json="earlier occurrence")
    if collision == "event_id":
        other.update(event_id=last["event_id"], event_key="other-key")
    elif collision == "identity":
        write(terminal, "UPDATE session_event_tail SET identity='same' WHERE event_id=?",
              (last["event_id"],))
        last = rows(terminal, r.EVENT_TABLE, "event_id=?", (last["event_id"],))[0]
        other.update(event_key="other-key", identity="same")
    preseed_archive(terminal, other)

    for _ in range(2):
        result = run_pass(terminal, cfg())
        assert result.events_retained == 1
        assert rows(terminal, r.EVENT_TABLE) == [last]
        archived = rows(archive_of(terminal), r.EVENT_TABLE, "1 ORDER BY event_id")
        assert archived == sorted([first, other], key=lambda row: row["event_id"])
        assert result.events_moved == (1 if _ == 0 else 0)


def test_identical_archived_event_is_already_copied(terminal):
    first, _ = rows(terminal, r.EVENT_TABLE, "1 ORDER BY event_id")
    preseed_archive(terminal, first)
    result = run_pass(terminal, cfg())
    assert (result.events_moved, result.events_retained) == (2, 0)
    assert rows(terminal, r.EVENT_TABLE) == []
    assert len(rows(archive_of(terminal), r.EVENT_TABLE)) == 2


@pytest.mark.timeout(20)
def test_batch_of_only_retained_rows_terminates(tmp_path):
    path = tmp_path / "sessions.db"
    seed(path, open_names=[], terminal_names=["closed-1"], events_per_session=5)
    events = rows(path, r.EVENT_TABLE, "1 ORDER BY event_id")
    for event in events[:4]:
        preseed_archive(path, dict(event, event_id=event["event_id"] + 100, event_json="earlier"))
    result = run_pass(path, cfg(batch_size=2))
    assert (result.events_moved, result.events_retained) == (1, 4)
    assert rows(path, r.EVENT_TABLE) == events[:4]


# --------------------------------------------------------------------------- #
# sessions keep today's name-reuse behavior, in the safe order
# --------------------------------------------------------------------------- #


def older_incarnation(db: Path) -> dict:
    newer = rows(db, "sessions", "session_name='closed-1'")[0]
    older = dict(newer, created_at="2026-07-01T00:00:00Z", objective="older incarnation")
    r.ensure_archive(archive_of(db), r.Schema(sqlite3.connect(db)))
    write(archive_of(db), f"INSERT INTO sessions ({','.join(older)}) VALUES "
                          f"({','.join('?' for _ in older)})", list(older.values()))
    return newer


def test_reused_session_name_replaces_the_archived_incarnation(terminal):
    newer = older_incarnation(terminal)
    result = run_pass(terminal, cfg())
    assert result.sessions_moved == 1
    assert rows(terminal, "sessions", "session_name='closed-1'") == []
    assert rows(archive_of(terminal), "sessions", "session_name='closed-1'") == [newer]


@pytest.mark.parametrize("stage", ["archive_precommit", "archive_committed", "verified"])
def test_reused_session_name_is_never_in_neither_file(terminal, monkeypatch, stage):
    newer = older_incarnation(terminal)

    def crash():
        raise RuntimeError("synthetic crash")
    monkeypatch.setattr(r, "history_checkpoint", at(stage, "sessions", crash))
    with pytest.raises(RuntimeError, match="synthetic crash"):
        run_pass(terminal, cfg())
    assert rows(terminal, "sessions", "session_name='closed-1'") == [newer]
    monkeypatch.setattr(r, "history_checkpoint", lambda *_: None)
    run_pass(terminal, cfg())
    assert rows(terminal, "sessions", "session_name='closed-1'") == []
    assert rows(archive_of(terminal), "sessions", "session_name='closed-1'") == [newer]


def test_identical_archived_session_is_a_retry(terminal):
    newer = rows(terminal, "sessions", "session_name='closed-1'")[0]
    r.ensure_archive(archive_of(terminal), r.Schema(sqlite3.connect(terminal)))
    write(archive_of(terminal), f"INSERT INTO sessions ({','.join(newer)}) VALUES "
                                f"({','.join('?' for _ in newer)})", list(newer.values()))
    assert run_pass(terminal, cfg()).sessions_moved == 1
    assert rows(archive_of(terminal), "sessions", "session_name='closed-1'") == [newer]


# --------------------------------------------------------------------------- #
# hot-side effects stay at the hot commit
# --------------------------------------------------------------------------- #


def test_hot_triggers_fire_with_the_hot_delete(terminal, monkeypatch):
    with closing(sqlite3.connect(terminal)) as db:
        for ddl in LINEAGE_CACHE_VERSION_DDL:
            db.execute(ddl)
        db.commit()

    def version():
        return rows(terminal, "_lineage_cache_version")[0]["version"]
    before = version()
    seen = {}
    monkeypatch.setattr(r, "history_checkpoint", lambda stage, table, _rows: seen.setdefault(
        (stage, table), version()) if table == "sessions" else None)
    run_pass(terminal, cfg())
    assert seen[("archive_committed", "sessions")] == seen[("verified", "sessions")] == before
    assert seen[("hot_deleted", "sessions")] == version() > before
