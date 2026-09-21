"""Read-only response and work projection over existing composite receipts."""
from __future__ import annotations

from datetime import datetime
import json


AWAITING_RESPONSE = frozenset({"queued", "routing", "awaiting_reply", "acknowledged"})


def _elapsed(start, end):
    if not start or not end:
        return None
    try:
        elapsed = int((datetime.fromisoformat(end.replace("Z", "+00:00")) -
                       datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds() * 1000)
        return elapsed if elapsed >= 0 else None
    except (ValueError, TypeError):
        return None


def project_input(route, publications, lanes):
    accepted = route["created_at"]
    first = publications[0]["created_at"] if publications else None
    final = None
    acknowledged = False
    question = False
    for publication in publications:
        payload = json.loads(publication["canonical_payload_json"] or "{}")
        marker = payload.get("response_state")
        if final is None and (marker == "final" or publication["publish_kind"] == "result"):
            final = publication["created_at"]
        acknowledged |= marker == "acknowledged"
        question = publication["publish_kind"] == "question"
    pending_question = any(lane.get("pending_question_id") for lane in lanes)
    phases = {lane["phase"] for lane in lanes}
    # Durable delivery uncertainty and explicit cancellation are safety states:
    # a contradictory publication marker must never make an unverified route
    # look answered.  The order is part of the public projection contract.
    if route["routing_state"] == "routing_failed" or route["delivery_state"] == "failed":
        state = "failed"
    elif route["delivery_state"] == "uncertain":
        state = "uncertain"
    elif phases and phases <= {"cancelled", "closed"} and "cancelled" in phases:
        state = "cancelled"
    elif final:
        state = "answered"
    elif pending_question or question and not lanes:
        state = "waiting_for_operator"
    elif acknowledged:
        state = "acknowledged"
    elif first:
        state = "reply_received"  # Historical prose is not a final-response receipt.
    elif route["routing_state"] == "queued":
        state = "queued"
    elif route["routing_state"] == "classifying":
        state = "routing"
    elif route["routing_state"] == "deferred":
        state = "waiting_for_dependency"
    else:
        state = "awaiting_reply"
    # Response completion is independent of project completion. No lane means
    # unknown global work, including coordination through existing owners.
    if not phases:
        work = "unknown"
    elif phases <= {"closed", "cancelled"}:
        work = "cancelled" if phases == {"cancelled"} else "closed"
    elif "waiting" in phases or pending_question:
        work = "waiting"
    elif "execution" in phases:
        work = "in_progress"
    elif "completed" in phases:
        work = "outcome_reported"
    else:
        work = "discussion"
    timing = json.loads(route["route_json"] or "{}").get("timings", {})
    return {
        "message_id": route["input_identity"], "accepted_at": accepted,
        "first_visible_at": first, "final_visible_at": final,
        "reply_latency_ms": _elapsed(accepted, first), "final_latency_ms": _elapsed(accepted, final),
        "routing_started_at": timing.get("routing_started_at"), "routed_at": timing.get("routed_at"),
        "delivery_recorded_at": timing.get("delivery_recorded_at"),
        "queue_latency_ms": _elapsed(accepted, timing.get("routing_started_at")),
        "routing_latency_ms": _elapsed(timing.get("routing_started_at"), timing.get("routed_at")),
        "response_state": state, "work_state": work, "error_code": route["error_code"],
        "dispatch_id": route["dispatch_id"],
        "lane_ids": [lane["lane_id"] for lane in lanes],
        "publication_ids": [p["publication_key"] for p in publications],
    }


def summarize_activity(inputs):
    pending = [item for item in inputs.values() if item["response_state"] in AWAITING_RESPONSE]
    waiting = sum(item["response_state"] == "waiting_for_operator" for item in inputs.values())
    # Live frames stay bounded; requested history is enriched by input ID.
    recent = sorted(inputs.values(), key=lambda item: item["accepted_at"], reverse=True)[:100]
    return {"version": 1, "pending_count": len(pending), "waiting_for_operator_count": waiting,
            "oldest_pending_at": min((p["accepted_at"] for p in pending), default=None),
            "inputs": {item["message_id"]: item for item in recent},
            "has_more": len(inputs) > len(recent)}
