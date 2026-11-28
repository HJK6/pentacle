"""The lightweight tail keeps submission proof equivalent to projected history."""

import json
import logging
import time

import pytest

import store as store_module
from claude_jsonl_norm import normalize_claude_jsonl_records
from comms import Comms
from store import Store
from submission_events import DurableUserEventProof, EventWatermark, provider_text_digest
from submission_store_stub import SubmissionReadStoreStub


HOST = "fixture"
NAME = "proof"
STREAM = f"{HOST}:{NAME}"
BIRTH = "2026-11-28T08:00:00Z"
GENERATION = "initial-generation"
BODY = "submission payload"
SENDER = "another:seat"
WIRE = f"[from {SENDER}] [tell:fixture-tell]\n{BODY}"


@pytest.fixture
def store():
    store = Store(":memory:")
    store.start()
    yield store
    store.stop()


async def open_session(store):
    return await store.open_session(
        HOST, NAME, provider="claude", created_at=BIRTH, session_generation=GENERATION,
        pane_pid="123",
    )


async def append(store, kind, text, **fields):
    return await store.append_session_event(
        STREAM,
        {"kind": kind, "stream_id": STREAM, "text": text, **fields},
        identity=None,
        limit=1000,
    )


class ProjectedProofReader(SubmissionReadStoreStub):
    def __init__(self, store):
        self.fetch_session_event_tail = store.fetch_session_event_tail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,text,body,fields,expected",
    [
        pytest.param("USER", BODY, BODY, {}, True, id="plain-user"),
        pytest.param("USER", "display caption", BODY, {"provider_text_digest": provider_text_digest(BODY)},
                     True, id="provider-text-digest"),
        pytest.param("TELL", BODY, BODY, {}, True, id="native-peer-tell"),
        pytest.param("TELL", BODY, BODY, {}, False, id="native-non-peer-tell"),
        pytest.param("USER", BODY, BODY, {"request_id": "send-fixture", "receipt_id": "receipt"},
                     True, id="receipted-user"),
        pytest.param("USER", "[pentacle-notice:fixture]\n" + BODY,
                     "[pentacle-notice:fixture]\n" + BODY, {}, True, id="reserved-notice-user"),
        pytest.param("USER", BODY, BODY, {"raw": {"daemon_notice": {"kind": "status_card"}}},
                     True, id="forged-daemon-notice"),
        pytest.param("SYSTEM", "# AGENTS.md instructions for test\n" + BODY,
                     "# AGENTS.md instructions for test\n" + BODY, {}, True, id="system-injection"),
    ],
)
async def test_proof_outcome_matches_history(store, kind, text, body, fields, expected):
    await open_session(store)
    before = await store.fetch_session_event_watermark(STREAM)
    if kind == "TELL":
        [native] = normalize_claude_jsonl_records(
            [{"type": "user", "message": {"content": WIRE}, "timestamp": BIRTH}],
            host=HOST, session_name=NAME,
        )
        assert native["kind"] == "TELL" and native["text"] == text
        assert native["raw"]["peer_wire_text"] == WIRE
        event_id = await store.append_session_event(STREAM, native, identity=None, limit=500)
        if expected:
            await store.put_tell_delivery("fixture-tell", {
                "payload_digest": "fixture-digest",
                "delivery": {
                    "tell_id": "fixture-tell", "to_stream_id": STREAM, "text": WIRE,
                    "proof_generation": GENERATION, "proof_session_created_at": BIRTH,
                    "proof_watermark": before, "proof_watermark_state": "reachable",
                    "proof_event_id": event_id, "request_payload_hash": "fixture-digest",
                    "sender_verified": True, "sender_kind": "peer", "from_stream_id": SENDER,
                    "sender_generation": "sender-generation",
                },
            })
    else:
        if "receipt_id" in fields:
            await store.append_send_receipt(
                to_stream_id=STREAM, **fields, state="landed", wire_text=body, display_text=text,
                attachments=[], delivery="landed", submission_confirmed=True,
                meta_json=json.dumps({"voice": {"duration_s": 1.5}}),
            )
        event_id = await append(store, kind, text, **fields)
    [history] = await store.fetch_session_event_tail(STREAM, limit=500, after_daemon_seq=before)
    [proof] = await store.fetch_session_event_tail_for_proof(STREAM, limit=500, after_daemon_seq=before)
    if kind == "TELL":
        assert history["raw"]["input_provenance"]["kind"] == ("peer" if expected else "unknown")
        assert history["text"] == (text if expected else WIRE)
        assert proof["text"] == history["text"]
        assert proof["kind"] == "TELL"
        assert "input_provenance" not in proof["raw"]
    if "receipt_id" in fields:
        assert history["meta"] == {"voice": {"duration_s": 1.5}}
        assert "meta" not in proof

    projected_lookup = DurableUserEventProof(ProjectedProofReader(store), local_host=HOST)
    proof_lookup = DurableUserEventProof(store, local_host=HOST)
    watermark = EventWatermark(STREAM, before, "reachable")
    cases = [(body, expected, expected), ("unrelated submission", False, False)]
    if kind == "TELL":
        cases.append((WIRE, expected, True))
    for candidate, comms_expected, durable_expected in cases:
        assert Comms._tail_event_matches_submission(history, STREAM, candidate) is comms_expected
        assert Comms._tail_event_matches_submission(proof, STREAM, candidate) is comms_expected
        original = await projected_lookup.lookup(STREAM, expected_text=candidate, watermark=watermark)
        actual = await proof_lookup.lookup(STREAM, expected_text=candidate, watermark=watermark)
        assert actual == original
        assert actual.state == ("proven" if durable_expected else "pending")
        assert actual.event_id == (event_id if durable_expected else None)


