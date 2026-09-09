"""Unit tests for the Codex rollout normalizer.

The assertions that matter are the ones about what is NOT a turn: a Codex
transcript opens with setup injections wearing the `user` role; counting them
as activity would make the setup look like a user turn.
"""

from __future__ import annotations

import json
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1]

from claude_jsonl_norm import normalize_claude_jsonl_records  # noqa: E402
from codex_rollout_norm import (  # noqa: E402
    codex_session_identity,
    normalize_codex_rollout_records,
)
from store import INBOUND_TURN_KINDS  # noqa: E402

FIXTURES = SERVICE_DIR / "tests" / "fixtures"


def _records(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line.strip()]


def _events(name: str = "codex_rollout_first_turn.jsonl") -> list[dict]:
    return normalize_codex_rollout_records(_records(name), host="h", session_name="v2-codex")


def _kinds_by_text(events: list[dict]) -> dict[str, str]:
    return {str(e.get("text") or "")[:24]: str(e.get("kind") or "") for e in events}


def test_environment_context_is_system_not_user() -> None:
    """Setup injections must not be classified as user activity."""
    events = _events()
    injections = [e for e in events if "<environment_context>" in str(e.get("text") or "")]
    assert injections, "fixture must contain the environment_context injection"
    for event in injections:
        assert event["kind"] == "SYSTEM", f"environment_context must be SYSTEM, got {event['kind']}"
        assert event["raw"].get("subtype") == "synthetic-user", event["raw"]


def test_developer_role_is_never_a_turn() -> None:
    """Injected instruction blocks arrive as role=developer."""
    events = _events()
    dev = [e for e in events if e["raw"].get("subtype") == "developer-instructions"]
    assert dev, "fixture must contain a developer-role injection"
    assert all(e["kind"] == "SYSTEM" for e in dev), dev


def test_typed_prompts_are_user_turns() -> None:
    """Only typed input records count as inbound turns."""
    events = _events()
    users = [e for e in events if e["kind"] == "USER"]
    # Three: two typed prompts plus one bare peer tell.
    assert len(users) == 3, _kinds_by_text(events)
    assert users[0]["text"].startswith("Inspect the example input")
    assert users[1]["text"].startswith("Inspect the next example")
    assert users[2]["text"].startswith("A synthetic coordination instruction")


def test_inbound_turn_count_is_zero_before_the_first_real_message() -> None:
    """The falsifier: replaying only the transcript prefix
    that precedes the operator's first message must yield ZERO inbound turns."""
    prefix = [r for r in _records("codex_rollout_first_turn.jsonl") if int(r.get("ordinal", 0)) <= 3]
    events = normalize_codex_rollout_records(prefix, host="h", session_name="v2-codex")
    assert events, "prefix should still normalize its injections"
    turns = [e for e in events if e["kind"] in INBOUND_TURN_KINDS]
    assert turns == [], f"no inbound turn may exist before the first typed message: {turns}"


def test_wire_shape_matches_the_claude_normalizer() -> None:
    """The normalized event keys are provider-independent."""
    codex = _events()[0]
    claude = normalize_claude_jsonl_records(
        _records("claude_jsonl_first_turn.jsonl"), host="h", session_name="v2-claude",
    )[0]
    assert set(codex) == set(claude), (sorted(codex), sorted(claude))
    assert codex["provider"] == "codex" and codex["stream_id"] == "h:v2-codex"


def test_identity_fields_are_stamped_for_dedup() -> None:
    """`jsonl_record_uuid` and `jsonl_event_index` form the durable
    unique index dedups on; without them a replay would double-insert."""
    for event in _events():
        raw = event["raw"]
        assert raw.get("jsonl_record_uuid"), event
        assert raw.get("jsonl_event_index") == 0, event
    uuids = [e["raw"]["jsonl_record_uuid"] for e in _events()]
    assert len(uuids) == len(set(uuids)), uuids


