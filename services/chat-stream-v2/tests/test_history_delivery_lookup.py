"""Connection delivery sharing must equal fresh construction after every step."""
from __future__ import annotations

import asyncio
import json
import random
import sqlite3

import pytest

import tell_proof
from store import Store
from test_read_projection_deliveries import corpus

STREAM = "host-a:proof"


async def outcome(store, inventory):
    try:
        if inventory:
            return await store.list_open_sessions_with_event_summary()
        return await store.fetch_session_event_page(STREAM, before_daemon_seq=None, limit=100)
    except sqlite3.Error as exc:
        # Malformed JSON must preserve the original SQL exception as well.
        return type(exc), str(exc)


async def equal_to_fresh(store, monkeypatch, inventory):
    shared = await outcome(store, inventory)
    with monkeypatch.context() as fresh:
        fresh.setattr(store, "_history_deliveries", lambda conn, shared: tell_proof.ProjectionDeliveries(),
                      raising=False)
        expected = await outcome(store, inventory)
    assert shared == expected, "shared projection differs from fresh table"
    return shared


async def write(store, sql, params=(), *, external=None, rollback=False):
    def op(conn):
        conn.execute(sql, params)
        (conn.rollback if rollback else conn.commit)()
    if external:
        with sqlite3.connect(external) as conn:
            op(conn)
    else:
        await store.submit(op)


async def scenario(store, monkeypatch, path, *, mutant=None):
    await corpus(store)
    original = await store.submit(lambda c: c.execute(
        "SELECT reply FROM v2_tell_deliveries WHERE tell_id='peer'").fetchone()[0])
    await equal_to_fresh(store, monkeypatch, False)
    if mutant == "trigger":
        def disable(conn):
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_temp_master WHERE type='trigger' AND name LIKE 'delivery_lookup_%'")]
            assert len(names) == 3, "mutant must disable installed invalidation triggers"
            for name in names:
                conn.execute(f'DROP TRIGGER "{name}"')
            assert not conn.execute(
                "SELECT 1 FROM sqlite_temp_master WHERE name LIKE 'delivery_lookup_%'").fetchone()
        await store.submit(disable)
    elif mutant == "version":
        cache = await store.submit(lambda c: c.delivery_lookup)
        version = await store.submit(cache._data_version)
        monkeypatch.setattr(cache, "_data_version", lambda conn: version)
        await write(store, "CREATE TABLE outside_probe(n)", external=path)
        assert await store.submit(lambda c: c.execute("PRAGMA main.data_version").fetchone()[0]) != version
        assert await store.submit(cache._data_version) == version

    # Guaranteed origin change makes each disabled invalidator observable.
    await write(store, "DELETE FROM v2_tell_deliveries WHERE tell_id='peer'",
                external=path if mutant == "version" else None)
    await equal_to_fresh(store, monkeypatch, False)
    await write(store, "INSERT INTO v2_tell_deliveries VALUES('peer',?,1)", (original,))

    rng = random.Random(481516)
    actions = ["insert", "update", "delete", "replace", "malformed", "duplicate",
               "unrelated", "rollback", "transaction", "external", "external_unrelated"] * 3
    rng.shuffle(actions)
    for action in actions:
        inventory = bool(rng.getrandbits(1))
        if action == "transaction":
            await store.submit(lambda c: c.execute("BEGIN"))
            await store.submit(lambda c: c.execute("DELETE FROM v2_tell_deliveries WHERE tell_id='peer'"))
            await equal_to_fresh(store, monkeypatch, inventory)
            await store.submit(lambda c: c.rollback())
        elif action in {"unrelated", "external_unrelated"}:
            await write(store, "UPDATE sessions SET title='changed' WHERE host='host-a'",
                        external=path if action.startswith("external") else None)
        elif action in {"delete", "rollback", "external"}:
            await write(store, "DELETE FROM v2_tell_deliveries WHERE tell_id='peer'",
                        rollback=action == "rollback", external=path if action == "external" else None)
        else:
            payload = json.loads(original)
            payload["delivery"]["sender_verified"] = bool(rng.getrandbits(1))
            value = json.dumps(payload)
            if action == "malformed":
                value = "{broken"
            elif action == "duplicate":
                value = original.replace('"delivery":', '"delivery":{},"delivery":', 1)
            if action == "update":
                await write(store, "UPDATE v2_tell_deliveries SET reply=? WHERE tell_id='peer'", (value,))
            else:
                await write(store, "INSERT OR REPLACE INTO v2_tell_deliveries VALUES('peer',?,1)", (value,))
        await equal_to_fresh(store, monkeypatch, inventory)
        await write(store, "INSERT OR REPLACE INTO v2_tell_deliveries VALUES('peer',?,1)", (original,))
        await equal_to_fresh(store, monkeypatch, not inventory)


@pytest.mark.parametrize("mutant", [None, "trigger", "version"])
def test_randomized_equality_and_disabled_invalidators(tmp_path, monkeypatch, mutant):
    async def run():
        path = str(tmp_path / "owned.db")
        store = Store(path)
        store.start()
        try:
            if mutant:
                with pytest.raises(AssertionError, match="shared projection differs"):
                    await scenario(store, monkeypatch, path, mutant=mutant)
            else:
                await scenario(store, monkeypatch, path)
        finally:
            store.stop()
    asyncio.run(run())


