"""File-backed history reader contracts using the production page projector."""

import asyncio
import json
import logging
import shutil
import sqlite3
import threading
import time
from contextlib import closing

import pytest

import retention
import store_call_diag
import store_host_transfer
from store import Store, StoreReadBusy
from test_receipt_route_retention import TARGET, append, archive_path, combined, hot_count, move
from test_store_reader import db, put, rendezvous, value

pytestmark = pytest.mark.asyncio


async def queued(verb, fn, **options):
    admitted = asyncio.Event()

    async def run():
        admitted.set()
        return await verb(fn, **options)

    task = asyncio.create_task(run())
    await asyncio.wait_for(admitted.wait(), 10)
    return task


async def page(db, *, before_daemon_seq=None, **options):
    return await db.fetch_session_event_page(TARGET, before_daemon_seq=before_daemon_seq, limit=10, **options)


async def receipt_page(db):
    await db.open_session("fixture", "retired", provider="claude", created_at="2026-09-01T00:00:00Z")
    await append(db, meta_json=json.dumps({"voice": {"duration_s": 1.5}}))
    await db.append_session_event(
        TARGET, {"kind": "USER", "text": "body", "stream_id": TARGET,
                 "receipt_id": "receipt-send-one", "request_id": "send-one"},
        identity=None, limit=10,
    )
    expected = await page(db)
    [stamped] = [event for event in expected if event.get("receipt_id") == "receipt-send-one"]
    assert stamped["meta"] == {"voice": {"duration_s": 1.5}}
    return expected


def retained(conn):
    return [dict(row) for row in retention.history_query(conn, "SELECT * FROM v2_send_receipts")]


