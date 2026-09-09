"""Normalize Claude-style JSONL records into provider-neutral events.

This module contains pure record-to-event transformations.  Input and output
are ordinary Python values, so applications can choose their own transport and
persistence boundaries.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any


def _to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text") or item.get("content")
            if isinstance(text, str) and text:
                parts.append(text)
            elif item.get("type") == "tool_reference" and isinstance(item.get("tool_name"), str):
                parts.append(item["tool_name"])
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content, separators=(",", ":"))


def _tool_use_text(name: str, tool_input: dict) -> str:
    if name == "Bash":
        return f"Bash({str(tool_input.get('command', '')).strip()})"
    if name == "Read":
        return f"Read {str(tool_input.get('file_path', '')).strip()}"
    if name == "Write":
        return f"Write {str(tool_input.get('file_path', '')).strip()}"
    if name == "Edit":
        return f"Edit {str(tool_input.get('file_path', '')).strip()}"
    if name == "Agent":
        desc = tool_input.get("description") or tool_input.get("subagent_type") or "Sub-agent"
        return f"Agent: {str(desc).strip()}"
    if tool_input:
        return f"{name} {json.dumps(tool_input, separators=(',', ':'))}"
    return name


def _content_blocks(record: dict) -> list[dict]:
    msg = record.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _defined(value: dict) -> dict:
    return {k: v for k, v in value.items() if v is not None}


def _stamp_jsonl_event_identity(events: list[dict], record_uuid: object) -> list[dict]:
    for index, event in enumerate(events):
        raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
        raw = dict(raw)
        if record_uuid is not None:
            raw["jsonl_record_uuid"] = record_uuid
        raw["jsonl_event_index"] = index
        event["raw"] = raw
    return events


_SYNTHETIC_USER_TAGS = (
    "<task-notification>",
    "<system-reminder>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<command-output>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<bash-input>",
    # Some providers place setup records in the USER role: `<environment_context>`
    # is emitted before a user types anything, so classifying it as a real
    # turn puts turn 1 at session birth (the first-turn-nudge defect). Kept in
    # this shared tuple rather than a provider-specific copy — one classifier, one
    # rule, both providers.
    "<environment_context>",
    "<user_instructions>",
    "<recommended_plugins>",
    "<turn_aborted>",
    "[Request interrupted",
)
#: The classifier is envelope-based rather than position-based: of the openers,
#: `<environment_context>` 103, `<recommended_plugins>` 2, and one session that
#: genuinely opens with a typed turn; `<turn_aborted>` appears mid-session.
#: Classification is therefore by ENVELOPE, never by position — the first
#: `role=user` record is usually an injection but not always, and a rule keyed on
#: "first record" would under-count that session's opening turn.
#: A deliberately conservative rule: a literal user message
#: that begins with one of these exact tags is classified SYSTEM.
_HIDDEN_SYSTEM_SUBTYPES: set[str] = set()
# Anchor `tell` or `send`: the daemon stamps agent-orch tell/send alike
# (the peer-delivery layer); both normalize to kind:TELL with sender/payload.
# The newline after the anchor is REQUIRED (the daemon always emits `\n` before the
# payload): without it, human prose like "[from bob] [send:notes] please send them"
# would be misclassified as a peer delivery.
_PEER_TELL_RE = re.compile(
    r"^\[from (?P<sender>[^\]]+)\] \[(?:tell|send):(?P<tell_id>[^\]]+)\]"
    r"(?: enqueued_at=(?P<enqueued_at>[^\s]+))?\r?\n(?P<payload>[\s\S]*)$"
)


_PEER_DELIVERY_HEADER_RE = re.compile(
    r"^\[from [^\]\r\n]+\] \[(?:tell|send):[^\]\r\n]+\](?: enqueued_at=\S+)?\r?\n"
)


def strip_peer_delivery_envelope(text: str) -> str:
    """Remove a leading peer-delivery header, leaving the payload. A bare (unstamped)
    body is returned unchanged. Shared by comms (proof/gate) and the send-receipt
    projection so a stamped wire and its header-stripped normalized event correlate."""
    return _PEER_DELIVERY_HEADER_RE.sub("", str(text or ""), count=1)


def _parse_peer_tell(text: str) -> dict[str, str] | None:
    match = _PEER_TELL_RE.match(str(text or "").strip())
    if match is None:
        return None
    payload = match.group("payload").strip()
    if not payload:
        return None
    return {
        "sender": match.group("sender"),
        "tell_id": match.group("tell_id"),
        "enqueued_at": match.group("enqueued_at") or "",
        "payload": payload,
    }


def _format_turn_duration_text(duration_ms: int, message_count: int | None) -> str:
    seconds = max(0, int(duration_ms) // 1000)
    if seconds < 60:
        duration = f"{seconds}s"
    else:
        minutes = seconds // 60
        rem = seconds % 60
        duration = f"{minutes}m {rem}s" if rem else f"{minutes}m"
    if message_count:
        return f"Worked for {duration} · {message_count} msgs"
    return f"Worked for {duration}"


def _classify_user_string(text: str) -> str:
    """Return 'user' for real human input, 'system' for synthetic envelopes
    (task notifications, system reminders, command outputs)."""
    stripped = text.lstrip()
    for tag in _SYNTHETIC_USER_TAGS:
        if stripped.startswith(tag):
            return "system"
    return "user"


def _typed_user_text(record: dict) -> str:
    if record.get("type") != "user":
        return ""
    msg = record.get("message") or {}
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") == "tool_result":
                continue
            text = _to_text(block.get("text") or block.get("content") or "")
            if text:
                parts.append(text)
        text = "\n".join(parts)
    else:
        return ""
    if not text.strip():
        return ""
    if _classify_user_string(text) != "user":
        return ""
    return text


def _is_interrupted_user_record(record: dict) -> bool:
    if record.get("type") != "user" or not record.get("interruptedMessageId"):
        return False
    msg = record.get("message") or {}
    if not isinstance(msg, dict):
        return False
    return _to_text(msg.get("content")).lstrip().startswith("[Request interrupted")


def _claude_user_delivery_states(records: list[dict]) -> dict[str, str]:
    children_by_parent: dict[str, list[int]] = {}
    siblings_by_parent: dict[str, list[int]] = {}

    for index, record in enumerate(records):
        parent_uuid = record.get("parentUuid")
        if isinstance(parent_uuid, str) and parent_uuid:
            children_by_parent.setdefault(parent_uuid, []).append(index)
            siblings_by_parent.setdefault(parent_uuid, []).append(index)

    def has_later_child(record_uuid: str, index: int) -> bool:
        return any(child_index > index for child_index in children_by_parent.get(record_uuid, []))

    states: dict[str, str] = {}
    for index, record in enumerate(records):
        record_uuid = record.get("uuid")
        if not isinstance(record_uuid, str) or not record_uuid:
            continue
        if _is_interrupted_user_record(record):
            states[record_uuid] = "interrupted"
            continue
        if record.get("isSidechain"):
            continue
        if not _typed_user_text(record) or record.get("interruptedMessageId"):
            continue
        if has_later_child(record_uuid, index):
            states[record_uuid] = "sent"
            continue

        parent_uuid = record.get("parentUuid")
        superseding_sibling = False
        if isinstance(parent_uuid, str) and parent_uuid:
            for sibling_index in siblings_by_parent.get(parent_uuid, []):
                if sibling_index == index:
                    continue
                sibling = records[sibling_index]
                sibling_uuid = sibling.get("uuid")
                if not isinstance(sibling_uuid, str) or not sibling_uuid:
                    continue
                if has_later_child(sibling_uuid, sibling_index):
                    superseding_sibling = True
                    break
        states[record_uuid] = "returned_to_prompt" if superseding_sibling else "sent"
    return states


def _annotate_user_delivery_state(payloads: list[dict], states: dict[str, str]) -> list[dict]:
    for payload in payloads:
        raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
        record_uuid = raw.get("jsonl_record_uuid") or raw.get("uuid")
        state = states.get(record_uuid) if isinstance(record_uuid, str) else None
        if not state:
            continue
        kind = payload.get("kind")
        if state in {"sent", "returned_to_prompt"} and kind != "USER":
            continue
        if state == "interrupted" and kind != "SYSTEM":
            continue
        raw = dict(raw)
        raw["user_delivery_state"] = state
        if state == "returned_to_prompt":
            raw["returned_to_prompt"] = True
        elif state == "interrupted":
            raw["interrupted"] = True
        payload["raw"] = raw
    return payloads


def normalize_claude_jsonl_record(
    record: dict,
    *,
    host: str,
    session_name: str,
) -> list[dict]:
    """Map one JSONL record to zero-or-more wire payloads.

    Returns payloads without `daemon_seq` — the daemon stamps that when
    broadcasting. Output shape mirrors `claudeJsonlNormalize.ts` so the
    cross-language fixtures agree on key structure.
    """
    if not isinstance(record, dict):
        return []
    record_type = record.get("type")
    session_id = str(record.get("sessionId") or record.get("session_id") or "")
    timestamp = str(record.get("timestamp") or "")
    record_uuid = record.get("uuid")
    msg = record.get("message") or {}
    usage = msg.get("usage") if isinstance(msg, dict) and isinstance(msg.get("usage"), dict) else None
    base_raw = _defined({
        "source": "structured",
        "transport": "claude-jsonl",
        "host": host,
        "provider": "claude",
        "session_name": session_name,
        "source_session_identity": session_id or None,
        "uuid": record_uuid,
        "parent_uuid": record.get("parentUuid"),
        "request_id": record.get("requestId"),
        "message_id": msg.get("id") if isinstance(msg, dict) else None,
        "is_sidechain": bool(record.get("isSidechain")),
        "stop_reason": msg.get("stop_reason") if isinstance(msg, dict) else None,
        "usage": usage,
        "model": msg.get("model") if isinstance(msg, dict) else None,
        "effort": record.get("effort"),
    })

    def make(kind: str, text: str, raw_extra: dict | None = None) -> dict:
        raw = dict(base_raw)
        if raw_extra:
            raw.update(_defined(raw_extra))
        return {
            "host": host,
            "provider": "claude",
            "session_id": session_id,
            "session_name": session_name,
            "stream_id": f"{host}:{session_name}",
            "timestamp": timestamp,
            "kind": kind,
            "text": text,
            "raw": raw,
        }

    if record_type == "system":
        subtype = record.get("subtype")
        if subtype in _HIDDEN_SYSTEM_SUBTYPES:
            return []
        if subtype == "turn_duration":
            duration_ms = int(record.get("durationMs") or 0)
            message_count = record.get("messageCount") or 0
            text = _format_turn_duration_text(duration_ms, message_count)
            return _stamp_jsonl_event_identity([make("SYSTEM", text, {
                "subtype": "turn-summary",
                "duration_ms": duration_ms,
                "message_count": message_count,
            })], record_uuid)
        text = _to_text(record.get("content") or msg.get("content") or subtype or "")
        if subtype == "local_command" and _classify_user_string(text) == "system":
            return _stamp_jsonl_event_identity(
                [make("SYSTEM", text, {"subtype": "synthetic-user", "claude_subtype": subtype})],
                record_uuid,
            )
        return _stamp_jsonl_event_identity([make("SYSTEM", text, {"subtype": subtype})], record_uuid)

    if record_type == "user":
        content = msg.get("content")
        if isinstance(content, str):
            peer_tell = _parse_peer_tell(content)
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
            classification = _classify_user_string(content)
            if classification == "system":
                return _stamp_jsonl_event_identity([make("SYSTEM", content, {"subtype": "synthetic-user"})], record_uuid)
            return _stamp_jsonl_event_identity([make("USER", content)], record_uuid)
        events: list[dict] = []
        user_text: list[str] = []
        for block in _content_blocks(record):
            btype = block.get("type")
            if btype == "tool_result":
                text = _to_text(block.get("content"))
                events.append(make("TOOL_RESULT", text, {
                    "tool_use_id": block.get("tool_use_id"),
                    "tool_content": text,
                    "is_error": bool(block.get("is_error")),
                }))
            else:
                text = _to_text(block.get("text") or block.get("content") or "")
                if text:
                    user_text.append(text)
        if user_text:
            joined = "\n".join(user_text)
            peer_tell = _parse_peer_tell(joined)
            if peer_tell is not None:
                events.insert(0, make("TELL", peer_tell["payload"], {
                    "sender": peer_tell["sender"],
                    "tell_id": peer_tell["tell_id"],
                    "enqueued_at": peer_tell["enqueued_at"],
                    "peer_payload": peer_tell["payload"],
                }))
                return _stamp_jsonl_event_identity(events, record_uuid)
            if _classify_user_string(joined) == "system":
                events.insert(0, make("SYSTEM", joined, {"subtype": "synthetic-user"}))
            else:
                events.insert(0, make("USER", joined))
        return _stamp_jsonl_event_identity(events, record_uuid)

    if record_type == "attachment":
        attachment = record.get("attachment")
        if not isinstance(attachment, dict):
            return []
        origin = attachment.get("origin")
        if (
            attachment.get("type") == "queued_command"
            and isinstance(origin, dict)
            and origin.get("kind") == "human"
        ):
            prompt = str(attachment.get("prompt") or "")
            if not prompt:
                return []
            return _stamp_jsonl_event_identity(
                [make("USER", prompt, {"subtype": "queued-command", "queued_at": timestamp})],
                record_uuid,
            )
        return []

    if record_type != "assistant":
        return []

    events = []
    for block in _content_blocks(record):
        btype = block.get("type")
        if btype == "text":
            events.append(make("ASSIST_TEXT", str(block.get("text") or "")))
        elif btype == "thinking":
            thinking = str(block.get("thinking") or "")
            events.append(make("THINKING", thinking or "Thinking", {"thinking": thinking}))
        elif btype == "tool_use":
            name = str(block.get("name") or "Tool")
            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            events.append(make("TOOL_USE", _tool_use_text(name, tool_input), {
                "tool_use_id": block.get("id"),
                "tool_name": name,
                "tool_input": tool_input,
            }))
    return _stamp_jsonl_event_identity(events, record_uuid)


def _parse_timestamp_epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _latest_event_timestamp(current: str | None, candidate: object) -> str | None:
    candidate_text = str(candidate or "")
    if not candidate_text:
        return current
    if current is None:
        return candidate_text
    current_epoch = _parse_timestamp_epoch(current)
    candidate_epoch = _parse_timestamp_epoch(candidate_text)
    if current_epoch is None:
        return candidate_text
    if candidate_epoch is None:
        return current
    return candidate_text if candidate_epoch >= current_epoch else current


def _normalize_queued_command_timestamp(payload: dict, *, min_timestamp: str | None) -> dict:
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    if payload.get("kind") != "USER" or raw.get("subtype") != "queued-command":
        return payload
    original_timestamp = str(payload.get("timestamp") or "")
    payload = dict(payload)
    raw = dict(raw)
    if original_timestamp:
        raw.setdefault("queued_at", original_timestamp)
    payload["raw"] = raw
    if not min_timestamp or not original_timestamp:
        return payload
    original_epoch = _parse_timestamp_epoch(original_timestamp)
    min_epoch = _parse_timestamp_epoch(min_timestamp)
    if original_epoch is None or min_epoch is None or original_epoch >= min_epoch:
        return payload
    payload["timestamp"] = min_timestamp
    return payload


def normalize_claude_jsonl_records_grouped(
    records: list[dict],
    *,
    host: str,
    session_name: str,
) -> list[list[dict]]:
    """Per-record grouping of :func:`normalize_claude_jsonl_records`.

    Same cross-record delivery/timestamp resolution (states computed over the
    whole span, ``latest_timestamp`` carried), but the payloads are grouped 1:1
    with `records` so a byte-accurate tail can advance past exactly the records
    it consumed. Flattening the groups reproduces the batch output verbatim.
    """
    states = _claude_user_delivery_states(records)
    groups: list[list[dict]] = []
    latest_timestamp: str | None = None
    for record in records:
        payloads = normalize_claude_jsonl_record(
            record,
            host=host,
            session_name=session_name,
        )
        group: list[dict] = []
        for payload in _annotate_user_delivery_state(payloads, states):
            payload = _normalize_queued_command_timestamp(payload, min_timestamp=latest_timestamp)
            group.append(payload)
            latest_timestamp = _latest_event_timestamp(latest_timestamp, payload.get("timestamp"))
        groups.append(group)
    return groups


def normalize_claude_jsonl_records(
    records: list[dict],
    *,
    host: str,
    session_name: str,
) -> list[dict]:
    """Normalize an ordered Claude JSONL transcript and resolve user delivery.

    ``returned_to_prompt`` is a cross-record state: a real typed user message
    with no child is only known to have been returned once a sibling branch
    advances. Keep the single-record mapper pure and add the delivery metadata
    here where the parent/child graph is available.
    """
    return [
        payload
        for group in normalize_claude_jsonl_records_grouped(
            records, host=host, session_name=session_name,
        )
        for payload in group
    ]
