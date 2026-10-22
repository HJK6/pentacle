"""Normalize Gemini CLI native chat-recording records into provider-neutral events.

The interactive pane cannot stream `-o stream-json` (that formatter only runs in
headless mode), so the transcript source is the CLI's always-on native chat
recording: `$HOME/.gemini/tmp/<project>/chats/session-<UTC-minute>-<shortId>.jsonl`.
Its schema (ground truth from real recordings):

  {"sessionId", "projectHash", "startTime", "lastUpdated", "kind"}   file header
  {"$set": {"lastUpdated": ...}}                                     metadata bump
  {"id", "timestamp", "type": "user",   "content": [{"text": ...}]}  user turn
  {"id", "timestamp", "type": "gemini", "content": str|[parts],
   "thoughts": [...], "toolCalls": [{"id", "name", "args", "result":
     [{"functionResponse": {"id", "name", "response": {"output": ...}}}]]}}
  {"id", "timestamp", "type": "error",  "content": str}              API error
  {"id", "timestamp", "type": "info",   "content": str}              CLI notice

`content` may be a string or a list of text parts on user and gemini records.
The CLI-injected `<session_context>` user record is classified SYSTEM so it
never opens turn 1 (first-turn-nudge parity with claude).
"""
from __future__ import annotations

import json
from typing import Any

from claude_jsonl_norm import _stamp_jsonl_event_identity, _classify_user_string, _to_text
from provider_wrappers import normalize_provider_user_text

_RECORD_SESSION_KEYS = ("sessionId", "session_id")


def _record_session_id(record: dict) -> str:
    for key in _RECORD_SESSION_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


#: `error`/`info` records carry no sessionId; the stream's bound identity fills
#: them in so every emitted event keeps its session attribution.
def _event_session_id(record: dict, default: str) -> str:
    return _record_session_id(record) or default


def normalize_gemini_jsonl_record(
    record: dict,
    *,
    host: str,
    session_name: str,
    default_session_id: str = "",
) -> list[dict]:
    if not isinstance(record, dict):
        return []

    record_type = record.get("type")
    session_id = _event_session_id(record, default_session_id)
    timestamp = str(record.get("timestamp") or "")
    record_uuid = record.get("id")

    base_raw = {
        "source": "structured",
        "transport": "gemini-jsonl",
        "host": host,
        "provider": "gemini",
        "session_name": session_name,
        "source_session_identity": session_id or None,
        "uuid": record_uuid,
    }

    def make(kind: str, text: str, raw_extra: dict | None = None) -> dict:
        raw = dict(base_raw)
        if raw_extra:
            raw.update({k: v for k, v in raw_extra.items() if v is not None})
        return {
            "host": host,
            "provider": "gemini",
            "session_id": session_id,
            "session_name": session_name,
            "stream_id": f"{host}:{session_name}",
            "timestamp": timestamp,
            "kind": kind,
            "text": text,
            "raw": raw,
        }

    # File header and `$set` metadata bumps carry no conversational content.
    if record_type is None:
        return []

    if record_type == "user":
        content = _to_text(record.get("content"))
        if not content.strip():
            return []
        # The CLI injects its own setup envelope into the USER role before the
        # first typed turn; it must not open turn 1.
        if content.lstrip().startswith("<session_context>"):
            return _stamp_jsonl_event_identity(
                [make("SYSTEM", "Gemini session context established")], record_uuid)
        norm_content, wrapper = normalize_provider_user_text(
            content, provider="gemini", authenticated=True,
        )
        kind = "SYSTEM" if _classify_user_string(norm_content) == "system" else "USER"
        event = make(kind, norm_content)
        if wrapper is not None:
            event["provider_wrapper"] = wrapper
            event["raw"]["provider_content"] = content
        return _stamp_jsonl_event_identity([event], record_uuid)

    if record_type == "gemini":
        events: list[dict] = []
        # Tool activity is recorded inline on the assistant record; the final
        # text is the turn's answer, so tool use/results come first.
        for tool_call in record.get("toolCalls") or []:
            if not isinstance(tool_call, dict):
                continue
            name = str(tool_call.get("name") or "Tool")
            args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
            events.append(make("TOOL_USE", f"{name}({json.dumps(args, separators=(',', ':'))})", {
                "tool_use_id": tool_call.get("id"),
                "tool_name": name,
                "tool_input": args,
            }))
            output = _gemini_tool_output(tool_call.get("result"))
            if output is not None:
                events.append(make("TOOL_RESULT", output, {
                    "tool_use_id": tool_call.get("id"),
                    "tool_content": output,
                }))
        text = _to_text(record.get("content"))
        if text.strip():
            events.append(_stamp_jsonl_event_identity(
                [make("ASSIST_TEXT", text)], record_uuid)[0])
        return events if events else []

    if record_type == "error":
        text = _to_text(record.get("content"))
        if not text.strip():
            return []
        return _stamp_jsonl_event_identity([make("SYSTEM", text, {
            "subtype": "api-error",
        })], record_uuid)

    if record_type == "info":
        text = _to_text(record.get("content"))
        if not text.strip():
            return []
        return _stamp_jsonl_event_identity([make("SYSTEM", text, {
            "subtype": "cli-info",
        })], record_uuid)

    # Unknown record types (future schema additions) are skipped, never guessed.
    return []


def _gemini_tool_output(result: Any) -> str | None:
    """Extract display text from a recorded toolCall `result` part list.

    Ground truth: `[{"functionResponse": {"id", "name",
    "response": {"output": ...}}}]`. Absent/empty results yield None so the
    TOOL_RESULT event is not emitted for calls still awaiting output.
    """
    if isinstance(result, str):
        return result
    if not isinstance(result, list):
        return None
    parts: list[str] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        response = (item.get("functionResponse") or {}).get("response")
        if isinstance(response, dict):
            output = response.get("output")
            if output is None:
                output = response.get("error")
            if output is not None:
                parts.append(_to_text(output))
        elif item.get("text"):
            parts.append(_to_text(item.get("text")))
    return "\n".join(parts) if parts else None


def normalize_gemini_jsonl_records(
    records: list[dict],
    *,
    host: str,
    session_name: str,
    default_session_id: str = "",
) -> list[dict]:
    # Flat map: the native recording is append-only and self-contained per
    # record, so no cross-record grouping is needed.
    payloads = []
    for r in records:
        payloads.extend(normalize_gemini_jsonl_record(
            r, host=host, session_name=session_name,
            default_session_id=default_session_id,
        ))
    return payloads