def test_session_identity_comes_from_session_meta() -> None:
    """The provider states identity once; reading `sessionId` per record (the
    Claude shape) would leave the contamination guard disarmed."""
    records = _records("codex_rollout_first_turn.jsonl")
    assert codex_session_identity(records[0]) == "session-public-1"
    assert all(codex_session_identity(r) == "" for r in records[1:])
    assert all("sessionId" not in r for r in records), "fixture must reflect the rollout schema"
    events = _events()
    assert all(e["session_id"] == "session-public-1" for e in events)


def test_bookkeeping_records_are_not_ingested() -> None:
    """`event_msg` / `token_count` are Codex bookkeeping; ingesting them would
    inflate every stream's tail with rows no view renders."""
    events = _events()
    assert all(e["raw"].get("codex_record_type") == "response_item" for e in events), events


def test_peer_tell_envelope_still_normalizes_to_tell() -> None:
    """A tell that DOES arrive enveloped is a TELL, matching the Claude path.
    (Pasted tells usually reach a Codex pane bare — which is exactly why the
    neutral count includes both USER and TELL.)"""
    record = {
        "type": "response_item", "ordinal": 1, "timestamp": "2026-08-08T18:00:00Z",
        "payload": {"type": "message", "id": "m1", "role": "user", "content": [
            {"type": "input_text", "text": "[from hosta:v2-abc] [tell:t1]\nthe payload"},
        ]},
    }
    events = normalize_codex_rollout_records([record], host="h", session_name="v2-codex")
    assert [e["kind"] for e in events] == ["TELL"], events
    assert events[0]["text"] == "the payload"


def test_normalization_never_amplifies_record_count() -> None:
    """A provider record yields at most one event and bookkeeping records yield
    none; normalization must not fan out or duplicate input."""
    records = _records("codex_rollout_first_turn.jsonl")
    events = normalize_codex_rollout_records(records, host="h", session_name="v2-codex")
    assert len(events) <= len(records), (len(events), len(records))
    for record in records:
        produced = normalize_codex_rollout_records([record], host="h", session_name="v2-codex")
        assert len(produced) <= 1, (record.get("ordinal"), len(produced))
    bookkeeping = [r for r in records if r.get("type") != "response_item"]
    assert bookkeeping, "fixture must contain bookkeeping records"
    assert normalize_codex_rollout_records(bookkeeping, host="h", session_name="v2-codex") == []


def test_inbound_agent_message_counts_outbound_does_not() -> None:
    """Agent messages use their address depth to distinguish inbound turns from
    the primary agent's own outbound dispatch."""
    events = _events("codex_rollout_agent_message.jsonl")
    kinds = [(e["kind"], e["raw"].get("recipient")) for e in events]
    assert ("TELL", "agent/primary") in kinds, kinds
    assert [e["kind"] for e in events if e["raw"].get("recipient") == "agent/primary/worker"] == ["SYSTEM"], kinds
    inbound = [e for e in events if e["kind"] in INBOUND_TURN_KINDS]
    assert len(inbound) == 1, "only the message addressed to the primary agent counts"
    assert inbound[0]["raw"].get("sender") == "agent/primary/worker"


def test_classifier_covers_the_synthetic_envelope_inventory() -> None:
    """The classifier recognizes the setup-envelope vocabulary."""
    for tag in ("<environment_context>", "<recommended_plugins>", "<turn_aborted>"):
        record = {
            "type": "response_item", "ordinal": 1, "timestamp": "2026-08-08T18:00:00Z",
            "payload": {"type": "message", "id": f"m{tag}", "role": "user",
                        "content": [{"type": "input_text", "text": f"{tag}\nExample body\n"}]},
        }
        events = normalize_codex_rollout_records([record], host="h", session_name="v2-codex")
        assert [e["kind"] for e in events] == ["SYSTEM"], (tag, events)


def test_a_session_opening_with_a_typed_turn_is_not_swallowed() -> None:
    """A typed first record remains a user turn; classification is by content,
    never by position."""
    record = {
        "type": "response_item", "ordinal": 1, "timestamp": "2026-08-08T18:00:00Z",
        "payload": {"type": "message", "id": "m1", "role": "user",
                    "content": [{"type": "input_text", "text": "Fix the failing test."}]},
    }
    events = normalize_codex_rollout_records([record], host="h", session_name="v2-codex")
    assert [e["kind"] for e in events] == ["USER"], events
