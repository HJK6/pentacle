"""Latest conversation preview shared by durable rebuild and live inventory."""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any

from message_envelopes import MESSAGE_ENVELOPES

# Startup work is bounded independently of durable history retention.
SUMMARY_TAIL_LIMIT = 500
MESSAGE_SUMMARY_FIELDS = frozenset({"last_text", "last_kind", "_last_message_at", "_last_message_seq"})
TOOL_SUMMARY_FIELDS = frozenset({"last_tool_activity", "_last_tool_seq", "_last_tool_at"})
_TOOL_CATEGORIES = {
    "claude": {"Read": "read", "Grep": "search", "Edit": "edit", "Write": "edit", "Bash": "command"},
    "cursor": {"read": "read", "write": "edit", "shell": "command"},
    "gemini": {"read_file": "read"},
}
_VISIBLE_KINDS = frozenset({"USER", "ASSIST_TEXT", "ASSIST", "TELL"})
_ENVELOPE_POLICIES = {entry.kind: entry.render_policy for entry in MESSAGE_ENVELOPES}


def event_timestamp_epoch(value: object) -> float | None:
    stamp = str(value or "").strip()
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp[:-1] + "+00:00" if stamp.endswith(("Z", "z")) else stamp)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        epoch = parsed.timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return epoch if math.isfinite(epoch) else None


def visible_message(event: dict[str, Any]) -> tuple[str, str] | None:
    """Reuse normalized conversation kinds and the envelope render registry.

    Tools/status/system rows are separate transcript activities. Registered
    notices have their own cards; only chat_prose belongs in this fallback.
    """
    kind = str(event.get("kind") or "").upper()
    text = str(event.get("text") or "").strip()
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    if kind not in _VISIBLE_KINDS or not text or raw.get("is_sidechain") or raw.get("isSidechain"):
        return None
    envelope = event.get("message_envelope")
    if kind == "USER" and isinstance(envelope, dict):
        if _ENVELOPE_POLICIES.get(str(envelope.get("kind") or "")) != "chat_prose":
            return None
    # The same source classifier already removes provider/system feedback from
    # transcript USER records. Reuse it for historical unannotated rows too.
    if kind == "USER":
        from claude_jsonl_norm import _classify_user_string
        if _classify_user_string(text) != "user":
            return None
    if raw.get("daemon_notice"):
        return None
    return text, kind


def apply_message_summary(row: dict[str, Any], event: dict[str, Any]) -> bool:
    """Advance only on a nonempty visible message in this lifecycle."""
    floor = event_timestamp_epoch(row.get("created_at"))
    epoch = event_timestamp_epoch(event.get("timestamp"))
    message = visible_message(event)
    if floor is None or epoch is None or epoch < floor or message is None:
        return False
    seq = event.get("daemon_seq")
    seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else 0
    prior_epoch = event_timestamp_epoch(row.get("_last_message_at"))
    prior_seq = row.get("_last_message_seq", 0)
    if prior_epoch is not None and (epoch, seq) <= (prior_epoch, prior_seq):
        return False
    row.update(last_text=message[0], last_kind=message[1],
               _last_message_at=event["timestamp"], _last_message_seq=seq)
    return True


def apply_tool_summary(row: dict[str, Any], event: dict[str, Any]) -> bool:
    """Reduce committed tool observations without retaining provider payloads."""
    if event.get("kind") != "TOOL_USE":
        return False
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    if raw.get("is_sidechain") or raw.get("isSidechain") or raw.get("daemon_notice"):
        return False
    seq = event.get("daemon_seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
        return False
    stamp = event.get("timestamp")
    prior = row.get("last_tool_activity") or {}
    prior_stamp = prior.get("timestamp") if isinstance(prior, dict) else None
    # The canonical timestamp already passed the floor and timezone checks.
    # Tail rebuild commonly encounters many records with that same timestamp.
    if isinstance(stamp, str) and stamp == prior_stamp:
        if seq <= row.get("_last_tool_seq", 0):
            return False
        canonical = stamp
    else:
        floor = event_timestamp_epoch(row.get("created_at"))
        if floor is None or not isinstance(stamp, str):
            return False
        try:
            parsed = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00").replace("z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.timestamp() < floor:
                return False
            parsed = parsed.astimezone(timezone.utc)
        except (ValueError, OverflowError, OSError):
            return False
        canonical = parsed.isoformat(timespec="microseconds" if parsed.microsecond else "seconds").replace("+00:00", "Z")
        if prior_stamp is not None:
            prior_dt = row.get("_last_tool_at") or datetime.fromisoformat(prior_stamp.replace("Z", "+00:00"))
            if (parsed, seq) <= (prior_dt, row.get("_last_tool_seq", 0)):
                return False
    name = raw.get("tool_name")
    provider = event.get("provider")
    category = _TOOL_CATEGORIES.get(provider, {}).get(name, "tool") if isinstance(name, str) and len(name) <= 128 and isinstance(provider, str) else "tool"
    row["last_tool_activity"] = {"category": category, "timestamp": canonical}
    row["_last_tool_seq"] = seq
    if stamp != prior_stamp:
        row["_last_tool_at"] = parsed
    return True
