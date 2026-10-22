"""Read-only attendance and report context on a caller's existing SQLite connection."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import PurePath

from store_specs import effective_assignment, normalize_spec_ids, _hydrate_spec_row, _row
from store_work_index import _state_conn, _observed_record, members_conn

LABEL = "in_progress with no assigned implementation lead"
REVIEW = "Review the report outcome and current item before selecting a disposition."


def assigned_owners_conn(conn, *, exclude: str | None = None) -> dict[str, list[str]]:
    """Authority tags never substitute for a current implementation assignment."""
    owners: dict[str, list[str]] = {}
    for raw in conn.execute("SELECT * FROM sessions WHERE status='open' AND role IN ('lead','nexus')"):
        row = dict(raw)
        stream = f"{row['host']}:{row['session_name']}"
        assigned = effective_assignment(row)
        if assigned and stream != exclude and not row.get("presumed_dead_at"):
            owners.setdefault(assigned, []).append(stream)
    return {identity: sorted(streams) for identity, streams in owners.items()}


def _epoch(stamp):
    if not stamp:
        return None
    try:
        value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return value.replace(tzinfo=timezone.utc).timestamp() if value.tzinfo is None else value.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def unattended_work_conn(conn, *, now: float | None = None) -> dict:
    state = _state_conn(conn)
    owners = assigned_owners_conn(conn)
    items, omitted = [], []
    for raw in conn.execute("SELECT spec_id,record_json FROM v2_work_item_observations ORDER BY spec_id"):
        record = _observed_record(json.loads(raw[1]), state)
        member = record["member"]
        observation = member.get("observation") or {}
        if not state["available"] or observation.get("quality") != "fresh":
            omitted.append({"spec_id": raw[0], "quality": observation.get("quality", "unknown"),
                            "error": observation.get("error")})
            continue
        if member.get("status") != "in_progress" or owners.get(raw[0]):
            continue
        stem = PurePath(record.get("path") or raw[0].removeprefix("spec_")).name
        stamp = member.get("source_changed_at")
        changed = _epoch(stamp)
        items.append({"spec_id": raw[0], "repo": stem.split("__", 1)[0], "title": member.get("title"),
                      "source_changed_at": stamp if changed is not None else None,
                      "age_s": max(0, int(now - changed)) if now is not None and changed is not None else None,
                      "observation": observation})
    items.sort(key=lambda item: (_epoch(item["source_changed_at"]) is None,
                                _epoch(item["source_changed_at"]) or 0, item["spec_id"]))
    return {"label": LABEL, "available": bool(state["available"]),
            "quality": "unavailable" if not state["available"] else "partial" if omitted else "fresh",
            "error": state.get("error"), "omitted_count": len(omitted), "omitted": omitted,
            "count": len(items), "counts_by_repo": dict(sorted(Counter(i["repo"] for i in items).items())),
            "items": items}


def unattended_signature(projection: dict) -> dict:
    """Hash the full population and quality, excluding observation/render clocks."""
    return {key: value for key, value in projection.items() if key != "items"} | {
        "items": [{key: value for key, value in item.items() if key not in ("age_s", "observation")}
                  for item in projection["items"]]}


def report_disposition_context_conn(conn, report: dict) -> dict:
    reporter = str(report.get("from_stream_id") or "")
    row = conn.execute("SELECT s.*,g.generation AS session_generation FROM sessions s "
                       "LEFT JOIN v2_session_generations g USING(host,session_name) "
                       "WHERE s.host || ':' || s.session_name=?", (reporter,)).fetchone()
    current = row is not None and row["session_generation"] == report.get("session_generation")
    session = _hydrate_spec_row(_row(row)) if current else {}
    ids = sorted({identity for value in (session.get("spec_ids"), session.get("qualified_spec_ids"),
                  session.get("assigned_spec_id"), session.get("spec_id"),
                  report.get("provenance_qualified_spec_ids")) for identity in normalize_spec_ids(value)})
    state = _state_conn(conn)
    members = {member["spec_id"]: member for member in members_conn(conn, {"members_json": json.dumps(ids)})}
    owners = assigned_owners_conn(conn, exclude=reporter)
    items = []
    for identity in sorted(ids):
        member = members.get(identity, {})
        observation = member.get("observation") or {}
        fresh = bool(state["available"]) and observation.get("quality") == "fresh"
        items.append({"spec_id": identity, "status": member.get("status") if fresh else "unknown",
                      "observed_at": observation.get("observed_at") if fresh else None,
                      "quality": "fresh" if fresh else "unknown", "other_owners": owners.get(identity, [])})
    return {"report_id": report.get("report_id"), "review": REVIEW,
            "bindings_quality": "known" if session and ids else "unknown", "items": items}
