"""Submission reads retain the legacy answer without projecting old rows."""
from __future__ import annotations

import asyncio
import json

import pytest

import store as store_module
from comms import Comms
from store import Store
from submission_events import DurableUserEventProof, EventWatermark
from test_read_projection_deliveries import corpus


STREAM = "host-a:proof"
BIRTH = "2026-10-01T00:00:00Z"


async def append_rows(store, count, *, malformed=False, birth=BIRTH):
    def write(conn):
        ids = []
        for index in range(count):
            kind = ("ASSIST_TEXT", "USER", "TELL", "SYSTEM")[index % 4]
            event = {"kind": kind, "stream_id": STREAM, "text": f"row-{index}"}
            payload = json.dumps(event)
            if malformed and index in {count - 1, count - 3, count - 499}:
                payload = "not json"
            elif malformed and index == count - 2:
                payload = "null"
            ids.append(conn.execute(
                "INSERT INTO session_event_tail "
                "(stream_id,session_created_at,event_key,event_json,recorded_at) "
                "VALUES(?,?,?,?,1)",
                (STREAM, birth, f"{birth}-bulk-{index}", payload),
            ).lastrowid)
        conn.commit()
        return ids
    return await store.submit(write)


@pytest.mark.parametrize("path", ["comms_watermark", "watermark", "wait_for_watermark",
                                  "comms_proof", "proof"])
def test_submission_projects_only_post_watermark_rows(monkeypatch, path):
    """RED on the old real Store: each entry projects all 500 rows."""
    async def run():
        store = Store()
        store.start()
        try:
            await corpus(store)
            ids = await append_rows(store, 500)
            projected = []
            original = store_module._project_session_events_conn

            def counted(conn, events, **kwargs):
                projected.extend(event["daemon_seq"] for event in events)
                return original(conn, events, **kwargs)

            monkeypatch.setattr(store_module, "_project_session_events_conn", counted)
            proof = DurableUserEventProof(store, local_host="host-a")
            comms = Comms(store, None, None)
            watermark = ids[-3]
            if path == "comms_watermark":
                assert await comms._submission_watermark(STREAM) == ids[-1]
            elif path == "watermark":
                assert (await proof.watermark(STREAM)).daemon_seq == ids[-1]
            elif path == "wait_for_watermark":
                assert (await proof.wait_for_watermark(STREAM)).daemon_seq == ids[-1]
            elif path == "comms_proof":
                await comms._submission_event_proven(STREAM, "missing", watermark)
            else:
                await proof.lookup(STREAM, expected_text="missing",
                                   watermark=EventWatermark(STREAM, watermark, "reachable"))
            assert sorted(projected) == ([] if "watermark" in path else ids[-2:])
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("count,malformed", [(0, False), (30, False), (1100, False),
                                            (1100, True)])
def test_after_read_equals_legacy_project_then_filter(count, malformed):
    async def run():
        store = Store()
        store.start()
        try:
            # Includes proven operator/peer/service inputs and unproven native
            # tells projected as USER, using actual durable proof bindings.
            await corpus(store)
            ids = await append_rows(store, count, malformed=malformed)
            legacy = await store.fetch_session_event_tail(STREAM, limit=500)
            latest = ids[-1] if ids else legacy[-1]["daemon_seq"]
            for watermark in (0, latest // 2, latest - 3, latest, latest + 1):
                expected = [e for e in legacy if e["daemon_seq"] > watermark]
                actual = await store.fetch_session_event_tail(
                    STREAM, limit=500, after_daemon_seq=watermark)
                assert actual == expected
            if count > 500:
                # LIMIT applies to raw rows, including invalid JSON and scalars.
                assert len(legacy) == (496 if malformed else 500)
        finally:
            store.stop()
    asyncio.run(run())


def test_current_session_watermark_and_reads_exclude_recreated_history():
    async def run():
        store = Store()
        store.start()
        try:
            assert await store.fetch_session_event_watermark(STREAM) == 0
            await corpus(store)
            await append_rows(store, 10)
            new_birth = "2026-10-02T00:00:00Z"
            await store.submit(lambda conn: (conn.execute(
                "UPDATE sessions SET created_at=? WHERE host='host-a' AND session_name='proof'",
                (new_birth,)), conn.commit()))
            assert await store.fetch_session_event_watermark(STREAM) == 0
            assert await store.fetch_session_event_tail(
                STREAM, limit=500, after_daemon_seq=0) == []
            ids = await append_rows(store, 3, birth=new_birth)
            # Old-generation rows with *higher* IDs must also be excluded.
            await append_rows(store, 1, birth="stale")
            assert await store.fetch_session_event_watermark(STREAM) == ids[-1]
            assert [e["daemon_seq"] for e in await store.fetch_session_event_tail(
                STREAM, limit=500, after_daemon_seq=0)] == ids
            await store.submit(lambda conn: (conn.execute(
                "UPDATE sessions SET status='closed' WHERE host='host-a' AND session_name='proof'"),
                conn.commit()))
            assert await store.fetch_session_event_watermark(STREAM) == ids[-1]
        finally:
            store.stop()
    asyncio.run(run())


def test_undecodable_newest_row_preserves_watermark_proof_outcome():
    async def run():
        store = Store()
        store.start()
        try:
            await corpus(store)
            ids = await append_rows(store, 10, malformed=True)
            legacy = await store.fetch_session_event_tail(STREAM, limit=500)
            old = max(e["daemon_seq"] for e in legacy)
            new = await store.fetch_session_event_watermark(STREAM)
            assert new == ids[-1] and new > old
            proof = DurableUserEventProof(store, local_host="host-a")

            async def outcomes():
                results = [await proof.lookup(
                    STREAM, expected_text="new proof",
                    watermark=EventWatermark(STREAM, value, "reachable")) for value in (old, new)]
                assert [(r.state, r.event_id, r.reason) for r in results[:1]] == [
                    (r.state, r.event_id, r.reason) for r in results[1:]]
                return results[0]

            assert (await outcomes()).state == "pending"
            await store.submit(lambda conn: (conn.execute(
                "INSERT INTO session_event_tail "
                "(stream_id,session_created_at,event_key,event_json,recorded_at) VALUES(?,?,?,?,1)",
                (STREAM, BIRTH, "new-proof", json.dumps({"kind": "USER", "stream_id": STREAM,
                                                       "text": "new proof"}))), conn.commit()))
            assert (await outcomes()).proven
        finally:
            store.stop()
    asyncio.run(run())
