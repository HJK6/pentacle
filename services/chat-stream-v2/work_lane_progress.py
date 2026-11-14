"""Additive member facts and aggregates, independent of seat presence."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import os
from typing import Any

INLINE_MEMBERS = 8


def aggregate_members(members: list[dict[str, Any]], lane_updated_at: str | None) -> dict[str, Any]:
    completed = [m for m in members if m["status"] == "completed"]
    dropped = [m for m in members if m["status"] == "deprecated"]
    unresolved = [m for m in members if m["status"] in ("missing", "ambiguous")]
    opened = [m for m in members if m["status"] not in ("completed", "deprecated", "missing", "ambiguous")]
    ac = [m for m in members if m["status"] not in ("missing", "ambiguous") and m.get("ac_total") is not None]
    non_exempt = [m for m in opened if not m.get("estimate_exempt", False)]
    estimated = [m for m in non_exempt if m.get("estimate") is not None]
    stamps = [str(m.get("_progress_changed_at", m.get("source_changed_at"))) for m in members
              if m.get("_progress_changed_at", m.get("source_changed_at"))]
    if lane_updated_at:
        stamps.append(lane_updated_at)
    return {
        "items_total": len(members), "items_completed": len(completed), "items_dropped": len(dropped),
        "items_open": len(opened), "items_unresolved": len(unresolved),
        "ac_checked": sum(m["ac_checked"] for m in ac) if ac else None,
        "ac_total": sum(m["ac_total"] for m in ac) if ac else None, "ac_members": len(ac),
        "open_estimate_h": ({key: sum(m["estimate"][key] for m in estimated) for key in ("p25", "p75", "median")}
                            if estimated else None),
        "open_estimated": len(estimated), "estimate_complete": len(estimated) == len(non_exempt),
        "freshness_at": max(stamps) if stamps else None,
    }


def stale_interval_h() -> float:
    try:
        value = float(os.environ.get("WORK_LANE_STALE_H", "24"))
    except ValueError:
        raise ValueError("work_lane_stale_interval_invalid") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError("work_lane_stale_interval_invalid")
    return value


def _epoch(stamp: str) -> float:
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()


def lane_estimate_missing(lane: dict[str, Any], *, index_available: bool | None = None) -> bool | None:
    """Internal episode predicate; preserve the existing lane wire projection."""
    members = lane.get("_members") or []
    available = lane.get("_work_index_available", False) if index_available is None else index_available
    if lane.get("work_state") == "done" or (lane.get("work_state") == "paused" and lane.get("parked_at")):
        return False
    if not available or any(m.get("observation", {}).get("quality") != "fresh"
                            or m.get("status") in ("missing", "ambiguous") for m in members):
        return None
    return any(m.get("terminal") not in ("completed", "deprecated")
               and not m.get("estimate_exempt", False)
               and m.get("estimate") is None for m in members)


def members_working(lane: dict[str, Any], sessions: list[dict[str, Any]] | None = None) -> bool:
    """Actual activity over all effective assignments, before presentation caps."""
    if sessions is None:
        return bool(lane.get("_members_working"))
    return bool(working_spec_ids(lane, sessions))


def working_spec_ids(lane: dict[str, Any], sessions: list[dict[str, Any]]) -> set[str]:
    """Members with a working effectively assigned seat."""
    from store_specs import effective_assignment
    # Residual execution on terminal/unresolved work stays observable. It grants
    # no ownership or admission, both of which have independent policy checks.
    members = {member["spec_id"] for member in lane.get("_members") or []}
    return {effective_assignment(row) for row in sessions if seat_working(row, descendants=False)} & members


def seat_live(row: dict[str, Any] | None) -> bool:
    if not row or row.get("status", "open") != "open" or row.get("presumed_dead_at"):
        return False
    if row.get("online") is False or row.get("offline_since_ts") is not None or row.get("pane_status") == "pane_dead":
        return False
    generation, capture = row.get("session_generation"), row.get("capture_generation")
    return not (generation and capture and generation != capture)


def seat_working(row: dict[str, Any] | None, *, descendants=True) -> bool:
    if not seat_live(row):
        return False
    busy = row.get("busy") and (descendants or not row.get("busy_reasons")
                               or bool({"generating", "background"} & set(row["busy_reasons"])))
    return bool(busy or row.get("working") or row.get("background_busy"))


TRAIL_LIMIT = 6


def _trail_entries(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Real progress in one stored item change: stage end, checked rise, new Status."""
    payload = event["payload"]
    prior, after = payload.get("prior") or {}, payload.get("next") or {}
    base = {"at": event["created_at"][:19] + "Z", "spec_id": payload["spec_id"]}
    entries = []
    if prior.get("status") != after.get("status") and after.get("status") == "completed":
        entries.append({**base, "kind": "done"})
    elif prior.get("status") != after.get("status") and after.get("status") == "blocked":
        line = after.get("blocked_line")
        entries.append({**base, "kind": "blocked", "text": line[:160] if isinstance(line, str) else None})
    checked = prior.get("ac_checked"), after.get("ac_checked")
    if all(isinstance(value, int) for value in checked) and checked[1] > checked[0]:
        entries.append({**base, "kind": "checks", "count": checked[1] - checked[0]})
    # The payload carries a null prior line when the policy revision changed.
    line = after.get("status_line")
    if isinstance(prior.get("status_line"), str) and isinstance(line, str) and line != prior["status_line"]:
        entries.append({**base, "kind": "note", "text": line[:160]})
    return entries


