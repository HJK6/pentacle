"""Bound-receipt short circuit and indexed delivery scans preserve effects."""
import asyncio
from copy import deepcopy
import json

import pytest

import native_input_legacy as legacy
import tell_proof
from store import Store
from test_native_input_lookup import seat, source, proof, with_store, TARGET, WIRE_A, WIRE_B


@pytest.mark.parametrize("bound", [None, 0, 7])
def test_bound_receipt_does_not_recompute_discarded_proof(monkeypatch, bound):
    calls = []
    payload = {"delivery": {"proof_event_id": bound}}
    monkeypatch.setattr(tell_proof, "send_proofs", lambda *args: iter([(payload, {})]))
    def retained(*args):
        calls.append(True)
        return None
    monkeypatch.setattr(tell_proof, "retained_proof", retained)
    async def run():
        store = Store()
        store.start()
        try:
            await store._reconcile_ingested_tell({"stream_id": TARGET, "kind": "ASSIST_TEXT", "text": "quoted wire"})
            assert len(calls) == (1 if bound is None else 0)
            assert payload == {"delivery": {"proof_event_id": bound}}
        finally:
            store.stop()
    asyncio.run(run())


class ScanPlans:
    def __init__(self, conn):
        self.conn = conn
        self.plans = []
    def execute(self, sql, params=()):
        if "SELECT reply FROM v2_tell_deliveries WHERE" in sql:
            self.plans.extend(row[3] for row in self.conn.execute("EXPLAIN QUERY PLAN " + sql, params))
        return self.conn.execute(sql, params)
    def __getattr__(self, name):
        return getattr(self.conn, name)


def test_indexed_split_preserves_wires_overlaps_and_existing_cuts():
    def check(conn):
        seat(conn)
        proof(conn, WIRE_B, "b")
        proof(conn, WIRE_A, "a")
        watched = ScanPlans(conn)
        for index, text in enumerate(("operator", WIRE_A, WIRE_B + "\nmiddle\n" + WIRE_A,
                                      WIRE_A + "\n" + WIRE_A, "prefix\n" + WIRE_B)):
            event = source(text, str(index))
            expected = legacy.split_native_input(conn, deepcopy(event))
            assert tell_proof.split_native_input(watched, deepcopy(event)) == expected
        # Another proof for the same span must retain ambiguity, not deduplicate.
        proof(conn, WIRE_A, "duplicate")
        event = source("prefix\n" + WIRE_A, "duplicate")
        assert tell_proof.split_native_input(watched, event) == legacy.split_native_input(conn, event)
        assert any("idx_delivery_proof_wire" in item for item in watched.plans), watched.plans
    with_store(check)


def test_malformed_delivery_remains_writable_and_split_skips_it():
    def check(conn):
        seat(conn)
        proof(conn, WIRE_A, "valid")
        event = source("prefix\n" + WIRE_A)
        expected = tell_proof.split_native_input(conn, event)
        conn.execute("INSERT INTO v2_tell_deliveries VALUES('malformed','{',1)")
        conn.execute("UPDATE v2_tell_deliveries SET reply='not json' WHERE tell_id='malformed'")
        assert tell_proof.split_native_input(conn, event) == expected
        assert conn.execute("SELECT reply FROM v2_tell_deliveries WHERE tell_id='malformed'").fetchone()[0] == "not json"
    with_store(check)


def test_notice_lookup_preserves_first_equal_wire_and_skips_malformed(monkeypatch):
    async def run():
        store = Store()
        store.start()
        selected = []
        wire = "[pentacle-notice:fixture] notice"
        async def selected_delivery(tell_id):
            selected.append(tell_id)
            return None
        store.get_tell_delivery = selected_delivery
        monkeypatch.setattr(tell_proof, "send_proofs", lambda *args: iter(()))
        try:
            def seed(conn):
                conn.execute("INSERT INTO v2_tell_deliveries VALUES('malformed','{',1)")
                # Insert keys in reverse lexical order to detect index reordering.
                for key in ("z-first", "a-second"):
                    conn.execute("INSERT INTO v2_tell_deliveries VALUES(?,?,1)",
                        (key, json.dumps({"delivery": {"to_stream_id": TARGET, "text": wire}})))
                conn.commit()
            await store.submit(seed)
            await store._reconcile_ingested_tell({"stream_id": TARGET, "kind": "USER", "text": wire})
            assert selected == ["z-first"]
        finally:
            store.stop()
    asyncio.run(run())
