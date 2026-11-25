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
    assert (result.events_moved, result.events_retained) == (0, 0)
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


# --------------------------------------------------------------------------- #
# nothing read before a transaction is trusted inside it
# --------------------------------------------------------------------------- #


SESSION_SELECTION = ("SELECT rowid,", "FROM sessions WHERE")
EVENT_SELECTION = ('FROM main."session_event_tail" WHERE', "temp.hot_streams")


def run_pass_between(db: Path, selection: tuple, action) -> r.PassResult:
    """A real pass; `action` runs once, after the first `selection` statement
    completes and before the next BEGIN takes any lock."""
    import asyncio
    from store import Store
    state, errors = {}, []

    def trace(statement):
        try:
            if "done" not in state and all(part in statement for part in selection):
                state["selected"] = True
            elif statement.startswith("BEGIN") and state.pop("selected", False):
                state["done"] = True
                action()
        except BaseException as error:  # a trace callback cannot raise into SQLite
            errors.append(error)

    async def go():
        store = Store(str(db))
        store.start()
        try:
            await store.submit(lambda conn: conn.set_trace_callback(trace))
            return await r.RetentionJob(store, cfg()).run_pass()
        finally:
            store.stop()
    result = asyncio.run(go())
    assert state.get("done") and not errors, (state, errors)
    return result


def test_a_session_another_pass_moved_after_selection_keeps_its_archive_copy(terminal):
    session = rows(terminal, "sessions")
    events = rows(terminal, r.EVENT_TABLE)
    run_pass_between(terminal, SESSION_SELECTION, lambda: run_pass(terminal, cfg()))
    assert rows(terminal, "sessions") == rows(terminal, r.EVENT_TABLE) == []
    assert rows(archive_of(terminal), "sessions") == session, "the archived session was deleted"
    assert rows(archive_of(terminal), r.EVENT_TABLE, "1 ORDER BY event_id") == events


def test_a_session_reopened_after_selection_is_not_archived(terminal):
    reopen = "UPDATE sessions SET status='open' WHERE session_name='closed-1'"
    result = run_pass_between(terminal, SESSION_SELECTION, lambda: write(terminal, reopen))
    assert result.sessions_moved == 0
    assert [row["status"] for row in rows(terminal, "sessions")] == ["open"]
    assert rows(archive_of(terminal), "sessions") == []


def test_an_exact_copy_whose_identity_another_archived_event_holds_stays_hot(terminal):
    write(terminal, "UPDATE session_event_tail SET identity='same' WHERE event_id=1")
    first, second = rows(terminal, r.EVENT_TABLE, "1 ORDER BY event_id")
    other = dict(first, event_id=99, event_key="other-key", event_json="different")
    preseed_archive(terminal, first)
    preseed_archive(terminal, other)
    result = run_pass(terminal, cfg())
    assert (result.events_moved, result.events_retained) == (1, 1)
    assert rows(terminal, r.EVENT_TABLE) == [first]
    assert rows(archive_of(terminal), r.EVENT_TABLE, "1 ORDER BY event_id") == [first, second, other]


def test_an_identity_changed_after_selection_is_checked_on_the_current_row(terminal):
    first, second = rows(terminal, r.EVENT_TABLE, "1 ORDER BY event_id")
    other = dict(first, event_id=99, event_key="other-key", identity="archived", event_json="different")
    preseed_archive(terminal, other)
    change = "UPDATE session_event_tail SET identity='archived' WHERE event_id=1"
    result = run_pass_between(terminal, EVENT_SELECTION, lambda: write(terminal, change))
    assert (result.events_moved, result.events_retained) == (1, 1)
    assert rows(terminal, r.EVENT_TABLE) == [dict(first, identity="archived")]
    assert rows(archive_of(terminal), r.EVENT_TABLE, "1 ORDER BY event_id") == [second, other]


def test_a_collision_archived_after_the_copy_keeps_the_event_hot(terminal, monkeypatch):
    write(terminal, "UPDATE session_event_tail SET identity='same' WHERE event_id=1")
    first, second = rows(terminal, r.EVENT_TABLE, "1 ORDER BY event_id")
    other = dict(first, event_id=99, event_key="other-key", event_json="different")
    monkeypatch.setattr(r, "history_checkpoint", at("archive_committed", r.EVENT_TABLE,
                                                    lambda: preseed_archive(terminal, other)))
    result = run_pass(terminal, cfg())
    assert rows(terminal, r.EVENT_TABLE) == [first], "an event whose identity another archived event holds was deleted"
    assert (result.events_moved, result.events_retained) == (1, 1)


def snapshot_then(db: Path, event_stage: str, action):
    """Save a same-pair archive with the session but no events, then run
    `action(snapshot)` at the event batch's `event_stage`."""
    snapshot = db.parent / "same-pair.db"

    def checkpoint(stage, table, _rows):
        if (stage, table) == ("archive_committed", "sessions"):
            with closing(sqlite3.connect(archive_of(db))) as src, closing(sqlite3.connect(snapshot)) as dst:
                src.backup(dst)
        elif (stage, table) == (event_stage, r.EVENT_TABLE):
            action(snapshot)
    return checkpoint


