"""Read-connection contracts on disposable Stores and the real alert pass."""

import asyncio
from collections import defaultdict
import json
import logging
from pathlib import Path
import sqlite3
import threading

import pytest

import store as store_module
import store_call_diag
from store import Store
from test_error_alerts_pass_reads import (
    emit,
    probe_family,  # noqa: F401 (shared fixture)
    subject,  # noqa: F401 (shared fixture)
    test_a_mixed_digest_rejected_mid_pass_still_alerts_the_active_member as mixed_digest,
)

pytestmark = pytest.mark.asyncio
SNAPSHOT = "ErrorAlerts.advisory_snapshot"
NOTICE_ROWS = "ErrorAlerts.notice_rows.locals.read"


@pytest.fixture
def db(tmp_path):
    store = Store(str(tmp_path / "reader #?.db"))
    store.start()
    try:
        yield store
    finally:
        assert store.stop()


def value(conn, key):
    return conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()["v"]


async def put(store, **pairs):
    def write(conn):
        with conn:
            conn.executemany("INSERT OR REPLACE INTO kv VALUES (?, ?)", pairs.items())

    await store.submit(write)


def rendezvous():
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def pause():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), "test did not release the Store worker"

    return entered, release, pause


async def queue_read(store, fn):
    started = asyncio.Event()

    async def run():
        started.set()
        return await store.read(fn)

    task = asyncio.create_task(run())
    # The task runs through read's admission before yielding to this waiter.
    await asyncio.wait_for(started.wait(), 10)
    return task


async def test_reader_completes_while_writer_is_mid_write(db):
    await put(db, fact="committed")
    read = db.read
    entered, release, pause = rendezvous()

    def write(conn):
        with conn:
            conn.execute("UPDATE kv SET v='uncommitted' WHERE k='fact'")
            pause()

    writer = asyncio.create_task(db.submit(write))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert await asyncio.wait_for(read(lambda conn: value(conn, "fact")), 10) == "committed"
    finally:
        release.set()
        await writer


async def test_next_read_sees_awaited_commit(db):
    await put(db, fact="before")
    assert await db.read(lambda conn: value(conn, "fact")) == "before"
    await put(db, fact="after")
    assert await db.read(lambda conn: value(conn, "fact")) == "after"


async def test_read_keeps_fact_and_dependency_in_one_snapshot(db, monkeypatch):
    await put(db, fact="first", dependency="first")
    read = db.read
    entered, release, pause = rendezvous()

    def select(conn):
        fact = value(conn, "fact")
        pause()
        return fact, value(conn, "dependency")

    reader = asyncio.create_task(read(select))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        await asyncio.wait_for(put(db, fact="second", dependency="second"), 10)
    finally:
        release.set()
        observed = await reader
    assert observed == ("first", "first")
    from work_lane_link import FIXTURE_LANE_ID, link_specs_conn
    import store_work_lanes
    await db.submit(lambda conn: link_specs_conn(conn, "spec_fixture"))
    entered, release, pause = rendezvous()
    events = store_work_lanes.lane_events_conn
    monkeypatch.setattr(store_work_lanes, "lane_events_conn", lambda *args: (pause(), events(*args))[1])
    reader = asyncio.create_task(db.get_work_lane(FIXTURE_LANE_ID, read=True))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        await db.submit(lambda conn: (conn.execute("UPDATE v2_assistant_composite_lanes SET title='Changed' WHERE lane_id=?", (FIXTURE_LANE_ID,)), conn.commit()))
    finally:
        release.set()
        lane = await reader
    assert lane["lane"]["title"] == lane["_projection_row"]["title"] == "Fixture lane"
    assert (await db.get_work_lane(FIXTURE_LANE_ID, read=True))["lane"]["title"] == "Changed"


def record_calls(monkeypatch):
    seen = defaultdict(list)
    record = store_call_diag.StoreCallDiag.record

    def counted(diag, name, *args):
        seen[diag].append(name)
        return record(diag, name, *args)

    monkeypatch.setattr(store_call_diag.StoreCallDiag, "record", counted)
    return seen


async def test_r024_mixed_digest_with_reader_delivers_one_replacement(subject, probe_family, monkeypatch):
    seen = record_calls(monkeypatch)
    await mixed_digest(subject, probe_family, monkeypatch, "batched")
    await subject.store.submit(lambda conn: None)
    assert SNAPSHOT not in seen[subject.store.call_diag]
    assert SNAPSHOT in seen[subject.store.read_diag]


