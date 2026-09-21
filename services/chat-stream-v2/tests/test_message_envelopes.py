"""RED/contract coverage for the v1 message-envelope registry."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from ingest import append_ingested_event
from store import Store


FIXTURE = json.loads(
    (Path(__file__).resolve().parents[3] / "pentacle-chat-core/tests/fixtures/message-envelopes.json").read_text()
)


def test_frozen_fixture_registry_build_match_and_negative_parity() -> None:
    from message_envelopes import build_message_envelope, match_message_envelope

    assert FIXTURE["schema_version"] == 1
    assert FIXTURE["validation_baselines"] == {
        "red": "aea8673509ba97b95841cf1d8a10e57e10fa998c",
        "final": "31afd4d32df37e01b2810975f7d830e75d6c3c38",
    }
    for case in FIXTURE["cases"]:
        wire = case["wire_text"]
        assert hashlib.sha256(wire.encode()).hexdigest() == case["wire_sha256"]
        built = build_message_envelope(case["kind"], **case["build_fields"])
        assert built == wire, case["key"]
        tag = match_message_envelope(wire)
        assert tag is not None, case["key"]
        for key, value in case["expected_tag"].items():
            assert tag[key] == value, case["key"]
        for negative in case["negative_texts"]:
            assert match_message_envelope(negative) is None, (case["key"], negative)


def test_registry_entries_are_immutable_and_have_render_policy() -> None:
    from message_envelopes import MESSAGE_ENVELOPES

    assert isinstance(MESSAGE_ENVELOPES, tuple)
    assert {entry.kind for entry in MESSAGE_ENVELOPES} == {
        "notice_marker",
        "notification_answer",
        "child_session_closed",
        "child_inactivity_threshold",
        "child_report_ready",
        "claude_pasted_content",
    }
    policies = {entry.kind: entry.render_policy for entry in MESSAGE_ENVELOPES}
    assert policies == {
        case["kind"]: case["render_policy"] for case in FIXTURE["cases"]
    }
    assert set(policies.values()) <= {
        "chat_prose", "structured_card", "persisted_only", "internal",
    }


def test_provider_adapter_requires_authenticated_context_and_preserves_wrapper() -> None:
    from message_envelopes import (
        annotate_message_envelope,
        build_message_envelope,
        match_message_envelope,
    )

    wrapper = build_message_envelope(
        "claude_pasted_content", body="Fixture display", id="8769",
    )
    assert match_message_envelope(wrapper, provider="claude", authenticated=False) is None
    assert match_message_envelope(wrapper, provider="codex", authenticated=True) is None

    normalized = annotate_message_envelope({
        "kind": "USER",
        "provider": "claude",
        "text": "Fixture display",
        "provider_wrapper": {
            "kind": "claude_pasted_content", "id": "8769", "provenance": "grammar",
        },
        "raw": {"provider_content": wrapper, "source": "claude-jsonl"},
    })
    assert normalized["message_envelope"] == {
        "kind": "claude_pasted_content", "id": "8769", "schema_version": 1,
    }
    assert normalized["provider_wrapper"]["id"] == "8769"
    assert normalized["raw"]["envelope_source"] == wrapper

    unauthenticated = annotate_message_envelope({
        "kind": "USER",
        "provider": "claude",
        "text": wrapper,
        "raw": {"source": "operator-input"},
    })
    assert "message_envelope" not in unauthenticated


def test_ingest_tags_user_event_and_counts_unmatched_marker(caplog) -> None:
    from message_envelopes import get_untagged_marker_count

    closed = FIXTURE["cases"][2]
    closed_text = closed["wire_text"]
    unknown_text = "[pentacle-notice:unknown-fixture]\noperator prose"

    async def run() -> list[dict]:
        store = Store(":memory:")
        store.start()
        broadcasts: list[dict] = []

        async def broadcast(frame: dict) -> None:
            broadcasts.append(frame)

        try:
            await store.open_session("host", "child", provider="codex")
            for index, text in enumerate((closed_text, unknown_text), start=1):
                event = {
                    "host": "host",
                    "provider": "codex",
                    "session_id": "host:child",
                    "session_name": "child",
                    "stream_id": "host:child",
                    "timestamp": "2026-09-20T00:00:00Z",
                    "kind": "USER",
                    "text": text,
                    "raw": {"source": "structured", "jsonl_record_uuid": f"fixture-{index}"},
                }
                assert await append_ingested_event(
                    store, broadcast, event, recent_limit=500,
                ) is not None
        finally:
            store.stop()
        return [frame["event"] for frame in broadcasts]

    before = get_untagged_marker_count()
    events = asyncio.run(run())
    assert events[0]["message_envelope"] == {
        "kind": "child_session_closed",
        "id": closed["expected_tag"]["id"],
        "schema_version": 1,
    }
    assert events[0]["raw"]["envelope_source"] == closed_text
    assert "message_envelope" not in events[1]
    assert events[1]["raw"]["envelope_source"] == unknown_text
    assert get_untagged_marker_count() == before + 1
    assert any(
        record.subsystem == "message_envelopes"
        and record.bug_ref == "spec_pentacle__message_envelope_format_registry_2026_09"
        for record in caplog.records
    )


def test_provider_wrapper_is_preserved_when_normalized_text_is_a_registered_notice() -> None:
    from message_envelopes import annotate_message_envelope

    closed = FIXTURE["cases"][2]
    wrapped = {
        "kind": "USER",
        "text": closed["wire_text"],
        "provider_wrapper": {
            "kind": "claude_pasted_content", "id": "8769", "provenance": "grammar",
        },
        "raw": {"provider_content": "wrapped-source", "source": "structured"},
    }
    result = annotate_message_envelope(wrapped)
    assert result["provider_wrapper"]["kind"] == "claude_pasted_content"
    assert result["message_envelope"]["kind"] == "child_session_closed"
    assert result["raw"]["envelope_source"] == "wrapped-source"
