"""Latest conversation preview shared by durable rebuild and live inventory."""
from __future__ import annotations

from datetime import datetime
import math
from typing import Any

from message_envelopes import MESSAGE_ENVELOPES

# Startup work is bounded independently of durable history retention.
SUMMARY_TAIL_LIMIT = 500
MESSAGE_SUMMARY_FIELDS = frozenset({"last_text", "last_kind", "_last_message_at", "_last_message_seq"})
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
    except (TypeError, ValueError):
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
