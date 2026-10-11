"""Paired-history acceptance: real Store APIs, synthetic data, bounded work.

Failure injection is at durable boundaries, never a mocked successful archive.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import resource
import sqlite3
import statistics
import time

import pytest

import retention as r
from assistant_composite import AssistantCompositeConfig
from store import Store

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
OLD = "2026-08-01T00:00:00Z"
CUTOFF = NOW - timedelta(days=30)
TARGET = "fixture:retired"


def running(tmp_path, name="hot.db", **options):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = Store(str(tmp_path / name), **options)
    store.start()
    return store


async def append(store, key="send-one", *, stamp=OLD, **changes):
    values = dict(to_stream_id=TARGET, request_id=key, receipt_id="receipt-" + key,
                  state="landed", wire_text="body", display_text="body", attachments=[],
                  delivery="landed", submission_confirmed=True, created_at=stamp)
    values.update(changes)
    return await store.append_send_receipt(**values)


async def seed_routes(store, count=1, **changes):
    def seed(conn):
        for i in range(count):
            values = dict(route_id=f"route-{i:06}", stream_id=TARGET, input_identity=f"input-{i}",
                          payload_digest="digest", input_request_id=f"route-request-{i}", body="body",
                          routing_state="resolved", delivery_state="landed", created_at=OLD, updated_at=OLD)
            values.update(changes)
            conn.execute(f"INSERT INTO {r.ROUTE_TABLE} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
        conn.commit()
    await store.submit(seed)


async def move(store, table=r.RECEIPT_TABLE, after=None, cutoff=CUTOFF):
    return await store.submit(lambda conn: r.move_history_batch(conn, table,
        after=after if after is not None else (0 if table == r.RECEIPT_TABLE else ""), cutoff=cutoff))


async def hot_count(store, table=r.RECEIPT_TABLE):
    return await store.submit(lambda conn: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


async def combined(store, table=r.RECEIPT_TABLE, where="1", args=()):
    return await store.submit(lambda conn: [dict(row) for row in r.history_query(conn,
        f"SELECT * FROM {table} WHERE {where} ORDER BY " + ("rowid" if table == r.RECEIPT_TABLE else "route_id"), args)])


async def archive_path(store):
    return await store.submit(lambda conn: conn.history_archive_path)


async def admit(store, identity):
    await store.ensure_assistant_composite_projection(stream_id="fixture:assistant")
    root = await store.open_session("fixture", "backend", provider="codex")
    return await store.admit_assistant_composite_input(
        stream_id="fixture:assistant", input_identity=identity, input_request_id="transport-" + identity,
        body="question", attachments=[], reply_to_message_id=None, reply_to_question_id=None,
        actor_stream_id="operator:synthetic", direct_primary=True,
        direct_target_stream_id="fixture:backend", direct_target_generation=root["session_generation"])


@pytest.mark.parametrize("seconds,moved", [(-1, 1), (0, 1), (1, 0)])
@pytest.mark.parametrize("table", r.HISTORY_TABLES)
def test_exact_thirty_day_boundary(tmp_path, seconds, moved, table):
    async def run():
        store = running(tmp_path)
        try:
            stamp = (CUTOFF + timedelta(seconds=seconds)).isoformat()
            if table == r.RECEIPT_TABLE:
                await append(store, stamp=stamp)
            else:
                await seed_routes(store, created_at=stamp, updated_at=stamp)
            assert (await move(store, table))["moved"] == moved
            assert len(await combined(store, table)) == 1
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("protection", ["accepted", "uncertain", "pending", "current_generation", "newer_attempt", "route_request", "route_identity"])
def test_receipt_protected_dependencies_remain_hot(tmp_path, protection):
    async def run():
        store = running(tmp_path)
        try:
            values = {}
            if protection == "accepted": values = dict(state="accepted", delivery="accepted", submission_confirmed=False)
            if protection == "uncertain": values = dict(state="accepted", delivery="uncertain", submission_confirmed=False)
            if protection == "pending": values = dict(state="accepted", delivery="committed_pending_proof", submission_confirmed=False)
            if protection == "route_identity": values["optimistic_id"] = "input-0"
            await append(store, **values)
            if protection == "current_generation":
                await store.open_session("fixture", "retired", created_at="2026-07-01T00:00:00Z")
            if protection == "newer_attempt":
                await append(store, state="accepted", delivery="committed_pending_proof", submission_confirmed=False)
            if protection in ("route_request", "route_identity"):
                await seed_routes(store, input_request_id="send-one" if protection == "route_request" else "unrelated")
            assert (await move(store))["moved"] == 0
            assert await hot_count(store) >= 1
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("protection", ["uncertain", "committed_pending", "queued", "lease", "operator", "lane", "operation", "reply", "dependency", "question", "current_generation", "publication"])
def test_route_protected_mutation_cohorts_remain_hot(tmp_path, protection):
    async def run():
        store = running(tmp_path)
        try:
            values = {}
            if protection in ("uncertain", "committed_pending"): values["delivery_state"] = protection
            if protection == "queued": values["routing_state"] = "queued"
            if protection == "lease": values["lease_until"] = 1
            if protection == "operator": values.update(actor_stream_id="operator:test", dispatch_id="dispatch")
            if protection == "lane": values["route_json"] = '{"lane_id":"retained-completed-lane"}'
            if protection == "question": values["reply_to_question_id"] = "question"
            if protection == "publication": values["dispatch_id"] = "dispatch"
            if protection == "current_generation":
                row = await store.open_session("fixture", "backend", created_at="2026-07-01T00:00:00Z")
                values.update(route_target="fixture:backend", route_target_generation=row["session_generation"])
            await seed_routes(store, **values)
            if protection == "operation":
                await store.submit(lambda c: (c.execute("INSERT INTO v2_assistant_composite_operations(operation_id,stream_id,reply_to_message_id,operation,payload_digest,created_at) VALUES('op',?,'input-0','lane.admit','digest',?)", (TARGET, OLD)), c.commit()))
            if protection in ("reply", "dependency"):
                await seed_routes(store, route_id="dependent", input_identity="dependent", input_request_id="dependent",
                                  **{"reply_to_message_id" if protection == "reply" else "depends_on_message_id": "input-0"}, routing_state="deferred")
            assert (await move(store, r.ROUTE_TABLE))["moved"] == 0
            assert await store.get_assistant_composite_route(stream_id=TARGET, input_identity="input-0")
        finally:
            store.stop()
    asyncio.run(run())


def test_oversize_and_protected_candidates_do_not_starve_later_keys(tmp_path, caplog):
    async def run():
        store = running(tmp_path)
        try:
            await append(store, "big", display_text="é" * r.HISTORY_MAX_BYTES)
            for i in range(505):
                await append(store, f"protected-{i}", state="accepted", delivery="uncertain", submission_confirmed=False)
            await append(store, "last")
            first = await move(store)
            assert first["examined"] == 500 and first["moved"] == 0 and first["oversized"] == 1
            second = await move(store, after=first["cursor"])
            assert second["moved"] == 1 and second["materialized_bytes"] <= r.HISTORY_MAX_BYTES
            assert "oversized" in caplog.text
            assert len(await combined(store)) == 507
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("table", r.HISTORY_TABLES)
def test_byte_bound_counts_protected_payload_and_bounds_every_transaction(tmp_path, table):
    async def run():
        store = running(tmp_path)
        try:
            if table == r.RECEIPT_TABLE:
                for i in range(12): await append(store, str(i), display_text="é" * 90000)
            else:
                await seed_routes(store, 12, body="é" * 90000)
            batch = await move(store, table)
            assert 1 < batch["moved"] < 12
            assert batch["examined"] <= 500 and batch["materialized_bytes"] <= r.HISTORY_MAX_BYTES
            assert len(await combined(store, table)) == 12
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("table", r.HISTORY_TABLES)
@pytest.mark.parametrize("stage", ["archive_committed", "verified", "hot_deleted"])
def test_crash_boundaries_retry_and_union_dedup(tmp_path, monkeypatch, table, stage):
    async def run():
        store = running(tmp_path)
        try:
            if table == r.RECEIPT_TABLE:
                for i in range(3): await append(store, str(i))
            else: await seed_routes(store, 3)
            before = await combined(store, table)
            def crash(actual, *_):
                if actual == stage: raise RuntimeError("synthetic crash")
            monkeypatch.setattr(r, "history_checkpoint", crash)
            with pytest.raises(RuntimeError, match="synthetic crash"): await move(store, table)
            assert await hot_count(store, table) == (0 if stage == "hot_deleted" else 3)
            assert await combined(store, table) == before
            monkeypatch.setattr(r, "history_checkpoint", lambda *_: None)
            await move(store, table)
            assert await hot_count(store, table) == 0
            assert await combined(store, table) == before
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("race", ["payload", "eligibility", "new_attempt"])
def test_hot_delete_revalidates_equality_and_eligibility(tmp_path, monkeypatch, race):
    async def run():
        store = running(tmp_path)
        try:
            await append(store)
            def change(stage, table, rows):
                if stage != "verified": return
                with sqlite3.connect(store.path) as c:
                    if race == "payload": c.execute("UPDATE v2_send_receipts SET display_text='changed'")
                    elif race == "eligibility": c.execute("INSERT INTO sessions(host,session_name,visibility,created_at) VALUES('fixture','retired','hidden','2026-07-01T00:00:00Z')")
                    else:
                        c.execute("INSERT INTO v2_send_receipts(to_stream_id,request_id,receipt_id,state,content_kind,delivery,created_at) VALUES(?, 'send-one','new','accepted','text','uncertain',?)", (TARGET, OLD))
            monkeypatch.setattr(r, "history_checkpoint", change)
            assert (await move(store))["moved"] == 0
            assert await hot_count(store) >= 1
            if race == "payload":
                with pytest.raises(r.RetentionError): await store.get_send_receipt(TARGET, "send-one")
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("table", r.HISTORY_TABLES)
def test_conflicting_duplicate_payload_is_never_replaced_or_hidden_by_filter(tmp_path, monkeypatch, table):
    async def run():
        store = running(tmp_path)
        try:
            if table == r.RECEIPT_TABLE: await append(store)
            else: await seed_routes(store)
            def crash(stage, *_):
                if stage == "archive_committed": raise RuntimeError("crash")
            monkeypatch.setattr(r, "history_checkpoint", crash)
            with pytest.raises(RuntimeError): await move(store, table)
            path = await archive_path(store)
            column = "display_text" if table == r.RECEIPT_TABLE else "body"
            with sqlite3.connect(path) as a: a.execute(f"UPDATE {table} SET {column}='conflict'")
            with pytest.raises(r.RetentionError): await combined(store, table)
            # Exercises the archive UNION arm, where the hot filter is false.
            with pytest.raises(r.RetentionError): await combined(store, table, column + "=?", ("conflict",))
            with pytest.raises(r.RetentionError): await move(store, table)
            assert await hot_count(store, table) == 1
            with sqlite3.connect(path) as a: assert a.execute(f"SELECT {column} FROM {table}").fetchone()[0] == "conflict"
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("damage", ["missing", "replaced", "schema", "pair", "corrupt"])
def test_archive_outage_is_gap_and_allocator_survives_restart(tmp_path, damage):
    async def run():
        store = running(tmp_path)
        try:
            first = await append(store)
            await move(store)
            assert (await store.get_send_receipt(TARGET, "send-one"))["state"] == "landed"
            path = await archive_path(store)
            if damage in ("missing", "replaced", "corrupt"):
                path.rename(path.with_suffix(".saved"))
                if damage == "replaced":
                    with sqlite3.connect(path) as a: a.execute("CREATE TABLE other(x)")
                if damage == "corrupt": path.write_bytes(b"not sqlite")
            else:
                with sqlite3.connect(path) as a:
                    a.execute("DROP TABLE v2_send_receipts" if damage == "schema" else "UPDATE v2_receipt_route_archive SET pair_id='wrong'")
            with pytest.raises((r.RetentionError, sqlite3.Error)): await store.get_send_receipt(TARGET, "absent")
            with pytest.raises((r.RetentionError, sqlite3.Error)): await r.RetentionJob(store).run_pass()
            newer = await append(store, "during-outage")
            assert newer["receipt_rowid"] > first["receipt_rowid"]
            store.stop(); store = running(tmp_path)
            assert (await append(store, "after-restart"))["receipt_rowid"] > newer["receipt_rowid"]
            with pytest.raises((r.RetentionError, sqlite3.Error)): await store.get_send_receipt(TARGET, "send-one")
        finally:
            store.stop()
    asyncio.run(run())


def test_legacy_missing_archive_cannot_seed_allocator(tmp_path):
    async def run():
        store = running(tmp_path)
        try:
            await store.submit(lambda c: (c.execute("DELETE FROM v2_send_receipt_sequence"), c.commit()))
            with pytest.raises(r.RetentionError, match="unavailable"): await append(store)
            assert await hot_count(store) == 0
            assert not (tmp_path / "sessions_archive.db").exists()
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("pathway", ["append", "composite"])
def test_both_allocators_highest_all_restart_concurrent_and_exhaustion(tmp_path, pathway):
    async def run():
        store = running(tmp_path)
        async def insert(i):
            if pathway == "append": return (await append(store, str(i)))["receipt_rowid"]
            await admit(store, str(i))
            return await store.submit(lambda c: c.execute("SELECT MAX(rowid) FROM v2_send_receipts").fetchone()[0])
        try:
            first, second = await insert(1), await insert(2)
            await store.submit(lambda c: (c.execute("DELETE FROM v2_send_receipts WHERE rowid=?", (second,)), c.commit()))
            third = await insert(3)
            assert first < second < third
            await store.submit(lambda c: (c.execute("DELETE FROM v2_send_receipts"), c.commit()))
            fourth = await insert(4)
            assert fourth > third
            store.stop(); store = running(tmp_path)
            fifth = await insert(5)
            assert fifth > fourth
            await asyncio.gather(*(insert(i) for i in range(6, 16)))
            ids = await store.submit(lambda c: [x[0] for x in c.execute("SELECT rowid FROM v2_send_receipts ORDER BY rowid")])
            assert len(ids) == len(set(ids)) == 12 and ids[-1] > fifth
            await store.submit(lambda c: (c.execute("UPDATE v2_send_receipt_sequence SET high_water=?", (r.MAX_RECEIPT_ROWID,)), c.commit()))
            with pytest.raises(r.RetentionError, match="exhausted"): await insert(99)
            assert await hot_count(store) == 12
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("pathway", ["append", "composite"])
def test_failed_real_insert_rolls_back_allocator_and_composite_event(tmp_path, pathway):
    async def run():
        store = running(tmp_path)
        try:
            initial = await append(store, "before")
            await store.submit(lambda c: (c.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON v2_send_receipts BEGIN SELECT RAISE(ABORT,'synthetic refusal'); END"), c.commit()))
            with pytest.raises(sqlite3.IntegrityError, match="synthetic refusal"):
                if pathway == "append": await append(store, "failure")
                else: await admit(store, "failure")
            assert await store.submit(lambda c: c.execute("SELECT high_water FROM v2_send_receipt_sequence").fetchone()[0]) == initial["receipt_rowid"]
            assert await store.submit(lambda c: c.execute("SELECT COUNT(*) FROM v2_assistant_composite_routes").fetchone()[0]) == 0
            assert await store.submit(lambda c: c.execute("SELECT COUNT(*) FROM session_event_tail WHERE event_key LIKE 'assistant-input:%'").fetchone()[0]) == 0
            await store.submit(lambda c: (c.execute("DROP TRIGGER reject_receipt"), c.commit()))
            assert (await append(store, "success"))["receipt_rowid"] == initial["receipt_rowid"] + 1
        finally: store.stop()
    asyncio.run(run())


def test_history_metadata_latest_coalesce_and_event_stamp_parity(tmp_path):
    async def run():
        store = running(tmp_path)
        try:
            await append(store, "send-key", meta_json='{"voice":{"duration_s":12}}', from_stream_id="fixture:peer")
            await append(store, "send-key", meta_json='{"voice":{"duration_s":44}}', display_text="shown", from_stream_id="fixture:peer")
            async def snapshot():
                return [await store.get_send_receipt(TARGET, "send-key"),
                        await store.stamp_event_with_send_receipt(dict(kind="USER", stream_id=TARGET, request_id="send-key", text="body")),
                        await store.stamp_event_with_send_receipt(dict(kind="TELL", stream_id=TARGET, text="body", raw={"tell_id":"send-key"})),
                        await store.stamp_event_with_send_receipt(dict(kind="USER", stream_id=TARGET, text="body"))]
            before = await snapshot()
            assert before[0]["meta"]["voice"]["duration_s"] == 12
            assert (await move(store))["moved"] == 2
            assert await snapshot() == before
            coalesced = await store.claim_or_coalesce_send(to_stream_id=TARGET, request_id="send-key", receipt_id="new", wire_text="body", display_text="shown", attachments=[], optimistic_id=None, window_floor=OLD)
            assert coalesced["coalesced"] and coalesced["winner"]["request_id"] == "send-key"
            assert await hot_count(store) == 0
            await append(store, "send-key", stamp=OLD, state="not_landed", delivery="not_landed", submission_confirmed=False)
            assert (await store.get_send_receipt(TARGET, "send-key"))["state"] == "not_landed"
            assert (await move(store))["moved"] == 1
            assert len(await combined(store)) == 3
        finally: store.stop()
    asyncio.run(run())


def test_historical_route_replay_activity_and_changed_payload_rejection(tmp_path):
    async def run():
        store = running(tmp_path)
        try:
            await store.ensure_assistant_composite_projection(stream_id=TARGET)
            original = await store.admit_assistant_composite_input(stream_id=TARGET, input_identity="stable", input_request_id="request", body="body", attachments=[], reply_to_message_id=None, reply_to_question_id=None, actor_stream_id=None)
            await store.submit(lambda c: (c.execute("UPDATE v2_assistant_composite_routes SET routing_state='resolved',delivery_state='landed',created_at=?,updated_at=?", (OLD, OLD)), c.commit()))
            before = await store.assistant_composite_activity(stream_id=TARGET, input_ids=["stable"])
            assert (await move(store, r.ROUTE_TABLE))["moved"] == 1
            assert await store.assistant_composite_activity(stream_id=TARGET, input_ids=["stable"]) == before
            replay = await store.admit_assistant_composite_input(stream_id=TARGET, input_identity="stable", input_request_id="rotated", body="body", attachments=[], reply_to_message_id=None, reply_to_question_id=None, actor_stream_id=None)
            assert replay["duplicate"] and replay["route_id"] == original["route_id"]
            with pytest.raises(ValueError, match="idempotency_conflict"):
                await store.admit_assistant_composite_input(stream_id=TARGET, input_identity="stable", input_request_id="changed", body="different", attachments=[], reply_to_message_id=None, reply_to_question_id=None, actor_stream_id=None)
            assert await hot_count(store, r.ROUTE_TABLE) == 0
        finally: store.stop()
    asyncio.run(run())


def test_paired_backup_relocation_restore_retains_intervening_writes(tmp_path):
    async def run():
        store = running(tmp_path / "original")
        try:
            for i in range(5): await append(store, str(i))
            await seed_routes(store, 3)
            receipt_before, route_before = await combined(store), await combined(store, r.ROUTE_TABLE)
            await move(store); await move(store, r.ROUTE_TABLE)
            pair = await archive_path(store)
            backup = tmp_path / "backup"; backup.mkdir()
            # Stopped writer + SQLite backup copies include committed WAL bytes.
            store.stop()
            for source, target in ((Path(store.path), backup / "hot.db"), (pair, backup / "archive.db")):
                with sqlite3.connect(source) as src, sqlite3.connect(target) as dst: src.backup(dst)
            store = running(backup, archive_path=str(backup / "archive.db"))
            intervening = await append(store, "intervening")
            for table in r.HISTORY_TABLES:
                cursor = 0 if table == r.RECEIPT_TABLE else ""
                while True:
                    result = await store.submit(lambda c, t=table, a=cursor: r.restore_history_batch(c, table=t, after=a))
                    cursor = result["cursor"]
                    if result["done"]: break
            assert await hot_count(store) == 6
            assert await hot_count(store, r.ROUTE_TABLE) == 3
            assert (await combined(store))[:5] == receipt_before
            assert await combined(store, r.ROUTE_TABLE) == route_before
            assert (await append(store, "after-recovery"))["receipt_rowid"] > intervening["receipt_rowid"]
            await move(store); await move(store, r.ROUTE_TABLE)
            store.stop(); store = running(backup)
            assert len(await combined(store)) == 7
            assert (await append(store, "restart"))["receipt_rowid"] > intervening["receipt_rowid"] + 1
            # Recovery never overwrites a differing intervening payload.
            await store.submit(lambda c: (c.execute("INSERT INTO v2_send_receipts(rowid,to_stream_id,request_id,receipt_id,state,content_kind,delivery,created_at) VALUES(1,'conflict','conflict','conflict','landed','text','landed',?)", (OLD,)), c.commit()))
            with pytest.raises(r.RetentionError, match="collision"):
                await store.submit(lambda c: r.restore_history_batch(c, table=r.RECEIPT_TABLE, after=0))
            assert await store.submit(lambda c: c.execute("SELECT to_stream_id FROM v2_send_receipts WHERE rowid=1").fetchone()[0]) == "conflict"
        finally: store.stop()
    asyncio.run(run())


def test_two_hot_databases_cannot_claim_the_same_archive_pair(tmp_path):
    async def run():
        first, second = running(tmp_path, "one.db"), running(tmp_path, "two.db")
        try:
            await append(first)
            with pytest.raises(r.RetentionError, match="pair"): await append(second)
            assert await hot_count(second) == 0
        finally: first.stop(); second.stop()
    asyncio.run(run())


def test_fixed_candidate_input_steps_do_not_scale_with_unrelated_history(tmp_path):
    async def run():
        measurements = []
        for count in (1000, 10000):
            store = running(tmp_path / str(count))
            try:
                def seed(c):
                    c.executemany("INSERT INTO v2_send_receipts(to_stream_id,request_id,receipt_id,state,content_kind,delivery,created_at) VALUES(?,?,?,'accepted','text','uncertain',?)", [(TARGET, str(i), str(i), OLD) for i in range(count)])
                    c.executemany("INSERT INTO v2_assistant_composite_operations(operation_id,stream_id,dispatch_id,reply_to_message_id,operation,payload_digest,created_at) VALUES(?,?,?,?,'lane.admit','digest',?)", [(str(i),'unrelated',str(i),str(i),OLD) for i in range(count)])
                    c.commit(); r.initialize_history_pair(c); c.commit(); r._attached_history(c)
                await store.submit(seed)
                await seed_routes(store, 500)
                steps = [0]
                def measured(c):
                    c.set_progress_handler(lambda: steps.__setitem__(0, steps[0]+1) or 0, 1)
                    try:
                        receipts = r.move_history_batch(c, r.RECEIPT_TABLE, after=0, cutoff=CUTOFF)
                        routes = r.move_history_batch(c, r.ROUTE_TABLE, after="", cutoff=CUTOFF)
                    finally: c.set_progress_handler(None, 0)
                    return receipts, routes
                receipts, routes = await store.submit(measured)
                assert receipts["examined"] == routes["examined"] == 500
                measurements.append(steps[0])
            finally: store.stop()
        print("history_vm_steps", measurements)
        assert measurements[1] <= 2 * measurements[0]
    asyncio.run(run())


def test_incident_fixture_bounded_batches_and_twenty_interleaved_reads(tmp_path):
    async def run():
        from loop_watchdog import RequestTiming, request_timing
        store = running(tmp_path)
        try:
            def seed(c):
                c.executemany("INSERT INTO v2_send_receipts(to_stream_id,request_id,receipt_id,state,display_text,content_kind,delivery,submission_confirmed,created_at) VALUES(?,?,?,'landed',?,'text','landed',1,?)", [(TARGET,str(i),str(i),"x"*256,OLD) for i in range(14469)])
                c.commit()
            await store.submit(seed); await seed_routes(store, 928, body="x"*256)
            def total_bytes(c):
                return {table: c.execute('SELECT SUM(' + '+'.join('COALESCE(octet_length("'+column+'"),0)' for column in r._columns(c,table)) + '+8) FROM '+table).fetchone()[0] for table in r.HISTORY_TABLES}
            family_bytes=await store.submit(total_bytes)
            job = r.RetentionJob(store)
            delays, queue_times, callbacks, loop_delays, highwater = [], [], [], [], [0]
            active = True
            async def loop_probe():
                while active:
                    start=time.perf_counter(); await asyncio.sleep(.001)
                    loop_delays.append(max(0, time.perf_counter()-start-.001)); highwater[0]=max(highwater[0],store.pending())
            async def reads():
                for _ in range(20):
                    timing=RequestTiming(); token=request_timing.set(timing); started=time.perf_counter()
                    try: assert await store.submit(lambda c:c.execute("SELECT 1").fetchone()[0]) == 1
                    finally: request_timing.reset(token)
                    delays.append(time.perf_counter()-started); queue_times.append(timing.queue_s); callbacks.append(timing.execution_s)
                    await asyncio.sleep(.002)
            probe=asyncio.create_task(loop_probe())
            result, _ = await asyncio.gather(job.run_pass(), reads())
            active=False; await probe
            assert 0 < result.receipts_moved + result.routes_moved <= 2000
            assert all(b['examined']<=500 and b['materialized_bytes']<=1024*1024 for b in result.history_batches)
            assert max(delays)<.2 and max(loop_delays)<.2
            def stats(values):
                ordered=sorted(values);return dict(p50=statistics.median(values),p95=ordered[max(0,int(len(ordered)*.95)-1)],max=max(values))
            report=dict(receipts=14469,routes=928,receipt_body_bytes=14469*256,route_body_bytes=928*256,total_payload_bytes=family_bytes,
                        request_s=stats(delays),queue_s=stats(queue_times),callback_s=stats(callbacks),loop_delay_s=stats(loop_delays),
                        queue_highwater=highwater[0],rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,host_load=os.getloadavg(),batches=result.history_batches)
            print("incident_metrics",json.dumps(report,sort_keys=True))
            assert len(delays)==20
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("pathway", ["append", "composite"])
def test_both_paths_archive_highest_all_append_second_drain_restart(tmp_path, pathway):
    async def run():
        store = running(tmp_path)
        async def insert(key):
            if pathway == "append": await append(store, key)
            else: await admit(store, key)
            return await store.submit(lambda c: c.execute("SELECT MAX(rowid) FROM v2_send_receipts").fetchone()[0])
        async def retire():
            def change(c):
                c.execute("UPDATE v2_send_receipts SET created_at=?", (OLD,))
                c.execute("UPDATE v2_assistant_composite_routes SET routing_state='resolved',delivery_state='landed',actor_stream_id=NULL,dispatch_id=NULL,route_target=NULL,route_target_generation=NULL,route_json=NULL,created_at=?,updated_at=?", (OLD, OLD))
                c.commit()
            await store.submit(change)
            await move(store, r.ROUTE_TABLE)
        try:
            low, highest = await insert("one"), await insert("two")
            await retire()
            # Only highest is old enough in this first real archival pass.
            await store.submit(lambda c: (c.execute("UPDATE v2_send_receipts SET created_at=? WHERE rowid=?", (NOW.isoformat(), low)), c.commit()))
            assert (await move(store))["moved"] == 1
            assert await store.submit(lambda c:c.execute("SELECT MAX(rowid) FROM v2_send_receipts").fetchone()[0]) == low
            third = await insert("three"); assert third > highest
            await retire(); assert (await move(store))["moved"] == 2
            assert await hot_count(store) == 0
            fourth = await insert("four"); assert fourth > third
            await retire(); assert (await move(store))["moved"] == 1
            store.stop(); store = running(tmp_path)
            fifth = await insert("five"); assert fifth > fourth
            assert len(await combined(store)) == 5
        finally: store.stop()
    asyncio.run(run())


def test_archive_configuration_cannot_abandon_event_history(tmp_path):
    async def run():
        store = running(tmp_path)
        try:
            await append(store)
            original = await archive_path(store)
            with sqlite3.connect(original) as c:
                c.execute("INSERT INTO session_event_tail(stream_id,event_key,event_json,recorded_at) VALUES('old:seat','old','{}',1)")
            alternate = tmp_path / "alternate.db"
            with sqlite3.connect(alternate) as c: c.execute("CREATE TABLE empty(x)")
            with pytest.raises(r.RetentionError, match="populated"):
                await store.submit(lambda c:r.bind_history_archive(c, alternate))
            assert await archive_path(store) == original
        finally: store.stop()
    asyncio.run(run())


def test_retro_older_window_combines_before_latest_and_cutoff(tmp_path):
    from tools import daily_retro as retro
    async def run():
        store = running(tmp_path)
        try:
            await store.open_session("fixture", "root", created_at="2026-07-01T00:00:00Z")
            await store.submit(lambda c:(c.execute("INSERT INTO v2_assistant_direct_binding VALUES(?,'fixture:root','generation',1,'2026-07-01T00:00:00Z')", (AssistantCompositeConfig().name,)),c.commit()))
            await append(store, "failure", stamp="2026-08-01T12:00:00Z", state="not_landed", delivery="not_landed", from_stream_id="fixture:root")
            await append(store, "failure", stamp="2026-08-02T12:00:00Z", from_stream_id="fixture:root")
            # Give archived recipient membership the established same-name schema.
            from store import SESSIONS_DDL
            archive = await archive_path(store)
            with sqlite3.connect(archive) as c:
                c.execute(SESSIONS_DDL)
                c.execute("INSERT INTO sessions(host,session_name,visibility,created_at,status,closed_at) VALUES('fixture','retired','hidden','2026-07-01T00:00:00Z','closed','2026-08-01T11:00:00Z')")
            settings=retro.Settings(tmp_path/'memory',tmp_path/'state','ws://127.0.0.1:12345',tmp_path/'token','fixture',isolated=True,primary_store=Path(store.path),primary_archive=archive)
            start=datetime(2026,8,1,tzinfo=timezone.utc);cutoff=start+timedelta(days=1)
            before=retro.primary_evidence(settings,start,cutoff)
            assert [x['kind'] for x in before['observations'] if x.get('receipt_id')]==['delivery_failure']
            assert (await move(store))["moved"]==2
            after=retro.primary_evidence(settings,start,cutoff)
            assert after['observations']==before['observations']
            assert after['coverage']['send_receipts']==before['coverage']['send_receipts']
            # Equal timestamps still choose global physical identity.
            await append(store, "equal", stamp="2026-08-01T12:00:00Z", state="not_landed", delivery="not_landed", from_stream_id="fixture:root")
            await move(store)
            await append(store, "equal", stamp="2026-08-01T12:00:00Z", from_stream_id="fixture:root")
            packet=retro.primary_evidence(settings,start,cutoff,max_rows=1)
            assert not any(x.get('request_id')=='equal' and x['kind']=='delivery_failure' for x in packet['observations'])
            archive.rename(archive.with_suffix('.missing'))
            outage=retro.primary_evidence(settings,start,cutoff)
            assert any(x['source']=='send_receipts' for x in outage['gaps'])
            assert outage['coverage']['send_receipts']=={'available':False}
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("owner", ["receipt", "route"])
def test_archive_only_managed_owner_survives_gc_and_outage(tmp_path,owner):
    from test_managed_attachment_upload import fixture, upload
    from test_managed_attachment_retention import age, path, sweep
    async def run():
        async with fixture(tmp_path) as (blobs,store):
            upload_row=await upload(blobs);await age(store,upload_row)
            payload=[dict(upload_row)]
            if owner=='receipt': await append(store, attachments=payload)
            else: await seed_routes(store,attachments_json=json.dumps(payload))
            table=r.RECEIPT_TABLE if owner=='receipt' else r.ROUTE_TABLE
            assert (await move(store,table))["moved"]==1
            archive=await archive_path(store)
            assert (await sweep(store,tmp_path,archive_path=archive))['deleted']==0
            assert path(tmp_path,upload_row).exists()
            archive.rename(archive.with_suffix('.missing'))
            with pytest.raises((sqlite3.Error,r.RetentionError)):
                await sweep(store,tmp_path,archive_path=archive)
            assert path(tmp_path,upload_row).exists()
            assert await store.attachment_upload(upload_row['upload_id'])
    asyncio.run(run())


@pytest.mark.parametrize('cohort',['reopenable_lane','episode','question_bridge','authority','pending_publication'])
def test_protected_census_joins_and_mutation_remain_available(tmp_path,cohort):
    from store_work_lane_episodes import lead_reported_done_conn
    async def run():
        store=running(tmp_path)
        try:
            values=dict(dispatch_id='dispatch',route_target='retired:target',route_target_generation='retired-generation')
            if cohort in ('reopenable_lane','episode'): values['route_json']='{"lane_id":"lane"}'
            if cohort=='authority': values['actor_stream_id']='operator:fixture'
            await seed_routes(store,**values)
            def dependencies(c):
                if cohort in ('reopenable_lane','episode','question_bridge'):
                    c.execute("INSERT INTO v2_assistant_composite_lanes(lane_id,stream_id,phase,created_at,updated_at) VALUES('lane',?,'completed',?,?)",(TARGET,OLD,OLD))
                if cohort=='episode':
                    c.execute("INSERT INTO v2_work_lane_episodes(episode_id,lane_id,kind,sequence,opened_at,fact_json) VALUES('episode','lane','completed',1,?,'{}')",(OLD,))
                if cohort=='question_bridge':
                    c.execute("INSERT INTO v2_assistant_composite_question_bridges(operation_id,stream_id,lane_id,operation,payload_digest,payload_json,actor_stream_id,expected_lane_version,state,created_at,updated_at) VALUES('bridge',?,'lane','question.open','digest','{}','lead',1,'prepared',?,?)",(TARGET,OLD,OLD))
                # Every cohort except pending publication is given a final
                # publication, so the specific dependency does the retaining.
                if cohort!='pending_publication':
                    c.execute("INSERT INTO v2_assistant_composite_publications(publication_key,stream_id,payload_digest,dispatch_id,reply_to_message_id,publish_kind,event_id,created_at) VALUES('final',?,'digest','dispatch','input-0','result',1,?)",(TARGET,OLD))
                c.commit()
                return lead_reported_done_conn(c,dict(stream_id=TARGET,lane_id='lane',completion_report_id=None))
            before=await store.submit(dependencies)
            await seed_routes(store,route_id='unrelated',input_identity='unrelated',input_request_id='unrelated')
            batch=await move(store,r.ROUTE_TABLE)
            assert batch['moved']==(0 if cohort=='question_bridge' else 1)
            assert await store.find_assistant_composite_route_by_dispatch('dispatch')
            after=await store.submit(lambda c:lead_reported_done_conn(c,dict(stream_id=TARGET,lane_id='lane',completion_report_id=None)))
            assert after==before
            # Existing mutation path still reaches exactly the retained row.
            updated=await store.update_assistant_composite_route('route-000000',routing_state='resolved',delivery_state='landed',expected_dispatch_id='dispatch')
            assert updated and updated['dispatch_id']=='dispatch'
        finally:store.stop()
    asyncio.run(run())


def test_legacy_allocator_seeds_from_archive_highest_identity(tmp_path):
    async def run():
        store=running(tmp_path)
        try:
            await append(store)
            await move(store)
            archive=await archive_path(store)
            with sqlite3.connect(archive) as c:
                c.execute('UPDATE v2_send_receipts SET receipt_rowid=9000')
            await store.submit(lambda c:(c.execute('DELETE FROM v2_send_receipt_sequence'),c.commit()))
            assert (await append(store,'after-seed'))['receipt_rowid']==9001
        finally:store.stop()
    asyncio.run(run())


def test_route_cross_store_unique_input_conflict_fails_closed(tmp_path):
    async def run():
        store=running(tmp_path)
        try:
            await seed_routes(store)
            await move(store,r.ROUTE_TABLE)
            await seed_routes(store,route_id='different-key')
            with pytest.raises(r.RetentionError):
                await store.get_assistant_composite_route(stream_id=TARGET,input_identity='input-0')
        finally:store.stop()
    asyncio.run(run())


def test_offline_recovery_cli_backups_and_restores_exact_pair(tmp_path,capsys):
    async def setup():
        store=running(tmp_path/'original')
        try:
            await append(store)
            await seed_routes(store)
            await move(store);await move(store,r.ROUTE_TABLE)
            return Path(store.path),await archive_path(store)
        finally:store.stop()
    hot,archive=asyncio.run(setup())
    backup=tmp_path/'backup'
    assert r.history_recovery_main(['backup','--hot',str(hot),'--archive',str(archive),'--output',str(backup),'--offline'])==0
    assert r.history_recovery_main(['restore','--hot',str(backup/'hot.db'),'--archive',str(backup/'archive.db'),'--offline'])==0
    with sqlite3.connect(backup/'hot.db') as conn:
        assert conn.execute('SELECT COUNT(*) FROM v2_send_receipts').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM v2_assistant_composite_routes').fetchone()[0]==1
    reports=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert reports[0]['pair_id']==reports[1]['pair_id']
    assert reports[0]['high_water']==reports[1]['high_water']==1


@pytest.mark.parametrize('history_side',['hot','archive'])
def test_combined_latest_stamp_and_peer_inputs_are_bounded_by_stream(tmp_path,history_side):
    async def run():
        measurements=[]
        for count in (1000,10000):
            store=running(tmp_path/str(count))
            try:
                await append(store,'send-one')
                await store.open_session('proof','target',created_at='2026-07-01T00:00:00Z',session_generation='generation')
                await append(store,'pending',to_stream_id='proof:target',state='accepted',delivery='committed_pending_proof',submission_confirmed=False)
                def seed(c):
                    state='landed' if history_side=='archive' else 'accepted'
                    c.executemany("INSERT INTO v2_send_receipts(to_stream_id,request_id,receipt_id,state,content_kind,delivery,submission_confirmed,created_at) VALUES('noise:target',?,?,?,'text',?,?,?)",[(str(i),str(i),state,state,int(state=='landed'),OLD) for i in range(count)])
                    c.commit()
                await store.submit(seed)
                if history_side=='archive':
                    cursor=0
                    while True:
                        batch=await move(store,after=cursor);cursor=batch['cursor']
                        if not cursor:break
                else:
                    await combined(store)  # initialize/cache before measurement
                observed={}
                operations={
                    'latest':lambda:store.get_send_receipt(TARGET,'send-one'),
                    'stamp':lambda:store.stamp_event_with_send_receipt(dict(kind='USER',stream_id=TARGET,text='body')),
                    'peers':lambda:store.reconcile_send_receipt('proof:target','pending',original_generation='generation'),
                }
                for name,operation in operations.items():
                    steps=[0]
                    await store.submit(lambda c:c.set_progress_handler(lambda:steps.__setitem__(0,steps[0]+1) or 0,1))
                    try:assert await operation()
                    finally:await store.submit(lambda c:c.set_progress_handler(None,0))
                    observed[name]=steps[0]
                measurements.append(observed)
            finally:store.stop()
        print('combined_reader_steps',history_side,json.dumps(measurements,sort_keys=True))
        for name in measurements[0]:assert measurements[1][name]<=2*measurements[0][name]
    asyncio.run(run())


def test_unsupported_byte_preflight_and_live_compaction_fail_closed(tmp_path):
    async def run():
        store=running(tmp_path)
        try:
            await append(store)
            statements=[]
            await store.submit(lambda c:c.set_trace_callback(statements.append))
            result=await r.RetentionJob(store,r.RetentionConfig(vacuum_min_bytes=0)).run_pass()
            assert not result.vacuumed
            assert not any(sql.upper().startswith('VACUUM') or 'WAL_CHECKPOINT(TRUNCATE)' in sql.upper() for sql in statements)
            await append(store,'after')
            def unavailable(value):raise RuntimeError('unsupported SQLite octet_length')
            await store.submit(lambda c:c.create_function('octet_length',1,unavailable))
            with pytest.raises(sqlite3.OperationalError):await r.RetentionJob(store).run_pass()
            assert await hot_count(store)==1
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('seconds,moved',[(-1,1),(0,1),(1,0)])
def test_route_final_publication_settlement_age(tmp_path,seconds,moved):
    async def run():
        store=running(tmp_path)
        try:
            await seed_routes(store,dispatch_id='dispatch',route_target='retired:target',route_target_generation='old-generation')
            stamp=(CUTOFF+timedelta(seconds=seconds)).isoformat()
            await store.submit(lambda c:(c.execute("INSERT INTO v2_assistant_composite_publications(publication_key,stream_id,payload_digest,dispatch_id,reply_to_message_id,publish_kind,event_id,created_at) VALUES('final',?,'digest','dispatch','input-0','result',1,?)",(TARGET,stamp)),c.commit()))
            assert (await move(store,r.ROUTE_TABLE))['moved']==moved
            assert await store.find_assistant_composite_route_by_dispatch('dispatch')
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('preimage',['absent','zero_byte','auxiliary_only'])
def test_fresh_bootstrap_uses_preexisting_store_schema_not_filename(tmp_path,preimage):
    db=tmp_path/'hot.db'
    if preimage=='zero_byte':db.touch()
    if preimage=='auxiliary_only':
        with sqlite3.connect(db) as c:c.execute('CREATE TABLE auxiliary_fixture(value TEXT)')
    async def run():
        store=running(tmp_path)
        try:
            assert (await append(store))['receipt_rowid']==1
            assert await hot_count(store)==1
            assert (tmp_path/'sessions_archive.db').exists()
            if preimage=='auxiliary_only':
                assert await store.submit(lambda c:c.execute("SELECT 1 FROM sqlite_master WHERE name='auxiliary_fixture'").fetchone())
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('existing_rows',[0,1])
def test_actual_legacy_store_never_bootstraps_missing_archive_after_restart(tmp_path,existing_rows):
    async def run():
        store=running(tmp_path)
        try:
            def legacy(c):
                c.execute('DELETE FROM v2_send_receipt_sequence')
                if existing_rows:
                    c.execute("INSERT INTO v2_send_receipts(to_stream_id,request_id,receipt_id,state,content_kind,delivery,created_at) VALUES('legacy:target','legacy','legacy','landed','text','landed',?)",(OLD,))
                c.commit()
            await store.submit(legacy)
            store.stop();store=running(tmp_path)
            with pytest.raises(r.RetentionError,match='unavailable'):await append(store)
            assert await hot_count(store)==existing_rows
            assert not (tmp_path/'sessions_archive.db').exists()
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('defect',['different_pair','incompatible_schema'])
def test_pending_bootstrap_retro_validates_archive_before_union(tmp_path,defect):
    from tools import daily_retro as retro
    async def run():
        first,second=running(tmp_path,'first.db'),running(tmp_path,'second.db')
        try:
            await append(first,'foreign',stamp='2026-08-01T12:00:00Z',state='not_landed',delivery='not_landed',from_stream_id='fixture:root')
            await move(first)
            await second.open_session('fixture','root',created_at='2026-07-01T00:00:00Z')
            await second.submit(lambda c:(c.execute("INSERT INTO v2_assistant_direct_binding VALUES(?,'fixture:root','generation',1,'2026-07-01T00:00:00Z')", (AssistantCompositeConfig().name,)),c.commit()))
            sequence=await second.submit(lambda c:dict(c.execute('SELECT * FROM v2_send_receipt_sequence').fetchone()))
            assert sequence['archive_ready']==0
            archive=await archive_path(first)
            if defect=='incompatible_schema':
                with sqlite3.connect(archive) as c:
                    c.execute('UPDATE v2_receipt_route_archive SET pair_id=?',(sequence['pair_id'],))
                    c.execute('ALTER TABLE v2_send_receipts DROP COLUMN meta_json')
            settings=retro.Settings(tmp_path/'memory',tmp_path/'state','ws://127.0.0.1:12345',tmp_path/'token','fixture',isolated=True,primary_store=Path(second.path),primary_archive=archive)
            before=archive.read_bytes()
            packet=retro.primary_evidence(settings,datetime(2026,8,1,tzinfo=timezone.utc),datetime(2026,8,2,tzinfo=timezone.utc))
            assert packet['coverage']['send_receipts']=={'available':False}
            assert any(g['source']=='send_receipts' for g in packet['gaps'])
            assert not any(o.get('request_id')=='foreign' for o in packet['observations'])
            assert archive.read_bytes()==before
        finally:first.stop();second.stop()
    asyncio.run(run())
