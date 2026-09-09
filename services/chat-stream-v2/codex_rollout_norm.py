"""Normalize Codex-style rollout records into provider-neutral events.

The implementation handles the rollout schema's session metadata, messages,
tool records, and provider setup messages without performing I/O.  It shares
the conservative user-content classifier with the Claude-style normalizer.
"""

from __future__ import annotations

from typing import Any

from claude_jsonl_norm import (
    _classify_user_string,
    _defined,
    _parse_peer_tell,
    _stamp_jsonl_event_identity,
    _to_text,
)

__all__ = [
    "normalize_codex_rollout_record",
    "normalize_codex_rollout_records",
    "codex_session_identity",
]

# `response_item` payload types carrying a tool invocation / its output. Codex
# splits these two ways (`function_call` for native tools, `custom_tool_call`
# for the freeform ones); both mean the same thing on the wire.
_TOOL_CALL_TYPES = ("function_call", "custom_tool_call")
_TOOL_OUTPUT_TYPES = ("function_call_output", "custom_tool_call_output")


def codex_session_identity(record: dict) -> str:
    """The session id a `session_meta` record declares, else "".

    Codex states identity ONCE per transcript instead of stamping every record,
    so the ingest guard against binding a foreign transcript has to read it here
    rather than from each line the way the Claude path does.
    """
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return ""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("id") or payload.get("session_id") or "")


def _is_primary_address(address: str) -> bool:
    """True for the primary endpoint (`/root`), false for a sub-agent
    (`/root/<child>`). Codex addresses its own fan-out by path depth, so depth
    is what separates inbound-to-this-seat from outbound traffic."""
    cleaned = str(address or "").strip().rstrip("/")
    return bool(cleaned) and cleaned.count("/") == 1


def _payload_text(payload: dict) -> str:
    """Concatenate a Codex message payload's content items into one string.

    Content is a list of `{type: input_text|output_text|text, text: ...}`; the
    key varies by item type, so fall back across the shapes the way `_to_text`
    does for Claude blocks.
    """
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = _to_text(item.get("text") or item.get("content") or "")
        if text:
            parts.append(text)
    return "\n".join(parts)


