"""First-class work lanes: product state on the existing composite lane rows.

A lane is first-class when ``v2_assistant_composite_lanes.work_state`` is set.
Routing ``phase`` is never read or written here; product operations and
routing operations share the lane ``version`` CAS counter.  Every product
operation writes one ``v2_work_lane_events`` row, and when it has an update
kind, one ``lane_update`` publication into the composite timeline, in the same
``BEGIN IMMEDIATE`` transaction as the lane change.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
import uuid
from typing import Any, Callable

log = logging.getLogger(__name__)

from work_lane_members import validate_members, validate_title
from _shared.lane_icons import validate_icon
from _shared.work_goals import EPIC_ID, derive_lane_id, goal_id, goal_progress, lane_work_status
from work_lane_progress import lane_lead_lost, seat_live, seat_working, working_spec_ids
from store_work_index import (ensure_work_index_schema, member_ids, members_conn, _state_conn,
                              effective_membership_conn, effective_membership_ids_conn,
                              effective_member_ids, enrich_goals_conn)
from store_work_lane_episodes import (ensure_work_lane_episodes_schema, lead_reported_done_conn,
                                       resolved_route_dispatch_map)
from work_lane_migration import upgrade_events
from assistant_policy import AssistantPolicy
from store_routing import _assistant_actor_conn, _record_publication_conn

WORK_LANE_COLUMNS = (
    ("work_state", "TEXT"),
    ("parked_at", "TEXT"),
    ("parked_reason", "TEXT"),
    ("work_state_reason", "TEXT"),
    ("blocker", "TEXT"),
    ("owner_kind", "TEXT"),
    ("title", "TEXT"),
    ("icon", "TEXT"),
    ("visible_chat_stream_id", "TEXT"),
    ("visible_chat_generation", "TEXT"),
    ("adoption_key", "TEXT"),
    ("first_admitted_at", "TEXT"),
    ("done_at", "TEXT"),
    ("last_update_id", "TEXT"),
    ("members_json", "TEXT"),
    ("no_spec_reason", "TEXT"),
    ("lead_reported_done", "INTEGER CHECK(lead_reported_done IN (0,1))"),
)
WORK_LANE_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS v2_work_lane_events (
    event_id TEXT PRIMARY KEY,
    lane_id TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN
        ('adopt','set_state','set_lead','set_chat','set_text','set_owner','update','lead_lost','lead_handoff','set_members','item_change')),
    source_id TEXT NOT NULL,
    actor_stream_id TEXT,
    actor_generation TEXT,
    prior_state TEXT,
    next_state TEXT,
    expected_lane_version INTEGER,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    update_kind TEXT CHECK(update_kind IS NULL OR update_kind IN
        ('major_decision','lane_started','lane_completed','lane_blocked','lane_unblocked','milestone')),
    update_id TEXT,
    publication_event_id INTEGER,
    consumed_question_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(lane_id, source_id)
)
"""
WORK_LANE_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_v2_work_lane_events_consumed_question "
    "ON v2_work_lane_events(consumed_question_id) WHERE consumed_question_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_v2_work_lane_events_lane ON v2_work_lane_events(lane_id, created_at)",
    # The bounded progress trail read walks this index newest first.
    "CREATE INDEX IF NOT EXISTS idx_v2_work_lane_events_trail "
    "ON v2_work_lane_events(lane_id, operation, created_at, event_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_v2_assistant_composite_lanes_adoption_key "
    "ON v2_assistant_composite_lanes(adoption_key) WHERE adoption_key IS NOT NULL",
)

WORK_STATES = ("active", "paused", "blocked", "done")
OPEN_WORK_STATES = ("active", "paused", "blocked")
OWNER_KINDS = ("operator", "fd")
PRODUCT_OPERATIONS = ("adopt", "set_state", "set_lead", "set_chat", "set_text", "set_owner", "update", "set_members")
UPDATE_KINDS = ("major_decision", "lane_started", "lane_completed", "lane_blocked", "lane_unblocked", "milestone")
GUARDED_ACTIONS = ("set_state:done", "set_owner:fd", "lane.close", "lane.decision:cancel")
TITLE_MAX = 120
SUMMARY_MAX = 512
UPDATE_TEXT_MAX = 280
LANE_FRAME_CAP = 64
LIST_LIMIT_MAX = 200
DAEMON_ACTOR = "daemon:reconciler"


class WorkLaneGoalUnresolved(ValueError):
    """The first named epic the daemon has not observed as a goal of this lane."""
    code = "work_lane_goal_unresolved"

    def __init__(self, epic_id: str):
        self.epic_id = epic_id
        super().__init__(f"{self.code}: {epic_id}")


MAX_GOALS = 16


def _validate_goal_ids(payload: dict[str, Any]) -> list[str]:
    """1 to 16 distinct epic ids. Missing or empty is required; any other bad shape invalid."""
    goals = payload.get("goals")
    if goals is None or (isinstance(goals, list) and not goals):
        raise ValueError("work_lane_goal_required")
    # Validate element shape/id before set() uniqueness: an unhashable dict/list
    # element would otherwise raise TypeError here instead of the specified refusal.
    if (not isinstance(goals, list) or len(goals) > MAX_GOALS
            or any(not isinstance(epic, str) or not EPIC_ID.fullmatch(epic) for epic in goals)
            or len(set(goals)) != len(goals)):
        raise ValueError("work_lane_payload_invalid")
    return goals


def _require_pending_goals_conn(conn, goals: list[str], target_lane_id: str) -> None:
    """Every named epic must sit in the work index as a valid, unique charter naming this lane.

    A charter the scan read as valid and unique but whose lane is not yet known
    is kept under the ``pending`` key (``advance_goals``). Index unavailability,
    an invalid or duplicated charter and a charter naming another lane all leave
    no matching pending facts, so adopt refuses them with the first offender.
    """
    state = _state_conn(conn)
    observations = state.get("goal_observations", {})
    for epic in goals:
        # The index being unavailable makes every pending record uncertain, so a
        # lingering pending entry never admits a lane while the scan is down.
        pending = (observations.get(epic) or {}).get("pending") if state.get("available") else None
        if not pending or (pending.get("facts") or {}).get("lane_id") != target_lane_id:
            raise WorkLaneGoalUnresolved(epic)


def _accept_goals_conn(conn, goals: list[str], lane_id: str, stamp: str) -> None:
    """Mark the named charters accepted for ``lane_id`` in the same adopt transaction.

    This changes the stored work-index state, so any index pass prepared before
    the adoption is recomputed against it by ``reconcile_work_observations``'s
    changed-base check and cannot overwrite the acceptance.
    """
    state = _state_conn(conn)
    observations = dict(state.get("goal_observations", {}))
    for epic in goals:
        record = dict(observations.get(epic) or {})
        pending = record.pop("pending", None) or {}
        record.update(facts=pending.get("facts"), source_hash=pending.get("source_hash"),
                      path=pending.get("path"), quality="fresh", error=None)
        observations[epic] = record
    state["goal_observations"] = observations
    conn.execute("INSERT INTO v2_work_index_state(singleton,state_json) VALUES (1,?) "
                 "ON CONFLICT(singleton) DO UPDATE SET state_json=excluded.state_json",
                 (json.dumps(state, separators=(",", ":")),))


class WorkLaneCloseBlocked(ValueError):
    """Current effective work that prevents a manual terminal transition."""

    def __init__(self, lane, work_status):
        self.code = "work_lane_close_" + work_status
        available = lane["_work_index_available"]
        members = lane["_members"]
        goals = lane["_goals"]
        by_goal = {goal_id(epic): epic for epic in goals}
        affected_members = []
        for member in members:
            observation = member["observation"]
            quality = observation["quality"]
            status = member.get("_goal_status", member.get("status"))
            if quality == "fresh" and status in ("completed", "deprecated"):
                continue
            epic = by_goal.get(member.get("goal_id"))
            affected_members.append({"spec_id": member["spec_id"], "source": "goal" if epic else "explicit",
                                     "epic_id": epic, "status": status, "quality": quality,
                                     "error": observation.get("error")})
        affected_goals = []
        for wire, epic in by_goal.items():
            record = goals[epic]
            group = [member for member in members if member.get("goal_id") == wire]
            if record["quality"] != "fresh" or goal_progress(group)["state"] != "done":
                affected_goals.append({"epic_id": epic, "quality": record["quality"], "error": record.get("error")})
        self.details = {"lane_id": lane["lane_id"], "lane_version": lane["version"], "index_available": available,
                        "members": affected_members, "member_count": len(affected_members),
                        "goals": affected_goals, "goal_count": len(affected_goals)}
        instruction = ("Finish or transfer the listed work and empty goals to their visible destination."
                       if work_status == "unfinished" else
                       "Verify the exact source ID before correcting an explicit entry. Restore unreadable sources "
                       "and retry after a fresh sweep.")
        super().__init__(f"{instruction} Read agent-orch work-lane show <lane> --members --json. "
                         "For explicit membership, use set-members with the current version and the full ordered list. "
                         "For goal membership, update the spec/summary pair or charter through the publisher. "
                         "To retain the work here, use set-state --to paused --parked --reason.")


def require_finished_lane(lane):
    status = lane_work_status(lane["_goals"], lane["_members"], lane["_work_index_available"])
    if status != "finished":
        raise WorkLaneCloseBlocked(lane, status)


def require_finished_routing_lane_conn(conn, lane):
    if lane.get("work_state") is None:
        return
    ids = effective_membership_ids_conn(conn)
    snapshot = effective_membership_conn(conn, spec_ids=effective_member_ids(conn, lane, ids), snapshot=ids)
    facts = {**lane, "_members": members_conn(conn, lane, snapshot), "_work_index_available": ids["state"]["available"],
             "_goals": {epic: record for epic, record in ids["goals"].items()
                        if record["facts"]["lane_id"] == lane["lane_id"]}}
    require_finished_lane(facts)


class WorkLanePauseRefused(ValueError):
    """Ordinary pause: current work or incomplete evidence, before any write."""

    def __init__(self, *, retryable, specs=(), packets=(), source=None):
        self.code = "work_lane_pause_activity_unavailable" if retryable else "work_lane_pause_working"
        self.details = {"retryable": retryable}
        if retryable:
            self.details["source"] = source
            message = f"{self.code}: {source}; retry after a current observation"
        else:
            self.details.update(spec_ids=list(specs), packet_refs=list(packets))
            message = f"{self.code}: " + ", ".join([*specs, *packets])
        super().__init__(message)


class WorkLaneMemberConflict(ValueError):
    """Stable wire code with the conflicting product-lane identity kept separate."""
    code = "work_lane_member_conflict"

    def __init__(self, lane_id: str, spec_id: str):
        self.lane_id, self.spec_id = lane_id, spec_id
        super().__init__(f"{self.code}: {lane_id} ({spec_id})")


class WorkLaneMemberInUse(ValueError):
    """A member removal that would strand a live assignment needs an explicit reason."""
    code = "work_lane_member_in_use"

    def __init__(self, seats: list[dict[str, str]]):
        self.seats = seats
        named = ", ".join(f"{seat['spec_id']} assigned to {seat['stream_id']}" for seat in seats)
        super().__init__(f"{self.code}: {named}; pass --reason to remove it anyway")


