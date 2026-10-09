"""Additive member facts and aggregates, independent of seat presence."""
from __future__ import annotations

from typing import Any

INLINE_MEMBERS = 8


def aggregate_members(members: list[dict[str, Any]], lane_updated_at: str | None) -> dict[str, Any]:
    completed = [m for m in members if m["status"] == "completed"]
    dropped = [m for m in members if m["status"] == "deprecated"]
    unresolved = [m for m in members if m["status"] in ("missing", "ambiguous")]
    opened = [m for m in members if m["status"] not in ("completed", "deprecated", "missing", "ambiguous")]
    ac = [m for m in members if m["status"] not in ("missing", "ambiguous") and m.get("ac_total") is not None]
    estimated = [m for m in opened if m.get("estimate") is not None]
    stamps = [str(m["source_changed_at"]) for m in members if m.get("source_changed_at")]
    if lane_updated_at:
        stamps.append(lane_updated_at)
    return {
        "items_total": len(members), "items_completed": len(completed), "items_dropped": len(dropped),
        "items_open": len(opened), "items_unresolved": len(unresolved),
        "ac_checked": sum(m["ac_checked"] for m in ac) if ac else None,
        "ac_total": sum(m["ac_total"] for m in ac) if ac else None, "ac_members": len(ac),
        "open_estimate_h": ({key: sum(m["estimate"][key] for m in estimated) for key in ("p25", "p75", "median")}
                            if estimated else None),
        "open_estimated": len(estimated), "estimate_complete": len(estimated) == len(opened),
        "freshness_at": max(stamps) if stamps else None,
    }


def lane_progress(lane: dict[str, Any], *, all_members: bool = False) -> dict[str, Any]:
    members = lane.get("_members") or []
    return {"members": members if all_members else members[:INLINE_MEMBERS], "members_total": len(members),
            "no_spec_reason": lane.get("no_spec_reason"),
            **aggregate_members(members, lane.get("_fd_updated_at", lane.get("updated_at")))}