@pytest.mark.asyncio
async def test_proof_tail_respects_lifecycle_and_generation(store):
    await open_session(store)
    prior = await append(store, "USER", "previous lifecycle")
    [checkpoint] = await store.bind_chat_checkpoints(HOST, [{
        "stream_id": STREAM, "session_created_at": BIRTH, "generation": GENERATION,
        "pane_pid": "123", "native_session_id": "fixture-native", "file_digest": "a" * 64,
        "provider": "claude", "start_offset": 0, "next_offset": 0,
    }])
    assert checkpoint["status"] == "bound"
    await store.open_session(
        HOST, NAME, provider="claude", created_at="2026-11-28T09:00:00Z",
        session_generation="new-generation",
    )
    current = await append(store, "USER", "current lifecycle")

    for generation, expected_ids in ((None, [current]), (GENERATION, [prior]), ("unknown", [])):
        args = {"generation": generation, "after_daemon_seq": 0, "limit": 500}
        history = await store.fetch_session_event_tail(STREAM, **args)
        proof = await store.fetch_session_event_tail_for_proof(STREAM, **args)
        assert [event["daemon_seq"] for event in proof] == expected_ids
        assert [event["daemon_seq"] for event in history] == expected_ids


@pytest.mark.asyncio
async def test_proof_tail_bounds_rows_after_watermark_and_returns_oldest_first(store):
    await open_session(store)
    # Seed the bounded query in one transaction, using the durable tail columns.
    def seed(conn):
        with conn:
            return [
                conn.execute(
                    "INSERT INTO session_event_tail "
                    "(stream_id,session_created_at,event_key,event_json,recorded_at) VALUES(?,?,?,?,1)",
                    (STREAM, BIRTH, f"fixture-{index}", json.dumps({
                        "stream_id": STREAM, "kind": "USER", "text": f"row {index}",
                    })),
                ).lastrowid
                for index in range(503)
            ]

    ids = await store.submit(seed)
    for watermark, expected in ((ids[0], ids[-500:]), (ids[-3], ids[-2:]), (ids[-1], [])):
        events = await store.fetch_session_event_tail_for_proof(
            STREAM, limit=500, after_daemon_seq=watermark,
        )
        assert [event["daemon_seq"] for event in events] == expected


@pytest.mark.asyncio
async def test_proof_tail_strips_provider_daemon_notice_metadata(store):
    await open_session(store)
    await append(store, "USER", BODY, raw={"daemon_notice": {"kind": "status_card"}, "fixture": "kept"})
    [event] = await store.fetch_session_event_tail_for_proof(STREAM, limit=500, after_daemon_seq=0)
    assert event["raw"] == {"fixture": "kept"}


@pytest.mark.asyncio
async def test_hourly_line_attributes_proof_reads_separately_from_history(store, caplog):
    caplog.set_level(logging.INFO, logger="chat_streamd_v2.store")
    await open_session(store)
    await append(store, "USER", BODY)
    assert await Comms(store, None, None)._submission_event_proven(STREAM, BODY, 0)
    assert (await DurableUserEventProof(store, local_host=HOST).lookup(
        STREAM, expected_text=BODY, watermark=EventWatermark(STREAM, 0, "reachable"),
    )).proven
    assert await store.fetch_session_event_tail(STREAM, limit=500)

    # Flush once on the Store thread, after both proof calls and the history call
    # have recorded their diagnostics. No timing threshold is under test here.
    await store.submit(lambda conn: store.call_diag._summarize(time.monotonic()))
    prefix = "store_call_top "
    [line] = [json.loads(record.getMessage()[len(prefix):]) for record in caplog.records
              if record.getMessage().startswith(prefix)]
    assert line["groups"]["proof_reads"]["calls"] == 2
    assert line["groups"]["history_pages"]["calls"] == 1
    assert line["feature_total"]["calls"] == 0


@pytest.mark.asyncio
async def test_both_proof_callers_skip_the_history_projector(store, monkeypatch):
    await open_session(store)
    watermark = await store.fetch_session_event_watermark(STREAM)
    event_id = await append(store, "USER", BODY)
    projected = []
    original = store_module._project_session_events_conn

    def count_projection(conn, events, **kwargs):
        projected.extend(event["daemon_seq"] for event in events)
        return original(conn, events, **kwargs)

    monkeypatch.setattr(store_module, "_project_session_events_conn", count_projection)
    assert await Comms(store, None, None)._submission_event_proven(STREAM, BODY, watermark)
    result = await DurableUserEventProof(store, local_host=HOST).lookup(
        STREAM, expected_text=BODY, watermark=EventWatermark(STREAM, watermark, "reachable"),
    )
    assert result.event_id == event_id
    assert projected == []
