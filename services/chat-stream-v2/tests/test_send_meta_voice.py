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