def normalize_codex_rollout_record(
    record: dict,
    *,
    host: str,
    session_name: str,
    session_id: str = "",
) -> list[dict]:
    """Map one rollout record to zero-or-more wire payloads.

    Mirrors `normalize_claude_jsonl_record`'s contract: payloads carry no
    `daemon_seq` (the caller stamps that when broadcasting) and the identity
    fields are stamped by `_stamp_jsonl_event_identity` so the durable dedup
    index treats a replay as exactly-once.
    """
    if not isinstance(record, dict):
        return []
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return []

    timestamp = str(record.get("timestamp") or "")
    # `payload.id` is the per-item identity (non-null on every message sampled);
    # `ordinal` is the transcript-wide monotonic counter. Prefer the former and
    # fall back to the latter so a payload without an id still dedups.
    record_uuid = payload.get("id")
    if record_uuid is None and record.get("ordinal") is not None:
        record_uuid = f"ordinal:{record.get('ordinal')}"

    base_raw = _defined({
        "source": "structured",
        "transport": "codex-rollout",
        "host": host,
        "provider": "codex",
        "session_name": session_name,
        "source_session_identity": session_id or None,
        "uuid": payload.get("id"),
        "ordinal": record.get("ordinal"),
        "codex_record_type": record.get("type"),
        "codex_payload_type": payload.get("type"),
    })

    def make(kind: str, text: str, raw_extra: dict | None = None) -> dict:
        raw = dict(base_raw)
        if raw_extra:
            raw.update(_defined(raw_extra))
        return {
            "host": host,
            "provider": "codex",
            "session_id": session_id,
            "session_name": session_name,
            "stream_id": f"{host}:{session_name}",
            "timestamp": timestamp,
            "kind": kind,
            "text": text,
            "raw": raw,
        }

    record_type = record.get("type")
    payload_type = payload.get("type")

    if record_type != "response_item":
        # `event_msg` (token counts, item_completed), `turn_context` and
        # `world_state` are Codex bookkeeping, not conversation. Ingesting them
        # would inflate every stream's tail with rows no view renders.
        return []

    if payload_type == "message":
        role = str(payload.get("role") or "")
        text = _payload_text(payload)
        if not text.strip():
            return []
        if role == "assistant":
            return _stamp_jsonl_event_identity([make("ASSIST_TEXT", text)], record_uuid)
        if role == "developer":
            # Injected instruction blocks. Never a turn — see module docstring.
            return _stamp_jsonl_event_identity(
                [make("SYSTEM", text, {"subtype": "developer-instructions"})], record_uuid,
            )
        if role != "user":
            return []
        peer_tell = _parse_peer_tell(text)
        if peer_tell is not None:
            return _stamp_jsonl_event_identity(
                [make("TELL", peer_tell["payload"], {
                    "sender": peer_tell["sender"],
                    "tell_id": peer_tell["tell_id"],
                    "enqueued_at": peer_tell["enqueued_at"],
                    "peer_payload": peer_tell["payload"],
                })],
                record_uuid,
            )
        if _classify_user_string(text) == "system":
            # `<environment_context>` and friends: harness injections wearing the
            # user role. Emitting USER here is the first-turn defect.
            return _stamp_jsonl_event_identity(
                [make("SYSTEM", text, {"subtype": "synthetic-user"})], record_uuid,
            )
        return _stamp_jsonl_event_identity([make("USER", text)], record_uuid)

    if payload_type == "agent_message":
        # the provider's own sub-agent protocol (`/root` <-> `/root/<child>`), not a
        # peer-delivery message. Direction decides: a message addressed TO the
        # pane's primary agent is inbound work arriving at this seat and counts;
        # one the primary agent sent outward does not. Dropping these entirely
        # (an earlier implementation did) makes real inbound messages invisible to the
        # turn count.
        text = _payload_text(payload)
        if not text.strip():
            return []
        recipient = str(payload.get("recipient") or "")
        author = str(payload.get("author") or "")
        directed = {"author": author, "recipient": recipient}
        if _is_primary_address(recipient):
            return _stamp_jsonl_event_identity(
                [make("TELL", text, {"sender": author, "peer_payload": text, **directed})],
                record_uuid,
            )
        return _stamp_jsonl_event_identity(
            [make("SYSTEM", text, {"subtype": "agent-message-outbound", **directed})],
            record_uuid,
        )

    if payload_type == "reasoning":
        summary = payload.get("summary")
        parts: list[str] = []
        if isinstance(summary, list):
            for item in summary:
                if isinstance(item, dict):
                    text = _to_text(item.get("text") or "")
                    if text:
                        parts.append(text)
        thinking = "\n".join(parts)
        return _stamp_jsonl_event_identity(
            [make("THINKING", thinking or "Thinking", {"thinking": thinking})], record_uuid,
        )

    if payload_type in _TOOL_CALL_TYPES:
        name = str(payload.get("name") or "Tool")
        tool_input = payload.get("arguments")
        if tool_input is None:
            tool_input = payload.get("input")
        return _stamp_jsonl_event_identity(
            [make("TOOL_USE", name, {
                "tool_use_id": payload.get("call_id") or payload.get("id"),
                "tool_name": name,
                "tool_input": tool_input,
            })],
            record_uuid,
        )

    if payload_type in _TOOL_OUTPUT_TYPES:
        output = payload.get("output")
        text = _to_text(output if not isinstance(output, dict) else output.get("content") or "")
        return _stamp_jsonl_event_identity(
            [make("TOOL_RESULT", text, {
                "tool_use_id": payload.get("call_id") or payload.get("id"),
                "tool_content": text,
            })],
            record_uuid,
        )

    return []


def normalize_codex_rollout_records_grouped(
    records: list[dict],
    *,
    host: str,
    session_name: str,
    session_id: str = "",
) -> list[list[dict]]:
    """Per-record grouping of :func:`normalize_codex_rollout_records`.

    Returns one payload list per input record — empty for a `session_meta` (it
    only updates `session_id`) or any record that yields nothing — aligned 1:1
    with `records`. A byte-accurate tail zips this against per-record end offsets
    to advance past exactly the records it consumed.
    """
    groups: list[list[dict]] = []
    for record in records:
        found = codex_session_identity(record)
        if found:
            session_id = found
            groups.append([])
            continue
        groups.append(normalize_codex_rollout_record(
            record, host=host, session_name=session_name, session_id=session_id,
        ))
    return groups


def normalize_codex_rollout_records(
    records: list[dict],
    *,
    host: str,
    session_name: str,
    session_id: str = "",
) -> list[dict]:
    """Normalize an ordered Codex rollout span.

    `session_meta` appears only at the head of a transcript, so a span read from
    a later offset has no identity of its own. The caller seeds `session_id`
    with what it already learned (ingest keeps it per stream); a `session_meta`
    inside this span overrides it.
    """
    return [
        payload
        for group in normalize_codex_rollout_records_grouped(
            records, host=host, session_name=session_name, session_id=session_id,
        )
        for payload in group
    ]
