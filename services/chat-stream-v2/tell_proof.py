"""Evidence-only reconciliation of an ordinary tell's original native input."""
from __future__ import annotations

import json

from submission_events import EventProof, provider_text_digest, submission_text_matches


def finalize_prepared(conn, original, result):
    """Only the exact original payload and pre-input anchors may complete custody."""
    old = original.get("delivery") or {}
    final = result.get("delivery") or {}
    reply = result.get("reply") or {}
    anchors = ("tell_id", "to_stream_id", "text", "request_payload_hash", "proof_generation",
               "proof_session_created_at", "proof_watermark", "proof_watermark_state", "proof_watermark_reason")
    if (original.get("payload_digest") != result.get("payload_digest")
            or old.get("delivery_status") != "proof_unavailable" or old.get("submission_attempts") != 0
            or old.get("notification_answer_generation")
            or final.get("delivery_status") not in {"delivered", "committed_pending_proof", "pasted_unsubmitted"}
            or reply.get("delivery_status") != final.get("delivery_status")
            or reply.get("tell_id") != final.get("tell_id")
            or reply.get("to_stream_id") != final.get("to_stream_id")
            or any(old.get(k) != final.get(k) for k in anchors)):
        return None
    target = old.get("to_stream_id") or ""
    host, _, name = target.partition(":")
    current = conn.execute("SELECT s.created_at,g.generation FROM sessions s JOIN v2_session_generations g "
        "ON g.host=s.host AND g.session_name=s.session_name WHERE s.host=? AND s.session_name=?",
        (host, name)).fetchone()
    if current is None or (current["created_at"], current["generation"]) != (
            old.get("proof_session_created_at"), old.get("proof_generation")):
        return None
    return result


def retained_proof(conn, payload):
    """Run inside the promotion transaction; never infer missing original anchors."""
    delivery = payload.get("delivery") or {}
    target = delivery.get("to_stream_id")
    generation = delivery.get("proof_generation")
    birth = delivery.get("proof_session_created_at")
    watermark = delivery.get("proof_watermark")
    text = delivery.get("text")
    if (not isinstance(target, str) or ":" not in target or not generation or not birth
            or type(watermark) is not int or watermark < 0
            or delivery.get("proof_watermark_state") != "reachable"
            or not isinstance(text, str) or not text
            or delivery.get("request_payload_hash") != payload.get("payload_digest")):
        return None
    host, name = target.split(":", 1)
    current = conn.execute(
        "SELECT s.created_at,g.generation FROM sessions s JOIN v2_session_generations g "
        "ON g.host=s.host AND g.session_name=s.session_name "
        "WHERE s.host=? AND s.session_name=?", (host, name)).fetchone()
    if current is None or (current["created_at"], current["generation"]) != (birth, generation):
        return None
    matches = []
    # The retained history may be older than the current 500-event live tail.
    for row in conn.execute(
            "SELECT event_id,event_json FROM session_event_tail "
            "WHERE stream_id=? AND session_created_at=? AND event_id>? ORDER BY event_id",
            (target, birth, watermark)):
        try:
            event = json.loads(row["event_json"])
            if (event.get("stream_id", target) == target
                    and (event.get("kind") == "USER" or (event.get("kind") == "TELL"
                         and isinstance((event.get("raw") or {}).get("peer_wire_text"), str)))
                    and submission_text_matches(event, text)):
                matches.append((row["event_id"], event.get("timestamp")))
        except (TypeError, ValueError, AttributeError):
            continue
    if len(matches) != 1:
        return None
    event_id, timestamp = matches[0]
    # Bare operator/system text lacks the unique peer tell-id envelope. Another
    # overlapping identical input makes ownership ambiguous, even with one USER.
    for row in conn.execute("SELECT tell_id,reply FROM v2_tell_deliveries"):
        if row["tell_id"] == delivery.get("tell_id"):
            continue
        try:
            other = json.loads(row["reply"]).get("delivery") or {}
            lower = other.get("proof_watermark")
            upper = other.get("proof_event_id")
            if (other.get("to_stream_id") == target
                    and other.get("proof_generation") == generation
                    and type(lower) is int and lower < event_id
                    and not (type(upper) is int and upper <= watermark)
                    and provider_text_digest(other.get("text")) == provider_text_digest(text)):
                return None
        except (TypeError, ValueError, AttributeError):
            return None  # Unverifiable ownership never establishes delivery.
    return EventProof("proven", target, watermark, event_id=event_id,
                      event_ts=str(timestamp) if timestamp else None).audit_fields()
