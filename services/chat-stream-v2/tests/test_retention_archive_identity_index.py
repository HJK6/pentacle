"""The archive's optional identity index: shape, placement, use and fallback.

`conflicts` probes the archive for another event holding a moving event's
identity. The index makes that probe a search on all three keys instead of a
read of every archived row of the stream and birth. Correctness never depends
on it: a failed build leaves today's probe.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import retention as r
from store import Store
from test_retention import CREATED_AT, EVENT_SCHEMA_SQL, HOST, cfg, run_pass, seed, sid
from test_retention_archive_order import archive_of, rows, write

INDEX = "idx_archive_session_event_tail_identity"
KEYS = ["stream_id", "session_created_at", "identity"]
SHAPE = {"unique": 0, "partial": 0, "keys": KEYS}
INDEX_WARNING = "identity index not built"
COLLISION_WARNING = "collides with a different archived event"


def attached(archive: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("ATTACH DATABASE ? AS arch", (str(archive),))
    return conn


def index_shape(archive: Path) -> dict | None:
    """The identity index as `PRAGMA arch.index_list` / `index_info` report it."""
    with closing(attached(archive)) as conn:
        for _seq, name, unique, _origin, partial in conn.execute(
                'PRAGMA arch.index_list("session_event_tail")'):
            if name == INDEX:
                keys = [info[2] for info in conn.execute(f'PRAGMA arch.index_info("{name}")')]
                return {"unique": unique, "partial": partial, "keys": keys}
    return None


def schema_version(archive: Path) -> int:
    with closing(attached(archive)) as conn:
        return conn.execute("PRAGMA arch.schema_version").fetchone()[0]


def identify(db: Path) -> None:
    write(db, "UPDATE session_event_tail SET identity='id-' || event_key")


def add_terminal(db: Path, name: str, events: int) -> None:
    """Another retired session with identified events, for a later pass."""
    write(db, "INSERT INTO sessions(host,session_name,status,visibility,created_at,closed_at) "
              f"VALUES('{HOST}','{name}','closed','visible','{CREATED_AT}','{CREATED_AT}')")
    with closing(sqlite3.connect(db)) as conn:
        conn.executemany(
            "INSERT INTO session_event_tail (stream_id, session_created_at, event_key,"
            " event_json, event_ts, recorded_at, identity) VALUES (?,?,?,?,?,?,?)",
            [(sid(name), CREATED_AT, f"{name}-{i}", "{}", CREATED_AT, float(i), f"id-{name}-{i}")
             for i in range(events)])
        conn.commit()


def first_event_copy(monkeypatch, action) -> None:
    """Run `action` once, when the pass first enters event archival."""
    real, state = r.archive_events, {}

    def wrapper(*args, **kwargs):
        if not state:
            state["seen"] = action()
        return real(*args, **kwargs)
    monkeypatch.setattr(r, "archive_events", wrapper)
    return state


def identified_db(directory: Path) -> Path:
    """One retired session with three identified events."""
    directory.mkdir(exist_ok=True)
    path = directory / "sessions.db"
    seed(path, open_names=[], terminal_names=["closed-1"], events_per_session=3)
    identify(path)
    return path


@pytest.fixture()
def identified(tmp_path: Path) -> Path:
    return identified_db(tmp_path)


def unindexed_pair(db: Path) -> None:
    """A previously initialized, populated pair as code before the index left it."""
    run_pass(db, cfg())
    write(archive_of(db), f"DROP INDEX IF EXISTS {INDEX}")
    assert rows(archive_of(db), r.EVENT_TABLE) and index_shape(archive_of(db)) is None
    add_terminal(db, "closed-2", 3)


# --------------------------------------------------------------------------- #
# shape and placement
# --------------------------------------------------------------------------- #


def test_a_fresh_pair_has_the_index_before_its_first_event_copy(identified, monkeypatch):
    archive = archive_of(identified)
    state = first_event_copy(monkeypatch, lambda: index_shape(archive))
    result = run_pass(identified, cfg())
    assert result.events_moved == 3
    assert state["seen"] == SHAPE
    version = schema_version(archive)
    run_pass(identified, cfg())
    assert index_shape(archive) == SHAPE and schema_version(archive) == version


def test_an_existing_pair_gets_the_index_at_the_event_stage_only(identified, monkeypatch):
    archive = archive_of(identified)
    unindexed_pair(identified)

    async def restart():
        store = Store(str(identified))
        store.start()
        store.stop()
    asyncio.run(restart())
    assert index_shape(archive) is None, "the index was built at Store startup"

    seen = {}

    def checkpoint(stage, table, _rows):
        if (stage, table) == ("archive_committed", "sessions") and "sessions" not in seen:
            seen["sessions"] = index_shape(archive)
    monkeypatch.setattr(r, "history_checkpoint", checkpoint)
    attempts = []
    real_connection = r._archive_index_connection
    monkeypatch.setattr(r, "_archive_index_connection",
                        lambda path: attempts.append(path) or real_connection(path))
    state = first_event_copy(monkeypatch, lambda: index_shape(archive))
    result = run_pass(identified, cfg(batch_size=1))

    assert result.events_moved == 3
    assert seen["sessions"] is None, "the index was built before the event stage"
    assert state["seen"] == SHAPE, "the index was missing at the first event copy"
    assert len(attempts) == 1, f"{len(attempts)} index attempts in one pass"
    version = schema_version(archive)
    run_pass(identified, cfg())
    assert index_shape(archive) == SHAPE and schema_version(archive) == version


# --------------------------------------------------------------------------- #
# the production probes use it
# --------------------------------------------------------------------------- #


def captured_probes(db: Path) -> list[str]:
    """Every archive alternate-key SELECT a real pass issues, values expanded."""
    seen: list[str] = []

    def trace(statement):
        if 'FROM arch."session_event_tail" WHERE "stream_id" IS' in statement:
            seen.append(statement)

    async def go():
        store = Store(str(db))
        store.start()
        try:
            await store.submit(lambda conn: conn.set_trace_callback(trace))
            return await r.RetentionJob(store, cfg()).run_pass()
        finally:
            store.stop()
    assert asyncio.run(go()).events_moved == 3
    return seen


def search(archive: Path, statement: str) -> str:
    with closing(attached(archive)) as conn:
        return " ".join(" ".join(row[3].split()) for row in conn.execute("EXPLAIN QUERY PLAN " + statement))


@pytest.mark.parametrize("column,index", [
    ("identity", INDEX),
    ("event_key", "sqlite_autoindex_session_event_tail_1"),
])
def test_the_production_probe_searches_all_three_keys(identified, column, index):
    probes = [s for s in captured_probes(identified) if f'"{column}" IN (' in s]
    assert probes, f"no {column} probe was issued"
    for statement in probes:
        plan = search(archive_of(identified), statement)
        assert plan.startswith("SEARCH") and f"INDEX {index} " in plan, plan
        for key in ("stream_id", "session_created_at", column):
            assert f"{key}=?" in plan, plan


# --------------------------------------------------------------------------- #
# duplicates and fallback
# --------------------------------------------------------------------------- #


def holder(db: Path, event: dict, event_id: int, key: str) -> dict:
    return dict(event, event_id=event_id, event_key=key, event_json="earlier occurrence")


def test_an_archive_holding_one_identity_twice_builds_the_index(identified, caplog):
    event = rows(identified, r.EVENT_TABLE, "1 ORDER BY event_id")[0]
    with closing(sqlite3.connect(archive_of(identified))) as archive:
        archive.executescript(EVENT_SCHEMA_SQL)
        for held in (holder(identified, event, 900, "old-a"), holder(identified, event, 901, "old-b")):
            archive.execute(f"INSERT INTO session_event_tail ({','.join(held)}) "
                            f"VALUES ({','.join('?' for _ in held)})", list(held.values()))
        archive.commit()
    with caplog.at_level(logging.WARNING, logger=r.log.name):
        result = run_pass(identified, cfg())
    assert index_shape(archive_of(identified)) == SHAPE
    assert (result.events_moved, result.events_retained) == (2, 1)
    assert [row["event_id"] for row in rows(identified, r.EVENT_TABLE)] == [event["event_id"]]
    archived = {row["event_id"] for row in rows(archive_of(identified), r.EVENT_TABLE)}
    assert {900, 901} <= archived and event["event_id"] not in archived
    assert sum(COLLISION_WARNING in m for m in caplog.messages) == 1


def collision_fixture(db: Path) -> None:
    """Three identified events; another archived id holds the first's identity."""
    event = rows(db, r.EVENT_TABLE, "1 ORDER BY event_id")[0]
    held = holder(db, event, 900, "old-a")
    with closing(sqlite3.connect(archive_of(db))) as archive:
        archive.execute(EVENT_SCHEMA_SQL.split(";")[0])
        archive.execute(f"INSERT INTO session_event_tail ({','.join(held)}) "
                        f"VALUES ({','.join('?' for _ in held)})", list(held.values()))
        archive.commit()


