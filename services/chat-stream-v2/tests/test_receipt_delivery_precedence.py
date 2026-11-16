"""Confirmed delivery survives pending updates, never conflicting submissions."""
import asyncio
import json
import sqlite3

import pytest

from test_send_receipt_late_proof import (
    BODY, CLAIM, GENERATION, TARGET, add_proof, intent, read_receipt, _RecordingComms,
)
from test_voice_receipt_identity import setup_voice_store, voice_intent
from store import Store
from retention import history_query

META = {"voice": {"duration_s": 61.943}}
ENRICHED = {**META, "pool_input": {"reason": "direct_operator_order", "wire_text": BODY},
            "_input_proof": {"to_stream_id": TARGET, "proof_generation": GENERATION,
                             "wire_text": BODY}}


async def append(store, key, state="accepted", meta=META, **changes):
    values = dict(to_stream_id=TARGET, request_id=key, receipt_id="synthetic-receipt",
                  state=state, wire_text=BODY, display_text=BODY, attachments=[],
                  delivery="landed" if state == "landed" else "committed_pending_proof",
                  submission_confirmed=state == "landed", attempts=1, created_at=CLAIM,
                  meta_json=json.dumps(meta, sort_keys=True, separators=(",", ":")))
    values.update(changes)
    return await store.append_send_receipt(**values)


async def count(store):
    return await store.submit(lambda c: history_query(c, "SELECT COUNT(*) FROM v2_send_receipts").fetchone()[0])


@pytest.mark.parametrize("kind", ["text", "voice", "relay"])
@pytest.mark.parametrize("reason", [None, "late_user_proof"])
@pytest.mark.parametrize("archived", [False, True])
def test_confirmed_delivery_precedes_pending_without_appending(tmp_path, kind, reason, archived):
    async def run():
        store, _, _ = await setup_voice_store(tmp_path)
        try:
            key, proof = voice_intent() if kind == "voice" else intent()
            if kind == "text":
                key, proof = "synthetic-text-request", None
            await append(store, key)
            await append(store, key, "landed", reason=reason)
            # Deliberately split retained receipt history at the confirmed row.
            if archived:
                def split(c):
                    rows = c.execute("SELECT rowid AS receipt_rowid,* FROM v2_send_receipts").fetchall()
                    with sqlite3.connect(c.history_archive_path) as archive:
                        names = rows[0].keys()
                        archive.executemany("INSERT INTO v2_send_receipts (" + ",".join(names) +
                                            ") VALUES (" + ",".join("?" for _ in names) + ")",
                                            [tuple(r) for r in rows])
                    c.execute("DELETE FROM main.v2_send_receipts")
                    c.commit()
                await store.submit(split)
            await append(store, key, meta=ENRICHED)
            before = await count(store)
            comms = _RecordingComms()
            for preimage in (None, proof, proof):
                row = await read_receipt(store, key, preimage, comms)
                assert row["state"] == row["delivery"] == "landed"
                assert row["submission_confirmed"] is True
                assert row["meta"] == META
            assert await count(store) == before
            assert comms.calls == []
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["voice", "relay"])
@pytest.mark.parametrize("enriched", [False, True])
def test_exact_native_proof_survives_enrichment_and_restart(tmp_path, kind, enriched):
    async def run():
        store, _, _ = await setup_voice_store(tmp_path)
        try:
            key, proof = voice_intent() if kind == "voice" else intent()
            await append(store, key)
            for _ in range(2):
                await append(store, key, meta=ENRICHED if enriched else META)
            await add_proof(store, key)
            comms = _RecordingComms()
            rows = await asyncio.gather(*(read_receipt(store, key, proof, comms) for _ in range(5)))
            assert all(r["state"] == "landed" and r["meta"] == META for r in rows)
            assert await count(store) == 4
            store.stop()
            store = Store(str(tmp_path / "sessions.db"))
            store.start()
            assert (await read_receipt(store, key, proof, comms))["state"] == "landed"
            assert await count(store) == 4
            assert comms.calls == []
            assert await store.submit(lambda c: c.execute("SELECT COUNT(*) FROM session_event_tail WHERE json_extract(event_json,'$.kind')='USER'").fetchone()[0]) == 1
        finally:
            store.stop()
    asyncio.run(run())


CONFLICTS = [
    {"receipt_id": "other-receipt"}, {"wire_text": "changed wire"},
    {"display_text": "changed display"}, {"attachments": [{"key": "a" * 64}]},
    {"content_kind": "attachment_only"}, {"optimistic_id": "other-optimistic"},
    {"from_stream_id": "fixture:sender"}, {"actor_stream_id": "fixture:actor"},
    {"actor_trusted": True}, {"meta_json": '{"voice":{"duration_s":62}}'},
    {"meta_json": '{"voice":{"duration_s":61.943},"unknown_client":true}'},
    {"state": "not_landed", "delivery": "not_landed", "submission_confirmed": False},
    {"state": "landed", "delivery": "landed", "submission_confirmed": False},
    {"state": "landed", "delivery": "uncertain", "submission_confirmed": True},
]


@pytest.mark.parametrize("changes", CONFLICTS)
@pytest.mark.parametrize("proved", [False, True])
def test_conflicts_never_borrow_or_gain_proof(tmp_path, changes, proved):
    async def run():
        store, _, _ = await setup_voice_store(tmp_path)
        try:
            key, proof = voice_intent()
            await append(store, key)
            if proved:
                await append(store, key, "landed")
            await append(store, key, **changes)
            await append(store, key)
            await add_proof(store, key)
            before = await count(store)
            for preimage in (None, proof):
                assert (await read_receipt(store, key, preimage))["state"] == "accepted"
            assert await count(store) == before
        finally:
            store.stop()
    asyncio.run(run())


def test_newest_confirmed_row_wins_equal_timestamps(tmp_path):
    async def run():
        store, _, _ = await setup_voice_store(tmp_path)
        try:
            key, proof = voice_intent()
            await append(store, key, "landed", reason="first-confirmation")
            await append(store, key)
            await append(store, key, "landed", reason="second-confirmation")
            await append(store, key)
            assert (await read_receipt(store, key, proof))["reason"] == "second-confirmation"
        finally:
            store.stop()
    asyncio.run(run())
