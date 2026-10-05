"""Additive ``meta.voice`` on send: persisted on the USER event and echoed to
clients so the voice-input mic-glyph caption survives reload/reconcile.

Covers spec_pentacle_mobile__voice_input_thoth_transcription_2026_09
§ Shared contract item 3 / C3: the daemon persists ``meta.voice={duration_s}``
durably on the send receipt and stamps it back onto the projected USER event.
``meta`` is whitelisted (only ``voice.duration_s``) and never part of the
send-dedup identity, so a resend with/without meta of the same text still
coalesces.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from comms import normalize_send_meta
from store import Store


TARGET = "workstation:voice-target"


def _store() -> Store:
    store = Store(":memory:")
    store.start()
    return store


# -- normalize_send_meta (pure whitelist) ----------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"voice": {"duration_s": 3}}, {"voice": {"duration_s": 3.0}}),
        ({"voice": {"duration_s": 4.25}}, {"voice": {"duration_s": 4.25}}),
        ({"voice": {"duration_s": 1.23456}}, {"voice": {"duration_s": 1.235}}),
        # rejected shapes -> {}
        (None, {}),
        ("nope", {}),
        ({}, {}),
        ({"voice": {}}, {}),
        ({"voice": {"duration_s": "5"}}, {}),
        ({"voice": {"duration_s": True}}, {}),
        ({"voice": {"duration_s": -2}}, {}),
        ({"other": {"x": 1}}, {}),
        # extra keys are dropped, only duration_s survives
        ({"voice": {"duration_s": 2, "sneaky": "x"}, "top": "y"}, {"voice": {"duration_s": 2.0}}),
    ],
)
def test_normalize_send_meta(raw, expected) -> None:
    assert normalize_send_meta(raw) == expected


# -- store round-trip -------------------------------------------------------

def test_receipt_persists_and_projects_meta() -> None:
    async def go() -> None:
        store = _store()
        try:
            await store.append_send_receipt(
                to_stream_id=TARGET, request_id="send-voice-1", receipt_id="r1",
                state="accepted", wire_text="hello there", display_text="hello there",
                attachments=[], delivery="accepted", submission_confirmed=False,
                meta_json=json.dumps({"voice": {"duration_s": 6.5}}),
            )
            projected = await store.get_send_receipt(TARGET, "send-voice-1")
            assert projected is not None
            assert projected["meta"] == {"voice": {"duration_s": 6.5}}
        finally:
            store.stop()

    asyncio.run(go())


def test_receipt_without_meta_has_no_meta_key() -> None:
    async def go() -> None:
        store = _store()
        try:
            await store.append_send_receipt(
                to_stream_id=TARGET, request_id="send-plain-1", receipt_id="r1",
                state="accepted", wire_text="hi", display_text="hi",
                attachments=[], delivery="accepted", submission_confirmed=False,
            )
            projected = await store.get_send_receipt(TARGET, "send-plain-1")
            assert projected is not None
            assert "meta" not in projected  # empty meta is never projected
        finally:
            store.stop()

    asyncio.run(go())


def test_claim_persists_meta() -> None:
    async def go() -> None:
        store = _store()
        try:
            claim = await store.claim_or_coalesce_send(
                to_stream_id=TARGET, request_id="send-claim-1", receipt_id="r1",
                wire_text="dictated words", display_text="dictated words",
                attachments=[], optimistic_id="opt-1", window_floor="",
                meta_json=json.dumps({"voice": {"duration_s": 12.0}}),
            )
            assert claim["coalesced"] is False
            assert claim["receipt"]["meta"] == {"voice": {"duration_s": 12.0}}
        finally:
            store.stop()

    asyncio.run(go())


def test_stamp_echoes_meta_onto_user_event() -> None:
    """The projected USER event carries meta so the mic-glyph caption survives
    reload/reconcile."""

    async def go() -> None:
        store = _store()
        try:
            await store.append_send_receipt(
                to_stream_id=TARGET, request_id="send-stamp-1", receipt_id="r1",
                state="accepted", wire_text="hello world", display_text="hello world",
                attachments=[], delivery="accepted", submission_confirmed=False,
                meta_json=json.dumps({"voice": {"duration_s": 8.0}}),
            )
            stamped = await store.stamp_event_with_send_receipt({
                "kind": "USER",
                "stream_id": TARGET,
                "request_id": "send-stamp-1",
                "text": "hello world",
            })
            assert stamped["request_id"] == "send-stamp-1"
            assert stamped["meta"] == {"voice": {"duration_s": 8.0}}
        finally:
            store.stop()

    asyncio.run(go())


def test_stamp_without_meta_leaves_event_meta_absent() -> None:
    async def go() -> None:
        store = _store()
        try:
            await store.append_send_receipt(
                to_stream_id=TARGET, request_id="send-stamp-2", receipt_id="r1",
                state="accepted", wire_text="plain", display_text="plain",
                attachments=[], delivery="accepted", submission_confirmed=False,
            )
            stamped = await store.stamp_event_with_send_receipt({
                "kind": "USER",
                "stream_id": TARGET,
                "request_id": "send-stamp-2",
                "text": "plain",
            })
            assert "meta" not in stamped
        finally:
            store.stop()

    asyncio.run(go())


def test_meta_not_part_of_dedup_identity() -> None:
    """A retry that omits meta still coalesces onto the winner that carried it —
    meta rides alongside the receipt, it is not in the dedup key."""

    async def go() -> None:
        store = _store()
        try:
            first = await store.claim_or_coalesce_send(
                to_stream_id=TARGET, request_id="send-dd-1", receipt_id="r1",
                wire_text="same text", display_text="same text",
                attachments=[], optimistic_id="opt-dd", window_floor="",
                meta_json=json.dumps({"voice": {"duration_s": 3.0}}),
            )
            assert first["coalesced"] is False
            # Mark it landed so the next identical logical send coalesces.
            await store.append_send_receipt(
                to_stream_id=TARGET, request_id="send-dd-1", receipt_id="r1b",
                state="landed", wire_text="same text", display_text="same text",
                attachments=[], delivery="landed", submission_confirmed=True,
                meta_json=json.dumps({"voice": {"duration_s": 3.0}}),
            )
            second = await store.claim_or_coalesce_send(
                to_stream_id=TARGET, request_id="send-dd-2", receipt_id="r2",
                wire_text="same text", display_text="same text",
                attachments=[], optimistic_id="opt-dd", window_floor="",
                meta_json="{}",  # retry without meta
            )
            assert second["coalesced"] is True  # identity match ignores meta

        finally:
            store.stop()

    asyncio.run(go())


@pytest.mark.parametrize("meta,expected", [
    ({"voice": {"duration_s": 7.923, "extra": "drop"}, "extra": True},
     {"voice": {"duration_s": 7.923}}),
    ({"voice": {"duration_s": "invalid"}}, {}),
    (None, {}),
])
def test_ordinary_send_outcomes_history_and_replay_keep_request_meta(tmp_path, meta, expected):
    """Run the actual send lane; only the provider counterpart is simulated."""
    from ingest import append_ingested_event
    from test_send_semantics import FakeTmux, _new_comms, _open, HOST, NAME

    async def go():
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        target = f"{HOST}:{NAME}"
        try:
            await _open(comms, sessions)
            result = await comms.send({
                "stream_id": target, "text": "voice transcript",
                "request_id": "send-voice-outcome", "meta": meta,
            })
            assert result["submission_confirmed"] is True
            original_rows = await store.submit(lambda conn: [dict(row) for row in conn.execute(
                "SELECT * FROM v2_send_receipts WHERE to_stream_id=? AND request_id=? ORDER BY rowid",
                (target, "send-voice-outcome"),
            )])
            assert [row["state"] for row in original_rows] == ["accepted", "landed"]
            assert [json.loads(row["meta_json"]) for row in original_rows] == [expected, expected]
            event = {
                "kind": "USER", "stream_id": target, "provider": "claude",
                "text": "voice transcript", "request_id": "send-voice-outcome",
                "timestamp": "2026-10-05T16:00:00Z",
                "raw": {"jsonl_record_uuid": "voice-outcome", "jsonl_event_index": 0},
            }
            casts = []
            async def broadcast(frame):
                casts.append(frame)
            assert await append_ingested_event(store, broadcast, event, recent_limit=50) is not None
            history = await store.fetch_session_event_tail(target, limit=50)
            assert history[0].get("meta", {}) == expected
            assert casts[0]["event"].get("meta", {}) == expected
            # Identical body under another request and stream must never lend metadata.
            for other_target, other_request in [(target, "send-other-voice"), ("other:seat", "send-voice-outcome")]:
                await store.append_send_receipt(
                    to_stream_id=other_target, request_id=other_request, receipt_id="other",
                    state="landed", wire_text="voice transcript", display_text="voice transcript",
                    attachments=[], delivery="landed", submission_confirmed=True,
                    meta_json=json.dumps({"voice": {"duration_s": 99.0}}),
                )
            replay = await store.stamp_event_with_send_receipt(history[0])
            assert replay["request_id"] == "send-voice-outcome"
            assert replay.get("meta", {}) == expected
            unchanged = await store.submit(lambda conn: [dict(row) for row in conn.execute(
                "SELECT * FROM v2_send_receipts WHERE to_stream_id=? AND request_id=? ORDER BY rowid",
                (target, "send-voice-outcome"),
            )])
            assert unchanged == original_rows
        finally:
            store.stop()

    asyncio.run(go())


def test_ordinary_failed_send_keeps_validated_meta(tmp_path):
    from test_send_semantics import FakeTmux, _new_comms, _open, HOST, NAME

    async def go():
        comms, store, sessions = _new_comms(FakeTmux(fail_phase="not_started"), tmp_path)
        try:
            await _open(comms, sessions)
            await comms.send({"stream_id": f"{HOST}:{NAME}", "text": "voice transcript",
                              "request_id": "send-voice-failed", "meta": {"voice": {"duration_s": 3.0}}})
            receipt = await store.get_send_receipt(f"{HOST}:{NAME}", "send-voice-failed")
            assert receipt["state"] == "not_landed"
            assert receipt["meta"] == {"voice": {"duration_s": 3.0}}
        finally:
            store.stop()

    asyncio.run(go())


def test_coalesced_request_keeps_its_own_meta_without_borrowing_winner(tmp_path):
    from test_send_semantics import FakeTmux, _new_comms, _open, HOST, NAME

    async def go():
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        target = f"{HOST}:{NAME}"
        try:
            await _open(comms, sessions)
            base = {"stream_id": target, "text": "same transcript", "optimistic_id": "opt-voice"}
            await comms.send({**base, "request_id": "send-voice-winner", "meta": {"voice": {"duration_s": 5.0}}})
            retry = await comms.send({**base, "request_id": "send-voice-retry", "meta": {"voice": {"duration_s": 8.0}}})
            assert retry["coalesced"] is True
            assert len(tmux.pastes) == 1
            assert (await store.get_send_receipt(target, "send-voice-winner"))["meta"] == {"voice": {"duration_s": 5.0}}
            assert (await store.get_send_receipt(target, "send-voice-retry"))["meta"] == {"voice": {"duration_s": 8.0}}
            replay = await comms.send({**base, "request_id": "send-voice-no-meta"})
            assert replay["coalesced"] is True
            assert "meta" not in await store.get_send_receipt(target, "send-voice-no-meta")
        finally:
            store.stop()

    asyncio.run(go())



def test_same_request_replay_retains_original_meta(tmp_path):
    from test_send_semantics import FakeTmux, _new_comms, _open, HOST, NAME

    async def go():
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        target = f"{HOST}:{NAME}"
        try:
            await _open(comms, sessions)
            base = {"stream_id": target, "text": "voice transcript", "request_id": "send-original-meta"}
            await comms.send({**base, "meta": {"voice": {"duration_s": 4.5}}})
            original = await store.submit(lambda conn: [dict(row) for row in conn.execute(
                "SELECT * FROM v2_send_receipts WHERE to_stream_id=? AND request_id=? ORDER BY rowid",
                (target, "send-original-meta"),
            )])
            for meta in [None, {"voice": {"duration_s": 77.0}}]:
                assert (await comms.send({**base, "meta": meta}))["coalesced"] is True
                receipt = await store.get_send_receipt(target, "send-original-meta")
                assert receipt["meta"] == {"voice": {"duration_s": 4.5}}
            assert len(tmux.pastes) == 1
            after = await store.submit(lambda conn: [dict(row) for row in conn.execute(
                "SELECT * FROM v2_send_receipts WHERE to_stream_id=? AND request_id=? ORDER BY rowid",
                (target, "send-original-meta"),
            )])
            assert after[:len(original)] == original
        finally:
            store.stop()

    asyncio.run(go())



def test_same_request_replay_after_rotated_retry_keeps_original_meta(tmp_path):
    from test_send_semantics import FakeTmux, _new_comms, _open, HOST, NAME
    from ingest import append_ingested_event

    async def go():
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        target = f"{HOST}:{NAME}"
        try:
            await _open(comms, sessions)
            base = {"stream_id": target, "text": "voice transcript", "optimistic_id": "opt-interleaved"}
            await comms.send({**base, "request_id": "send-interleaved-original",
                              "meta": {"voice": {"duration_s": 5.0}}})
            original_rows = await store.submit(lambda conn: [dict(row) for row in conn.execute(
                "SELECT * FROM v2_send_receipts WHERE to_stream_id=? AND request_id=? ORDER BY rowid",
                (target, "send-interleaved-original"),
            )])
            casts = []
            async def broadcast(frame):
                casts.append(frame)
            event = {
                "kind": "USER", "stream_id": target, "provider": "claude",
                "text": "voice transcript", "request_id": "send-interleaved-original",
                "timestamp": "2026-10-05T16:00:00Z",
                "raw": {"jsonl_record_uuid": "interleaved-voice", "jsonl_event_index": 0},
            }
            await append_ingested_event(store, broadcast, event, recent_limit=50)
            await comms.send({**base, "request_id": "send-interleaved-rotated",
                              "meta": {"voice": {"duration_s": 8.0}}})
            await store.append_send_receipt(
                to_stream_id="other:seat", request_id="send-interleaved-original", receipt_id="other",
                state="landed", wire_text="voice transcript", display_text="voice transcript",
                attachments=[], delivery="landed", submission_confirmed=True,
                meta_json=json.dumps({"voice": {"duration_s": 99.0}}),
            )
            replay = await comms.send({**base, "request_id": "send-interleaved-original"})
            assert replay["coalesced"] is True
            assert len(tmux.pastes) == 1
            assert (await store.get_send_receipt(target, "send-interleaved-original"))["meta"] == {"voice": {"duration_s": 5.0}}
            assert (await store.get_send_receipt(target, "send-interleaved-rotated"))["meta"] == {"voice": {"duration_s": 8.0}}
            history = await store.fetch_session_event_tail(target, limit=50)
            assert history[0]["meta"] == {"voice": {"duration_s": 5.0}}
            assert (await store.stamp_event_with_send_receipt(history[0]))["meta"] == {"voice": {"duration_s": 5.0}}
            assert casts[0]["event"]["meta"] == {"voice": {"duration_s": 5.0}}
            after = await store.submit(lambda conn: [dict(row) for row in conn.execute(
                "SELECT * FROM v2_send_receipts WHERE to_stream_id=? AND request_id=? ORDER BY rowid",
                (target, "send-interleaved-original"),
            )])
            assert after[:len(original_rows)] == original_rows
        finally:
            store.stop()

    asyncio.run(go())
