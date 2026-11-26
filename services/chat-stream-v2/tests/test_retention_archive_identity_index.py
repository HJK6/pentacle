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


def first_event_move(monkeypatch, action) -> dict:
    """Run `action` once, at the first event copy: after ATTACH and the index
    step, before the copy transaction. Also records whether a transaction was
    left open there."""
    real, state = r._move_copies, {}

    def wrapper(conn, archive_path, binding, table, *args, **kwargs):
        if table == r.EVENT_TABLE and "seen" not in state:
            state["open_transaction"] = conn.in_transaction
            state["seen"] = action()
        return real(conn, archive_path, binding, table, *args, **kwargs)
    monkeypatch.setattr(r, "_move_copies", wrapper)
    return state


@pytest.fixture()
def identified(tmp_path: Path) -> Path:
    return identified_db(tmp_path)


def store_pass(db: Path, config: r.RetentionConfig, on_store_connection) -> r.PassResult:
    """One pass through a real Store whose connection `on_store_connection` prepares."""
    async def go():
        store = Store(str(db))
        store.start()
        try:
            await store.submit(on_store_connection)
            return await r.RetentionJob(store, config).run_pass()
        finally:
            store.stop()
    return asyncio.run(go())


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
    state = first_event_move(monkeypatch, lambda: index_shape(archive))
    result = run_pass(identified, cfg())
    assert result.events_moved == 3
    assert state["seen"] == SHAPE and state["open_transaction"] is False
    version = schema_version(archive)
    run_pass(identified, cfg())
    assert index_shape(archive) == SHAPE and schema_version(archive) == version


def test_an_existing_pair_gets_the_index_in_the_event_stage_only(identified, monkeypatch):
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

    def trace(statement):
        if statement.startswith(f"CREATE INDEX IF NOT EXISTS arch.{INDEX}"):
            attempts.append(statement)
    entry = first_event_copy(monkeypatch, lambda: index_shape(archive))
    state = first_event_move(monkeypatch, lambda: index_shape(archive))
    result = store_pass(identified, cfg(batch_size=1), lambda conn: conn.set_trace_callback(trace))

    assert result.events_moved == 3
    assert seen["sessions"] is None, "the index was built before the event stage"
    assert entry["seen"] is None, "the index was built before the event stage attached the archive"
    assert state["seen"] == SHAPE, "the index was not committed before the first copy transaction"
    assert state["open_transaction"] is False
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
    state = first_event_move(monkeypatch, lambda: None)
    with caplog.at_level(logging.WARNING, logger=r.log.name):
        got = outcome(identified, store_pass(identified, cfg(), lambda conn: conn.set_authorizer(deny)))
    assert state["open_transaction"] is False, "the index transaction was left open"

    assert fired == [INDEX], "the denial never fired"
    assert got[:3] == (1, 2, 1), "the pass must move events and keep the collision"
    assert got == expected
    assert index_shape(archive_of(identified)) is None
    index_warnings = [m for m in caplog.messages if INDEX_WARNING in m]
    assert len(index_warnings) == 1 and "not authorized" in index_warnings[0], index_warnings
    assert [m for m in caplog.messages if INDEX_WARNING not in m] == control_warnings

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
    real_build = r._build_identity_index
    monkeypatch.setattr(r, "_build_identity_index", lambda *args: None)
    expected = failure(baseline, monkeypatch)
    assert r._build_identity_index is real_build
    got = failure(identified, monkeypatch)
    assert got == expected
    assert "locked" in got[1], got[1]
    assert len(got[2]) == 3 and got[3] == [], "an event moved during the outage"


def test_a_lock_held_across_only_the_index_step_keeps_the_copy(identified, monkeypatch, caplog):
    # A writer holds the archive from just before the first event chunk and
    # lets go as soon as the index step gives up, so only that step meets it.
    import store_usage
    collision_fixture(identified)
    archive = archive_of(identified)
    real_prune, held = store_usage.prune_generation_history_conn, []

    def between(conn, now):
        result = real_prune(conn, now)
        holder_conn = sqlite3.connect(archive, isolation_level=None, check_same_thread=False)
        holder_conn.execute("BEGIN IMMEDIATE")
        held.append(holder_conn)
        return result
    monkeypatch.setattr(store_usage, "prune_generation_history_conn", between)
    state = first_event_move(monkeypatch, lambda: None)

    class Release(logging.Handler):
        def emit(self, record):
            if INDEX_WARNING in record.getMessage():
                for conn in held:
                    conn.rollback()
                    conn.close()
                held.clear()
    release = Release()
    r.log.addHandler(release)
    try:
        with caplog.at_level(logging.WARNING, logger=r.log.name):
            result = run_pass(identified, cfg())
    finally:
        r.log.removeHandler(release)
        for conn in held:
            conn.rollback()
            conn.close()
    index_warnings = [m for m in caplog.messages if INDEX_WARNING in m]
    assert len(index_warnings) == 1 and "locked" in index_warnings[0], index_warnings
    assert (result.events_moved, result.events_retained) == (2, 1), "the copy did not proceed"
    assert state["open_transaction"] is False, "the index transaction was left open"
    assert index_shape(archive) is None
    with closing(attached(archive)) as conn:
        assert conn.execute("PRAGMA arch.integrity_check").fetchone()[0] == "ok"
    monkeypatch.undo()
    run_pass(identified, cfg())
    assert index_shape(archive) == SHAPE


