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
import re
import sqlite3
import time
import uuid
from typing import Any, Callable

from work_lane_members import validate_members, validate_title
from store_work_index import ensure_work_index_schema, member_ids, members_conn, _state_conn
from store_work_lane_episodes import ensure_work_lane_episodes_schema, lead_reported_done_conn
from work_lane_migration import upgrade_events
from assistant_policy import AssistantPolicy
from store_routing import _assistant_actor_conn, _record_publication_conn

WORK_LANE_COLUMNS = (
    ("work_state", "TEXT"),
    ("work_state_reason", "TEXT"),
    ("blocker", "TEXT"),
    ("owner_kind", "TEXT"),
    ("title", "TEXT"),
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


def ensure_work_lane_schema(conn: sqlite3.Connection) -> None:
    """Additive migration only: nullable columns, a new table, partial indexes."""
    present = {row[1] for row in conn.execute("PRAGMA table_info(v2_assistant_composite_lanes)")}
    for column, ddl in WORK_LANE_COLUMNS:
        if column not in present:
            conn.execute(f"ALTER TABLE v2_assistant_composite_lanes ADD COLUMN {column} {ddl}")
    upgrade_events(conn, WORK_LANE_EVENTS_DDL)
    ensure_work_index_schema(conn)
    ensure_work_lane_episodes_schema(conn)
    for ddl in WORK_LANE_INDEX_DDL:
        conn.execute(ddl)


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
    return out


def _protected(row: dict[str, Any] | None) -> bool:
    """``AssistantPolicy.protects(row)`` for every configured assistant (bart and daff)."""
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


def lead_static_eligible(row: dict[str, Any] | None) -> bool:
    """Role/visibility/kind part of lead eligibility (no open/generation test)."""
    if row is None or _is_composite(row) or _protected(row):
        return False
    if (row.get("role") or "") == "qa":
        return False
    if (row.get("visibility") or "default") not in ("default", "visible"):
        return False
    return (row.get("role") in ("lead", "nexus")) or not row.get("parent_stream_id")


def work_lane_lead_qualifies(lane: dict[str, Any], row: dict[str, Any] | None) -> bool:
    """The one visible-lead predicate (operation guard, reconciler and projection)."""
    if not lane.get("bound_stream_id") or row is None:
        return False
    if row.get("status") != "open" or row.get("session_generation") != lane.get("bound_generation"):
        return False
    return lead_static_eligible(row)


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


def lane_row_conn(conn, lane_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM v2_assistant_composite_lanes WHERE lane_id=?", (lane_id,)).fetchone()
    if row is None:
        return None
    lane = dict(row)
    lane["members"] = member_ids(lane)
    return lane


def _event_out(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["payload"] = json.loads(out.pop("payload_json") or "{}")
    return out


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


def work_lane_rows_conn(conn, *, include_done: bool = False) -> list[dict[str, Any]]:
    states = WORK_STATES if include_done else OPEN_WORK_STATES
    index_available = bool(_state_conn(conn)["available"])
    marks = ",".join("?" for _ in states)
    lanes = [dict(r) for r in conn.execute(
        f"SELECT * FROM v2_assistant_composite_lanes WHERE work_state IN ({marks})", states)]
    for lane in lanes:
        lane["_members"] = members_conn(conn, lane)
        lane["_work_index_available"] = index_available
        marks = ",".join("?" for _ in PRODUCT_OPERATIONS)
        fd_stamp = conn.execute(
            f"SELECT MAX(created_at) FROM v2_work_lane_events WHERE lane_id=? AND operation IN ({marks})",
            (lane["lane_id"], *PRODUCT_OPERATIONS)).fetchone()[0]
        lane["_fd_updated_at"] = fd_stamp or lane.get("first_admitted_at")
        lead = _session_conn(conn, lane["bound_stream_id"]) if lane.get("bound_stream_id") else None
        lane["_lead_row"] = lead
        lane["_qualifies"] = work_lane_lead_qualifies(lane, lead)
        lane["_lead_reported_done"] = lead_reported_done_conn(conn, lane)
        lane["_chat_kind"], lane["_chat_available"] = _chat_available(conn, lane)
        last = conn.execute(
            "SELECT update_id,update_kind,publication_event_id,created_at FROM v2_work_lane_events "
            "WHERE lane_id=? AND update_id IS NOT NULL ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (lane["lane_id"],)).fetchone()
        lane["_last_update"] = ({"update_id": last[0], "kind": last[1], "event_id": last[2], "ts": last[3]}
                                if last else None)
    return lanes


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
                                     confirmation=confirmation, digest=digest, fault=fault)
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise
        return await self.submit(_op)

    async def get_work_lane(self, lane_id: str) -> dict[str, Any] | None:
        def _op(conn):
            lane = lane_row_conn(conn, lane_id)
            if lane is None or lane.get("work_state") is None:
                return None
            events = lane_events_conn(conn, lane_id)
            updates = [{"update_id": e["update_id"], "kind": e["update_kind"],
                        "event_id": e["publication_event_id"], "ts": e["created_at"],
                        "source_id": e["source_id"]} for e in events if e.get("update_kind")]
            return {"lane": lane, "events": events, "updates": updates, "members": members_conn(conn, lane)}
        return await self.submit(_op)

    async def work_lane_rows(self, *, include_done: bool = False) -> list[dict[str, Any]]:
        return await self.submit(lambda conn: work_lane_rows_conn(conn, include_done=include_done))

    async def reconcile_work_lanes(self) -> list[dict[str, Any]]:
        """Daemon-side lead-loss reconciliation: stored ``active`` without a visible lead -> ``paused``."""
        def _op(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                changed = []
                for lane in [dict(r) for r in conn.execute(
                        "SELECT * FROM v2_assistant_composite_lanes WHERE work_state='active'")]:
                    lead = _session_conn(conn, lane["bound_stream_id"]) if lane.get("bound_stream_id") else None
                    if work_lane_lead_qualifies(lane, lead):
                        continue
                    stamp = _now()
                    source = f"lead_lost:{lane['lane_id']}:{lane['version']}"
                    payload = {"lead": {"stream_id": lane.get("bound_stream_id"),
                                        "generation": lane.get("bound_generation")}}
                    conn.execute(
                        "UPDATE v2_assistant_composite_lanes SET work_state='paused', work_state_reason='lead_lost',"
                        " version=version+1, updated_at=? WHERE lane_id=? AND version=?",
                        (stamp, lane["lane_id"], lane["version"]))
                    _insert_event(conn, event_id=source, lane=lane, operation="lead_lost", source_id=source,
                                  actor=DAEMON_ACTOR, generation=None, prior="active", nxt="paused",
                                  expected=lane["version"], payload=payload, stamp=stamp)
                    changed.append({"lane_id": lane["lane_id"], "event_id": source})
                conn.commit()
                return changed
            except BaseException:
                conn.rollback()
                raise
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

    async def work_lane_adopt_preview(self, *, composite_stream_id: str,
                                      env_binding: dict[str, str] | None) -> list[dict[str, Any]]:
        def _op(conn):
            return _adopt_preview_conn(conn, composite_stream_id, env_binding)
        return await self.submit(_op)


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


def _apply_conn(conn, *, stream_id, request_id, operation, lane_id, expected, payload, actor, generation,
                binding_name, env_binding, confirmation, digest, fault) -> dict[str, Any]:
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
        if expected != int(lane["version"]):
            raise ValueError("assistant_lane_version_conflict")
        prior_state = lane["work_state"]
        source_id = request_id
        consumed = None
        event_payload = payload
        update = None
        changes: dict[str, Any] = {}
        if operation == "set_state":
            allowed = {"to", "blocker", "outcome", "resolution", "reason", "operator_confirmation"}
            if set(payload) - allowed or payload.get("to") not in WORK_STATES:
                raise ValueError("work_lane_payload_invalid")
            to = payload["to"]
            if to == prior_state:
                raise ValueError("work_lane_state_unchanged")
            reason = _text(payload.get("reason"), UPDATE_TEXT_MAX, "work_lane_payload_invalid")
            # D5: a supplied confirmation must name exactly this lane and action;
            # an operator lane's done always needs one.
            if "operator_confirmation" in payload or (to == "done" and lane.get("owner_kind") == "operator"):
                consumed = _verify_confirmation(payload.get("operator_confirmation") and confirmation,
                                                lane_id=lane["lane_id"], action="set_state:" + to,
                                                actor_stream_id=actor)
                _consume_confirmation_conn(conn, consumed)
            changes["work_state"] = to
            changes["work_state_reason"] = "reopened" if prior_state == "done" else "fd"
            if to == "active":
                _require_qualifying_lead(lane, conn)
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
                changes["done_at"] = stamp
                if lane.get("owner_kind") == "operator":
                    changes["work_state_reason"] = "operator_confirmed"
                update = ("lane_completed", "transition", outcome)
            elif prior_state == "blocked" and to in ("active", "paused"):
                resolution = _text(payload.get("resolution"), UPDATE_TEXT_MAX, "work_lane_payload_invalid")
                update = ("lane_unblocked", "transition",
                          resolution or f"Blocker cleared: {lane.get('blocker') or ''}"[:UPDATE_TEXT_MAX])
            if prior_state == "blocked":
                event_payload = dict(payload, cleared_blocker=lane.get("blocker"))
            if prior_state == "done":
                update = None  # D6: reopen emits no update, whatever the reopened state
            del reason  # recorded on the event row only, never published
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
                if not lead_static_eligible(row):
                    raise ValueError("work_lane_lead_ineligible")
                changes.update(bound_stream_id=lead["stream_id"], bound_generation=lead["generation"],
                               bound_backend_kind="lead")
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
            if not payload or set(payload) - {"title", "summary"}:
                raise ValueError("work_lane_payload_invalid")
            if "title" in payload:
                changes["title"] = validate_title(_text(payload["title"], TITLE_MAX, "work_lane_title_invalid", required=True))
            if "summary" in payload:
                changes["summary"] = _text(payload["summary"], SUMMARY_MAX, "work_lane_payload_invalid") or ""
        elif operation == "set_members":
            if set(payload) - {"members", "no_spec_reason"} or "members" not in payload:
                raise ValueError("work_lane_payload_invalid")
            members, reason = validate_members(payload["members"], payload.get("no_spec_reason"))
            if members == member_ids(lane) and reason == lane.get("no_spec_reason"):
                raise ValueError("work_lane_members_unchanged")
            changes.update(members_json=_canonical(members), no_spec_reason=reason)
            if members != member_ids(lane):
                # Membership is a new completion condition even if both sets
                # are terminal. Clear atomically with the committed replacement;
                # request replay/unchanged refusal returns before this point.
                conn.execute("UPDATE v2_work_lane_episodes SET cleared_at=? "
                             "WHERE lane_id=? AND kind='completed' AND cleared_at IS NULL",
                             (stamp, lane["lane_id"]))
            event_payload = {"members": members, "no_spec_reason": reason}
        elif operation == "set_owner":
            if set(payload) - {"to", "operator_confirmation"} or payload.get("to") not in OWNER_KINDS:
                raise ValueError("work_lane_payload_invalid")
            to = payload["to"]
            if to == lane.get("owner_kind"):
                raise ValueError("work_lane_owner_unchanged")
            changes["owner_kind"] = to
            if to == "fd" or "operator_confirmation" in payload:
                consumed = _verify_confirmation(payload.get("operator_confirmation") and confirmation,
                                                lane_id=lane["lane_id"], action="set_owner:" + to,
                                                actor_stream_id=actor)
                _consume_confirmation_conn(conn, consumed)
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
               "visible_chat", "lane_id", "emit_started", "evidence", "members", "no_spec_reason"}
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
        if not lead_static_eligible(row):
            raise ValueError("work_lane_lead_ineligible")
        bound = (lead["stream_id"], lead["generation"], "lead")
    if owner == "fd" and not _fd_lineage_conn(conn, stream_id, payload, env_binding):
        raise ValueError("work_lane_owner_kind_unverified")
    emit = payload.get("emit_started", False)
    if not isinstance(emit, bool):
        raise ValueError("work_lane_payload_invalid")
    members, reason = validate_members(payload.get("members", []), payload.get("no_spec_reason"))
    target_id = payload.get("lane_id")
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
        if summary:
            columns["summary"] = summary
        columns["version"] = int(existing["version"]) + 1
        sets = ", ".join(f"{k}=?" for k in columns)
        conn.execute(f"UPDATE v2_assistant_composite_lanes SET {sets} WHERE lane_id=?",
                     (*columns.values(), existing["lane_id"]))
        lane_id = existing["lane_id"]
    else:
        lane_id = "wl-" + hashlib.sha256((stream_id + "\x00" + key).encode()).hexdigest()[:24]
        columns.update(lane_id=lane_id, stream_id=stream_id, phase="discussion", summary=summary,
                       version=1, created_at=stamp)
        names = ",".join(columns)
        conn.execute(f"INSERT INTO v2_assistant_composite_lanes({names}) VALUES ({','.join('?' for _ in columns)})",
                     tuple(columns.values()))
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
    return out


def new_request_id(prefix: str = "wl") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"
