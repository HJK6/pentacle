"""Receipt proof indexing preserves valid results and accepts malformed writes."""
import asyncio
import hashlib
import json
import sqlite3

import pytest

import retention
import tell_proof
from store import Store

TARGET = "fixture:receipt-index"
WIRE = "proof wire"


async def receipt(store, key, meta=None):
    if meta is None:
        meta = json.dumps({"_input_proof": {"wire_text": WIRE, "wire_length": len(WIRE),
            "wire_sha256": hashlib.sha256(WIRE.encode()).hexdigest()}})
    return await store.append_send_receipt(to_stream_id=TARGET, request_id=key,
        receipt_id=key, state="landed", wire_text=WIRE, display_text=WIRE,
        attachments=[], delivery="landed", submission_confirmed=True, meta_json=meta)


def test_receipt_write_accepts_malformed_metadata_and_proof_skips_it():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            await receipt(store, "valid")
            for index, malformed in enumerate(("{", "not json", '{"_input_proof":')):
                await receipt(store, f"malformed-{index}", malformed)
            proofs = await store.submit(lambda c: list(tell_proof.send_proofs(c, TARGET, WIRE)))
            assert [row["receipt_id"] for _, row in proofs] == ["valid"]
            assert await store.submit(lambda c: c.execute(
                "SELECT count(*) FROM v2_send_receipts").fetchone()[0]) == 4
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("upgrade", [False, True])
def test_proof_query_uses_hot_and_archive_index(tmp_path, monkeypatch, upgrade):
    query = []
    original = tell_proof.history_query
    def capture(conn, sql, parameters=(), **kwargs):
        query.append((sql, parameters))
        return original(conn, sql, parameters, **kwargs)
    monkeypatch.setattr(tell_proof, "history_query", capture)
    async def run():
        path = tmp_path / "hot.db"
        store = Store(str(path))
        store.start()
        try:
            await receipt(store, "hot")
            await receipt(store, "malformed", "{")
            archive_path = await store.submit(lambda c: c.history_archive_path)
            def move(conn):
                retention.initialize_history_pair(conn)
                names = ["receipt_rowid" if col[1] == "rowid" else col[1]
                         for col in conn.execute("PRAGMA table_info(v2_send_receipts)")]
                rows = conn.execute("SELECT rowid,* FROM v2_send_receipts").fetchall()
                with sqlite3.connect(archive_path) as archive:
                    archive.executemany("INSERT INTO v2_send_receipts(receipt_rowid," +
                        ",".join(names) + ") VALUES(" + ",".join("?" for _ in rows[0]) + ")",
                        [tuple(row) for row in rows])
                conn.execute("DELETE FROM v2_send_receipts")
                conn.commit()
            await store.submit(move)
            await receipt(store, "second")
            if upgrade:
                # Simulate a previously paired archive and hot database.
                await store.submit(lambda c: (c.execute("DROP INDEX IF EXISTS idx_history_receipt_proof_wire"), c.commit()))
                with sqlite3.connect(archive_path) as archive:
                    archive.execute("DROP INDEX IF EXISTS idx_archive_receipt_proof_wire")
                store.stop()
                store = Store(str(path))
                store.start()
            proofs = await store.submit(lambda c: list(tell_proof.send_proofs(c, TARGET, WIRE)))
            assert [row["receipt_id"] for _, row in proofs] == ["hot", "second"]
            sql, params = query[-1]
            def plans(conn):
                # Actual historical union, including deduplication joins.
                schema = retention._attached_history(conn)
                union = retention.history_union_sql(conn, retention.RECEIPT_TABLE, schema)
                combined = ("WITH retained_receipts AS NOT MATERIALIZED (" + union + ") " +
                            sql.replace("v2_send_receipts", "retained_receipts"))
                return [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + combined, params)]
            plan = await store.submit(plans)
            assert any("idx_history_receipt_proof_wire" in item for item in plan), plan
            assert any("idx_archive_receipt_proof_wire" in item for item in plan), plan
        finally:
            store.stop()
    asyncio.run(run())