def outcome(db: Path, result: r.PassResult) -> tuple:
    return (result.sessions_moved, result.events_moved, result.events_retained,
            rows(db, "sessions"), rows(db, r.EVENT_TABLE, "1 ORDER BY event_id"),
            rows(archive_of(db), "sessions"),
            rows(archive_of(db), r.EVENT_TABLE, "1 ORDER BY event_id"))


def test_a_denied_index_build_keeps_the_pass_and_is_retried(identified, monkeypatch, caplog):
    collision_fixture(identified)
    control = identified_db(identified.parent / "control")
    collision_fixture(control)
    with caplog.at_level(logging.WARNING, logger=r.log.name):
        expected = outcome(control, run_pass(control, cfg()))
    control_warnings = list(caplog.messages)
    caplog.clear()

    fired = []

    def deny(action, arg1, *_rest):
        if action == sqlite3.SQLITE_CREATE_INDEX and arg1 == INDEX:
            fired.append(arg1)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    real_connection = r._archive_index_connection

    def denied(path):
        conn = real_connection(path)
        conn.set_authorizer(deny)
        return conn
    monkeypatch.setattr(r, "_archive_index_connection", denied)
    with caplog.at_level(logging.WARNING, logger=r.log.name):
        got = outcome(identified, run_pass(identified, cfg()))

    assert fired == [INDEX], "the denial never fired"
    assert got[:3] == (1, 2, 1), "the pass must move events and keep the collision"
    assert got == expected
    assert index_shape(archive_of(identified)) is None
    assert sum(INDEX_WARNING in m for m in caplog.messages) == 1
    assert [m for m in caplog.messages if INDEX_WARNING not in m] == control_warnings

    monkeypatch.setattr(r, "_archive_index_connection", real_connection)
    run_pass(identified, cfg())
    assert index_shape(archive_of(identified)) == SHAPE