async def test_reader_error_is_local_and_stop_joins_in_flight_read(db):
    def invalid_select(conn):
        conn.execute("SELECT * FROM nonexistent_table").fetchall()

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        await db.read(invalid_select)
    assert await db.read(lambda conn: conn.execute("PRAGMA query_only").fetchone()[0]) == 1

    entered, release, pause = rendezvous()
    threads = []

    def held(conn):
        threads.append(threading.current_thread())
        pause()
        return 42

    reader = asyncio.create_task(db.read(held))
    pending = None
    try:
        await asyncio.wait_for(entered.wait(), 10)
        pending = await queue_read(db, lambda conn: pytest.fail("queued read ran during shutdown"))
        assert not db.stop(timeout=0)
        with pytest.raises(RuntimeError, match="store is not running"):
            await db.read(lambda conn: None)
        assert threads[0].is_alive()
    finally:
        release.set()
        assert await reader == 42
        if pending is not None:
            with pytest.raises(RuntimeError, match="store stopped"):
                await pending
    assert await asyncio.to_thread(db.stop)
    assert not threads[0].is_alive()


async def test_reader_releases_wal_snapshot_between_calls(db):
    await put(db, fact="one")
    boundaries = []

    def traced_read(conn):
        def trace(sql):
            if sql.upper().split()[0] in {"BEGIN", "COMMIT"}:
                boundaries.append((sql.upper().split()[0], conn.in_transaction))

        conn.set_trace_callback(trace)
        assert conn.in_transaction
        return value(conn, "fact")

    for expected in ("one", "two"):
        await put(db, fact=expected)
        assert await db.read(traced_read) == expected
        wal = Path(db.path + "-wal")
        assert wal.stat().st_size > 0

        def truncate(conn):
            return tuple(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())

        assert await asyncio.wait_for(db.submit(truncate), 10) == (0, 0, 0)
        assert wal.stat().st_size == 0

    assert boundaries == [("COMMIT", True), ("BEGIN", False), ("COMMIT", True)]


async def test_file_backed_alert_pass_records_only_advisory_snapshot_on_reader(
    subject, probe_family, monkeypatch, caplog,
):
    store = subject.store
    seen = record_calls(monkeypatch)
    await emit(subject, "first")
    await subject.service.reconcile()
    await store.submit(lambda conn: None)
    assert seen[store.call_diag].count(NOTICE_ROWS) == 3  # link repair, pending rebind, prune
    assert SNAPSHOT not in seen[store.call_diag]
    assert seen[store.read_diag] == [SNAPSHOT]

    monkeypatch.setattr(store_call_diag, "SUMMARY_INTERVAL_S", 0)
    with caplog.at_level(logging.INFO, logger="chat_streamd_v2.store"):
        await subject.service.reconcile()
        await store.submit(lambda conn: None)
    summaries = defaultdict(list)
    for record in caplog.records:
        label, _, body = record.getMessage().partition(" ")
        if label in {"store_call_top", "store_read_top"}:
            summaries[label].append(json.loads(body)["groups"])
    [reader_groups] = summaries["store_read_top"]
    assert reader_groups["alert_snapshot"]["calls"] == 1
    assert reader_groups["alert_notice_reads"]["calls"] == 0
    assert summaries["store_call_top"]
    assert all(groups["alert_snapshot"]["calls"] == 0 for groups in summaries["store_call_top"])
    assert sum(groups["alert_notice_reads"]["calls"] for groups in summaries["store_call_top"]) == 6


async def test_fifth_waiting_read_is_refused_and_next_alert_pass_runs(
    subject, probe_family, caplog,
):
    store = subject.store
    read = store.read
    entered, release, pause = rendezvous()

    def held(conn):
        pause()
        return "running"

    reader = asyncio.create_task(read(held))
    waiting = []
    try:
        await asyncio.wait_for(entered.wait(), 10)
        for index in range(4):
            waiting.append(await queue_read(store, lambda conn, i=index: i))
        with pytest.raises(store_module.StoreReadBusy):
            await asyncio.wait_for(read(lambda conn: None), 10)

        await emit(subject, "during-full-reader")
        await asyncio.wait_for(subject.queue.drain_once(), 10)
        assert sum("action=reconcile_failed" in record.getMessage() for record in caplog.records) == 1
        assert not await subject.service.notice_rows()
    finally:
        release.set()
        assert await reader == "running"
        assert await asyncio.gather(*waiting) == list(range(len(waiting)))

    await subject.queue.drain_once()
    assert len(await subject.service.notice_rows()) == 1


@pytest.mark.parametrize("path", [":memory:", ""])
async def test_unshareable_store_reads_on_writer(path):
    store = Store(path)
    store.start()
    try:
        await put(store, fact="writer")

        def read_back(conn):
            return threading.get_ident(), value(conn, "fact")

        assert await store.read(read_back) == await store.submit(read_back)
    finally:
        assert store.stop()