# --------------------------------------------------------------------------- #
# a refused archive is never written
# --------------------------------------------------------------------------- #


def catalog(archive: Path) -> dict:
    with closing(sqlite3.connect(archive.absolute().as_uri() + "?mode=ro", uri=True)) as conn:
        return {"schema_version": conn.execute("PRAGMA schema_version").fetchone()[0],
                "indexes": sorted(n for n, in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"))}


def relocate(db: Path, archive: Path) -> Path:
    """A supported relocation of the pair to a copy of its archive."""
    relocated = db.parent / "relocated.db"
    with closing(sqlite3.connect(archive)) as src, closing(sqlite3.connect(relocated)) as dst:
        src.backup(dst)
    with closing(sqlite3.connect(db, factory=r.HistoryConnection, check_same_thread=False)) as other:
        other.row_factory = sqlite3.Row
        r.bind_history_archive(other, relocated)
    return relocated


@pytest.mark.parametrize("boundary", ["before_first_chunk", "after_attach"])
@pytest.mark.parametrize("change,refusal", [
    ("rebind", "history archive rebound; no hot rows deleted"),
    ("wrong_pair", "history archive pair mismatch"),
])
def test_an_archive_refused_before_the_first_event_copy_is_not_indexed(
        identified, monkeypatch, change, refusal, boundary):
    # A supported relocation, or a pair marker that no longer matches, between
    # session archival and the first event chunk, or between the event
    # stage's ATTACH and its index transaction.
    import store_usage
    collision_fixture(identified)
    archive = archive_of(identified)
    hot = rows(identified, r.EVENT_TABLE, "1 ORDER BY event_id")
    seen = {}

    def apply():
        if change == "rebind":
            relocate(identified, archive)
        else:
            write(archive, "UPDATE v2_receipt_route_archive SET pair_id='unrelated-pair'")
        seen["before"] = catalog(archive)
    if boundary == "before_first_chunk":
        real_prune = store_usage.prune_generation_history_conn

        def between(conn, now):
            result = real_prune(conn, now)
            apply()
            return result
        monkeypatch.setattr(store_usage, "prune_generation_history_conn", between)
    else:
        real_build = r._build_identity_index

        def after_attach(*args):
            apply()
            return real_build(*args)
        monkeypatch.setattr(r, "_build_identity_index", after_attach)
    with pytest.raises(r.RetentionError) as raised:
        run_pass(identified, cfg())
    assert str(raised.value) == refusal
    assert catalog(archive) == seen["before"], "the refused archive was written"
    assert INDEX not in seen["before"]["indexes"]
    assert rows(identified, r.EVENT_TABLE, "1 ORDER BY event_id") == hot


def test_a_rebind_after_the_index_commits_leaves_it_on_the_former_archive(identified, monkeypatch):
    # The guarantee is that a stale binding cannot receive the index write. A
    # rebind after the index commit may leave a valid index on the former
    # archive; the copy then lands there as it does today and the delete
    # refuses. The next pass targets the new pair.
    collision_fixture(identified)
    archive = archive_of(identified)
    hot = rows(identified, r.EVENT_TABLE, "1 ORDER BY event_id")
    real_build, order, where = r._build_identity_index, [], {}

    def then_rebind(*args):
        real_build(*args)
        order.append(("index committed", index_shape(archive) == SHAPE))
        where["relocated"] = relocate(identified, archive)
        order.append(("rebound", True))
    monkeypatch.setattr(r, "_build_identity_index", then_rebind)
    with pytest.raises(r.RetentionError) as raised:
        run_pass(identified, cfg())
    assert str(raised.value) == "history archive rebound; no hot rows deleted"
    assert order == [("index committed", True), ("rebound", True)]
    assert rows(identified, r.EVENT_TABLE, "1 ORDER BY event_id") == hot
    former = {row["event_id"] for row in rows(archive, r.EVENT_TABLE)}
    relocated = where["relocated"]
    assert {e["event_id"] for e in hot[1:]} <= former, "the copy did not land on the former archive"
    assert not {e["event_id"] for e in hot} & {row["event_id"] for row in rows(relocated, r.EVENT_TABLE)}

    monkeypatch.undo()
    result = run_pass(identified, cfg())
    assert (result.events_moved, result.events_retained) == (2, 1)
    assert index_shape(relocated) == SHAPE
    assert {e["event_id"] for e in hot[1:]} <= {row["event_id"] for row in rows(relocated, r.EVENT_TABLE)}