def progress_trail(events: list[dict[str, Any]], spec_ids: set[str], limit: int = TRAIL_LIMIT) -> list[dict[str, Any]]:
    """Newest-first entries over current members only, from an already bounded read."""
    trail = []
    for event in events:
        if event["payload"].get("spec_id") not in spec_ids:
            continue
        for entry in _trail_entries(event):
            trail.append(entry)
            if len(trail) == limit:
                return trail
    return trail


def lane_state(lane: dict[str, Any]) -> tuple[str, str | None]:
    state, reason = lane.get("work_state"), lane.get("work_state_reason")
    if lane_lead_lost(lane):
        return "active", "lead_lost"
    return state, reason


def lane_lead_lost(lane: dict[str, Any]) -> bool:
    """Unexpected owner loss is a fault until a qualifying lead or a deliberate hold.

    Legacy rows stored ``paused/lead_lost`` by the former reconciler are the same fault
    while their lead does not qualify; a qualifying lead clears it either way."""
    state = lane.get("work_state")
    legacy = state == "paused" and lane.get("work_state_reason") == "lead_lost"
    return (state == "active" or legacy) and not lane.get("_qualifies")


def lane_progress(lane: dict[str, Any], *, all_members: bool = False,
                  now_iso: str | None = None, index_available: bool | None = None) -> dict[str, Any]:
    members = lane.get("_members") or []
    facts = aggregate_members(members, lane.get("_fd_updated_at", lane.get("updated_at")))
    available = lane.get("_work_index_available", False) if index_available is None else index_available
    if lane.get("work_state") == "done":
        completion = False
    elif not available or any(m.get("observation", {}).get("quality") in ("stale", "error") for m in members):
        completion = None
    else:
        completion = bool(members and not facts["items_unresolved"] and facts["items_completed"]
                          and all(m.get("observation", {}).get("quality") == "fresh"
                                  and m.get("terminal") in ("completed", "deprecated") for m in members))
    threshold = stale_interval_h() * 3600
    active = lane_state(lane)[0] == "active" and bool(lane.get("_qualifies"))
    now = _epoch(now_iso) if now_iso else datetime.now(timezone.utc).timestamp()
    stale = bool(active and facts["freshness_at"] and now - _epoch(facts["freshness_at"]) > threshold)
    return {"members": members if all_members else members[:INLINE_MEMBERS], "members_total": len(members),
            "no_spec_reason": lane.get("no_spec_reason"),
            **facts, "completion_pending": completion,
            "lead_reported_done": lane.get("_lead_reported_done"), "stale": stale}


def estimate_projection(lanes: list[dict[str, Any]], scan: dict) -> dict:
    """Stable offline projection of a validated complete product-lane manifest.

    No Store, clocks or last-good state: uncertain inputs always fail the gate.
    Discovery and parsing belong to SpecsSubsystem; arithmetic is shared above.
    """
    from _shared.work_observations import unresolved_member
    errors: set[tuple[str | None, str]] = set()
    if not scan["available"]:
        errors.add((None, scan.get("error") or "work_root_unavailable"))
    for error in scan.get("errors", {}).values():
        errors.add((None, error["error"]))
    resolved = {}
    for identity, candidates in scan.get("candidates", {}).items():
        if len(candidates) != 1:
            resolved[identity] = unresolved_member(identity, "ambiguous")
            errors.add((identity, "work_member_ambiguous"))
        elif candidates[0]["quality"] != "fresh":
            resolved[identity] = unresolved_member(identity)
            errors.add((identity, candidates[0].get("error") or "work_member_unsettled"))
        else:
            resolved[identity] = {"spec_id": identity, "estimate_exempt": False, **candidates[0]["facts"]}
    output, active_owners = [], {}
    for lane in sorted(lanes, key=lambda item: (item["composite_stream_id"], item["lane_id"])):
        members = []
        for identity in lane["members"]:
            member = resolved.get(identity)
            if member is None:
                member = unresolved_member(identity)
                errors.add((identity, "work_member_missing"))
            members.append(member)
            if lane["state"] != "done":
                key = lane["composite_stream_id"], identity
                if key in active_owners:
                    errors.add((identity, "work_lane_member_conflict"))
                active_owners[key] = lane["lane_id"]
        aggregate = aggregate_members(members, None)
        numbers = aggregate["open_estimate_h"]
        if numbers is not None and not all(math.isfinite(n) for n in numbers.values()):
            errors.add((None, "work_estimate_sum_nonfinite"))
            aggregate["open_estimate_h"] = None
        output.append({**lane, "members": [{k: member[k] for k in
                       ("spec_id", "status", "estimate", "estimate_exempt")} for member in members],
                       **{k: aggregate[k] for k in ("open_estimate_h", "open_estimated", "estimate_complete")}})
    return {"schema": "work_lane_estimate_projection_v1", "lanes": output,
            "errors": [{"spec_id": identity, "code": code} for identity, code in
                       sorted(errors, key=lambda value: (value[0] or "", value[1]))]}