def member_conflicts_conn(conn, composite_stream_id: str, members: list[str],
                          target_lane_id: str | None = None, *, refuse: bool = False) -> list[dict[str, str]]:
    """One deterministic check, reused inside the writer transaction and previews."""
    wanted = set(members)
    snapshot = effective_membership_ids_conn(conn)
    conflicts = []
    for identity in sorted(wanted):
        target = snapshot["assigned"].get(identity)
        if target is not None and target != target_lane_id:
            if refuse:
                raise ValueError("work_lane_members_source_epic_required: change the source epic to move goal-derived work")
            conflicts.append({"spec_id": identity, "lane_id": target})
    if wanted:
        for raw in conn.execute(
                "SELECT lane_id,members_json FROM v2_assistant_composite_lanes "
                "WHERE stream_id=? AND work_state IS NOT NULL AND work_state!='done' ORDER BY lane_id",
                (composite_stream_id,)):
            lane = dict(raw)
            if lane["lane_id"] != target_lane_id:
                conflicts.extend({"spec_id": identity, "lane_id": lane["lane_id"]}
                                 for identity in sorted(wanted.intersection(effective_member_ids(conn, lane, snapshot))))
    if conflicts and refuse:
        raise WorkLaneMemberConflict(conflicts[0]["lane_id"], conflicts[0]["spec_id"])
    return conflicts


def _guard_open_members_conn(conn, lane, operation, payload, confirmation, actor):
    """Guard valid membership-opening requests before CAS, without side effects.

    Invalid payloads retain their existing CAS/validation ordering. Actor and
    idempotency checks have already run; all reads and the later write share
    BEGIN IMMEDIATE. Confirmation verification here never consumes a question.
    """
    members = None
    try:
        if operation == "set_members" and lane["work_state"] != "done":
            if set(payload) - {"members", "no_spec_reason", "reason"} or "members" not in payload:
                return
            snapshot = effective_membership_ids_conn(conn)
            derived = any(target == lane["lane_id"] for target in snapshot["assigned"].values())
            members, _ = validate_members(payload["members"], payload.get("no_spec_reason"), derived=derived)
        elif operation == "set_state" and lane["work_state"] == "done" and payload.get("to") in OPEN_WORK_STATES:
            if set(payload) - {"to", "blocker", "outcome", "resolution", "reason", "next_action", "operator_confirmation", "parked"}:
                return
            parking = _parking_reason(payload)
            _text(parking if parking is not None else payload.get("reason"), UPDATE_TEXT_MAX, "work_lane_payload_invalid")
            if "operator_confirmation" in payload:
                question = _verify_confirmation(payload.get("operator_confirmation") and confirmation,
                    lane_id=lane["lane_id"], action="set_state:" + payload["to"], actor_stream_id=actor)
                _consume_confirmation_conn(conn, question)
            elif lane.get("owner_kind") == "operator":
                _text(parking if parking is not None else payload.get("reason"), UPDATE_TEXT_MAX,
                      "work_lane_override_reason_required", required=True)
            if payload["to"] == "active":
                _require_lead_or_work_seat(lane, conn)
            if payload["to"] in {"paused", "blocked"} and parking is None:
                _text(payload.get("reason"), UPDATE_TEXT_MAX, "work_lane_reason_required", required=True)
                _text(payload.get("next_action"), UPDATE_TEXT_MAX, "work_lane_next_action_required", required=True)
            if payload["to"] == "blocked":
                _text(payload.get("blocker"), UPDATE_TEXT_MAX, "work_lane_blocker_required", required=True)
            members = effective_member_ids(conn, lane)
    except ValueError:
        return  # Let the unchanged operation validator report its original error.
    if members is not None:
        member_conflicts_conn(conn, lane["stream_id"], members, lane["lane_id"], refuse=True)


from store_work_index import index_status_conn
from store_lane_hues import (ensure_lane_hues_schema, enter_open_conn, prepare_hues_conn,
                             repair_hues_conn, hue_transaction)


def ensure_latest_report_schema(conn) -> None:
    from work_report_freshness import ensure_schema
    ensure_schema(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS v2_work_item_latest_report ("
                 "spec_id TEXT PRIMARY KEY, kind TEXT NOT NULL, at TEXT NOT NULL, report_id TEXT NOT NULL)")


def record_latest_report_conn(conn, report: dict) -> bool:
    attribution = (report.get("usage_snapshot") or {}).get("attribution") or {}
    spec_id = (report.get("status_freshness") or {}).get("spec_id") or attribution.get("spec_id")
    if (report.get("status") != "done" or report.get("completion_kind") == "seat_only"
            or attribution.get("mode") != "exclusive_primary" or not spec_id):
        return False
    kind = {"accept": "review_passed", "reject": "review_failed"}.get(report.get("qa_verdict"), "reported_done")
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(report["created_at"]))
    return bool(conn.execute("INSERT INTO v2_work_item_latest_report (spec_id,kind,at,report_id) VALUES (?,?,?,?) "
        "ON CONFLICT(spec_id) DO UPDATE SET kind=excluded.kind,at=excluded.at,report_id=excluded.report_id "
        "WHERE excluded.at >= v2_work_item_latest_report.at",
        (spec_id, kind, at, report["report_id"])).rowcount)


def enrich_latest_reports_conn(conn, members: list[dict]) -> None:
    for member in members:
        row = conn.execute("""SELECT l.kind,l.at,l.report_id,
            json_extract(r.status_freshness,'$.covers_event'),
            json_extract(r.status_freshness,'$.spec_id'),
            json_extract(r.status_freshness,'$.report_id'),
            json_extract(r.status_freshness,'$.source_revisions'), r.created_at
            FROM v2_work_item_latest_report l LEFT JOIN v2_reports r USING(report_id)
            WHERE l.spec_id=?""", (member["spec_id"],)).fetchone()
        member["_latest_event"] = {"kind": row[0], "at": row[1]} if row else None
        member["_latest_report_id"] = row[2] if row else None
        member["_latest_report_epoch"] = row[7] if row else None
        if row and row[3] and row[4] == member["spec_id"] and row[5] == row[2]:
            member["_status_reflected_report"] = row[2]
        if row and row[6] and row[4] == member["spec_id"] and row[5] == row[2]:
            if json.loads(row[6]) == member.get("_progress_source_revisions"):
                # A delayed scan of the already reported edit is not an update after the report.
                member["_progress_precedes_report"] = row[2]


def ensure_work_lane_schema(conn: sqlite3.Connection) -> None:
    """Additive migration only: nullable columns, a new table, partial indexes."""
    present = {row[1] for row in conn.execute("PRAGMA table_info(v2_assistant_composite_lanes)")}
    for column, ddl in WORK_LANE_COLUMNS:
        if column not in present:
            conn.execute(f"ALTER TABLE v2_assistant_composite_lanes ADD COLUMN {column} {ddl}")
    upgrade_events(conn, WORK_LANE_EVENTS_DDL)
    ensure_work_index_schema(conn)
    ensure_latest_report_schema(conn)
    ensure_work_lane_episodes_schema(conn)
    for ddl in WORK_LANE_INDEX_DDL:
        conn.execute(ddl)
    ensure_lane_hues_schema(conn)


