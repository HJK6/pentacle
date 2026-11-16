"""Single-instruction prevention; no historical/provider echo suppression.

Fixture text/block topology is sanitized from retained split rows and a real
captioned control. New single-text captionless echoes are DERIVED control-shape
fixtures, not captured provider output. They do not prove provider behavior.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from claude_jsonl_norm import normalize_claude_jsonl_record
from comms import AttachmentValidationError, build_attachment_inject_text
from store import Store
from test_send_semantics import FakeTmux, VALID, _new_comms, _run, HOST, NAME

FIXTURE = json.loads((Path(__file__).parent / "fixtures/captionless_wire_control.json").read_text())
STREAM = "fixture:wire"


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["id"])
def test_retained_request_sets_use_control_instruction_bytes(case: dict) -> None:
    # Literal equality must precede normalized wire digest/receipt projection.
    assert build_attachment_inject_text(case["paths"], "").encode() == case["expected_wire"].encode()


@pytest.mark.parametrize("count", [1, 5])
@pytest.mark.parametrize("caption", ["", " \n\t "])
def test_instruction_limits_and_empty_caption(count: int, caption: str) -> None:
    paths = [f"/fixture/{i}.jpg" for i in range(count)]
    noun = "file" if count == 1 else "files"
    assert build_attachment_inject_text(paths, caption) == (
        f"Look at the image {noun} at {', '.join(paths)}, then respond to the user's message."
    )


def test_captioned_control_and_empty_path_validation_unchanged() -> None:
    text = FIXTURE["captioned_control"]["message"]["content"]
    prefix, caption = text.split(", then respond to the user's message: ", 1)
    paths = prefix.removeprefix("Look at the image files at ").split(", ")
    assert build_attachment_inject_text(paths, caption) == text
    with pytest.raises(AttachmentValidationError):
        build_attachment_inject_text([], "")


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["id"])
def test_derived_single_instruction_uses_existing_projection(case: dict) -> None:
    async def go() -> None:
        wire = build_attachment_inject_text(case["paths"], "")
        assert wire.encode() == case["expected_wire"].encode()  # before any digest
        events = normalize_claude_jsonl_record(case["derived_record"], host="fixture", session_name="wire")
        assert len(events) == 1 and events[0]["text"] == wire
        store = Store(":memory:")
        store.start()
        try:
            await store.append_send_receipt(
                to_stream_id=STREAM, request_id=case["request_id"], receipt_id="receipt-" + case["id"],
                state="accepted", wire_text=wire, display_text="", attachments=case["attachments"],
                delivery="accepted", submission_confirmed=False, optimistic_id=case["optimistic_id"],
            )
            projected = await store.stamp_event_with_send_receipt(events[0])
            assert projected["text"] == ""
            assert projected["attachments"] == case["attachments"]
            assert projected["request_id"] == case["request_id"]
            assert projected["optimistic_id"] == case["optimistic_id"]
            assert projected["raw"] == events[0]["raw"]
            # The old main/companion topology is not rewritten by the new wire.
            for record in FIXTURE["historical_rows"]:
                old = normalize_claude_jsonl_record(record, host="fixture", session_name="wire")
                assert len(old) == 1
                before = copy.deepcopy(old[0])
                assert await store.stamp_event_with_send_receipt(old[0]) == before
        finally:
            store.stop()
    _run(go())


def test_materialized_captionless_receipt_precedes_one_paste_and_replay(tmp_path: Path) -> None:
    async def go() -> None:
        projected = []
        class IngestingTmux(FakeTmux):
            async def paste(self, name: str, text: str) -> None:
                receipt = await store.get_send_receipt(f"{HOST}:{NAME}", "send-wire-materialized")
                assert receipt["state"] == "accepted" and receipt["content_kind"] == "attachment_only"
                expected = f"Look at the image file at {tmp_path / 'attachments' / (VALID['key'] + '.png')}, then respond to the user's message."
                assert text.encode() == expected.encode()  # before normalized matching
                projected.append(await store.stamp_event_with_send_receipt({
                    "kind": "USER", "stream_id": f"{HOST}:{NAME}", "text": text,
                }))
                await super().paste(name, text)
        tmux = IngestingTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        try:
            await sessions.open(HOST, NAME, provider="claude")
            message = {"stream_id": f"{HOST}:{NAME}", "request_id": "send-wire-materialized", "text": "",
                       "optimistic_id": "optimistic-wire", "attachments": [VALID]}
            first = await comms.send(message)
            assert first["submission_confirmed"] is True
            assert projected[0]["text"] == ""
            assert projected[0]["attachments"] == [VALID]
            assert projected[0]["request_id"] == message["request_id"]
            assert projected[0]["optimistic_id"] == message["optimistic_id"]
            assert (await comms.send(message))["coalesced"] is True
            assert len(tmux.pastes) == len(projected) == 1
        finally:
            store.stop()
    _run(go())


@pytest.mark.parametrize("text", [
    "Look at the image file at /fixture/example.jpg, then respond to the user's message.",
    "Images at /fixture/example.jpg",
    "[Image #4]\n<pasted_content id=\"ab\">\nImages at /fixture/example.jpg\n</pasted_content id=\"ab\">",
])
def test_operator_typed_lookalikes_without_attachments_unchanged(tmp_path: Path, text: str) -> None:
    async def go() -> None:
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        try:
            await sessions.open(HOST, NAME, provider="claude")
            await comms.send({"stream_id": f"{HOST}:{NAME}", "request_id": "send-typed-control", "text": text})
            assert tmux.pastes == [text]
        finally:
            store.stop()
    _run(go())
