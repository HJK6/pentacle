"""Boundary coverage for the existing backfill event/frame contract."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

import server


@pytest.mark.parametrize("force_chunks", [False, True])
def test_single_oversized_event_preserves_identity_metadata_and_input(monkeypatch, force_chunks):
    monkeypatch.setattr(server, "STREAM_EVENTS_FRAME_BUDGET_BYTES", 1024)
    event = {
        "daemon_seq": 7, "stream_id": "testhost:session", "kind": "ASSIST_TEXT",
        "timestamp": "2026-10-27T00:00:00Z", "event_id": "event-7",
        "message_id": "message-7", "reply_to_message_id": "message-6",
        "provider": "codex", "host": "testhost", "session_name": "session",
        "raw": {"jsonl_record_uuid": "record-7", "source": "codex-jsonl"},
        "attachments": [{"key": "abc", "mime": "image/png"}],
        "text": '雪😀\n"\\' * 1024,
    }
    original = deepcopy(event)
    frames = server._assemble_event_frames(
        [event], request_id="request-7", stream_id=event["stream_id"], force_chunks=force_chunks,
    )
    assert len(frames) == 1
    assert len(frames[0].payload.encode("utf-8")) <= 1024
    received = json.loads(frames[0].payload)["events"][0]
    assert received["text"].startswith('雪😀\n"\\')
    assert "[Event truncated for backfill" in received["text"]
    assert {k: v for k, v in received.items() if k != "text"} == {
        k: v for k, v in event.items() if k != "text"
    }
    assert event == original


@pytest.mark.parametrize("force_chunks", [False, True])
def test_projected_peer_body_matches_existing_client_contract_fixture(monkeypatch, force_chunks):
    monkeypatch.setattr(server, "STREAM_EVENTS_FRAME_BUDGET_BYTES", 1024)
    fixture = Path(__file__).resolve().parents[3] / "pentacle-chat-core/tests/fixtures/oversized-event-preview.json"
    expected = json.loads(fixture.read_text())
    event = deepcopy(expected)
    event["text"] = "x" * 2048
    event["raw"]["peer_payload"] = "short peer body"
    frames = server._assemble_event_frames(
        [event], request_id="request-7", stream_id=event["stream_id"], force_chunks=force_chunks,
    )
    assert len(frames[0].payload.encode("utf-8")) <= 1024
    assert json.loads(frames[0].payload)["events"] == [expected]
    assert event["text"] == "x" * 2048
    assert event["raw"]["peer_payload"] == "short peer body"


@pytest.mark.parametrize("force_chunks", [False, True])
def test_exact_singleton_budget_is_not_truncated(monkeypatch, force_chunks):
    event = {"daemon_seq": 1, "stream_id": "testhost:session", "kind": "USER", "text": "exact"}
    frame_type = "request_stream_events.chunk" if force_chunks else "request_stream_events.ok"
    prefix, suffix = server._event_frame_parts(
        frame_type=frame_type, request_id="request-1", stream_id=event["stream_id"],
        complete=not force_chunks,
    )
    exact_budget = len(prefix) + len(server._json_bytes(event)) + len(suffix)
    monkeypatch.setattr(server, "STREAM_EVENTS_FRAME_BUDGET_BYTES", exact_budget)
    frames = server._assemble_event_frames(
        [event], request_id="request-1", stream_id=event["stream_id"], force_chunks=force_chunks,
    )
    assert len(frames) == 1
    assert len(frames[0].payload.encode("utf-8")) == exact_budget
    assert json.loads(frames[0].payload)["events"] == [event]