def failure(db: Path, monkeypatch) -> tuple:
    """Run a pass whose archive gains a held writer once sessions are archived."""
    holder_conn: list[sqlite3.Connection] = []

    def checkpoint(stage, table, _rows):
        if (stage, table) == ("archive_committed", "sessions") and not holder_conn:
            conn = sqlite3.connect(archive_of(db), isolation_level=None, check_same_thread=False)
            conn.execute("BEGIN IMMEDIATE")
            holder_conn.append(conn)
    monkeypatch.setattr(r, "history_checkpoint", checkpoint)
    try:
        with pytest.raises(Exception) as raised:
            run_pass(db, cfg())
    finally:
        for conn in holder_conn:
            conn.rollback()
            conn.close()
        monkeypatch.undo()
    return (type(raised.value), str(raised.value),
            rows(db, r.EVENT_TABLE, "1 ORDER BY event_id"),
            rows(archive_of(db), r.EVENT_TABLE, "1 ORDER BY event_id"))


def test_a_persistent_outage_keeps_todays_failure_and_rows(identified, monkeypatch):
    baseline = identified_db(identified.parent / "baseline")
    real_ensure = r.ensure_event_identity_index
    monkeypatch.setattr(r, "ensure_event_identity_index", lambda path, ev, **kw: r.ensure_archive_table(
        path, ev.create_sql, ev.table, require_existing=kw.get("require_existing", False)))
    expected = failure(baseline, monkeypatch)
    assert r.ensure_event_identity_index is real_ensure
    got = failure(identified, monkeypatch)
    assert got == expected
    assert "locked" in got[1], got[1]
    assert len(got[2]) == 3 and got[3] == [], "an event moved during the outage"