def _now() -> str:
    now = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int(now % 1 * 1000):03d}Z"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _readable_title(text: Any, fallback: str) -> str:
    """Derive an operator-readable lane title within TITLE_MAX.

    Whole text when it fits; otherwise its first sentence when that fits;
    otherwise a word-boundary cut with an ellipsis. Never a mid-word cut: the
    preview's title is what the FD adopts, and the full text stays in summary."""
    whole = " ".join(str(text or "").split()) or fallback
    if len(whole) <= TITLE_MAX:
        return whole
    first = re.split(r"(?<=[.;!?])\s", whole, maxsplit=1)[0].strip()
    if 0 < len(first) <= TITLE_MAX:
        return first
    cut = whole[: TITLE_MAX - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-") + "\u2026"


def _text(value: Any, limit: int, code: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValueError(code)
        return None
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(code)
    value = value.strip()
    return value or None


def _parking_reason(payload: dict[str, Any]) -> str | None:
    """Validate the optional pause-only flag and its bounded, trimmed record."""
    if "parked" in payload and (type(payload["parked"]) is not bool or payload.get("to") != "paused"):
        raise ValueError("work_lane_payload_invalid")
    if not payload.get("parked"):
        return None
    value = payload.get("reason")
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError("work_lane_reason_required")
    if not isinstance(value, str):
        raise ValueError("work_lane_parked_reason_invalid")
    value = value.strip()
    if len(value) > 160 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("work_lane_parked_reason_invalid")
    return value


def _split(stream_id: str) -> tuple[str, str]:
    host, sep, name = str(stream_id or "").partition(":")
    return (host, name) if sep and host and name else ("", "")


def _session_conn(conn: sqlite3.Connection, stream_id: str) -> dict[str, Any] | None:
    host, name = _split(stream_id)
    if not host:
        return None
    row = conn.execute(
        "SELECT s.*, g.generation AS session_generation FROM sessions s "
        "LEFT JOIN v2_session_generations g ON g.host=s.host AND g.session_name=s.session_name "
        "WHERE s.host=? AND s.session_name=?", (host, name),
    ).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["stream_id"] = stream_id
    out["_fd_backend"] = fd_backend_conn(conn, stream_id)
    return out


def _protected(row: dict[str, Any] | None) -> bool:
    """``AssistantPolicy.protects(row)`` for every configured assistant."""
    return AssistantPolicy(None, "").protects(row)


def _is_composite(row: dict[str, Any] | None) -> bool:
    return bool(row) and (row.get("provider") == "composite"
                          or str(row.get("stream_id") or "").endswith(":assistant"))


def _binding_streams_conn(conn: sqlite3.Connection, env_binding: dict[str, str] | None) -> set[str]:
    """Current and every recorded direct-primary binding stream."""
    streams: set[str] = set()
    if env_binding and env_binding.get("stream_id"):
        streams.add(str(env_binding["stream_id"]))
    for row in conn.execute("SELECT stream_id FROM v2_assistant_direct_binding"):
        if row[0]:
            streams.add(str(row[0]))
    for row in conn.execute("SELECT old_binding_json,new_binding_json FROM v2_assistant_rebind_audit"):
        for raw in row:
            try:
                value = json.loads(raw) if raw else None
            except ValueError:
                value = None
            if isinstance(value, dict) and value.get("stream_id"):
                streams.add(str(value["stream_id"]))
    return streams


def lead_static_eligible(row: dict[str, Any] | None, owner_kind: str | None = None) -> bool:
    """A dedicated lead/nexus, independent of management ownership or visibility."""
    if row is None or _is_composite(row) or _protected(row) or row.get("_fd_backend"):
        return False
    return (row.get("role") in {"lead", "nexus"}
            and row.get("visibility", "default") in {"default", "visible", "nested", "hidden"})


def work_lane_lead_qualifies(lane: dict[str, Any], row: dict[str, Any] | None) -> bool:
    """The one lead predicate (operation guard, reconciler and projection)."""
    if not lane.get("bound_stream_id") or row is None:
        return False
    if not seat_live(row) or row.get("session_generation") != lane.get("bound_generation"):
        return False
    return lead_static_eligible(row, lane.get("owner_kind"))


def fd_backend_conn(conn, stream_id, front_desk=None):
    """Authenticated current bindings, never role/title/caller assertions."""
    if conn.execute("SELECT 1 FROM v2_assistant_direct_binding WHERE stream_id=?", (stream_id,)).fetchone():
        return True
    front_desk = front_desk or getattr(conn, "lifecycle_front_desk", None)
    if front_desk:
        from store_assistant_binding import _binding_conn
        binding = _binding_conn(conn, front_desk["env_binding"], name=front_desk["name"], include_target=False)
        return binding.get("stream_id") == stream_id
    return False




def _chat_available(conn: sqlite3.Connection, lane: dict[str, Any]) -> tuple[str, str]:
    """(kind, available) of the lane's visible-chat pointer."""
    target = lane.get("visible_chat_stream_id")
    if not target:
        return "session", "unavailable"
    if target == lane.get("stream_id"):
        return "composite", "open"
    row = _session_conn(conn, target)
    if row is None or row.get("session_generation") != lane.get("visible_chat_generation"):
        return "session", "unavailable"
    # Re-apply the pointer validator: a chat that became hidden, protected,
    # composite or a direct-primary binding is unavailable, never a destination.
    if ((row.get("visibility") or "default") not in ("default", "visible") or _is_composite(row)
            or _protected(row) or target in _binding_streams_conn(conn, None)):
        return "session", "unavailable"
    if row.get("status") == "open":
        return "session", "open"
    tail = conn.execute(
        "SELECT 1 FROM session_event_tail WHERE stream_id=? LIMIT 1", (target,),
    ).fetchone()
    return "session", ("history" if tail is not None else "unavailable")


def _validate_chat_pointer_conn(conn, lane_stream_id: str, chat: Any,
                                env_binding: dict[str, str] | None) -> tuple[str, str | None]:
    if not isinstance(chat, dict) or set(chat) - {"stream_id", "generation"}:
        raise ValueError("work_lane_visible_chat_invalid")
    stream = str(chat.get("stream_id") or "")
    generation = chat.get("generation")
    if stream == lane_stream_id:
        if generation not in (None, ""):
            raise ValueError("work_lane_visible_chat_invalid")
        return stream, None
    row = _session_conn(conn, stream)
    if (row is None or not generation or row.get("session_generation") != generation
            or (row.get("visibility") or "default") not in ("default", "visible")
            or _is_composite(row) or _protected(row)
            or stream in _binding_streams_conn(conn, env_binding)):
        raise ValueError("work_lane_visible_chat_invalid")
    return stream, str(generation)


def visible_chat_readable_conn(conn, stream_id: str, generation: str | None,
                               env_binding: dict[str, str] | None) -> bool:
    """Closed-history read exception: the pointer of a first-class lane, re-validated now."""
    row = _session_conn(conn, stream_id)
    if row is None or row.get("status") == "open" or not generation:
        return False
    lane = conn.execute(
        "SELECT stream_id FROM v2_assistant_composite_lanes WHERE work_state IS NOT NULL "
        "AND visible_chat_stream_id=? AND visible_chat_generation=? LIMIT 1", (stream_id, generation),
    ).fetchone()
    if lane is None:
        return False
    try:
        _validate_chat_pointer_conn(conn, lane["stream_id"], {"stream_id": stream_id, "generation": generation},
                                    env_binding)
    except ValueError:
        return False
    return True


def _verify_confirmation(confirmation: dict[str, Any] | None, *, lane_id: str, action: str,
                         actor_stream_id: str) -> str:
    """Return the question id after checking the pre-read, immutable answered question."""
    if not confirmation or not confirmation.get("question_id"):
        raise ValueError("work_lane_operator_confirmation_required")
    payload = confirmation.get("work_lane_confirmation") or {}
    if (confirmation.get("producer_stream_id") != actor_stream_id
            or not isinstance(payload, dict)
            or payload.get("lane_id") != lane_id or payload.get("action") != action
            or confirmation.get("answer") != "Confirm"
            or confirmation.get("actor_class") != "direct_operator"):
        raise ValueError("work_lane_operator_confirmation_mismatch")
    return str(confirmation["question_id"])


def _consume_confirmation_conn(conn, question_id: str) -> None:
    if conn.execute("SELECT 1 FROM v2_work_lane_events WHERE consumed_question_id=?",
                    (question_id,)).fetchone():
        raise ValueError("work_lane_operator_confirmation_consumed")


def lane_row_conn(conn, lane_id: str, snapshot: dict | None = None) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM v2_assistant_composite_lanes WHERE lane_id=?", (lane_id,)).fetchone()
    if row is None:
        return None
    lane = dict(row)
    lane.pop("hue_open_order", None)
    # An optional same-transaction identity snapshot lets a caller resolve
    # membership once; every other caller keeps the default fresh resolution.
    lane["members"] = effective_member_ids(conn, lane, snapshot)
    return lane


def _event_out(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["payload"] = json.loads(out.pop("payload_json") or "{}")
    return out


TRAIL_EVENT_BOUND = 500


def trail_events_conn(conn, lane_id: str) -> list[dict[str, Any]]:
    """The lane's newest item changes, newest first, never its whole history."""
    return [{"event_id": row[0], "created_at": row[1], "payload": json.loads(row[2])} for row in conn.execute(
        "SELECT event_id,created_at,payload_json FROM v2_work_lane_events WHERE lane_id=? AND operation='item_change' "
        "ORDER BY created_at DESC, event_id DESC LIMIT ?", (lane_id, TRAIL_EVENT_BOUND))]


def unblocks_conn(conn) -> dict[str, list[dict[str, Any]]]:
    """Work currently in blocked, by each spec its source ``blocked_by`` names."""
    from _shared.work_goals import wire_title
    result: dict[str, list[dict[str, Any]]] = {}
    for spec_id, raw, title in conn.execute(
            "SELECT spec_id,json_extract(record_json,'$.last_good._blocked_by'),json_extract(record_json,'$.member.title') "
            "FROM v2_work_item_observations WHERE json_extract(record_json,'$.member.status')='blocked' "
            "AND json_type(record_json,'$.last_good._blocked_by')='array' ORDER BY spec_id"):
        for target in dict.fromkeys(json.loads(raw)):
            entries = result.setdefault(target, [])
            if len(entries) < 4:
                # The goal title clip keeps the detail member inside its byte bound.
                entries.append({"spec_id": spec_id, "title": wire_title(title) if isinstance(title, str) else None})
    return result


def lane_events_conn(conn, lane_id: str) -> list[dict[str, Any]]:
    return [_event_out(r) for r in conn.execute(
        "SELECT * FROM v2_work_lane_events WHERE lane_id=? ORDER BY created_at, rowid", (lane_id,))]


def operator_lane_guard_conn(conn, *, lane_id: str | None, action: str, actor_stream_id: str,
                             confirmation: dict[str, Any] | None) -> str | None:
    """Routing-path guard: ``lane.close``/``cancel`` on an operator lane need a confirmation.

    Returns the question id to consume, or None when the lane is not operator-owned.
    """
    if not lane_id:
        if confirmation is not None:
            raise ValueError("work_lane_operator_confirmation_mismatch")
        return None
    row = conn.execute("SELECT owner_kind FROM v2_assistant_composite_lanes WHERE lane_id=?",
                       (lane_id,)).fetchone()
    if (row is None or row["owner_kind"] != "operator") and confirmation is None:
        return None
    question_id = _verify_confirmation(confirmation, lane_id=lane_id, action=action,
                                       actor_stream_id=actor_stream_id)
    _consume_confirmation_conn(conn, question_id)
    return question_id


def record_routing_confirmation_conn(conn, *, lane_id: str, stream_id: str, operation_id: str,
                                     action: str, question_id: str, actor_stream_id: str,
                                     actor_generation: str | None) -> None:
    """Persist the consumed confirmation of a routing close/cancel in the routing transaction."""
    payload = {"routing_operation": action, "operation_id": operation_id}
    conn.execute(
        """INSERT INTO v2_work_lane_events(event_id,lane_id,stream_id,operation,source_id,
           actor_stream_id,actor_generation,payload_json,payload_digest,consumed_question_id,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("routing-confirm:" + operation_id, lane_id, stream_id, "set_state", "routing-confirm:" + operation_id,
         actor_stream_id, actor_generation, _canonical(payload), _digest(payload), question_id, _now()),
    )


def work_lane_rows_conn(conn, *, include_done: bool = False, lane_id: str | None = None, external_config=None,
                        ids_snapshot: dict | None = None, activity_inputs: bool = False) -> list[dict[str, Any]]:
    states = WORK_STATES if include_done else OPEN_WORK_STATES
    # The coherent reader shares one identity resolution across its lane reads,
    # the filtered projection and the packet snapshot; other callers resolve here.
    reuse = ids_snapshot is not None
    ids_snapshot = ids_snapshot if reuse else effective_membership_ids_conn(conn)
    index_available = bool(ids_snapshot["state"]["available"])
    marks = ",".join("?" for _ in states)
    condition = f"work_state IN ({marks})" + (" AND lane_id=?" if lane_id is not None else "")
    parameters = (*states, lane_id) if lane_id is not None else states
    lanes = [dict(r) for r in conn.execute(
        f"SELECT * FROM v2_assistant_composite_lanes WHERE {condition}", parameters)]
    # Decode observations on demand: only the identities the selected lanes name.
    # This runs on the caller's connection and transaction (BEGIN IMMEDIATE in the
    # reconcile pass), so the rows it reads are the ones that transaction sees.
    named = [identity for lane in lanes for identity in effective_member_ids(conn, lane, ids_snapshot)]
    snapshot = effective_membership_conn(conn, spec_ids=named, snapshot=ids_snapshot)
    route_dispatch_map = resolved_route_dispatch_map(conn)
    if activity_inputs:
        external, external_known = _activity_packets_conn(conn, external_config)
    else:
        from store_external_work import current_packets_conn
        # Reuse the already-resolved snapshot and decoded observations for the packet
        # terminal filter only on the coherent reader; other callers resolve fresh.
        external, external_known = current_packets_conn(conn, config=external_config,
                                    snapshot=ids_snapshot if reuse else None,
                                    decoded=snapshot["members"] if reuse else None,
                                    decoded_ids=set(named) if reuse else None), False
    for lane in lanes:
        lane["_external_packets"] = external
        lane["_activity_external_known"] = external_known
        lane["_members"] = members_conn(conn, lane, snapshot)
        enrich_latest_reports_conn(conn, lane["_members"])
        enrich_goals_conn(conn, lane, snapshot)
        lane["_work_index_available"] = index_available
        lane["_has_work_seat"] = _has_work_seat_conn(conn, lane)
        lane["_has_external_work"] = _has_external_work(lane)
        marks = ",".join("?" for _ in PRODUCT_OPERATIONS)
        fd_stamp = conn.execute(
            f"SELECT MAX(created_at) FROM v2_work_lane_events WHERE lane_id=? AND operation IN ({marks})",
            (lane["lane_id"], *PRODUCT_OPERATIONS)).fetchone()[0]
        lane["_fd_updated_at"] = fd_stamp or lane.get("first_admitted_at")
        # Entry into the current state; renames and membership edits keep their state.
        state_at = conn.execute(
            "SELECT created_at FROM v2_work_lane_events WHERE lane_id=? AND next_state IS NOT NULL "
            "AND prior_state IS NOT next_state ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (lane["lane_id"],)).fetchone()
        lane["_state_at"] = state_at[0][:19] + "Z" if state_at else None
        lead = _session_conn(conn, lane["bound_stream_id"]) if lane.get("bound_stream_id") else None
        lane["_lead_row"] = lead
        lane["_qualifies"] = work_lane_lead_qualifies(lane, lead)
        lane["_lead_reported_done"] = lead_reported_done_conn(
            conn, lane, route_dispatch_ids=route_dispatch_map.get((lane["stream_id"], lane["lane_id"]), set()))
        lane["_chat_kind"], lane["_chat_available"] = _chat_available(conn, lane)
        last = conn.execute(
            "SELECT update_id,update_kind,publication_event_id,created_at FROM v2_work_lane_events "
            "WHERE lane_id=? AND update_id IS NOT NULL ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (lane["lane_id"],)).fetchone()
        lane["_last_update"] = ({"update_id": last[0], "kind": last[1], "event_id": last[2], "ts": last[3]}
                                if last else None)
    return lanes


def work_lane_census_ids_conn(conn) -> set[str]:
    """The due worked-time census: the union of effective member IDs over the
    product lanes, resolved once from the canonical identity snapshot.

    Unlike ``work_lane_rows_conn`` this builds no enriched rows and decodes no
    observation records, reports, goals, routes, packets, hues or events -- it
    only needs the spec IDs the ledger pass reads. The union is taken over the
    same ``WORK_STATES`` lanes the enriched reader selects, not the wider
    ``work_state IS NOT NULL`` population, so a representable noncanonical state
    never leaks its members; assignment resolution still sees the full population.
    All reads run inside one connection-local read transaction so the lane and
    epic SELECTs observe a single committed snapshot (a concurrent membership
    commit yields the complete old or new answer, never a mix).
    """
    marks = ",".join("?" for _ in WORK_STATES)
    conn.execute("BEGIN")
    try:
        membership = effective_membership_ids_conn(conn)["membership"]
        eligible = {row[0] for row in conn.execute(
            f"SELECT lane_id FROM v2_assistant_composite_lanes WHERE work_state IN ({marks})", WORK_STATES)}
    finally:
        conn.rollback()
    return {spec_id for lane_id, identities in membership.items()
            if lane_id in eligible for spec_id in identities}


class _WorkLanesStoreMixin:
    """Store methods for first-class work lanes (mixed into ``Store``)."""

    # Test hook: raised after the lane UPDATE and before the publication insert.
    _work_lane_fault: Callable[[str], None] | None = None

    async def apply_work_lane_operation(
        self, *, stream_id: str, request_id: str, operation: str, lane_id: str | None,
        expected_lane_version: int | None, payload: dict[str, Any], actor_stream_id: str,
        actor_generation: str, binding_name: str, env_binding: dict[str, str] | None,
        confirmation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if operation not in PRODUCT_OPERATIONS or not request_id or not isinstance(payload, dict):
            raise ValueError("work_lane_operation_invalid")
        if operation != "adopt" and (not lane_id or isinstance(expected_lane_version, bool)
                                     or not isinstance(expected_lane_version, int)):
            raise ValueError("work_lane_operation_invalid")
        identity = {"stream_id": stream_id, "operation": operation, "lane_id": lane_id,
                    "expected_lane_version": expected_lane_version, "payload": payload,
                    "actor_stream_id": actor_stream_id}
        digest = _digest(identity)
        fault = self._work_lane_fault
        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = _apply_conn(conn, stream_id=stream_id, request_id=request_id, operation=operation,
                                     lane_id=lane_id, expected=expected_lane_version, payload=payload,
                                     actor=actor_stream_id, generation=actor_generation,
                                     binding_name=binding_name, env_binding=env_binding,
                                     confirmation=confirmation, digest=digest, fault=fault,
                                     activity_reader=getattr(self, "lane_presence_reader", None),
                                     external_config=getattr(self, "external_work_config", None))
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise
        async with self.session_graph_lock:
            return await self.submit(_op)


    async def lane_provisioning_allowed(self, auth):
        def check(conn):
            sid = auth.get("stream_id")
            row = _session_conn(conn, sid) if sid else None
            return bool(auth.get("token_verified") and row and seat_live(row)
                        and row.get("session_generation") == auth.get("session_generation")
                        and fd_backend_conn(conn, sid, getattr(self, "lifecycle_front_desk", None)))
        return await self.submit(check)


    async def get_work_lane(self, lane_id: str) -> dict[str, Any] | None:
        def _op(conn):
            # Match the base early-out: an absent lane returns before any global
            # read, so a fault reading unrelated index state cannot turn a missing
            # lane into an error. Present rows keep the single shared resolution.
            if conn.execute("SELECT 1 FROM v2_assistant_composite_lanes WHERE lane_id=?",
                            (lane_id,)).fetchone() is None:
                return None
            # Resolve canonical membership once and share it across both lane
            # reads and the filtered projection within this coherent read.
            snapshot = effective_membership_ids_conn(conn)
            lane = lane_row_conn(conn, lane_id, snapshot)
            if lane is None or lane.get("work_state") is None:
                return None
            events = lane_events_conn(conn, lane_id)
            updates = [{"update_id": e["update_id"], "kind": e["update_kind"],
                        "event_id": e["publication_event_id"], "ts": e["created_at"],
                        "source_id": e["source_id"]} for e in events if e.get("update_kind")]
            prepare_hues_conn(conn, [lane_id])
            row = next(iter(work_lane_rows_conn(conn, include_done=True, lane_id=lane_id,
                       external_config=getattr(self, "external_work_config", None), ids_snapshot=snapshot)), None)
            lane = lane_row_conn(conn, lane_id, snapshot)
            internal_members = row["_members"] if row is not None else members_conn(conn, lane, snapshot)
            # Public Store members retain the observation contract. Projection
            # keeps its private last-good/freshness inputs in _projection_row.
            public_members = [{key: value for key, value in member.items() if not key.startswith("_")}
                              for member in internal_members]
            return {"lane": lane, "events": events, "updates": updates,
                    "members": public_members,
                    "_projection_row": row, "_work_index": index_status_conn(conn),
                    "_trail_events": trail_events_conn(conn, lane_id), "_unblocks": unblocks_conn(conn)}
        def coherent(conn):
            with hue_transaction(conn):
                return _op(conn)
        return await self.submit(coherent)

    async def work_item_lane_membership(self, spec_id: str) -> dict:
        """Admission uses effective membership in every state, in one store read."""
        def read(conn):
            from store_work_index import index_status_conn
            status = index_status_conn(conn)
            if status["available"] is False:
                return {"available": False, "lane_ids": []}
            snapshot = effective_membership_ids_conn(conn)
            ids = [row["lane_id"] for row in conn.execute(
                "SELECT * FROM v2_assistant_composite_lanes WHERE work_state IS NOT NULL ORDER BY lane_id")
                if spec_id in effective_member_ids(conn, dict(row), snapshot)]
            return {"available": status["available"], "lane_ids": ids}
        return await self.submit(read)

    async def open_lanes_for_spec(self, spec_id: str) -> list[dict[str, Any]]:
        """Non-done product lanes whose membership names this canonical spec."""
        def _op(conn):
            snapshot = effective_membership_ids_conn(conn)
            result = []
            for row in conn.execute(
                    "SELECT * FROM v2_assistant_composite_lanes WHERE work_state IN ('active','paused','blocked') ORDER BY lane_id"):
                lane = dict(row)
                identities = effective_member_ids(conn, lane, snapshot)
                if spec_id in identities:
                    result.append({"lane_id": lane["lane_id"], "version": int(lane["version"]),
                                   "title": lane["title"], "members": identities})
            return result
        return await self.submit(_op)

    async def work_lane_rows(self, *, include_done: bool = False) -> list[dict[str, Any]]:
        # Internal attendance/episode/aggregation readers never allocate hues.
        return await self.submit(lambda conn: work_lane_rows_conn(conn, include_done=include_done, external_config=getattr(self, "external_work_config", None)))

    async def work_lane_census_spec_ids(self) -> set[str]:
        """Spec IDs the due worked-time census reads; no enriched projection."""
        return await self.submit(work_lane_census_ids_conn)

    async def work_lane_snapshot(self, *, include_done: bool = False,
                                 select_rows: Callable | None = None):
        """Select wire rows before lazy allocation, in one coherent writer turn.

        The projection supplies a pure selection function; all rows remain in
        the snapshot for uncapped inventory counts and membership aggregates.
        """
        def _op(conn):
            with hue_transaction(conn):
                rows = work_lane_rows_conn(conn, include_done=include_done, external_config=getattr(self, "external_work_config", None))
                if select_rows is not None:
                    selected = select_rows(rows)
                    prepare_hues_conn(conn, [row["lane_id"] for row in selected])
                    # Authorized collider repair can affect an existing colour
                    # outside this page; refresh colours without allocating it.
                    hues = dict(conn.execute("SELECT lane_id,hue FROM v2_assistant_composite_lanes"))
                    for row in rows:
                        row["hue"] = hues.get(row["lane_id"])
                return rows, index_status_conn(conn)
        return await self.submit(_op)

    async def work_lane_handoff(self, predecessor: str, successor: str) -> list[dict[str, Any]]:
        """Decision D-2: lanes led by ``predecessor`` follow the handoff successor; state unchanged."""
        def _op(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                succ = _session_conn(conn, successor)
                if succ is None or not succ.get("session_generation"):
                    conn.commit()
                    return []
                moved = []
                for lane in [dict(r) for r in conn.execute(
                        "SELECT * FROM v2_assistant_composite_lanes WHERE work_state IS NOT NULL "
                        "AND bound_stream_id=?", (predecessor,))]:
                    if (not work_lane_lead_qualifies({**lane, "bound_stream_id": successor,
                                                     "bound_generation": succ["session_generation"]}, succ)
                            or fd_backend_conn(conn, successor, getattr(self, "lifecycle_front_desk", None))):
                        raise ValueError("work_lane_lead_ineligible")
                    stamp = _now()
                    source = f"lead_handoff:{lane['lane_id']}:{successor}:{succ['session_generation']}"
                    if conn.execute("SELECT 1 FROM v2_work_lane_events WHERE event_id=?", (source,)).fetchone():
                        continue
                    chat = {}
                    if lane.get("visible_chat_stream_id") == predecessor:
                        chat = {"visible_chat_stream_id": successor,
                                "visible_chat_generation": succ["session_generation"]}
                    sets = ", ".join(f"{k}=?" for k in chat)
                    conn.execute(
                        "UPDATE v2_assistant_composite_lanes SET bound_stream_id=?, bound_generation=?,"
                        " bound_backend_kind='lead', version=version+1, updated_at=?"
                        + (", " + sets if sets else "") + " WHERE lane_id=?",
                        (successor, succ["session_generation"], stamp, *chat.values(), lane["lane_id"]))
                    payload = {"from": {"stream_id": predecessor, "generation": lane.get("bound_generation")},
                               "to": {"stream_id": successor, "generation": succ["session_generation"]},
                               "visible_chat_followed": bool(chat)}
                    _insert_event(conn, event_id=source, lane=lane, operation="lead_handoff", source_id=source,
                                  actor=DAEMON_ACTOR, generation=None, prior=lane["work_state"],
                                  nxt=lane["work_state"], expected=lane["version"], payload=payload, stamp=stamp)
                    moved.append({"lane_id": lane["lane_id"], "event_id": source})
                conn.commit()
                return moved
            except BaseException:
                conn.rollback()
                raise
        return await self.submit(_op)

    async def work_lane_history_readable(self, stream_id: str, generation: str | None,
                                         env_binding: dict[str, str] | None) -> bool:
        return await self.submit(lambda conn: visible_chat_readable_conn(conn, stream_id, generation, env_binding))

    async def work_lane_preview_conflicts(self, *, composite_stream_id: str,
                                          candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return await self.submit(lambda conn: [dict(candidate, member_conflicts=member_conflicts_conn(
            conn, composite_stream_id, candidate.get("members") or [], candidate.get("lane_id")))
            for candidate in candidates])

    async def work_lane_adopt_preview(self, *, composite_stream_id: str,
                                      env_binding: dict[str, str] | None) -> list[dict[str, Any]]:
        def _op(conn):
            return _adopt_preview_conn(conn, composite_stream_id, env_binding)
        return await self.submit(_op)

    async def reconcile_lane_lifecycle(self) -> dict[str, Any]:
        """Complete, settle and reopen lanes from their goals (spec §2 and §3, option A).

        Runs after the work index is reconciled and before the inventory frame is
        built. Every transition is certain: an uncertain lane is never completed
        or reopened in either direction.
        """
        def _op(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = _reconcile_lane_lifecycle_conn(
                    conn, external_config=getattr(self, "external_work_config", None),
                    activity_reader=getattr(self, "lane_presence_reader", None))
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise
        return await self.submit(_op)

    async def lane_id_for(self, adoption_key: str, composite_stream_id: str) -> str:
        """The lane id a new adopt of ``adoption_key`` would assign; writes nothing."""
        key = _text(adoption_key, 300, "work_lane_adoption_key_invalid", required=True)
        if not key.startswith(("spec:", "stream:", "request:")):
            raise ValueError("work_lane_adoption_key_invalid")
        return derive_lane_id(composite_stream_id, key)


def _insert_event(conn, *, event_id, lane, operation, source_id, actor, generation, prior, nxt,
                  expected, payload, stamp, digest=None, update_kind=None, update_id=None,
                  publication_event_id=None, consumed_question_id=None) -> None:
    conn.execute(
        """INSERT INTO v2_work_lane_events(event_id,lane_id,stream_id,operation,source_id,actor_stream_id,
           actor_generation,prior_state,next_state,expected_lane_version,payload_json,payload_digest,
           update_kind,update_id,publication_event_id,consumed_question_id,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (event_id, lane["lane_id"], lane["stream_id"], operation, source_id, actor, generation, prior, nxt,
         expected, _canonical(payload), digest or _digest(payload), update_kind, update_id,
         publication_event_id, consumed_question_id, stamp),
    )


def _publish_update_conn(conn, *, lane: dict[str, Any], kind: str, source_type: str, source_id: str,
                         summary: str, prior_state: str | None, actor: str, generation: str,
                         grouped_ids: list[str] | None, stamp: str) -> tuple[str, int, dict[str, Any]]:
    update_id = f"lane-update:{lane['lane_id']}:{source_id}"
    source = {"type": source_type, "id": source_id}
    if grouped_ids:
        source["grouped_ids"] = list(grouped_ids)
    lane_update = {"update_id": update_id, "lane_id": lane["lane_id"], "kind": kind, "summary": summary,
                   "source": source, "state": lane.get("work_state"), "prior_state": prior_state,
                   "owner_kind": lane.get("owner_kind"), "title": lane.get("title"), "ts": stamp}
    canonical = {"composite_stream_id": lane["stream_id"], "publish_kind": "lane_update",
                 "message": summary, "lane_update": lane_update}
    event = {
        "stream_id": lane["stream_id"], "provider": "composite", "kind": "ASSIST_TEXT", "text": summary,
        "message_id": "publication:" + update_id, "reply_to_message_id": None, "reply_to_question_id": None,
        "publish_kind": "lane_update", "attachments": [], "timestamp": stamp,
        "raw": {"assistant_composite": {"published_by": {"stream_id": actor, "generation": generation}},
                "publish_kind": "lane_update", "dispatch_id": "", "actor_stream_id": actor,
                "lane_update": lane_update},
    }
    stored = _record_publication_conn(
        conn, stream_id=lane["stream_id"], publication_key=update_id, dispatch_id="",
        reply_to_message_id=None, reply_to_question_id=None, publish_kind="lane_update",
        attachment_ids=[], evidence_refs=[], canonical_payload=canonical, event=event,
    )
    if stored.get("duplicate"):
        raise ValueError("work_lane_update_duplicate")
    return update_id, int(stored["event_id"]), stored["event"]


def _require_qualifying_lead(lane: dict[str, Any], conn) -> None:
    lead = _session_conn(conn, lane["bound_stream_id"]) if lane.get("bound_stream_id") else None
    if not work_lane_lead_qualifies(lane, lead):
        raise ValueError("work_lane_visible_lead_required")


def _assigned_seats_conn(conn, spec_ids: list[str]) -> list[dict[str, str]]:
    """Open seats whose effective work assignment is one of these specs (same transaction)."""
    if not spec_ids:
        return []
    marks = ",".join("?" for _ in spec_ids)
    return [{"spec_id": r[0], "stream_id": f"{r[1]}:{r[2]}"} for r in conn.execute(
        "SELECT COALESCE(assigned_spec_id, spec_id), host, session_name FROM sessions WHERE status='open' AND ("
        f"assigned_spec_id IN ({marks}) OR (assigned_spec_id IS NULL AND spec_id IN ({marks})"
        " AND json_valid(spec_ids) AND json_array_length(spec_ids)=1)) ORDER BY host, session_name",
        (*spec_ids, *spec_ids))]


_ACTIVITY_READ_ERRORS = (sqlite3.Error, OSError, ValueError, TypeError, KeyError, RuntimeError)

def _activity_packets_conn(conn, config):
    """Read authority separately from the inventory's empty disabled projection."""
    from store_external_work import current_packets_conn
    if config is None or getattr(config, "status", None) == "disabled":
        return {"label": "", "packets": []}, True
    if getattr(config, "status", None) != "enabled" or not getattr(config, "watch_id", None):
        return {"label": "", "packets": []}, False
    try:
        result = current_packets_conn(conn, config=config)
        packets = result["packets"]
        if (not isinstance(packets, list) or any(not isinstance(row, dict)
                or not isinstance(row.get("tag"), str) or not row["tag"] or row.get("state") not in {"queued", "working", "review"}
                or not isinstance(row.get("spec_id"), str) or not row["spec_id"] for row in packets)):
            raise ValueError("invalid external packet snapshot")
        return result, True
    except _ACTIVITY_READ_ERRORS:
        return {"label": "", "packets": []}, False


class _LaneActivitySnapshot:
    """Read each source once in the caller's current Store transaction."""

    def __init__(self, conn, reader):
        self.conn = conn
        self.reader = reader
        self.assignments = None
        self.presence = None

    def _assignments(self):
        from store_specs import effective_assignment
        if self.assignments is None:
            grouped = {}
            for row in self.conn.execute(
                "SELECT s.host,s.session_name,s.spec_id,s.spec_ids,s.assigned_spec_id,s.pane_status,"
                "s.offline_since_ts,s.presumed_dead_at,g.generation FROM sessions s "
                "LEFT JOIN v2_session_generations g ON g.host=s.host AND g.session_name=s.session_name "
                "WHERE s.status='open' AND (s.assigned_spec_id IS NOT NULL OR s.spec_id IS NOT NULL OR s.spec_ids IS NOT NULL)"
            ):
                row = dict(row)
                identity = effective_assignment(row)
                if identity:
                    grouped.setdefault(identity, []).append(row)
            self.assignments = grouped
        return self.assignments

    def _presence(self):
        if self.presence is None:
            from presence import PresenceConfig
            from reconciler import ReconcileConfig
            observed = self.reader() if self.reader else None
            if not isinstance(observed, (list, tuple)):
                raise ValueError("activity presence unavailable")
            rows = {}
            for row in observed:
                if not isinstance(row, dict) or not isinstance(row.get("stream_id"), str):
                    raise ValueError("invalid activity presence")
                identity = row["stream_id"]
                if identity in rows:
                    raise ValueError("duplicate activity presence")
                rows[identity] = row
            # Idle seats follow the ordinary reconciliation cadence, not the
            # fast, hot-working cadence. Allow one list timeout for that sweep.
            max_age = ReconcileConfig.from_env().interval_s + PresenceConfig.from_env().list_timeout_s
            self.presence = rows, time.time(), max_age
        return self.presence

    def work(self, lane):
        if not lane.get("_work_index_available") or any(
                goal.get("quality") != "fresh" for goal in (lane.get("_goals") or {}).values()):
            return (), (), "membership"
        if not lane.get("_activity_external_known"):
            return (), (), "external_work"
        members = {row["spec_id"] for row in lane["_members"]}
        seats = []
        try:
            for identity in sorted(members):
                for row in self._assignments().get(identity, ()):
                    if not row["generation"]:
                        return (), (), "assignment_generation"
                    durable_disconnect = (row["pane_status"] == "pane_dead"
                                          or row["offline_since_ts"] is not None or row["presumed_dead_at"])
                    try:
                        live, now, max_age = self._presence()
                    except _ACTIVITY_READ_ERRORS:
                        if durable_disconnect:
                            continue
                        return (), (), "presence"
                    observed = live.get(f"{row['host']}:{row['session_name']}")
                    if not observed or observed.get("session_generation") != row["generation"]:
                        if durable_disconnect:
                            continue
                        return (), (), "presence"
                    if observed.get("pane_status") == "pane_dead" or (
                            observed.get("online") is not True and (durable_disconnect
                            or observed.get("offline_since_ts") is not None or observed.get("presumed_dead_at"))):
                        continue
                    stamp = observed.get("capture_observed_at")
                    if (observed.get("capture_generation") != row["generation"]
                            or observed.get("capture_liveness") != "idle"
                            or type(stamp) not in (int, float) or not 0 <= now - stamp <= max_age
                            or type(observed.get("working")) is not bool):
                        return (), (), "presence"
                    # Only the durable assignment grants membership. Presence
                    # contributes execution, never an assignment or replacement generation.
                    seats.append({**observed, "assigned_spec_id": identity})
        except _ACTIVITY_READ_ERRORS:
            return (), (), "assignment_or_presence"
        specs = tuple(sorted(working_spec_ids(lane, seats)))
        packets = tuple(sorted(row["tag"] for row in lane["_external_packets"]["packets"]
                               if row["state"] == "working" and row["spec_id"] in members))
        return specs, packets, None


def _has_work_seat_conn(conn, lane: dict[str, Any]) -> bool:
    members = lane.get("_members")
    if members is None:
        enriched = next(iter(work_lane_rows_conn(conn, include_done=True, lane_id=lane["lane_id"])), None)
        members = enriched.get("_members", []) if enriched else []
    ids = [m["spec_id"] for m in members if m.get("status") not in ("completed", "deprecated")]
    for assigned in _assigned_seats_conn(conn, ids):
        row = _session_conn(conn, assigned["stream_id"])
        if row and seat_live(row) and not (_is_composite(row) or _protected(row) or row.get("_fd_backend")) and row.get("role") != "planner":
            return True
    return False


def _require_lead_or_work_seat(lane, conn):
    if not _has_work_seat_conn(conn, lane) and not _has_external_work(lane):
        _require_qualifying_lead(lane, conn)


def _has_external_work(lane):
    ids = {m["spec_id"] for m in lane.get("_members") or []}
    return any(row["spec_id"] in ids for row in (lane.get("_external_packets") or {}).get("packets", []))


def _lead_lost_conn(lane: dict[str, Any], conn) -> bool:
    lead = _session_conn(conn, lane["bound_stream_id"]) if lane.get("bound_stream_id") else None
    return lane_lead_lost({**lane, "_qualifies": work_lane_lead_qualifies(lane, lead),
                           "_has_work_seat": _has_work_seat_conn(conn, lane), "_has_external_work": _has_external_work(lane)})


def _override_audit(actor: str, generation: str, action: str, prior: str, nxt: str,
                    reason: str) -> dict[str, Any]:
    """Typed audit on the existing mutation event; no schema or extra replay key."""
    return {"schema": "WorkLaneOverrideV1", "actor_stream_id": actor,
            "actor_generation": generation, "action": action, "from": prior, "to": nxt,
            "reason": reason}


def _lane_work_status_conn(conn, lane_id: str) -> str:
    """'finished' | 'unfinished' | 'uncertain' for one enriched lane row."""
    row = next(iter(work_lane_rows_conn(conn, include_done=True, lane_id=lane_id)), None)
    if row is None:
        return "uncertain"
    return lane_work_status(row.get("_goals") or {}, row.get("_members") or [],
                            bool(row.get("_work_index_available")))


def _apply_conn(conn, *, stream_id, request_id, operation, lane_id, expected, payload, actor, generation,
                binding_name, env_binding, confirmation, digest, fault, activity_reader=None, external_config=None) -> dict[str, Any]:
    from store_assistant_binding import _binding_conn
    binding = _binding_conn(conn, env_binding or {}, name=binding_name, include_target=False)
    if (not actor or not generation or binding.get("stream_id") != actor
            or binding.get("generation") != generation):
        raise ValueError("work_lane_actor_unverified")
    try:
        _assistant_actor_conn(conn, actor, generation)
    except ValueError as exc:
        raise ValueError("work_lane_actor_unverified") from exc
    prior = conn.execute("SELECT * FROM v2_work_lane_events WHERE event_id=?", (request_id,)).fetchone()
    if prior is not None:
        if prior["payload_digest"] != digest:
            raise ValueError("work_lane_idempotency_conflict")
        event = _event_out(prior)
        lane = lane_row_conn(conn, prior["lane_id"])
        return {"duplicate": True, "event": event, "lane": lane,
                "update": ({"update_id": event["update_id"], "event_id": event["publication_event_id"]}
                           if event.get("update_id") else None)}
    stamp = _now()
    if operation == "adopt":
        lane, prior_state, update = _adopt_conn(conn, stream_id=stream_id, request_id=request_id,
                                                payload=payload, actor=actor, generation=generation,
                                                env_binding=env_binding, stamp=stamp)
        source_id = request_id
        consumed = None
        event_payload = payload
    else:
        lane = lane_row_conn(conn, lane_id)
        if lane is None or lane["stream_id"] != stream_id or lane.get("work_state") is None:
            raise ValueError("work_lane_not_found")
        ordinary_pause = (operation == "set_state" and payload.get("to") == "paused"
                          and not payload.get("parked"))
        if operation == "set_state":
            try:
                enriched = next(iter(work_lane_rows_conn(conn, include_done=True, lane_id=lane_id,
                    external_config=external_config, activity_inputs=ordinary_pause)), None)
            except _ACTIVITY_READ_ERRORS:
                if ordinary_pause:
                    raise WorkLanePauseRefused(retryable=True, source="membership_or_assignment") from None
                raise
            if enriched:
                lane.update({key: enriched[key] for key in ("_members", "_goals", "_work_index_available",
                            "_external_packets", "_has_work_seat", "_has_external_work", "_activity_external_known")})
        _guard_open_members_conn(conn, lane, operation, payload, confirmation, actor)
        if expected != int(lane["version"]):
            raise ValueError("assistant_lane_version_conflict")
        prior_state = lane["work_state"]
        source_id = request_id
        consumed = None
        event_payload = payload
        update = None
        changes: dict[str, Any] = {}
        if operation == "set_state":
            allowed = {"to", "blocker", "outcome", "resolution", "reason", "next_action", "operator_confirmation", "parked"}
            if set(payload) - allowed or payload.get("to") not in WORK_STATES:
                raise ValueError("work_lane_payload_invalid")
            to = payload["to"]
            # Manual reopening is the only other way into the open inventory. It
            # refuses unless the lane certainly has unfinished work, and reports
            # uncertainty rather than opening a lane on stale data.
            if prior_state == "done" and to in OPEN_WORK_STATES:
                reopen_status = _lane_work_status_conn(conn, lane["lane_id"])
                if reopen_status == "finished":
                    raise ValueError("work_lane_goal_required")
                if reopen_status == "uncertain":
                    raise ValueError("work_lane_goal_unresolved")
            parking = _parking_reason(payload)
            was_parked = prior_state == "paused" and bool(lane.get("parked_at"))
            park_changed = to == "paused" and was_parked != (parking is not None)
            lead_lost = _lead_lost_conn(lane, conn)
            # A legacy stored paused/lead_lost is a fault, so a deliberate pause restates it.
            if to == prior_state and not park_changed and not (lead_lost and to == "paused" and not was_parked):
                raise ValueError("work_lane_state_unchanged")
            if parking is not None:
                reason = parking
            elif lead_lost and to == "paused":
                reason = _text(payload.get("reason"), UPDATE_TEXT_MAX, "work_lane_reason_required",
                               required=True)
            else:
                reason = _text(payload.get("reason"), UPDATE_TEXT_MAX, "work_lane_payload_invalid")
            # Explicit confirmations retain their exact binding and single-use checks.
            # The verified bound FD can instead override operator ownership with a reason.
            if "operator_confirmation" in payload:
                consumed = _verify_confirmation(payload.get("operator_confirmation") and confirmation,
                                                lane_id=lane["lane_id"], action="set_state:" + to,
                                                actor_stream_id=actor)
                _consume_confirmation_conn(conn, consumed)
            elif lane.get("owner_kind") == "operator":
                reason = _text(parking if parking is not None else payload.get("reason"), UPDATE_TEXT_MAX,
                               "work_lane_override_reason_required", required=True)
                event_payload = dict(payload, override=_override_audit(
                    actor, generation, "set_state:" + to, prior_state, to, reason))
            if to == "paused" and parking is None:
                _text(payload.get("reason"), UPDATE_TEXT_MAX, "work_lane_reason_required", required=True)
                _text(payload.get("next_action"), UPDATE_TEXT_MAX, "work_lane_next_action_required", required=True)
                specs, packets, unavailable = _LaneActivitySnapshot(conn, activity_reader).work(lane)
                if unavailable:
                    raise WorkLanePauseRefused(retryable=True, source=unavailable)
                if specs:
                    raise WorkLanePauseRefused(retryable=False, specs=specs, packets=packets)
            changes.update(parked_at=stamp if parking is not None else None, parked_reason=parking)
            changes["work_state"] = to
            changes["work_state_reason"] = "reopened" if prior_state == "done" else "fd"
            if to == "active":
                _require_lead_or_work_seat(lane, conn)
                if lane.get("bound_stream_id") == actor:
                    raise ValueError("work_lane_lead_ineligible")
            if to in {"paused", "blocked"} and parking is None:
                _text(payload.get("reason"), UPDATE_TEXT_MAX, "work_lane_reason_required", required=True)
                _text(payload.get("next_action"), UPDATE_TEXT_MAX, "work_lane_next_action_required", required=True)
            if prior_state == "blocked" and to == "active":
                _text(payload.get("resolution"), UPDATE_TEXT_MAX, "work_lane_resolution_required", required=True)
            if to == "blocked":
                blocker = _text(payload.get("blocker"), UPDATE_TEXT_MAX, "work_lane_blocker_required",
                                required=True)
                changes["blocker"] = blocker
                update = ("lane_blocked", "transition", blocker)
            else:
                changes["blocker"] = None
            if to == "done":
                outcome = _text(payload.get("outcome"), UPDATE_TEXT_MAX, "work_lane_outcome_required",
                                required=True)
                require_finished_lane(lane)
                changes.update(done_at=stamp, work_state_reason="goals_done")
                update = ("lane_completed", "transition", outcome)
            elif prior_state == "blocked" and to in ("active", "paused"):
                resolution = _text(payload.get("resolution"), UPDATE_TEXT_MAX, "work_lane_payload_invalid")
                update = ("lane_unblocked", "transition",
                          resolution or f"Blocker cleared: {lane.get('blocker') or ''}"[:UPDATE_TEXT_MAX])
            if prior_state == "blocked":
                event_payload = dict(event_payload, cleared_blocker=lane.get("blocker"))
            if prior_state == "done":
                update = None  # D6: reopen emits no update, whatever the reopened state
            del reason  # pause/override reasons are never published
        elif operation == "set_lead":
            if set(payload) - {"lead", "visible_chat"} or "lead" not in payload:
                raise ValueError("work_lane_payload_invalid")
            lead = payload["lead"]
            if lead is None:
                changes.update(bound_stream_id=None, bound_generation=None, bound_backend_kind=None)
            else:
                if not isinstance(lead, dict) or set(lead) != {"stream_id", "generation"}:
                    raise ValueError("work_lane_payload_invalid")
                row = _session_conn(conn, str(lead["stream_id"]))
                if row is None or row.get("session_generation") != lead["generation"]:
                    raise ValueError("work_lane_lead_unknown")
                if not lead_static_eligible(row, lane.get("owner_kind")):
                    raise ValueError("work_lane_lead_ineligible")
                if lead["stream_id"] == actor:
                    raise ValueError("work_lane_lead_ineligible")
                changes.update(bound_stream_id=lead["stream_id"], bound_generation=lead["generation"],
                               bound_backend_kind="lead")
            if prior_state == "active":
                _require_qualifying_lead({**lane, **changes}, conn)
            if "visible_chat" in payload:
                chat, chat_gen = _validate_chat_pointer_conn(conn, stream_id, payload["visible_chat"], env_binding)
                changes.update(visible_chat_stream_id=chat, visible_chat_generation=chat_gen)
            event_payload = dict(payload, prior_lead={"stream_id": lane.get("bound_stream_id"),
                                                      "generation": lane.get("bound_generation")})
        elif operation == "set_chat":
            if set(payload) != {"visible_chat"}:
                raise ValueError("work_lane_payload_invalid")
            chat, chat_gen = _validate_chat_pointer_conn(conn, stream_id, payload["visible_chat"], env_binding)
            changes.update(visible_chat_stream_id=chat, visible_chat_generation=chat_gen)
        elif operation == "set_text":
            if not payload or set(payload) - {"title", "summary", "icon"}:
                raise ValueError("work_lane_payload_invalid")
            if "title" in payload:
                changes["title"] = validate_title(_text(payload["title"], TITLE_MAX, "work_lane_title_invalid", required=True))
            if "summary" in payload:
                changes["summary"] = _text(payload["summary"], SUMMARY_MAX, "work_lane_payload_invalid") or ""
            if "icon" in payload:
                changes["icon"] = validate_icon(payload["icon"])
        elif operation == "set_members":
            if set(payload) - {"members", "no_spec_reason", "reason"} or "members" not in payload:
                raise ValueError("work_lane_payload_invalid")
            snapshot = effective_membership_ids_conn(conn)
            prior_members = effective_member_ids(conn, lane, snapshot)
            derived = {identity for identity, target in snapshot["assigned"].items() if target == lane["lane_id"]}
            members, reason = validate_members(payload["members"], payload.get("no_spec_reason"), derived=bool(derived))
            if (not derived <= set(members) or any(snapshot["assigned"].get(identity, lane["lane_id"]) != lane["lane_id"]
                                                  for identity in members)):
                raise ValueError("work_lane_members_source_epic_required: change the source epic to move goal-derived work")
            legacy = [identity for identity in members if identity not in derived]
            removal_reason = _text(payload.get("reason"), UPDATE_TEXT_MAX, "work_lane_payload_invalid")
            in_use = _assigned_seats_conn(conn, [identity for identity in prior_members if identity not in members])
            if members == prior_members and reason == lane.get("no_spec_reason"):
                raise ValueError("work_lane_members_unchanged")
            changes.update(members_json=_canonical(legacy), no_spec_reason=reason)
            if members != prior_members:
                # Membership is a new completion condition even if both sets
                # are terminal. Clear atomically with the committed replacement;
                # request replay/unchanged refusal returns before this point.
                conn.execute("UPDATE v2_work_lane_episodes SET cleared_at=? "
                             "WHERE lane_id=? AND kind='completed' AND cleared_at IS NULL",
                             (stamp, lane["lane_id"]))
            event_payload = {"members": members, "no_spec_reason": reason}
            if in_use:
                # Removing a live assignment is explicit, never silent (A7).
                if not removal_reason:
                    raise WorkLaneMemberInUse(in_use)
                event_payload.update(reason=removal_reason, assigned_seats=in_use)
        elif operation == "set_owner":
            if set(payload) - {"to", "reason", "operator_confirmation"} or payload.get("to") not in OWNER_KINDS:
                raise ValueError("work_lane_payload_invalid")
            to = payload["to"]
            if to == lane.get("owner_kind"):
                raise ValueError("work_lane_owner_unchanged")
            changes["owner_kind"] = to
            if "operator_confirmation" in payload:
                consumed = _verify_confirmation(payload.get("operator_confirmation") and confirmation,
                                                lane_id=lane["lane_id"], action="set_owner:" + to,
                                                actor_stream_id=actor)
                _consume_confirmation_conn(conn, consumed)
            elif to == "fd":
                reason = _text(payload.get("reason"), UPDATE_TEXT_MAX,
                               "work_lane_override_reason_required", required=True)
                event_payload = dict(payload, override=_override_audit(
                    actor, generation, "set_owner:" + to, lane["owner_kind"], to, reason))
            if to == "fd":
                update = ("major_decision", "decision",
                          f"Handed to FD: {lane.get('title') or lane['lane_id']}"[:UPDATE_TEXT_MAX])
        elif operation == "update":
            if set(payload) - {"kind", "source_id", "summary", "grouped_source_ids"} \
                    or payload.get("kind") not in ("major_decision", "milestone"):
                raise ValueError("work_lane_payload_invalid")
            source_id = _text(payload.get("source_id"), 200, "work_lane_payload_invalid", required=True)
            summary = _text(payload.get("summary"), UPDATE_TEXT_MAX, "work_lane_payload_invalid", required=True)
            grouped = payload.get("grouped_source_ids")
            if grouped is not None and (not isinstance(grouped, list)
                                        or not all(isinstance(g, str) and g for g in grouped)):
                raise ValueError("work_lane_payload_invalid")
            if conn.execute("SELECT 1 FROM v2_work_lane_events WHERE lane_id=? AND source_id=?",
                            (lane["lane_id"], source_id)).fetchone():
                raise ValueError("work_lane_update_duplicate")
            update = (payload["kind"], "decision" if payload["kind"] == "major_decision" else "milestone",
                      summary, grouped)
        changes["version"] = int(lane["version"]) + 1
        changes["updated_at"] = stamp
        sets = ", ".join(f"{k}=?" for k in changes)
        cur = conn.execute(f"UPDATE v2_assistant_composite_lanes SET {sets} WHERE lane_id=? AND version=?",
                           (*changes.values(), lane["lane_id"], lane["version"]))
        if cur.rowcount != 1:
            raise ValueError("assistant_lane_version_conflict")
        lane = lane_row_conn(conn, lane["lane_id"])
    if operation == "adopt" or (operation == "set_state" and
            (prior_state == "done") != (lane["work_state"] == "done")):
        if operation == "adopt" or lane["work_state"] != "done":
            enter_open_conn(conn, lane["lane_id"])
            prepare_hues_conn(conn, [lane["lane_id"]])
        repair_hues_conn(conn)
        lane = lane_row_conn(conn, lane["lane_id"])
    if fault is not None:
        fault(operation)
    update_ref = None
    update_kind = update_id = publication_event_id = None
    published = None
    if update is not None:
        kind, source_type, summary = update[0], update[1], update[2]
        grouped = update[3] if len(update) > 3 else None
        update_id, publication_event_id, published = _publish_update_conn(
            conn, lane=lane, kind=kind, source_type=source_type, source_id=source_id, summary=summary,
            prior_state=prior_state, actor=actor, generation=generation, grouped_ids=grouped, stamp=stamp)
        update_kind = kind
        conn.execute("UPDATE v2_assistant_composite_lanes SET last_update_id=? WHERE lane_id=?",
                     (update_id, lane["lane_id"]))
        lane["last_update_id"] = update_id
        update_ref = {"update_id": update_id, "event_id": publication_event_id}
    _insert_event(conn, event_id=request_id, lane=lane, operation=operation, source_id=source_id,
                  actor=actor, generation=generation, prior=prior_state, nxt=lane.get("work_state"),
                  expected=expected, payload=event_payload, stamp=stamp, digest=digest,
                  update_kind=update_kind, update_id=update_id, publication_event_id=publication_event_id,
                  consumed_question_id=consumed)
    event = _event_out(conn.execute("SELECT * FROM v2_work_lane_events WHERE event_id=?",
                                    (request_id,)).fetchone())
    return {"duplicate": False, "event": event, "lane": lane, "update": update_ref,
            "publication": published}


def _adopt_conn(conn, *, stream_id, request_id, payload, actor, generation, env_binding, stamp):
    allowed = {"adoption_key", "title", "summary", "owner_kind", "work_state", "blocker", "lead",
               "visible_chat", "lane_id", "emit_started", "evidence", "members", "no_spec_reason", "goals"}
    if set(payload) - allowed:
        raise ValueError("work_lane_payload_invalid")
    key = _text(payload.get("adoption_key"), 300, "work_lane_adoption_key_invalid", required=True)
    if not key.startswith(("spec:", "stream:", "request:")):
        raise ValueError("work_lane_adoption_key_invalid")
    if conn.execute("SELECT 1 FROM v2_assistant_composite_lanes WHERE adoption_key=?", (key,)).fetchone():
        raise ValueError("work_lane_adoption_key_exists")
    title = validate_title(_text(payload.get("title"), TITLE_MAX, "work_lane_title_invalid", required=True))
    summary = _text(payload.get("summary"), SUMMARY_MAX, "work_lane_payload_invalid") or ""
    owner = payload.get("owner_kind")
    if owner is None:
        raise ValueError("work_lane_owner_kind_required")
    if owner not in OWNER_KINDS:
        raise ValueError("work_lane_payload_invalid")
    state = payload.get("work_state")
    if state not in OPEN_WORK_STATES:
        raise ValueError("work_lane_payload_invalid")
    blocker = None
    if state == "blocked":
        blocker = _text(payload.get("blocker"), UPDATE_TEXT_MAX, "work_lane_blocker_required", required=True)
    if "visible_chat" not in payload:
        raise ValueError("work_lane_visible_chat_invalid")
    chat, chat_gen = _validate_chat_pointer_conn(conn, stream_id, payload["visible_chat"], env_binding)
    lead = payload.get("lead")
    bound = (None, None, None)
    if lead is not None:
        if not isinstance(lead, dict) or set(lead) != {"stream_id", "generation"}:
            raise ValueError("work_lane_payload_invalid")
        row = _session_conn(conn, str(lead["stream_id"]))
        if row is None or row.get("session_generation") != lead["generation"]:
            raise ValueError("work_lane_lead_unknown")
        if not lead_static_eligible(row, owner):
            raise ValueError("work_lane_lead_ineligible")
        if lead["stream_id"] == actor:
            raise ValueError("work_lane_lead_ineligible")
        bound = (lead["stream_id"], lead["generation"], "lead")
    if owner == "fd" and not _fd_lineage_conn(conn, stream_id, payload, env_binding):
        raise ValueError("work_lane_owner_kind_unverified")
    emit = payload.get("emit_started", False)
    if not isinstance(emit, bool):
        raise ValueError("work_lane_payload_invalid")
    target_id = payload.get("lane_id")
    # The lane id the charter must name: the existing routing row for a promotion,
    # the derived id for a new lane. Both are checked before any write.
    target_lane_id = str(target_id) if target_id is not None else derive_lane_id(stream_id, key)
    goals = _validate_goal_ids(payload)
    _require_pending_goals_conn(conn, goals, target_lane_id)
    # Valid observed goals justify a lane, so goals-only adoption needs no members
    # and no no_spec_reason (spec Creation 1.6); missing goals already refused above.
    members, reason = validate_members(payload.get("members", []), payload.get("no_spec_reason"),
                                       allow_empty_without_reason=True)
    columns = {"work_state": state, "work_state_reason": "fd", "blocker": blocker, "owner_kind": owner,
               "title": title, "visible_chat_stream_id": chat, "visible_chat_generation": chat_gen,
               "adoption_key": key, "first_admitted_at": stamp, "updated_at": stamp,
               "members_json": _canonical(members), "no_spec_reason": reason}
    if bound[0] is not None or target_id is None:
        columns.update(bound_stream_id=bound[0], bound_generation=bound[1], bound_backend_kind=bound[2])
    if target_id is not None:
        existing = lane_row_conn(conn, str(target_id))
        if existing is None or existing["stream_id"] != stream_id or existing.get("work_state") is not None:
            raise ValueError("work_lane_adopt_target_invalid")
        if state == "active":
            _require_qualifying_lead({**existing, **columns}, conn)
        member_conflicts_conn(conn, stream_id, members, existing["lane_id"], refuse=True)
        if summary:
            columns["summary"] = summary
        columns["version"] = int(existing["version"]) + 1
        sets = ", ".join(f"{k}=?" for k in columns)
        conn.execute(f"UPDATE v2_assistant_composite_lanes SET {sets} WHERE lane_id=?",
                     (*columns.values(), existing["lane_id"]))
        lane_id = existing["lane_id"]
    else:
        if state == "active":
            _require_qualifying_lead(columns, conn)
        member_conflicts_conn(conn, stream_id, members, refuse=True)
        lane_id = target_lane_id
        columns.update(lane_id=lane_id, stream_id=stream_id, phase="discussion", summary=summary,
                       version=1, created_at=stamp)
        names = ",".join(columns)
        conn.execute(f"INSERT INTO v2_assistant_composite_lanes({names}) VALUES ({','.join('?' for _ in columns)})",
                     tuple(columns.values()))
    # Accept the named charters for this lane inside the one adopt transaction,
    # so `work-lane show` returns the goals from the moment the lane exists.
    _accept_goals_conn(conn, goals, lane_id, stamp)
    lane = lane_row_conn(conn, lane_id)
    if state == "active":
        _require_qualifying_lead(lane, conn)
    update = None
    if emit:
        goal = summary.split("\n", 1)[0] if summary else ""
        update = ("lane_started", "adoption", (title + (f" — {goal}" if goal else ""))[:UPDATE_TEXT_MAX])
    return lane, None, update


def _fd_lineage_conn(conn, stream_id: str, payload: dict[str, Any], env_binding) -> bool:
    """FD lineage: a routing lane with an admit route, or a parent chain reaching a direct-primary binding."""
    target_id = payload.get("lane_id")
    if target_id is not None:
        if conn.execute("SELECT 1 FROM v2_assistant_composite_operations WHERE lane_id=? AND operation='lane.admit'",
                        (target_id,)).fetchone():
            return True
    bindings = _binding_streams_conn(conn, env_binding)
    candidates = []
    for key in ("lead", "visible_chat"):
        value = payload.get(key)
        if isinstance(value, dict) and value.get("stream_id"):
            candidates.append(str(value["stream_id"]))
    for start in candidates:
        seen: set[str] = set()
        current = start
        while current and current not in seen:
            seen.add(current)
            row = _session_conn(conn, current)
            if row is None:
                break
            parent = row.get("parent_stream_id")
            if parent in bindings:
                return True
            current = parent
    return False


def _adopt_preview_conn(conn, composite_stream_id: str, env_binding) -> list[dict[str, Any]]:
    from store_specs import _hydrate_spec_row
    bindings = _binding_streams_conn(conn, env_binding)
    adopted_streams = {r[0] for r in conn.execute(
        "SELECT adoption_key FROM v2_assistant_composite_lanes WHERE adoption_key IS NOT NULL")}
    out: list[dict[str, Any]] = []
    for raw in conn.execute(
            "SELECT s.*, g.generation AS session_generation FROM sessions s "
            "LEFT JOIN v2_session_generations g ON g.host=s.host AND g.session_name=s.session_name "
            "WHERE s.status='open' ORDER BY s.created_at, s.host, s.session_name"):
        row = _hydrate_spec_row(dict(raw))
        sid = f"{row['host']}:{row['session_name']}"
        row["stream_id"] = sid
        if (row.get("visibility") or "default") not in ("default", "visible") or _is_composite(row) \
                or _protected(row) or sid in bindings:
            continue
        try:
            spec_ids = row.get("spec_ids") or []
        except ValueError:
            spec_ids = []
        if not (row.get("role") in ("lead", "nexus") or (not row.get("parent_stream_id") and spec_ids)):
            continue
        key = f"stream:{sid}"
        if key in adopted_streams:
            continue
        lane = {"bound_stream_id": sid, "bound_generation": row.get("session_generation")}
        try:
            provenance = row.get("spec_binding_provenance") or []
        except ValueError:
            provenance = []
        out.append({
            "adoption_key": key, "title": _readable_title(row.get("title") or row.get("objective"), sid),
            # Display-only: the id a new-lane adopt would assign. `adopt --apply`
            # never sends it, and a new-lane entry carries no `lane_id`.
            "new_lane_id": derive_lane_id(composite_stream_id, key),
            "lead": {"stream_id": sid, "generation": row.get("session_generation")},
            "visible_chat": {"stream_id": sid, "generation": row.get("session_generation")},
            "owner_kind": None,
            "members": row.get("qualified_spec_ids") or [],
            "member_sources": provenance,
            "no_spec_reason": None,
            "work_state": "active" if work_lane_lead_qualifies(lane, row) else "paused",
            "evidence": {
                "parent_stream_id": row.get("parent_stream_id"), "role": row.get("role"),
                "role_source": row.get("role_source"),
                "granting_principals": [p.get("granting_principal") for p in provenance if isinstance(p, dict)],
                "opened_by_host_id": row.get("opened_by_host_id"),
                "handoff_from_stream_id": row.get("handoff_from_stream_id"),
                "admit_route": False, "spec_ids": spec_ids,
            },
        })
    for raw in conn.execute(
            "SELECT * FROM v2_assistant_composite_lanes WHERE stream_id=? AND work_state IS NULL "
            "AND phase IN ('discussion','execution','waiting') ORDER BY created_at, lane_id",
            (composite_stream_id,)):
        lane = dict(raw)
        lead_row = _session_conn(conn, lane["bound_stream_id"]) if lane.get("bound_stream_id") else None
        qualified = _hydrate_spec_row(lead_row) if lead_row else {}
        admit = conn.execute(
            "SELECT payload_json FROM v2_assistant_composite_operations WHERE lane_id=? AND operation='lane.admit' "
            "ORDER BY created_at LIMIT 1", (lane["lane_id"],)).fetchone()
        has_admit = admit is not None
        try:
            request_message_id = json.loads(admit[0]).get("request_message_id") if admit else None
        except (ValueError, AttributeError):
            request_message_id = None
        out.append({
            # One request may admit several lanes; the lane id keeps each key unique
            # under the UNIQUE adoption_key index (prefix stays `request:` for the apply validator).
            "adoption_key": (f"request:{request_message_id}:{lane['lane_id']}" if request_message_id
                             else f"request:{lane['lane_id']}"),
            "lane_id": lane["lane_id"],
            "title": _readable_title(lane.get("summary"), lane["lane_id"]), "summary": lane.get("summary") or "",
            "lead": ({"stream_id": lane["bound_stream_id"], "generation": lane["bound_generation"]}
                     if lane.get("bound_stream_id") and lead_static_eligible(lead_row) else None),
            "visible_chat": {"stream_id": composite_stream_id},
            "owner_kind": None,
            "work_state": "active" if work_lane_lead_qualifies(lane, lead_row) else "paused",
            "members": qualified.get("qualified_spec_ids") or [],
            "member_sources": qualified.get("spec_binding_provenance") or [],
            "no_spec_reason": None,
            "evidence": {"phase": lane["phase"], "admit_route": has_admit,
                         "bound_backend_kind": lane.get("bound_backend_kind")},
        })
    for candidate in out:
        candidate["member_conflicts"] = member_conflicts_conn(
            conn, composite_stream_id, candidate["members"], candidate.get("lane_id"))
    return out


def _open_completed_episode_conn(conn, lane_id: str, stamp: str) -> None:
    """Open one ``completed`` episode so the FD gets the existing alert once.

    An uncleared ``completed`` episode opened under today's rule before the
    release is kept; no second one is opened while the lane stays done."""
    if conn.execute("SELECT 1 FROM v2_work_lane_episodes WHERE lane_id=? AND kind='completed' "
                    "AND cleared_at IS NULL", (lane_id,)).fetchone():
        return
    sequence = conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM v2_work_lane_episodes "
                            "WHERE lane_id=? AND kind='completed'", (lane_id,)).fetchone()[0]
    episode_id = f"{lane_id}:completed:{sequence}"
    fact = {"lane_id": lane_id, "kind": "completed", "episode_id": episode_id, "opened_at": stamp,
            "family": "work_lane.v1", "principal": "daemon:work-lanes", "condition": "active"}
    conn.execute("INSERT INTO v2_work_lane_episodes "
                 "(episode_id,lane_id,kind,sequence,opened_at,emitted_ref,fact_json) VALUES (?,?,?,?,?,NULL,?)",
                 (episode_id, lane_id, "completed", sequence, stamp,
                  json.dumps(fact, sort_keys=True, separators=(",", ":"))))


def _complete_lane_conn(conn, lane: dict[str, Any], stamp: str) -> None:
    """An open lane with no unfinished work becomes done, no confirmation, no publication."""
    new_version = int(lane["version"]) + 1
    goals = sorted((lane.get("_goals") or {}).keys())
    cur = conn.execute(
        "UPDATE v2_assistant_composite_lanes SET work_state='done', work_state_reason='goals_done',"
        " done_at=?, blocker=NULL, parked_at=NULL, parked_reason=NULL, version=?, updated_at=? "
        "WHERE lane_id=? AND version=?", (stamp, new_version, stamp, lane["lane_id"], lane["version"]))
    if cur.rowcount != 1:
        raise ValueError("assistant_lane_version_conflict")
    event_id = f"lifecycle:done:{lane['lane_id']}:{new_version}"
    _insert_event(conn, event_id=event_id, lane=lane, operation="set_state", source_id=event_id,
                  actor=DAEMON_ACTOR, generation=None, prior=lane["work_state"], nxt="done",
                  expected=None, payload={"to": "done", "auto": "goals_done", "goals": goals}, stamp=stamp)
    _open_completed_episode_conn(conn, lane["lane_id"], stamp)
    repair_hues_conn(conn)


def _settle_lane_conn(conn, lane: dict[str, Any], stamp: str) -> None:
    """A hand-closed lane with no unfinished work is marked settled; state and done_at stay."""
    new_version = int(lane["version"]) + 1
    cur = conn.execute(
        "UPDATE v2_assistant_composite_lanes SET work_state_reason='goals_done', version=? "
        "WHERE lane_id=? AND version=?", (new_version, lane["lane_id"], lane["version"]))
    if cur.rowcount != 1:
        raise ValueError("assistant_lane_version_conflict")
    event_id = f"lifecycle:settled:{lane['lane_id']}:{new_version}"
    _insert_event(conn, event_id=event_id, lane=lane, operation="set_state", source_id=event_id,
                  actor=DAEMON_ACTOR, generation=None, prior="done", nxt="done",
                  expected=None, payload={"to": "done", "auto": "settled"}, stamp=stamp)


def _reopen_lane_conn(conn, lane: dict[str, Any], stamp: str) -> bool:
    """A settled lane with unfinished work moves done to paused; never active or parked.

    A member conflict leaves the lane done and is logged for the front desk."""
    members = [member["spec_id"] for member in lane.get("_members") or []]
    conflicts = member_conflicts_conn(conn, lane["stream_id"], members, lane["lane_id"])
    if conflicts:
        log.info("work lane reopen deferred lane_id=%s member_conflict=%s", lane["lane_id"], conflicts[0])
        return False
    new_version = int(lane["version"]) + 1
    cur = conn.execute(
        "UPDATE v2_assistant_composite_lanes SET work_state='paused', work_state_reason='reopened',"
        " blocker=NULL, version=?, updated_at=? WHERE lane_id=? AND version=?",
        (new_version, stamp, lane["lane_id"], lane["version"]))
    if cur.rowcount != 1:
        raise ValueError("assistant_lane_version_conflict")
    event_id = f"lifecycle:reopen:{lane['lane_id']}:{new_version}"
    _insert_event(conn, event_id=event_id, lane=lane, operation="set_state", source_id=event_id,
                  actor=DAEMON_ACTOR, generation=None, prior="done", nxt="paused",
                  expected=None, payload={"to": "paused", "auto": "work_added"}, stamp=stamp)
    enter_open_conn(conn, lane["lane_id"])
    prepare_hues_conn(conn, [lane["lane_id"]])
    repair_hues_conn(conn)
    return True


def _lifecycle_lane_rows_conn(conn, external_config=None) -> list[dict[str, Any]]:
    """Every product lane with just the inputs the lifecycle rule needs.

    Lighter than ``work_lane_rows_conn``: only membership, goals and index
    availability, never the lead, chat, hue, episode or trail projection."""
    ids_snapshot = effective_membership_ids_conn(conn)
    index_available = bool(ids_snapshot["state"]["available"])
    lanes = [dict(row) for row in conn.execute(
        "SELECT * FROM v2_assistant_composite_lanes WHERE work_state IS NOT NULL")]
    named = [identity for lane in lanes for identity in effective_member_ids(conn, lane, ids_snapshot)]
    snapshot = effective_membership_conn(conn, spec_ids=named, snapshot=ids_snapshot)
    external, external_known = _activity_packets_conn(conn, external_config)
    for lane in lanes:
        lane.pop("hue_open_order", None)
        lane["_members"] = members_conn(conn, lane, snapshot)
        lane["_external_packets"] = external
        lane["_activity_external_known"] = external_known
        enrich_latest_reports_conn(conn, lane["_members"])
        enrich_goals_conn(conn, lane, snapshot)
        lane["_work_index_available"] = index_available
    return lanes


def _reconcile_lane_lifecycle_conn(conn, external_config=None, activity_reader=None) -> dict[str, Any]:
    stamp = _now()
    completed: list[str] = []
    settled: list[str] = []
    reopened: list[str] = []
    resumed: list[str] = []
    try:
        lanes = _lifecycle_lane_rows_conn(conn, external_config)
    except _ACTIVITY_READ_ERRORS:
        # An unavailable membership read is not a known empty lane.
        return {"completed": completed, "settled": settled, "reopened": reopened, "resumed": resumed}
    activity = _LaneActivitySnapshot(conn, activity_reader)
    for lane in lanes:
        # Spec F's reversible archive mark is skipped by settling and reopening.
        if lane.get("archived_at"):
            continue
        status = lane_work_status(lane.get("_goals") or {}, lane.get("_members") or [],
                                  bool(lane.get("_work_index_available")))
        state = lane["work_state"]
        if state in OPEN_WORK_STATES:
            if status == "finished":
                _complete_lane_conn(conn, lane, stamp)
                completed.append(lane["lane_id"])
            elif state == "paused" and not lane.get("parked_at"):
                specs, _, unavailable = activity.work(lane)
                if unavailable or not specs:
                    continue
                version = int(lane["version"]) + 1
                cur = conn.execute("UPDATE v2_assistant_composite_lanes SET work_state='active', work_state_reason='work_started', version=?, updated_at=? WHERE lane_id=? AND version=?",
                                   (version, stamp, lane["lane_id"], lane["version"]))
                if cur.rowcount != 1:
                    raise ValueError("assistant_lane_version_conflict")
                event_id = f"lifecycle:resume:{lane['lane_id']}:{version}"
                _insert_event(conn, event_id=event_id, lane=lane, operation="set_state", source_id=event_id,
                              actor=DAEMON_ACTOR, generation=None, prior="paused", nxt="active", expected=None,
                              payload={"to": "active", "auto": "work_started"}, stamp=stamp)
                resumed.append(lane["lane_id"])
        elif state == "done":
            if lane.get("work_state_reason") != "goals_done":
                # Hand-closed: settle once it certainly has no unfinished work.
                if status == "finished":
                    _settle_lane_conn(conn, lane, stamp)
                    settled.append(lane["lane_id"])
            elif status == "unfinished":
                # Settled: reopen the moment it certainly has unfinished work.
                if _reopen_lane_conn(conn, lane, stamp):
                    reopened.append(lane["lane_id"])
    return {"completed": completed, "settled": settled, "reopened": reopened, "resumed": resumed}


def new_request_id(prefix: str = "wl") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"
