"""Normalize Gemini CLI stream-json records into provider-neutral events."""
from __future__ import annotations

import json
from typing import Any

from provider_wrappers import log_provider_classification, normalize_provider_user_text
from claude_jsonl_norm import _stamp_jsonl_event_identity, _defined, _classify_user_string, _to_text

def normalize_gemini_jsonl_record(
    record: dict,
    *,
    host: str,
    session_name: str,
) -> list[dict]:
    if not isinstance(record, dict):
        return []

    record_type = record.get("type")
    session_id = str(record.get("session_id") or "")
    timestamp = str(record.get("timestamp") or "")
    record_uuid = record.get("uuid") # Maybe gemini doesn't have uuid, but we pass it anyway

    base_raw = _defined({
        "source": "structured",
        "transport": "gemini-jsonl",
        "host": host,
        "provider": "gemini",
        "session_name": session_name,
        "source_session_identity": session_id or None,
        "uuid": record_uuid,
        "model": record.get("model"),
    })

    def make(kind: str, text: str, raw_extra: dict | None = None) -> dict:
        raw = dict(base_raw)
        if raw_extra:
            raw.update(_defined(raw_extra))
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

    if record_type == "init":
        return _stamp_jsonl_event_identity([make("SYSTEM", "Gemini session initialized")], record_uuid)

    elif record_type == "message":
        role = record.get("role")
        content = record.get("content", "")
        
        if role == "user":
            norm_content, wrapper = normalize_provider_user_text(
                content, provider="gemini", authenticated=True,
            )
            kind = "SYSTEM" if _classify_user_string(norm_content) == "system" else "USER"
            event = make(kind, norm_content)
            if wrapper is not None:
                event["provider_wrapper"] = wrapper
                event["raw"]["provider_content"] = content
            return _stamp_jsonl_event_identity([event], record_uuid)
            
        elif role == "assistant":
            # Just emit ASSIST_TEXT. If delta is true, it's just text to append.
            return _stamp_jsonl_event_identity([make("ASSIST_TEXT", content, {"delta": record.get("delta")})], record_uuid)

    elif record_type == "tool_use":
        name = record.get("tool_name", "Tool")
        tool_id = record.get("tool_id")
        params = record.get("parameters", {})
        
        # Format bash tools similarly to Claude
        if name == "run_shell_command" and "command" in params:
            text = f"Bash({params['command']})"
        elif name == "read_files":
            text = f"Read {params}"
        else:
            text = f"{name}({json.dumps(params)})"
            
        return _stamp_jsonl_event_identity([make("TOOL_USE", text, {
            "tool_use_id": tool_id,
            "tool_name": name,
            "tool_input": params,
        })], record_uuid)

    elif record_type == "tool_result":
        tool_id = record.get("tool_id")
        output = _to_text(record.get("output", ""))
        is_error = record.get("status") == "error"
        
        return _stamp_jsonl_event_identity([make("TOOL_RESULT", output, {
            "tool_use_id": tool_id,
            "tool_content": output,
            "is_error": is_error,
        })], record_uuid)
        
    elif record_type == "result":
        stats = record.get("stats", {})
        duration = stats.get("duration_ms", 0)
        return _stamp_jsonl_event_identity([make("SYSTEM", f"Turn completed in {duration}ms", {
            "subtype": "turn-summary",
            "duration_ms": duration,
            "usage": stats,
        })], record_uuid)

    return []

def normalize_gemini_jsonl_records(
    records: list[dict],
    *,
    host: str,
    session_name: str,
) -> list[dict]:
    # Grouping is not strictly necessary for gemini right now, just flat map
    payloads = []
    for r in records:
        payloads.extend(normalize_gemini_jsonl_record(r, host=host, session_name=session_name))
    return payloads
