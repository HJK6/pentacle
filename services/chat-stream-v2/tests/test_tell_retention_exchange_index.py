"""Tell retention finds exchange-held rows by index (R-018).

Every `put_tell_delivery` runs the retention DELETE; its exchange custody check
must search `v2_child_exchange_ref` instead of scanning the exchange table once
per candidate row, and the index must not change which rows retention keeps.
"""
import asyncio

import store as store_module
from store import Store

HELD = ("held-0", "held-1")


async def _exchange(store, kind, ref_id, n):
    def insert(conn):
        conn.execute(
            "INSERT INTO v2_child_exchange (parent,child,parent_generation,child_generation,epoch,kind,"
            "ref_id,direction,ts,text,truncated,payload_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("host-a:parent", "host-a:child", "pg", f"cg-{n}", "epoch", kind, ref_id, "down",
             "2026-11-26T00:00:00Z", "text", 0, "digest"))
        conn.commit()
    await store.submit(insert)


async def _retained(drop_index, statements=None):
    store = Store(":memory:"); store.start()
    try:
        if drop_index:
            await store.submit(lambda conn: conn.execute("DROP INDEX IF EXISTS v2_child_exchange_ref"))
        for n, tell_id in enumerate(HELD):
            await _exchange(store, "tell", tell_id, n)
        await _exchange(store, "report", "report-shares-id", 2)
        for tell_id in (*HELD, "report-shares-id", "old-0", "old-1"):
            await store.put_tell_delivery(tell_id, {"reply": {"delivery_status": "delivered"}})
        if statements is not None:
            await store.submit(lambda conn: conn.set_trace_callback(statements.append))
        for i in range(10):
            await store.put_tell_delivery(f"new-{i}", {"reply": {"delivery_status": "delivered"}})
        plans = []
        if statements is not None:
            await store.submit(lambda conn: conn.set_trace_callback(None))
            delete = next(s for s in statements if s.lstrip().startswith("DELETE FROM v2_tell_deliveries"))
            plans = await store.submit(lambda conn: [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + delete)])
        rows = await store.submit(lambda conn: [r[0] for r in conn.execute("SELECT tell_id FROM v2_tell_deliveries")])
        return set(rows), plans
    finally:
        store.stop()


def test_retention_exchange_check_uses_the_ref_index_and_keeps_the_same_rows(monkeypatch):
    monkeypatch.setattr(store_module, "TELL_RETENTION", 8)

    async def run():
        indexed, plans = await _retained(False, statements=[])
        unindexed, _ = await _retained(True)
        return indexed, unindexed, plans
    indexed, unindexed, plans = asyncio.run(run())
    exchange = [p for p in plans if p.split()[1:2] == ["e"]]
    assert exchange and all(p.startswith("SEARCH e USING COVERING INDEX v2_child_exchange_ref") for p in exchange), plans
    assert indexed == unindexed
    assert set(HELD) <= indexed and not {"report-shares-id", "old-0", "old-1"} & indexed
    assert {f"new-{i}" for i in range(2, 10)} <= indexed
