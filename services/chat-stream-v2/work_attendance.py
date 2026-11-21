"""Generation-bound implementation attendance on the daemon's existing writer."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import NamedTuple
import time

from store_specs import effective_assignment, normalize_spec_binding_provenance, normalize_spec_ids
from store_watch_wake import _session
from store_work_index import _state_conn
from work_owed import _observation, disposition_commands


EXPLICIT_GRANTS = {"operator_v2", "spawn_explicit"}


class Attendance(NamedTuple):
    owners: dict[str, list[str]]
    uncertain: dict[str, list[str]]
    successors: list[str]
    desk: str


def attendance_ids(row, *, front_desk=False):
    """Explicit work assignment, plus explicit grants for implementation seats."""
    if front_desk:
        return set(normalize_spec_ids(spec_id=row.get("assigned_spec_id")))
    if row.get("role") not in {"lead", "nexus"} or row.get("provider") == "composite":
        return set()
    assigned = effective_assignment(row)
    grants = normalize_spec_binding_provenance(row.get("spec_binding_provenance"), spec_ids=row.get("spec_ids"))
    if row.get("assigned_spec_id") is None and any(
            grant["spec_id"] == assigned and grant["provenance"] not in EXPLICIT_GRANTS
            for grant in grants):
        assigned = None
    identities = {grant["spec_id"] for grant in grants if grant["provenance"] in EXPLICIT_GRANTS}
    if assigned:
        identities.add(assigned)
    return identities


def attendance_conn(conn) -> Attendance:
    from store_lifecycle_authority import configured, front_desk_principal
    from work_lane_progress import seat_live
    fd_unknown = False
    try:
        fd = front_desk_principal(conn, configured(conn)) or {}
    except ValueError:
        fd, fd_unknown = {}, True
    desk = fd.get("stream_id", "")
    owners, uncertain, eligible = {}, {}, []
    for raw in conn.execute(
            "SELECT s.host,s.session_name,s.role,s.status,s.provider,s.pane_status,s.offline_since_ts,"
            "s.presumed_dead_at,s.spec_id,s.spec_ids,s.assigned_spec_id,s.spec_binding_provenance,"
            "g.generation AS session_generation FROM sessions s "
            "LEFT JOIN v2_session_generations g USING(host,session_name) WHERE s.status='open' "
            "AND (s.role IN ('lead','nexus') OR s.host || ':' || s.session_name=? OR ?)", (desk, fd_unknown)):
        row = dict(raw)
        stream = f"{row['host']}:{row['session_name']}"
        if (row.get("provider") == "composite" or stream.endswith(":assistant")
                or row.get("presumed_dead_at") or row.get("pane_status") == "pane_dead"):
            continue
        is_fd = stream == desk and row["session_generation"] == fd.get("session_generation")
        ids = attendance_ids(row, front_desk=is_fd)
        known_live = seat_live(row) and row.get("pane_status") == "pane_alive" and row["session_generation"]
        if fd_unknown and row.get("assigned_spec_id"):
            uncertain.setdefault(row["assigned_spec_id"], []).append(stream)
        for identity in ids:
            (owners if known_live else uncertain).setdefault(identity, []).append(stream)
        if known_live and not ids and (is_fd or row.get("role") in {"lead", "nexus"}):
            eligible.append(stream)
    return Attendance(owners, uncertain, sorted(eligible), desk)


def assigned_owners_conn(conn, *, exclude: str | None = None) -> dict[str, list[str]]:
    owners = attendance_conn(conn).owners
    return {identity: remaining for identity, streams in owners.items()
            if (remaining := sorted(set(streams) - {exclude}))}


def record_attendance_cause(conn, spec, stream, generation, role, gate, cause):
    # Retain the latest observation per item and gate for the daily fallback.
    conn.execute("INSERT INTO v2_work_attendance_causes "
                 "(spec_id,gate,stream_id,generation,seat_role,cause,observed_at) VALUES (?,?,?,?,?,?,?) "
                 "ON CONFLICT(spec_id,gate) DO UPDATE SET stream_id=excluded.stream_id,"
                 "generation=excluded.generation,seat_role=excluded.seat_role,cause=excluded.cause,"
                 "observed_at=excluded.observed_at",
                 (spec, gate, stream, generation, role, cause, datetime.now(timezone.utc).isoformat()))


def commission_ownership_conn(conn, spec, auth, small, deadline):
    observation = _observation(conn, _state_conn(conn), spec)
    attendance = attendance_conn(conn)
    if observation["known"] and (attendance.owners.get(spec) or
            (not small and observation["status"] not in {"ready_for_dev", "in_progress"})):
        return None
    cause = ("source_unverified" if not observation["known"] else
             "seat_liveness_unverified" if attendance.uncertain.get(spec) else None)
    if time.monotonic() >= deadline:
        cause = "store_timeout"
    if cause:
        actor = _session(conn, auth.get("stream_id")) or {}
        with conn:
            record_attendance_cause(conn, spec, auth.get("stream_id"), auth.get("session_generation"),
                                    actor.get("role"), "commission", cause)
        return cause
    return "owner_required"


def departure_conn(conn, stream, generation, changes, gate, *, observation_available=True,
                   recovery=None, deadline=None):
    """Under the session graph fence, check only attendance this action removes."""
    from sessions import VerbError
    row = _session(conn, stream)
    if not row or row["status"] != "open" or row["session_generation"] != generation:
        return []
    # Most departures and binding updates concern helpers with no attendance.
    if not row.get("assigned_spec_id") and not row.get("spec_id") and not normalize_spec_ids(row.get("spec_ids")):
        return []
    attendance = attendance_conn(conn)
    ids = attendance_ids(row, front_desk=stream == attendance.desk)
    if changes is not None:
        ids -= attendance_ids({**row, **changes}, front_desk=stream == attendance.desk)
    refused, warnings = [], []
    if not ids:
        return []
    state = _state_conn(conn)
    for spec in sorted(ids):
        other = set(attendance.owners.get(spec, ())) - {stream}
        if other:
            continue
        observation = _observation(conn, state, spec)
        if observation_available and observation["known"] and observation["status"] != "in_progress":
            continue
        cause = recovery
        if cause is None and deadline is not None and time.monotonic() >= deadline:
            cause = "store_timeout"
        if cause is None and (not observation_available or not observation["known"]):
            cause = "source_unverified"
        if not cause and (stream not in attendance.owners.get(spec, ()) or attendance.uncertain.get(spec)):
            cause = "seat_liveness_unverified"
        context = {**observation, "spec_id": spec, "stream_id": stream, "session_generation": generation}
        if not cause:
            successor = next((seat for seat in attendance.successors if seat != stream), None)
            commands = disposition_commands(context, pause_for_owner=True,
                                            successors=[successor] if successor else [])
            if commands:
                refused.append({"spec_id": spec, "expected_generation": generation, "next_commands": commands})
                continue
            cause = "disposition_revisions_unverified"
        warnings.append({"kind": "attendance", "spec_id": spec, "generation": generation, "gate": gate,
                         "status": "unknown" if not recovery else "recovery", "cause": cause})
    if refused:
        raise VerbError("work_last_owner", "Keep an attending owner on " + ", ".join(item["spec_id"] for item in refused)
                        + ". Assign a live successor or record a truthful disposition and retry.",
                        expected_generation=generation, items=refused)
    for entry in warnings:
        record_attendance_cause(conn, entry["spec_id"], stream, generation, row.get("role"), gate, entry["cause"])
    return warnings