async def test_history_fifo_is_independent_of_writer_and_full_advisory(db):
    await append(db)
    writer_entered, writer_release, hold_writer = rendezvous()
    advisory_entered, advisory_release, hold_advisory = rendezvous()
    history_entered, history_release, hold_history = rendezvous()
    order = []

    def write(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            hold_writer()
        finally:
            conn.rollback()

    writer = asyncio.create_task(db.submit(write))
    advisory = asyncio.create_task(db.read(lambda conn: hold_advisory()))
    history = None
    waiters = []
    try:
        await asyncio.wait_for(writer_entered.wait(), 10)
        await asyncio.wait_for(advisory_entered.wait(), 10)
        for _ in range(4):
            waiters.append(await queued(db.read, lambda conn: None))
        with pytest.raises(StoreReadBusy):
            await db.read(lambda conn: None)
        history = asyncio.create_task(db.read_history(lambda conn: hold_history()))
        await asyncio.wait_for(history_entered.wait(), 10)
        later = [await queued(db.read_history, lambda conn, i=i: order.append(i)) for i in range(7)]
        history_release.set()
        await asyncio.wait_for(asyncio.gather(*later), 10)
        assert order == list(range(7))
    finally:
        writer_release.set()
        advisory_release.set()
        history_release.set()
        await asyncio.gather(writer, advisory, *waiters)
        if history is not None:
            await history


async def test_history_commit_visibility_and_rollback(db):
    await append(db)
    await put(db, fact="old")
    entered, release, pause = rendezvous()

    def pending(conn):
        conn.execute("UPDATE kv SET v='pending' WHERE k='fact'")
        try:
            pause()
        finally:
            conn.rollback()

    writer = asyncio.create_task(db.submit(pending))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert await asyncio.wait_for(db.read_history(lambda conn: value(conn, "fact")), 10) == "old"
    finally:
        release.set()
        await writer
    assert await db.read_history(lambda conn: value(conn, "fact")) == "old"
    await put(db, fact="committed")
    assert await db.read_history(lambda conn: value(conn, "fact")) == "committed"


async def test_readers_see_the_same_front_desk_binding_as_writer(db):
    await append(db)

    def binding(conn):
        return conn.lifecycle_front_desk

    for stream_id in ("fixture:original-desk", "fixture:replacement-desk"):
        db.lifecycle_front_desk = {"env_binding": stream_id, "name": "front-desk"}
        expected = await db.submit(binding)
        assert await db.read(binding) == expected
        assert await db.read_history(binding) == expected


async def test_reader_wait_and_execution_charged_once(db, monkeypatch, caplog):
    await append(db)
    # Advance the shared window without a wall-clock wait. The writer call
    # before the reader is its anchor; the threshold is crossed by the fixture.
    await db.submit(lambda conn: None)
    monkeypatch.setattr(store_call_diag, "SUMMARY_INTERVAL_S", 0)
    monkeypatch.setattr(store_call_diag, "SLOW_LINE_S", -1)
    monkeypatch.setattr(store_call_diag, "ALERT_S", -1)
    caplog.set_level(logging.INFO, logger="chat_streamd_v2.store")
    entered, release, pause = rendezvous()
    alerted = asyncio.Event()
    facts = []

    async def alert(fact):
        facts.append(fact)
        alerted.set()

    db.history_read_diag.alert = alert

    def held(conn):
        pause()

    def live(conn):
        return 42

    def dropped(conn):
        pytest.fail("cancelled or expired callback ran")

    running = asyncio.create_task(db.read_history(held))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        cancelled = await queued(db.read_history, dropped)
        expired = await queued(db.read_history, dropped, deadline=0)
        kept = await queued(db.read_history, live)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
    finally:
        release.set()
        await running
    with pytest.raises(TimeoutError):
        await expired
    assert await kept == 42
    await db.read_history(live)
    await asyncio.wait_for(alerted.wait(), 10)
    lines = [(kind, json.loads(body)) for record in caplog.records
             for kind, _, body in [record.getMessage().partition(" ")]
             if kind in {"store_read_top", "store_call_slow"}]
    slow = [data for kind, data in lines if kind == "store_call_slow" and data.get("worker") == "history"]
    assert len([data for data in slow if data["name"].endswith(".live")]) == 2
    assert all("queue_ms" in data and "execution_ms" in data for data in slow)
    assert not any(data["name"].endswith(".dropped") for data in slow)
    summaries = [data for kind, data in lines if kind == "store_read_top"]
    assert sum(data["expired"] for data in summaries) == 2
    assert sum(data["all_calls"]["calls"] for data in summaries) == 3
    assert any(fact.episode_id.split(":")[0].endswith(".live") for fact in facts)


async def test_success_error_and_cancel_release_history_snapshot(db):
    await append(db)
    entered, release, pause = rendezvous()

    def read(conn):
        return retained(conn)

    def fail(conn):
        read(conn)
        raise ValueError("synthetic failure")

    def held(conn):
        read(conn)
        pause()

    async def checkpoint():
        await put(db, fact="new WAL frame")
        rc = await db.submit(lambda conn: tuple(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()))
        assert rc == (0, 0, 0)

    assert await db.read_history(read) == await combined(db)
    await checkpoint()
    with pytest.raises(ValueError, match="synthetic failure"):
        await db.read_history(fail)
    await checkpoint()
    task = asyncio.create_task(db.read_history(held))
    await asyncio.wait_for(entered.wait(), 10)
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    await db.read_history(read)
    await checkpoint()


async def test_history_reader_cannot_write_main_archive_or_temp(db):
    await append(db)

    def read(conn):
        assert retained(conn)
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert not conn.execute("SELECT * FROM sqlite_temp_master").fetchall()
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TEMP TABLE forbidden(id)")
        # Prove each URI itself is read-only, independently of query_only.
        conn.execute("PRAGMA query_only=0")
        try:
            for schema in ("main", "receipt_archive"):
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    conn.execute(f"CREATE TABLE {schema}.forbidden(id)")
        finally:
            conn.execute("PRAGMA query_only=1")

    await db.read_history(read)


async def test_archive_outage_preserves_empty_page(db):
    await append(db, "warmup")
    await db.open_session("fixture", "retired", provider="claude", created_at="2026-09-01T00:00:00Z")
    seq = await db.append_session_event(
        TARGET, {"kind": "ASSISTANT", "text": "hi"}, identity=None, limit=10)
    assistant_page = await page(db)
    assert [event["kind"] for event in assistant_page] == ["ASSISTANT"]
    path = await archive_path(db)
    offline = path.with_suffix(".offline")
    path.rename(offline)
    assert await page(db, before_daemon_seq=seq, history=True) == []
    assert await page(db, history=True) == assistant_page
    offline.rename(path)
    await receipt_page(db)
    path.rename(offline)
    with pytest.raises(retention.RetentionError):
        await page(db, history=True)
    assert not path.exists()


async def test_fresh_history_bootstraps_on_writer(db):
    path = await archive_path(db)
    writer = await db.submit(lambda conn: threading.get_ident())
    assert not path.exists()
    assert await db.read_history(lambda conn: (retained(conn), threading.get_ident())) == ([], writer)
    assert path.exists()
    assert await db.read_history(lambda conn: threading.get_ident()) != writer


async def test_warm_history_reader_uses_authorized_relocation(db):
    expected = await receipt_page(db)
    await move(db)
    assert await page(db, history=True) == expected
    old = await archive_path(db)
    moved = old.with_suffix(".moved")
    shutil.copyfile(old, moved)
    await db.submit(lambda conn: retention.bind_history_archive(conn, moved))
    old.unlink()
    assert await page(db, history=True) == expected


async def test_history_refuses_replacement_before_first_attach(db):
    await receipt_page(db)
    path = await archive_path(db)
    old = path.with_suffix(".old")
    path.rename(old)
    shutil.copyfile(old, path)
    with pytest.raises(retention.RetentionError, match="replaced"):
        await page(db, history=True)


@pytest.mark.parametrize("phase", ["after-main-snapshot", "equal-copy", "conflicting-copy"])
async def test_page_straddles_archive_move(db, monkeypatch, phase):
    expected = await receipt_page(db)
    if phase == "after-main-snapshot":
        entered, release, pause = rendezvous()
        original = retention._attached_history
        paused = False

        def at_archive(conn):
            nonlocal paused
            if conn.history_reader and not paused:
                paused = True
                pause()
            return original(conn)

        monkeypatch.setattr(retention, "_attached_history", at_archive)
        reader = asyncio.create_task(page(db, history=True))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            assert (await asyncio.wait_for(move(db), 10))["moved"] == 1
        finally:
            release.set()
            observed = await reader
        assert observed == expected
        return

    def after_copy(stage, *_):
        if stage == "archive_committed":
            raise RuntimeError("hold hot deletion")

    monkeypatch.setattr(retention, "history_checkpoint", after_copy)
    with pytest.raises(RuntimeError, match="hold hot deletion"):
        await move(db)
    if phase == "equal-copy":
        assert await page(db, history=True) == expected
    else:
        path = await archive_path(db)
        with closing(sqlite3.connect(path)) as archive, archive:
            archive.execute("UPDATE v2_send_receipts SET display_text='conflict'")
        with pytest.raises(retention.RetentionError, match="conflicting identity"):
            await page(db, history=True)


async def test_history_delivery_lookup_reuses_main_version(db):
    expected = await receipt_page(db)
    statements = []
    await db.read_history(lambda conn: conn.set_trace_callback(statements.append))

    def lookup_builds():
        return sum(sql.startswith("SELECT tell_id,reply,json_extract") for sql in statements)

    assert await page(db, history=True) == expected
    assert await page(db, history=True) == expected
    assert lookup_builds() == 1
    await put(db, fact="new main commit")
    assert await page(db, history=True) == expected
    assert lookup_builds() == 2


async def test_actor_read_orders_after_pending_retirement(db):
    await append(db)
    entered, release, pause = rendezvous()

    def retire(conn):
        pause()
        with conn:
            conn.execute("INSERT INTO v2_host_transfer_retirements VALUES (?, ?, 'fixture', 0)",
                         (TARGET, "generation"))

    retiring = asyncio.create_task(db.submit(retire))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        token = store_host_transfer.actor_context.set((TARGET, "generation"))
        try:
            actor_read = await queued(db.read_history, lambda conn: "unauthorized")
        finally:
            store_host_transfer.actor_context.reset(token)
    finally:
        release.set()
        await retiring
    with pytest.raises(store_host_transfer.TransferError, match="authority_retired"):
        await actor_read


@pytest.mark.parametrize("mode", ["delete", "wal"])
async def test_archive_copy_commit_with_held_history_read(db, mode, monkeypatch):
    await append(db)
    path = await archive_path(db)
    with closing(sqlite3.connect(path)) as archive:
        assert archive.execute(f"PRAGMA journal_mode={mode}").fetchone()[0] == mode
    before = await combined(db)
    entered, release, pause = rendezvous()
    timestamps = {}
    open_archive = retention._open_archive

    def record_archive_open(path, **options):
        archive = open_archive(path, **options)
        if not options.get("readonly"):
            assert archive.execute("PRAGMA busy_timeout").fetchone()[0] == 100
            archive.set_trace_callback(lambda sql: timestamps.setdefault(sql, time.monotonic()))
        return archive

    def record_archive_commit(stage, *_):
        if stage == "archive_committed":
            timestamps["archive_committed"] = time.monotonic()

    monkeypatch.setattr(retention, "_open_archive", record_archive_open)
    monkeypatch.setattr(retention, "history_checkpoint", record_archive_commit)

    def held(conn):
        rows = retained(conn)
        pause()
        return rows

    read = asyncio.create_task(db.read_history(held))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        if mode == "delete":
            with pytest.raises(sqlite3.OperationalError) as error:
                await move(db)
            assert error.value.sqlite_errorname == "SQLITE_BUSY"
            wait = timestamps["ROLLBACK"] - timestamps["COMMIT"]
            print(f"archiver journal=delete copy_commit_wait_ms={wait * 1000:.1f} sqlite_busy_timeout_ms=100")
            assert await hot_count(db) == 1
            assert await combined(db) == before
        else:
            assert (await asyncio.wait_for(move(db), 10))["moved"] == 1
            wait = timestamps["archive_committed"] - timestamps["COMMIT"]
            print(f"archiver journal=wal copy_commit_ms={wait * 1000:.1f}")
    finally:
        release.set()
        assert await read == before
    if mode == "delete":
        assert (await move(db))["moved"] == 1
    assert await hot_count(db) == 0
    assert await combined(db) == before


async def test_archive_commit_lock_returns_full_page_or_typed_gap(db):
    expected = await receipt_page(db)
    assert await page(db, history=True) == expected
    timeout = await db.read_history(lambda conn: conn.execute("PRAGMA busy_timeout").fetchone()[0])
    path = await archive_path(db)
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def lock_archive():
        with closing(sqlite3.connect(path)) as archive:
            assert archive.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            archive.execute("BEGIN EXCLUSIVE")
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(60), "test did not release archive commit lock"
            archive.commit()

    archiver = asyncio.create_task(asyncio.to_thread(lock_archive))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        started = time.monotonic()
        with pytest.raises(retention.RetentionError) as error:
            await asyncio.wait_for(page(db, history=True), 50)
        assert error.value.__cause__.sqlite_errorname == "SQLITE_BUSY"
        print(f"history page_busy_ms={(time.monotonic() - started) * 1000:.1f} sqlite_busy_timeout_ms={timeout}")
    finally:
        release.set()
        await archiver
    assert await page(db, history=True) == expected


async def test_history_reader_stop_drains_and_store_restarts(db):
    await append(db)
    entered, release, pause = rendezvous()
    executing = asyncio.create_task(db.read_history(lambda conn: (pause(), "completed")[1]))
    pending = None
    try:
        await asyncio.wait_for(entered.wait(), 10)
        pending = await queued(db.read_history, lambda conn: pytest.fail("read ran after stop"))
        assert not db.stop(timeout=0)
        with pytest.raises(RuntimeError, match="stopping"):
            db.start()
    finally:
        release.set()
        assert await executing == "completed"
    with pytest.raises(RuntimeError, match="stopped"):
        await pending
    assert await asyncio.to_thread(db.stop)
    db.start()
    assert await db.read_history(retained) == await combined(db)


async def test_history_reader_ends_cleanly_when_writer_start_fails(tmp_path, monkeypatch):
    db = Store(str(tmp_path / "broken.db"))
    db.start()
    try:
        await append(db)
    finally:
        assert await asyncio.to_thread(db.stop)
    entered, release, pause = rendezvous()

    def fail(*args, **kwargs):
        pause()
        raise sqlite3.OperationalError("fixture startup failure")

    with monkeypatch.context() as active:
        active.setattr("store.prepare_history_schema", fail)
        starting = asyncio.create_task(asyncio.to_thread(db.start))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            def unexpected(conn):
                pytest.fail("read ran after failed startup")

            pending = [await queued(verb, unexpected) for verb in (db.read, db.read_history)]
        finally:
            release.set()
            with pytest.raises(sqlite3.OperationalError, match="fixture startup failure"):
                await starting
        for task in pending:
            with pytest.raises(RuntimeError, match="failed to start|stopped"):
                await task
        assert await asyncio.to_thread(db.stop)
    db.start()
    try:
        assert await db.read_history(retained) == await combined(db)
    finally:
        assert await asyncio.to_thread(db.stop)