def refused(db: Path) -> str:
    try:
        run_pass(db, cfg())
    except r.RetentionError as error:
        return str(error)
    return ""


def test_an_archive_replaced_after_verification_keeps_the_hot_rows(terminal, monkeypatch):
    events = rows(terminal, r.EVENT_TABLE)
    monkeypatch.setattr(r, "history_checkpoint", snapshot_then(
        terminal, "verified", lambda snapshot: snapshot.replace(archive_of(terminal))))
    refusal = refused(terminal)
    assert rows(terminal, r.EVENT_TABLE) == events, "hot events deleted against a replaced archive"
    assert rows(archive_of(terminal), r.EVENT_TABLE) == []
    assert "replaced" in refusal


def test_an_archive_rebound_before_the_hot_delete_keeps_the_hot_rows(terminal, monkeypatch):
    events = rows(terminal, r.EVENT_TABLE)

    def rebind(snapshot):
        with closing(sqlite3.connect(terminal, factory=r.HistoryConnection)) as other:
            other.row_factory = sqlite3.Row
            r.bind_history_archive(other, snapshot)
    monkeypatch.setattr(r, "history_checkpoint", snapshot_then(terminal, "archive_committed", rebind))
    refusal = refused(terminal)
    assert rows(terminal, r.EVENT_TABLE) == events, "hot events deleted against an obsolete binding"
    configured = Path(rows(terminal, "v2_send_receipt_sequence")[0]["archive_path"])
    assert configured == terminal.parent / "same-pair.db"
    assert rows(configured, r.EVENT_TABLE) == []
    assert "rebound" in refusal


# --------------------------------------------------------------------------- #
# every refusal at the delete is classified in the pass that makes it
# --------------------------------------------------------------------------- #


COLLIDES = "collides with a different archived event"


@pytest.mark.parametrize("side", ["hot", "archive", "unchanged"])
def test_a_same_id_copy_that_differs_at_the_delete_is_retained_in_this_pass(tmp_path, monkeypatch, caplog, side):
    db = tmp_path / "sessions.db"
    seed(db, open_names=[], terminal_names=["closed-1"], events_per_session=1)
    original = rows(db, r.EVENT_TABLE)[0]
    target = {"hot": db, "archive": archive_of(db)}.get(side)

    def change():
        assert rows(db, r.EVENT_TABLE) == rows(archive_of(db), r.EVENT_TABLE) == [original]
        if target:
            write(target, "UPDATE session_event_tail SET event_json='different' WHERE event_id=?",
                  (original["event_id"],))
    monkeypatch.setattr(r, "history_checkpoint", at("archive_committed", r.EVENT_TABLE, change))
    with caplog.at_level("WARNING", logger=r.log.name):
        result = run_pass(db, cfg())
    warned = [m for m in caplog.messages if COLLIDES in m]
    if target is None:
        assert (result.events_moved, result.events_retained, warned) == (1, 0, [])
        assert (rows(db, r.EVENT_TABLE), rows(archive_of(db), r.EVENT_TABLE)) == ([], [original])
        return
    assert (result.events_moved, result.events_retained) == (0, 1), "a same-id collision was not counted in this pass"
    assert len(warned) == 1
    hot, archived = rows(db, r.EVENT_TABLE), rows(archive_of(db), r.EVENT_TABLE)
    assert len(hot) == len(archived) == 1 and hot[0]["event_json"] != archived[0]["event_json"]
    assert (hot if side == "archive" else archived) == [original]


def test_a_hot_event_deleted_after_the_copy_is_left_archived(terminal, monkeypatch, caplog):
    events = rows(terminal, r.EVENT_TABLE, "1 ORDER BY event_id")
    monkeypatch.setattr(r, "history_checkpoint", at("archive_committed", r.EVENT_TABLE,
                                                    lambda: write(terminal, "DELETE FROM session_event_tail")))
    with caplog.at_level("WARNING", logger=r.log.name):
        result = run_pass(terminal, cfg())
    assert (result.events_moved, result.events_retained) == (0, 0)
    assert not [m for m in caplog.messages if COLLIDES in m]
    assert rows(archive_of(terminal), r.EVENT_TABLE, "1 ORDER BY event_id") == events


def test_an_archived_copy_missing_at_the_delete_is_copied_by_the_next_pass(terminal, monkeypatch, caplog):
    events = rows(terminal, r.EVENT_TABLE, "1 ORDER BY event_id")
    monkeypatch.setattr(r, "history_checkpoint", at("archive_committed", r.EVENT_TABLE,
                                                    lambda: write(archive_of(terminal), "DELETE FROM session_event_tail")))
    with caplog.at_level("WARNING", logger=r.log.name):
        result = run_pass(terminal, cfg())
    assert (result.events_moved, result.events_retained) == (0, 0)
    assert not [m for m in caplog.messages if COLLIDES in m]
    assert rows(terminal, r.EVENT_TABLE, "1 ORDER BY event_id") == events
    monkeypatch.setattr(r, "history_checkpoint", lambda *_: None)
    assert run_pass(terminal, cfg()).events_moved == 2
    assert rows(archive_of(terminal), r.EVENT_TABLE, "1 ORDER BY event_id") == events