def test_one_delivery_scan_across_local_unrelated_write():
    async def run():
        store = Store()
        store.start()
        try:
            await corpus(store)
            statements = []
            await store.submit(lambda c: c.set_trace_callback(statements.append))
            await outcome(store, False)
            await write(store, "UPDATE sessions SET title='unrelated'")
            await outcome(store, True)
            await outcome(store, False)
            scans = [sql for sql in statements if sql.startswith("SELECT tell_id,reply,json_extract")]
            assert len(scans) == 1, "delivery table rebuilt across unrelated writes"
        finally:
            store.stop()
    asyncio.run(run())


def test_reuse_across_unrelated_writes_and_inventory(tmp_path, monkeypatch):
    async def run():
        path = str(tmp_path / "owned.db")
        store = Store(path)
        store.start()
        try:
            await corpus(store)
            await outcome(store, False)
            table = await store.submit(lambda c: c.delivery_lookup.table)
            assert table._by_target is not None
            await write(store, "UPDATE sessions SET title='local unrelated'")
            await equal_to_fresh(store, monkeypatch, True)
            assert await store.submit(lambda c: c.delivery_lookup.table) is table
            # An unrelated other-connection commit deliberately rebuilds.
            await write(store, "UPDATE sessions SET title='external unrelated'", external=path)
            await equal_to_fresh(store, monkeypatch, False)
            assert await store.submit(lambda c: c.delivery_lookup.table) is not table
        finally:
            store.stop()
    asyncio.run(run())


def test_unclean_lookup_is_sticky_but_equal(monkeypatch):
    async def run():
        store = Store()
        store.start()
        try:
            await corpus(store)
            await outcome(store, False)
            def unclean(conn):
                table = conn.delivery_lookup.table
                assert table.rows_for(conn, STREAM, "bad\x00lookup") is None
                assert table.rows_for(conn, STREAM, "input peer") is None
            await store.submit(unclean)
            await equal_to_fresh(store, monkeypatch, False)
            assert await store.submit(lambda c: c.delivery_lookup.table._legacy)
        finally:
            store.stop()
    asyncio.run(run())


def test_callback_fault_never_fails_write(monkeypatch):
    async def run():
        store = Store()
        store.start()
        try:
            await corpus(store)
            await outcome(store, False)
            cache = await store.submit(lambda c: c.delivery_lookup)
            def fault():
                raise BaseException("forced callback fault")
            monkeypatch.setattr(cache, "_invalidate", fault)
            await write(store, "DELETE FROM v2_tell_deliveries WHERE tell_id='peer'")
            assert cache.disabled
            await equal_to_fresh(store, monkeypatch, False)
            await equal_to_fresh(store, monkeypatch, True)
        finally:
            store.stop()
    asyncio.run(run())


def test_partial_trigger_installation_is_rolled_back(monkeypatch):
    async def run():
        from retention import HistoryConnection
        original = HistoryConnection.execute
        def fail_second(self, sql, *args, **kwargs):
            if "CREATE TEMP TRIGGER delivery_lookup_update" in sql:
                raise sqlite3.OperationalError("forced trigger creation failure")
            return original(self, sql, *args, **kwargs)
        monkeypatch.setattr(HistoryConnection, "execute", fail_second)
        store = Store()
        store.start()
        try:
            assert await store.submit(lambda c: c.delivery_lookup.disabled)
            assert not await store.submit(lambda c: c.execute(
                "SELECT name FROM sqlite_temp_master WHERE name LIKE 'delivery_lookup_%'").fetchall())
            await corpus(store)
            await equal_to_fresh(store, monkeypatch, False)
            await write(store, "DELETE FROM v2_tell_deliveries WHERE tell_id='peer'")
            await equal_to_fresh(store, monkeypatch, True)
        finally:
            store.stop()
    asyncio.run(run())


def test_notice_queries_use_recipient_index_without_changing_results():
    async def run():
        store = Store()
        store.start()
        try:
            await corpus(store)
            def seed(conn):
                for i, kind in enumerate(("status_card", "notification_answer")):
                    conn.execute(
                        "INSERT INTO v2_outbound_notices(notice_id,kind,dedupe_key,recipient_stream_id,"
                        "tell_id,body,payload_digest,created_at,proof_binding,delivered_at) "
                        "VALUES(?,?,?,?,?,'body','digest','now','{}','now')",
                        (str(i), kind, str(i), STREAM, "peer" if i else "service"))
                conn.commit()
            await store.submit(seed)
            statements = []
            await store.submit(lambda c: c.set_trace_callback(statements.append))
            before = await outcome(store, False)
            await store.submit(lambda c: c.set_trace_callback(None))
            queries = [sql for sql in statements if sql.lstrip().startswith("SELECT")
                       and "FROM v2_outbound_notices" in sql and "recipient_stream_id=" in sql]
            assert len(queries) == 2
            def check(conn):
                rows = []
                for sql in queries:
                    plan = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql)]
                    assert any("idx_v2_outbound_notices_recipient" in item for item in plan), plan
                    rows.append([tuple(r) for r in conn.execute(sql)])
                assert all(rows), "both notice reads must exercise real rows"
                conn.execute("DROP INDEX idx_v2_outbound_notices_recipient")
                assert rows == [[tuple(r) for r in conn.execute(sql)] for sql in queries]
            await store.submit(check)
            assert await outcome(store, False) == before
        finally:
            store.stop()
    asyncio.run(run())
