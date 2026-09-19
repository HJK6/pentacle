"""Routing-integrity persistence owned by the v2 Store façade.

This module contains the routing capability, lifecycle guard, and routing
table operations. It never opens SQLite itself; callers reach the same
Store.submit worker boundary through the composed façade.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from typing import Any

from store_specs import _row, _session_row

# v2-only. One current routing episode per stream. The sessions row carries the
# latest observation (`routing_integrity`, reason, and timestamp); this table is
# the durable drift episode and notice-dedupe state. Keeping it separate
# preserves the shared v1 sessions shape while making evidence survive restart.
ROUTING_INTEGRITY_DDL = """
CREATE TABLE IF NOT EXISTS v2_routing_integrity (
    stream_id TEXT PRIMARY KEY,
    host TEXT NOT NULL,
    session_name TEXT NOT NULL,
    provider TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    requested_model TEXT,
    requested_effort TEXT,
    effective_model TEXT,
    effective_effort TEXT,
    reason TEXT,
    first_observed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    parent_stream_id TEXT,
    notified_at TEXT
)
"""
ROUTING_INTEGRITY_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS v2_routing_integrity_audit (
    audit_id TEXT PRIMARY KEY,
    stream_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL
)
"""
OUTBOUND_NOTICE_DDL = """
CREATE TABLE IF NOT EXISTS v2_outbound_notices (
    notice_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    recipient_stream_id TEXT NOT NULL,
    tell_id TEXT NOT NULL UNIQUE,
    body TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    source_stream_id TEXT,
    episode_id TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until REAL,
    last_error TEXT,
    next_action TEXT,
    delivered_at TEXT,
    terminal_at TEXT,
    terminal_reason TEXT,
    proof_binding TEXT
)
"""
OUTBOUND_NOTICE_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_v2_outbound_notices_due "
    "ON v2_outbound_notices (terminal_at, delivered_at, lease_until, next_attempt_at)"
)
ROUTING_INTEGRITY_NOTICE_DDL = OUTBOUND_NOTICE_DDL

# Assistant-composite is deliberately a narrow extension of the daemon's
# existing routing store.  It records one synthetic conversation's admission,
# routing and publication evidence; it is not a general task broker.
ASSISTANT_COMPOSITE_ROUTES_DDL = """
CREATE TABLE IF NOT EXISTS v2_assistant_composite_routes (
    route_id TEXT PRIMARY KEY,
    stream_id TEXT NOT NULL,
    input_identity TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    input_request_id TEXT NOT NULL,
    event_id INTEGER,
    body TEXT NOT NULL,
    attachments_json TEXT NOT NULL DEFAULT '[]',
    reply_to_message_id TEXT,
    reply_to_question_id TEXT,
    actor_stream_id TEXT,
    routing_state TEXT NOT NULL CHECK(routing_state IN
        ('queued','classifying','fallback_dispatched','deferred','resolved','routing_failed')),
    delivery_state TEXT CHECK(delivery_state IN
        ('intent','committed_pending','landed','uncertain','failed')),
    dispatch_id TEXT UNIQUE,
    route_target TEXT,
    route_target_generation TEXT,
    route_json TEXT,
    depends_on_message_id TEXT,
    error_code TEXT,
    lease_owner TEXT,
    lease_until REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(stream_id, input_identity)
)
"""
ASSISTANT_COMPOSITE_ROUTES_DUE_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_v2_assistant_composite_routes_due "
    "ON v2_assistant_composite_routes (stream_id, routing_state, lease_until, created_at)"
)
ASSISTANT_COMPOSITE_PUBLICATIONS_DDL = """
CREATE TABLE IF NOT EXISTS v2_assistant_composite_publications (
    publication_key TEXT PRIMARY KEY,
    stream_id TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    canonical_payload_json TEXT NOT NULL DEFAULT '{}',
    dispatch_id TEXT NOT NULL,
    reply_to_message_id TEXT,
    reply_to_question_id TEXT,
    publish_kind TEXT NOT NULL DEFAULT 'prose',
    attachment_ids_json TEXT NOT NULL DEFAULT '[]',
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    event_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
)
"""
ASSISTANT_COMPOSITE_LANES_DDL = """
CREATE TABLE IF NOT EXISTS v2_assistant_composite_lanes (
    lane_id TEXT PRIMARY KEY,
    stream_id TEXT NOT NULL,
    parent_lane_id TEXT,
    phase TEXT NOT NULL CHECK(phase IN
        ('discussion','execution','waiting','completed','cancelled','closed')),
    bound_stream_id TEXT,
    bound_generation TEXT,
    bound_backend_kind TEXT,
    completion_report_id TEXT,
    summary TEXT NOT NULL DEFAULT '',
    pending_question_id TEXT,
    question_bridge_operation_id TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""
ASSISTANT_COMPOSITE_OPERATIONS_DDL = """
CREATE TABLE IF NOT EXISTS v2_assistant_composite_operations (
    operation_id TEXT PRIMARY KEY,
    stream_id TEXT NOT NULL DEFAULT '',
    dispatch_id TEXT NOT NULL DEFAULT '',
    lane_id TEXT,
    reply_to_message_id TEXT,
    operation TEXT NOT NULL CHECK(operation IN
        ('lane.admit','lane.bind','lane.decision','lane.close','question.open','question.cancel','route.resolve')),
    payload_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    expected_lane_version INTEGER,
    actor_stream_id TEXT,
    prior_phase TEXT,
    next_phase TEXT,
    created_at TEXT NOT NULL
)
"""
ASSISTANT_COMPOSITE_TERMINAL_REPORTS_DDL = """
CREATE TABLE IF NOT EXISTS v2_assistant_composite_terminal_reports (
    report_id TEXT PRIMARY KEY,
    stream_id TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    dispatch_id TEXT NOT NULL,
    actor_stream_id TEXT NOT NULL,
    actor_generation TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""
ASSISTANT_COMPOSITE_QUESTION_BRIDGES_DDL = """
CREATE TABLE IF NOT EXISTS v2_assistant_composite_question_bridges (
    operation_id TEXT PRIMARY KEY,
    stream_id TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('question.open','question.cancel')),
    payload_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    actor_stream_id TEXT NOT NULL,
    expected_lane_version INTEGER NOT NULL,
    question_id TEXT,
    state TEXT NOT NULL CHECK(state IN ('prepared','committed','failed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

def _insert_outbound_notice_conn(
    conn: sqlite3.Connection, *, notice_id: str, kind: str, dedupe_key: str,
    recipient_stream_id: str, tell_id: str, body: str,
    source_stream_id: str | None = None, episode_id: str | None = None,
    metadata: dict[str, Any] | None = None, created_at: str | None = None,
) -> dict[str, Any]:
    """Transaction-local insertion; caller owns commit/rollback."""
    stamp = created_at or _routing_iso_now()
    metadata_json = json.dumps(metadata or {}, sort_keys=True, separators=(",", ":"))
    digest = _outbound_notice_digest(
        kind,
        dedupe_key,
        recipient_stream_id,
        tell_id,
        body,
        metadata_json,
        source_stream_id or "",
        episode_id or "",
    )
    existing = conn.execute(
        """SELECT * FROM v2_outbound_notices
           WHERE notice_id=? OR dedupe_key=? OR tell_id=?
           LIMIT 1""",
        (notice_id, dedupe_key, tell_id),
    ).fetchone()
    if existing is not None:
        prior = dict(existing)
        if (
            prior.get("payload_digest") != digest
            or prior.get("kind") != kind
            or prior.get("dedupe_key") != dedupe_key
            or prior.get("recipient_stream_id") != recipient_stream_id
            or prior.get("tell_id") != tell_id
        ):
            raise ValueError(
                "outbound_notice_conflict: identity reused with a different payload "
                f"(notice_id={notice_id}, dedupe_key={dedupe_key})"
            )
        prior["created"] = False
        return prior
    conn.execute(
        """INSERT INTO v2_outbound_notices (
            notice_id, kind, dedupe_key, recipient_stream_id, tell_id, body,
            payload_digest, source_stream_id, episode_id, metadata, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            notice_id, kind, dedupe_key, recipient_stream_id, tell_id, body,
            digest, source_stream_id, episode_id, metadata_json, stamp,
        ),
    )
    row = conn.execute(
        "SELECT * FROM v2_outbound_notices WHERE notice_id=?", (notice_id,)
    ).fetchone()
    out = dict(row)
    out["created"] = True
    return out


class _RoutingIntegrityLifecycleEntry:
    """Reference-counted per-stream lock entry.

    A plain lock cannot be evicted safely: a task may have fetched it and be
    waiting while a close path removes the dictionary entry. New work could
    then acquire a different lock for the same stream. References cover both
    owners and waiters, so retirement happens only after the last guard exits.
    """

    __slots__ = ("store", "stream_id", "lock", "references", "retire_requested")

    def __init__(self, store: Any, stream_id: str) -> None:
        self.store = store
        self.stream_id = stream_id
        self.lock = asyncio.Lock()
        self.references = 0
        self.retire_requested = False

class _RoutingIntegrityLifecycleGuard:
    """Async context manager retaining a lifecycle entry through acquisition."""

    __slots__ = ("entry", "_released", "_acquired")

    def __init__(self, entry: _RoutingIntegrityLifecycleEntry) -> None:
        self.entry = entry
        entry.references += 1
        # A new lifecycle use means the stream is active again (for example a
        # close racing with a name reopen), so do not retire this entry.
        entry.retire_requested = False
        self._released = False
        self._acquired = False

    async def __aenter__(self) -> "_RoutingIntegrityLifecycleGuard":
        try:
            await self.entry.lock.acquire()
        except BaseException:
            self._release_reference()
            raise
        self._acquired = True
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._acquired:
            self.entry.lock.release()
        self._release_reference()

    def _release_reference(self) -> None:
        if self._released:
            return
        self._released = True
        self.entry.references -= 1
        self.entry.store._maybe_retire_routing_integrity_lifecycle_lock(self.entry)

def _assistant_actor_conn(conn, actor, generation):
    host, _, name = str(actor or "").partition(":")
    row = conn.execute(
        "SELECT s.status,s.handoff_from_stream_id,g.generation FROM sessions s "
        "JOIN v2_session_generations g ON g.host=s.host AND g.session_name=s.session_name "
        "WHERE s.host=? AND s.session_name=?", (host, name),
    ).fetchone()
    if row is None or row["status"] != "open" or row["generation"] != generation:
        raise ValueError("assistant_actor_generation_unverified")
    return dict(row)


def _assistant_dispatch_conn(conn, stream_id, dispatch_id, actor, generation):
    current = _assistant_actor_conn(conn, actor, generation)
    row = conn.execute(
        "SELECT * FROM v2_assistant_composite_routes WHERE stream_id=? AND dispatch_id=?",
        (stream_id, dispatch_id),
    ).fetchone()
    if row is None:
        raise ValueError("assistant_dispatch_scope_unverified")
    route = dict(row)
    target = route["route_target"]
    if actor == target:
        if route["route_target_generation"] != generation:
            raise ValueError("assistant_dispatch_generation_unverified")
    elif current["handoff_from_stream_id"] != target:
        # Binding delegates the admitted input to its lead. The first lead
        # response must not depend on a second operator input routed to it.
        lanes = conn.execute(
            "SELECT * FROM v2_assistant_composite_lanes WHERE stream_id=? "
            "AND bound_stream_id=? AND bound_generation=?",
            (stream_id, actor, generation),
        ).fetchall()
        if not any(_assistant_route_lane_conn(conn, stream_id, route, lane["lane_id"]) for lane in lanes):
            raise ValueError("assistant_dispatch_actor_unverified")
    return route


def _assistant_authority_context_conn(conn, stream_id, lane_id, authority):
    host, _, name = str(authority or "").partition(":")
    successor = conn.execute(
        "SELECT handoff_from_stream_id FROM sessions WHERE host=? AND session_name=?",
        (host, name),
    ).fetchone()
    previous = successor["handoff_from_stream_id"] if successor is not None else None
    row = conn.execute(
        "SELECT r.dispatch_id,r.input_identity,l.version FROM v2_assistant_composite_operations o "
        "JOIN v2_assistant_composite_routes r ON r.dispatch_id=o.dispatch_id AND r.stream_id=o.stream_id "
        "JOIN v2_assistant_composite_lanes l ON l.lane_id=o.lane_id AND l.stream_id=o.stream_id "
        "WHERE o.stream_id=? AND o.lane_id=? AND o.operation='lane.admit' "
        "AND r.routing_state='resolved' AND (r.route_target=? OR r.route_target=?) "
        "ORDER BY o.created_at DESC LIMIT 1", (stream_id, lane_id, authority, previous),
    ).fetchone()
    if row is None:
        return {}
    return {"dispatch_id": row["dispatch_id"], "original_message_id": row["input_identity"],
            "lane_id": lane_id, "expected_lane_version": row["version"]}


def _assistant_route_lane_conn(conn, stream_id, route, lane_id):
    # One operator request may intentionally split into several admitted lanes.
    # Admission receipts are durable lane provenance even if route_json names
    # the most recent admission; a shared actor alone is never sufficient.
    if json.loads(route["route_json"] or "{}").get("lane_id") == lane_id:
        return True
    return conn.execute(
        "SELECT 1 FROM v2_assistant_composite_operations WHERE stream_id=? AND dispatch_id=? "
        "AND lane_id=? AND operation='lane.admit' LIMIT 1",
        (stream_id, route["dispatch_id"], lane_id),
    ).fetchone() is not None


def _assistant_operation_scope_conn(conn, *, stream_id, dispatch_id, actor, generation,
                                    authority, operation, lane_id, payload, reply_id):
    route = _assistant_dispatch_conn(conn, stream_id, dispatch_id, actor, generation)
    fallback = route["routing_state"] == "fallback_dispatched"
    if operation == "route.resolve":
        if not fallback or reply_id != route["input_identity"]:
            raise ValueError("assistant_route_resolve_scope_unverified")
        return route
    if route["routing_state"] != "resolved":
        raise ValueError("assistant_operation_dispatch_state_invalid")
    if operation == "lane.admit":
        if actor != authority or payload.get("request_message_id") != route["input_identity"]:
            raise ValueError("assistant_admission_input_scope_unverified")
        return route
    if not lane_id or not _assistant_route_lane_conn(conn, stream_id, route, lane_id):
        raise ValueError("assistant_operation_lane_scope_unverified")
    row = conn.execute(
        "SELECT * FROM v2_assistant_composite_lanes WHERE stream_id=? AND lane_id=?",
        (stream_id, lane_id),
    ).fetchone()
    if row is None:
        raise ValueError("assistant_lane_not_found")
    lane = dict(row)
    if actor != authority or operation.startswith("question."):
        if (lane["bound_stream_id"], lane["bound_generation"]) != (actor, generation):
            raise ValueError("assistant_operation_bound_lead_required")
    if operation == "question.cancel" and payload.get("question_id") != lane["pending_question_id"]:
        raise ValueError("assistant_question_binding_unverified")
    if operation == "question.open" and lane["pending_question_id"]:
        raise ValueError("assistant_question_already_pending")
    if operation == "lane.decision":
        for basis in payload.get("operator_basis_message_ids", []):
            evidence = conn.execute(
                "SELECT actor_stream_id,route_json,dispatch_id FROM v2_assistant_composite_routes "
                "WHERE stream_id=? AND input_identity=?", (stream_id, basis),
            ).fetchone()
            if (evidence is None or not str(evidence["actor_stream_id"] or "").startswith("operator:")
                    or not _assistant_route_lane_conn(conn, stream_id, evidence, lane_id)):
                raise ValueError("assistant_operation_basis_scope_unverified")
    if operation == "lane.close":
        report = conn.execute(
            "SELECT 1 FROM v2_assistant_composite_terminal_reports WHERE stream_id=? AND lane_id=? AND report_id=?",
            (stream_id, lane_id, payload.get("completion_message_id")),
        ).fetchone()
        if report is None or lane["completion_report_id"] != payload.get("completion_message_id"):
            raise ValueError("assistant_operation_completion_scope_unverified")
    return route


class _RoutingStoreMixin:
    async def preflight_assistant_composite_question_operation(
        self, *, stream_id: str, lane_id: str, expected_lane_version: int, operation_id: str,
        operation: str, dispatch_id: str, payload: dict[str, Any], evidence_refs: list[str],
        actor_stream_id: str | None,
        actor_generation: str | None = None,
        authority_stream_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate the lane receipt boundary before touching the question store.

        The existing question request is keyed by operation_id, so retry after a
        post-side-effect crash reuses that durable question identity rather than
        opening another card.  This preflight is intentionally a store read;
        It records a minimal prepared bridge before the existing question-store
        side effect.  Thus a post-effect crash has one recoverable, idempotent
        request identity rather than an orphan card or a second question system.
        """
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        evidence_json = json.dumps(evidence_refs, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        receipt_json = json.dumps({
            "stream_id": stream_id, "dispatch_id": dispatch_id, "lane_id": lane_id,
            "operation": operation, "reply_to_message_id": None,
            "payload": payload, "evidence_refs": evidence_refs,
            "expected_lane_version": expected_lane_version, "actor_stream_id": actor_stream_id,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()

        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if actor_generation is not None:
                    _assistant_actor_conn(conn, actor_stream_id, actor_generation)
                prior = conn.execute(
                    "SELECT * FROM v2_assistant_composite_operations WHERE operation_id=?", (operation_id,),
                ).fetchone()
                if prior is not None:
                    if str(prior["payload_digest"] or "") != digest:
                        raise ValueError("assistant_operation_idempotency_conflict")
                    out = dict(prior)
                    out.update({"committed": True, "duplicate": True})
                    conn.commit()
                    return out
                if actor_generation is not None:
                    _assistant_operation_scope_conn(conn, stream_id=stream_id, dispatch_id=dispatch_id,
                        actor=actor_stream_id, generation=actor_generation, authority=authority_stream_id,
                        operation=operation, lane_id=lane_id, payload=payload, reply_id=None)
                row = conn.execute(
                    """SELECT version,phase,question_bridge_operation_id FROM v2_assistant_composite_lanes
                       WHERE stream_id=? AND lane_id=?""", (stream_id, lane_id),
                ).fetchone()
                if row is None or str(row["phase"]) not in {"discussion", "execution", "waiting"}:
                    raise ValueError("assistant_question_lane_not_current")
                bridge = conn.execute(
                    "SELECT * FROM v2_assistant_composite_question_bridges WHERE operation_id=?", (operation_id,),
                ).fetchone()
                if bridge is not None:
                    saved = dict(bridge)
                    if (
                        saved["stream_id"], saved["lane_id"], saved["operation"], saved["payload_digest"],
                        saved["actor_stream_id"], int(saved["expected_lane_version"]),
                    ) != (stream_id, lane_id, operation, digest, actor_stream_id or "", expected_lane_version):
                        raise ValueError("assistant_question_bridge_conflict")
                    if saved["state"] == "failed":
                        if int(row["version"]) != expected_lane_version:
                            raise ValueError("assistant_lane_version_conflict")
                        if str(row["question_bridge_operation_id"] or "") not in {"", operation_id}:
                            raise ValueError("assistant_question_bridge_in_progress")
                        conn.execute(
                            "UPDATE v2_assistant_composite_question_bridges SET state='prepared',updated_at=? WHERE operation_id=?",
                            (_routing_iso_now(), operation_id),
                        )
                        conn.execute(
                            "UPDATE v2_assistant_composite_lanes SET question_bridge_operation_id=?,updated_at=? WHERE stream_id=? AND lane_id=?",
                            (operation_id, _routing_iso_now(), stream_id, lane_id),
                        )
                    conn.commit()
                    return {"prepared": True, "duplicate": True, "state": "prepared"}
                if int(row["version"]) != expected_lane_version:
                    raise ValueError("assistant_lane_version_conflict")
                if str(row["question_bridge_operation_id"] or ""):
                    raise ValueError("assistant_question_bridge_in_progress")
                stamp = _routing_iso_now()
                conn.execute(
                    """INSERT INTO v2_assistant_composite_question_bridges(
                        operation_id,stream_id,lane_id,operation,payload_digest,payload_json,actor_stream_id,
                        expected_lane_version,state,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (operation_id, stream_id, lane_id, operation, digest, payload_json, actor_stream_id or "",
                     expected_lane_version, "prepared", stamp, stamp),
                )
                conn.execute(
                    """UPDATE v2_assistant_composite_lanes
                       SET question_bridge_operation_id=?,updated_at=? WHERE stream_id=? AND lane_id=?""",
                    (operation_id, stamp, stream_id, lane_id),
                )
                conn.commit()
                return {"prepared": True, "duplicate": False, "state": "prepared"}
            except BaseException:
                conn.rollback()
                raise
        return await self.submit(_op)

    async def fail_assistant_composite_question_bridge(
        self, *, stream_id: str, lane_id: str, operation_id: str,
    ) -> None:
        """Release only a known pre-effect failure; ambiguous effects stay prepared."""
        def _op(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN IMMEDIATE")
            try:
                changed = conn.execute(
                    """UPDATE v2_assistant_composite_question_bridges SET state='failed',updated_at=?
                       WHERE operation_id=? AND stream_id=? AND lane_id=? AND state='prepared'""",
                    (_routing_iso_now(), operation_id, stream_id, lane_id),
                ).rowcount
                if changed:
                    conn.execute(
                        """UPDATE v2_assistant_composite_lanes SET question_bridge_operation_id=NULL,updated_at=?
                           WHERE stream_id=? AND lane_id=? AND question_bridge_operation_id=?""",
                        (_routing_iso_now(), stream_id, lane_id, operation_id),
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        await self.submit(_op)

    """Route and outbound-notice persistence mixed into Store."""

    def _init_routing_integrity_state(self) -> None:
        self._routing_lifecycle_locks: dict[str, _RoutingIntegrityLifecycleEntry] = {}

    async def ensure_assistant_composite_projection(
        self, *, stream_id: str, title: str = "Assistant",
    ) -> dict[str, Any]:
        """Create the one daemon-owned, pane-less session exactly once.

        ``open_session`` intentionally refreshes a normal observation row, so
        it is the wrong primitive here: doing that at boot would rewrite
        ``created_at``.  This narrow insert-if-absent preserves the synthetic
        chat identity and its generation across every daemon restart.
        """
        host, separator, session_name = str(stream_id).partition(":")
        if not separator or not host or not session_name:
            raise ValueError("assistant_composite_invalid_stream_id")

        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE host=? AND session_name=?",
                    (host, session_name),
                ).fetchone()
                if row is not None:
                    existing = _session_row(conn, row)
                    if str((existing or {}).get("provider") or "") != "composite":
                        raise ValueError("assistant_composite_stream_conflict")
                    if str((existing or {}).get("status") or "") != "open":
                        raise ValueError("assistant_composite_projection_closed")
                    conn.commit()
                    return dict(existing or {})
                created_at = _routing_iso_now()
                generation = "assistant-composite-v1-" + hashlib.sha256(
                    (stream_id + "\x00" + created_at).encode("utf-8")
                ).hexdigest()[:24]
                conn.execute(
                    """INSERT INTO sessions (
                        host,session_name,visibility,created_at,status,provider,role,phase,
                        pane_status,title,self_close_on_completion
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,0)""",
                    (
                        host, session_name, "default", created_at, "open", "composite",
                        "assistant_composite", "discussion", "no_pane", title,
                    ),
                )
                conn.execute(
                    "INSERT INTO v2_session_generations (host,session_name,generation) VALUES (?,?,?)",
                    (host, session_name, generation),
                )
                inserted = _session_row(conn, conn.execute(
                    "SELECT * FROM sessions WHERE host=? AND session_name=?", (host, session_name),
                ).fetchone())
                conn.commit()
                return dict(inserted or {})
            except BaseException:
                conn.rollback()
                raise

        return await self.submit(_op)

    async def admit_assistant_composite_input(
        self,
        *,
        stream_id: str,
        input_identity: str,
        input_request_id: str,
        body: str,
        attachments: list[dict[str, Any]],
        reply_to_message_id: str | None,
        reply_to_question_id: str | None,
        actor_stream_id: str | None,
    ) -> dict[str, Any]:
        """Atomically append the visible USER event and its routing receipt."""
        if not input_identity:
            raise ValueError("assistant_input_identity_required")
        canonical_attachments = json.dumps(attachments, sort_keys=True, separators=(",", ":"))
        material = json.dumps({
            "body": body,
            "attachments": attachments,
            "reply_to_message_id": reply_to_message_id,
            "reply_to_question_id": reply_to_question_id,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        route_id = "assistant-route-" + hashlib.sha256(
            (stream_id + "\x00" + input_identity).encode("utf-8")
        ).hexdigest()[:32]
        host, separator, session_name = stream_id.partition(":")
        if not separator or not host or not session_name:
            raise ValueError("assistant_composite_invalid_stream_id")

        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute("BEGIN IMMEDIATE")
            try:
                prior = conn.execute(
                    "SELECT * FROM v2_assistant_composite_routes WHERE stream_id=? AND input_identity=?",
                    (stream_id, input_identity),
                ).fetchone()
                if prior is not None:
                    record = dict(prior)
                    if record["payload_digest"] != digest:
                        raise ValueError("assistant_input_idempotency_conflict")
                    record["duplicate"] = True
                    conn.commit()
                    return record
                session = conn.execute(
                    "SELECT created_at,status,provider FROM sessions WHERE host=? AND session_name=?",
                    (host, session_name),
                ).fetchone()
                if session is None or session["status"] != "open" or session["provider"] != "composite":
                    raise ValueError("assistant_composite_projection_unavailable")
                timestamp = _routing_iso_now()
                event = {
                    "stream_id": stream_id,
                    "provider": "composite",
                    "kind": "USER",
                    "text": body,
                    "message_id": input_identity,
                    "optimistic_id": input_identity,
                    "reply_to_message_id": reply_to_message_id,
                    "reply_to_question_id": reply_to_question_id,
                    "attachments": attachments,
                    "timestamp": timestamp,
                    "raw": {
                        "assistant_composite": True,
                        "input_identity": input_identity,
                        "input_request_id": input_request_id,
                        "attachments": attachments,
                        "reply_to_message_id": reply_to_message_id,
                        "reply_to_question_id": reply_to_question_id,
                        "actor_stream_id": actor_stream_id,
                    },
                }
                event_json = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                event_key = hashlib.sha256(event_json.encode("utf-8")).hexdigest()
                cur = conn.execute(
                    """INSERT INTO session_event_tail(
                        stream_id,session_created_at,event_key,event_json,event_ts,recorded_at,identity
                    ) VALUES (?,?,?,?,?,?,?)""",
                    (
                        stream_id, str(session["created_at"]), event_key, event_json,
                        timestamp, time.time(), "assistant-input:" + input_identity,
                    ),
                )
                event_id = int(cur.lastrowid)
                conn.execute(
                    """INSERT INTO v2_assistant_composite_routes(
                        route_id,stream_id,input_identity,payload_digest,input_request_id,event_id,body,
                        attachments_json,reply_to_message_id,reply_to_question_id,actor_stream_id,
                        routing_state,delivery_state,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        route_id, stream_id, input_identity, digest, input_request_id, event_id, body,
                        canonical_attachments, reply_to_message_id, reply_to_question_id, actor_stream_id,
                        "queued", None, timestamp, timestamp,
                    ),
                )
                row = dict(conn.execute(
                    "SELECT * FROM v2_assistant_composite_routes WHERE route_id=?", (route_id,),
                ).fetchone())
                row["event"] = event | {"daemon_seq": event_id}
                row["duplicate"] = False
                conn.commit()
                return row
            except BaseException:
                conn.rollback()
                raise

        return await self.submit(_op)

    async def claim_assistant_composite_route(
        self, *, stream_id: str, owner: str, lease_seconds: float = 60.0,
    ) -> dict[str, Any] | None:
        """Claim exactly one queued route.  Classification remains sequential."""
        now = time.time()

        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """SELECT * FROM v2_assistant_composite_routes
                       WHERE stream_id=? AND routing_state='queued'
                         AND (lease_until IS NULL OR lease_until<?)
                       ORDER BY created_at,route_id LIMIT 1""",
                    (stream_id, now),
                ).fetchone()
                if row is None:
                    conn.commit()
                    return None
                route_id = str(row["route_id"])
                stamp = _routing_iso_now()
                conn.execute(
                    """UPDATE v2_assistant_composite_routes
                       SET routing_state='classifying', lease_owner=?, lease_until=?, updated_at=?
                       WHERE route_id=?""",
                    (owner, now + max(1.0, lease_seconds), stamp, route_id),
                )
                claimed = dict(conn.execute(
                    "SELECT * FROM v2_assistant_composite_routes WHERE route_id=?", (route_id,),
                ).fetchone())
                conn.commit()
                return claimed
            except BaseException:
                conn.rollback()
                raise

        return await self.submit(_op)

    async def recover_assistant_composite_routes(self, *, stream_id: str) -> dict[str, int]:
        """Map interrupted route phases across a daemon restart.

        ``classifying`` has not crossed a provider boundary and can safely be
        returned to the one sequential router.  ``intent`` and
        ``committed_pending`` may have crossed that boundary, so they are
        retained as uncertain until a receipt/provider evidence path resolves
        them.  This is deliberately narrow recovery, not a delivery broker.
        """
        def _op(conn: sqlite3.Connection) -> dict[str, int]:
            conn.execute("BEGIN IMMEDIATE")
            try:
                stamp = _routing_iso_now()
                requeued = conn.execute(
                    """UPDATE v2_assistant_composite_routes
                       SET routing_state='queued', lease_owner=NULL, lease_until=NULL,
                           error_code='recovered_classification', updated_at=?
                       WHERE stream_id=? AND routing_state='classifying'""",
                    (stamp, stream_id),
                ).rowcount
                uncertain = conn.execute(
                    """UPDATE v2_assistant_composite_routes
                       SET delivery_state='uncertain',
                           error_code='recovery_delivery_evidence_required',
                           lease_owner=NULL, lease_until=NULL, updated_at=?
                       WHERE stream_id=?
                         AND routing_state IN ('resolved','fallback_dispatched')
                         AND delivery_state IN ('intent','committed_pending')""",
                    (stamp, stream_id),
                ).rowcount
                conn.commit()
                return {
                    "requeued_classifying": int(requeued),
                    "retained_uncertain": int(uncertain),
                }
            except BaseException:
                conn.rollback()
                raise

        return await self.submit(_op)

    async def update_assistant_composite_route(
        self,
        route_id: str,
        *,
        routing_state: str,
        delivery_state: str | None = None,
        dispatch_id: str | None = None,
        route_target: str | None = None,
        route_target_generation: str | None = None,
        route_payload: dict[str, Any] | None = None,
        depends_on_message_id: str | None = None,
        error_code: str | None = None,
        expected_dispatch_id: str | None = None,
        expected_routing_state: str | None = None,
    ) -> dict[str, Any] | None:
        allowed_routing = {
            "queued", "classifying", "fallback_dispatched", "deferred", "resolved", "routing_failed",
        }
        allowed_delivery = {None, "intent", "committed_pending", "landed", "uncertain", "failed"}
        if routing_state not in allowed_routing or delivery_state not in allowed_delivery:
            raise ValueError("assistant_route_state_invalid")
        route_json = json.dumps(route_payload, sort_keys=True, separators=(",", ":")) if route_payload is not None else None

        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM v2_assistant_composite_routes WHERE route_id=?", (route_id,),
                ).fetchone()
                if row is None:
                    conn.commit()
                    return None
                if (expected_dispatch_id is not None and row["dispatch_id"] != expected_dispatch_id
                        or expected_routing_state is not None and row["routing_state"] != expected_routing_state):
                    conn.commit()
                    return None
                existing = dict(row)
                conn.execute(
                    """UPDATE v2_assistant_composite_routes SET
                         routing_state=?, delivery_state=COALESCE(?,delivery_state),
                         dispatch_id=COALESCE(?,dispatch_id), route_target=COALESCE(?,route_target),
                         route_target_generation=COALESCE(?,route_target_generation),
                         route_json=COALESCE(?,route_json), depends_on_message_id=COALESCE(?,depends_on_message_id),
                         error_code=?, lease_owner=NULL, lease_until=NULL, updated_at=?
                       WHERE route_id=?""",
                    (
                        routing_state, delivery_state, dispatch_id, route_target, route_target_generation, route_json,
                        depends_on_message_id, error_code, _routing_iso_now(), route_id,
                    ),
                )
                updated = dict(conn.execute(
                    "SELECT * FROM v2_assistant_composite_routes WHERE route_id=?", (route_id,),
                ).fetchone())
                if existing.get("dispatch_id") and dispatch_id and existing["dispatch_id"] != dispatch_id:
                    raise ValueError("assistant_dispatch_id_conflict")
                conn.commit()
                return updated
            except BaseException:
                conn.rollback()
                raise

        return await self.submit(_op)

    async def find_assistant_composite_route_by_dispatch(
        self, dispatch_id: str,
    ) -> dict[str, Any] | None:
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT * FROM v2_assistant_composite_routes WHERE dispatch_id=?", (dispatch_id,),
            ).fetchone()
            return dict(row) if row is not None else None
        return await self.submit(_op)

    async def authorize_assistant_composite_dispatch(
        self, *, stream_id: str, dispatch_id: str, actor: str, generation: str,
    ) -> dict[str, Any]:
        return await self.submit(lambda conn: _assistant_dispatch_conn(
            conn, stream_id, dispatch_id, actor, generation))

    async def get_assistant_composite_route(
        self, *, stream_id: str, input_identity: str,
    ) -> dict[str, Any] | None:
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT * FROM v2_assistant_composite_routes WHERE stream_id=? AND input_identity=?",
                (stream_id, input_identity),
            ).fetchone()
            return dict(row) if row is not None else None
        return await self.submit(_op)

    async def list_assistant_composite_unresolved(
        self, *, stream_id: str, exclude_route_id: str, limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Small causal-context hint for the next sequential classification."""
        effective_limit = max(0, min(int(limit), 32))
        if not effective_limit:
            return []
        def _op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                """SELECT route_id,input_identity,body,routing_state,depends_on_message_id
                   FROM v2_assistant_composite_routes
                   WHERE stream_id=? AND route_id<>?
                     AND routing_state IN ('queued','classifying','fallback_dispatched','deferred')
                   ORDER BY created_at DESC,route_id DESC LIMIT ?""",
                (stream_id, exclude_route_id, effective_limit),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]
        return await self.submit(_op)

    async def list_assistant_composite_admitted_lanes(self, *, stream_id: str, dispatch_id: str):
        def _op(conn):
            return [row[0] for row in conn.execute(
                "SELECT DISTINCT lane_id FROM v2_assistant_composite_operations "
                "WHERE stream_id=? AND dispatch_id=? AND operation='lane.admit'",
                (stream_id, dispatch_id),
            ).fetchall()]
        return await self.submit(_op)

    async def get_assistant_composite_publication(self, *, stream_id: str, publication_key: str):
        def _op(conn):
            row = conn.execute(
                "SELECT * FROM v2_assistant_composite_publications WHERE stream_id=? AND publication_key=?",
                (stream_id, publication_key),
            ).fetchone()
            return dict(row) if row is not None else None
        return await self.submit(_op)

    async def record_assistant_composite_publication(
        self,
        *,
        stream_id: str,
        publication_key: str,
        dispatch_id: str,
        reply_to_message_id: str | None,
        reply_to_question_id: str | None,
        publish_kind: str,
        attachment_ids: list[str],
        evidence_refs: list[str],
        canonical_payload: dict[str, Any],
        event: dict[str, Any],
        actor_stream_id: str | None = None,
        actor_generation: str | None = None,
    ) -> dict[str, Any]:
        """Append one assistant event and its idempotency receipt together."""
        if not publication_key or not dispatch_id:
            raise ValueError("assistant_publication_identity_required")
        canonical_json = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        event_json = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        host, separator, session_name = stream_id.partition(":")
        if not separator or not host or not session_name:
            raise ValueError("assistant_composite_invalid_stream_id")

        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if actor_generation is not None:
                    _assistant_actor_conn(conn, actor_stream_id, actor_generation)
                prior = conn.execute(
                    "SELECT * FROM v2_assistant_composite_publications WHERE publication_key=?",
                    (publication_key,),
                ).fetchone()
                if prior is not None:
                    saved = dict(prior)
                    if saved["payload_digest"] != digest:
                        raise ValueError("assistant_publication_idempotency_conflict")
                    replay_event = json.loads(conn.execute(
                        "SELECT event_json FROM session_event_tail WHERE event_id=?", (saved["event_id"],),
                    ).fetchone()[0])
                    replay_event["daemon_seq"] = int(saved["event_id"])
                    saved.update({"event": replay_event, "duplicate": True})
                    conn.commit()
                    return saved
                if actor_generation is not None:
                    route = _assistant_dispatch_conn(conn, stream_id, dispatch_id, actor_stream_id, actor_generation)
                    if route["routing_state"] != "resolved":
                        raise ValueError("assistant_publish_dispatch_state_invalid")
                    if reply_to_message_id != route["input_identity"]:
                        raise ValueError("assistant_publish_reply_unverified")
                    if publish_kind != "question" and reply_to_question_id != route["reply_to_question_id"]:
                        raise ValueError("assistant_publish_question_unverified")
                    if publish_kind != "prose":
                        correlated = False
                        for ref in evidence_refs:
                            receipt = conn.execute(
                                "SELECT * FROM v2_assistant_composite_operations "
                                "WHERE operation_id=? AND stream_id=? AND dispatch_id=? AND actor_stream_id=?",
                                (ref, stream_id, dispatch_id, actor_stream_id),
                            ).fetchone()
                            if receipt is not None:
                                if publish_kind == "question":
                                    question = conn.execute(
                                        "SELECT question_id FROM v2_assistant_composite_question_bridges "
                                        "WHERE operation_id=? AND state='committed'", (ref,),
                                    ).fetchone()
                                    current_question = conn.execute(
                                        "SELECT pending_question_id FROM v2_assistant_composite_lanes "
                                        "WHERE stream_id=? AND lane_id=? AND phase IN ('discussion','execution','waiting')",
                                        (stream_id, receipt["lane_id"]),
                                    ).fetchone()
                                    correlated = (receipt["operation"] == "question.open" and question is not None
                                                  and question["question_id"] == reply_to_question_id
                                                  and current_question is not None
                                                  and current_question["pending_question_id"] == reply_to_question_id)
                                elif publish_kind == "result":
                                    correlated = receipt["operation"] == "lane.close"
                                else:
                                    correlated = receipt["operation"] != "route.resolve"
                            if publish_kind == "result" and not correlated:
                                correlated = conn.execute(
                                    "SELECT 1 FROM v2_assistant_composite_terminal_reports "
                                    "WHERE report_id=? AND stream_id=? AND dispatch_id=? AND actor_stream_id=?",
                                    (ref, stream_id, dispatch_id, actor_stream_id),
                                ).fetchone() is not None
                            if correlated:
                                break
                        if not correlated:
                            raise ValueError("assistant_publish_operation_receipt_required")
                session = conn.execute(
                    "SELECT created_at,status,provider FROM sessions WHERE host=? AND session_name=?",
                    (host, session_name),
                ).fetchone()
                if session is None or session["status"] != "open" or session["provider"] != "composite":
                    raise ValueError("assistant_composite_projection_unavailable")
                event_key = hashlib.sha256(event_json.encode("utf-8")).hexdigest()
                identity = "assistant-publish:" + publication_key
                cur = conn.execute(
                    """INSERT INTO session_event_tail(
                        stream_id,session_created_at,event_key,event_json,event_ts,recorded_at,identity
                    ) VALUES (?,?,?,?,?,?,?)""",
                    (
                        stream_id, str(session["created_at"]), event_key, event_json,
                        event.get("timestamp"), time.time(), identity,
                    ),
                )
                event_id = int(cur.lastrowid)
                stamp = _routing_iso_now()
                conn.execute(
                    """INSERT INTO v2_assistant_composite_publications(
                        publication_key,stream_id,payload_digest,canonical_payload_json,dispatch_id,
                        reply_to_message_id,reply_to_question_id,publish_kind,attachment_ids_json,
                        evidence_refs_json,event_id,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        publication_key, stream_id, digest, canonical_json, dispatch_id,
                        reply_to_message_id, reply_to_question_id, publish_kind,
                        json.dumps(attachment_ids, separators=(",", ":")),
                        json.dumps(evidence_refs, separators=(",", ":")), event_id, stamp,
                    ),
                )
                stored = dict(conn.execute(
                    "SELECT * FROM v2_assistant_composite_publications WHERE publication_key=?", (publication_key,),
                ).fetchone())
                stored.update({"event": dict(event) | {"daemon_seq": event_id}, "duplicate": False})
                conn.commit()
                return stored
            except BaseException:
                conn.rollback()
                raise

        return await self.submit(_op)

    async def apply_assistant_composite_operation(
        self,
        *,
        stream_id: str,
        operation_id: str,
        operation: str,
        lane_id: str | None,
        actor_stream_id: str | None,
        payload: dict[str, Any],
        dispatch_id: str = "",
        actor_generation: str | None = None,
        authority_stream_id: str | None = None,
        reply_to_message_id: str | None = None,
        expected_lane_version: int | None = None,
        evidence_refs: list[str] | None = None,
        route_id: str | None = None,
        route_dispatch_id: str | None = None,
        route_target: str | None = None,
        route_target_generation: str | None = None,
        route_defer_dependency: str | None = None,
        question_id: str | None = None,
        authority_wake_recipient: str | None = None,
        authority_wake_tell_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist the fixed authority-operation matrix; never interpret prose."""
        allowed = {
            "lane.admit", "lane.bind", "lane.decision", "lane.close",
            "question.open", "question.cancel", "route.resolve",
        }
        if operation not in allowed or not operation_id:
            raise ValueError("assistant_operation_invalid")
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        evidence_json = json.dumps(evidence_refs or [], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        receipt_json = json.dumps({
            "stream_id": stream_id, "dispatch_id": dispatch_id, "lane_id": lane_id,
            "operation": operation, "reply_to_message_id": reply_to_message_id,
            "payload": payload, "evidence_refs": evidence_refs or [],
            "expected_lane_version": expected_lane_version, "actor_stream_id": actor_stream_id,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()

        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if actor_generation is not None:
                    _assistant_actor_conn(conn, actor_stream_id, actor_generation)
                prior = conn.execute(
                    "SELECT * FROM v2_assistant_composite_operations WHERE operation_id=?", (operation_id,),
                ).fetchone()
                if prior is not None:
                    record = dict(prior)
                    if record["payload_digest"] != digest or record["operation"] != operation:
                        raise ValueError("assistant_operation_idempotency_conflict")
                    record["duplicate"] = True
                    conn.commit()
                    return record
                scoped_route = None
                if actor_generation is not None:
                    scoped_route = _assistant_operation_scope_conn(conn,
                        stream_id=stream_id, dispatch_id=dispatch_id, actor=actor_stream_id,
                        generation=actor_generation, authority=authority_stream_id,
                        operation=operation, lane_id=lane_id, payload=payload,
                        reply_id=reply_to_message_id)
                prior_phase = next_phase = None
                lane: dict[str, Any] | None = None
                audit_lane_id = lane_id

                def require_lane_version(row: sqlite3.Row) -> None:
                    expected = expected_lane_version
                    if isinstance(expected, bool) or not isinstance(expected, int) or expected != int(row["version"]):
                        raise ValueError("assistant_lane_version_conflict")

                def require_no_question_bridge(row: sqlite3.Row) -> None:
                    bridge_id = str(row["question_bridge_operation_id"] or "")
                    if bridge_id and bridge_id != operation_id:
                        raise ValueError("assistant_question_bridge_in_progress")

                if operation == "lane.admit":
                    mode = str(payload.get("mode") or payload.get("admission") or "")
                    request_message_id = str(payload.get("request_message_id") or "").strip()
                    if mode not in {"new", "fold"} or not request_message_id:
                        raise ValueError("assistant_lane_admit_invalid")
                    if mode == "new":
                        subject = str(payload.get("subject") or "").strip()
                        if not audit_lane_id or not subject or len(subject) > 512:
                            raise ValueError("assistant_lane_admit_invalid")
                        existing = conn.execute(
                            "SELECT * FROM v2_assistant_composite_lanes WHERE lane_id=?", (audit_lane_id,),
                        ).fetchone()
                        if existing is not None:
                            raise ValueError("assistant_lane_exists")
                        stamp = _routing_iso_now()
                        conn.execute(
                            """INSERT INTO v2_assistant_composite_lanes(
                                lane_id,stream_id,parent_lane_id,phase,summary,created_at,updated_at
                            ) VALUES (?,?,?,?,?,?,?)""",
                            (audit_lane_id, stream_id, payload.get("parent_lane_id"), "discussion", subject, stamp, stamp),
                        )
                        next_phase = "discussion"
                    else:
                        target_lane_id = str(payload.get("target_lane_id") or audit_lane_id or "")
                        row = conn.execute(
                            "SELECT * FROM v2_assistant_composite_lanes WHERE lane_id=? AND stream_id=?",
                            (target_lane_id, stream_id),
                        ).fetchone()
                        if row is None or str(row["phase"]) not in {"discussion", "execution", "waiting"}:
                            raise ValueError("assistant_lane_fold_state_invalid")
                        require_lane_version(row)
                        audit_lane_id = target_lane_id
                        prior_phase = str(row["phase"])
                        next_phase = prior_phase
                        conn.execute(
                            "UPDATE v2_assistant_composite_lanes SET version=version+1,updated_at=? WHERE lane_id=?",
                            (_routing_iso_now(), target_lane_id),
                        )
                    if scoped_route is not None:
                        scoped_payload = json.loads(scoped_route["route_json"] or "{}")
                        scoped_payload["lane_id"] = audit_lane_id
                        conn.execute("UPDATE v2_assistant_composite_routes SET route_json=? WHERE route_id=?",
                                     (json.dumps(scoped_payload, sort_keys=True), scoped_route["route_id"]))
                elif operation in {"lane.bind", "lane.decision", "lane.close"}:
                    if not audit_lane_id:
                        raise ValueError("assistant_lane_id_required")
                    row = conn.execute(
                        "SELECT * FROM v2_assistant_composite_lanes WHERE lane_id=? AND stream_id=?",
                        (audit_lane_id, stream_id),
                    ).fetchone()
                    if row is None:
                        raise ValueError("assistant_lane_not_found")
                    require_no_question_bridge(row)
                    require_lane_version(row)
                    lane = dict(row)
                    prior_phase = str(lane["phase"])
                    if operation == "lane.bind":
                        if prior_phase not in {"discussion", "execution", "waiting"}:
                            raise ValueError("assistant_lane_bind_state_invalid")
                        bound = str(payload.get("backend_stream_id") or "").strip()
                        generation = str(payload.get("backend_generation") or "").strip()
                        backend_kind = str(payload.get("backend_kind") or "").strip()
                        if not bound or not generation or not backend_kind:
                            raise ValueError("assistant_lane_bind_invalid")
                        if actor_generation is not None:
                            _assistant_actor_conn(conn, bound, generation)
                        conn.execute(
                            """UPDATE v2_assistant_composite_lanes
                               SET bound_stream_id=?,bound_generation=?,bound_backend_kind=?,
                                   version=version+1,updated_at=? WHERE lane_id=?""",
                            (bound, generation, backend_kind, _routing_iso_now(), audit_lane_id),
                        )
                        next_phase = prior_phase
                    elif operation == "lane.decision":
                        transition = str(payload.get("transition") or "")
                        decision_id = str(payload.get("decision_id") or "").strip()
                        requested_from = str(payload.get("from_phase") or "")
                        requested_to = str(payload.get("to_phase") or "")
                        matrix = {
                            "start": {("discussion", "execution"), ("waiting", "execution")},
                            "wait": {("discussion", "waiting"), ("execution", "waiting")},
                            "reopen": {("completed", "discussion"), ("cancelled", "discussion")},
                            "cancel": {("discussion", "cancelled"), ("execution", "cancelled"), ("waiting", "cancelled")},
                        }
                        if (not decision_id or (prior_phase, requested_to) not in matrix.get(transition, set())
                                or requested_from != prior_phase):
                            raise ValueError("assistant_lane_decision_transition_invalid")
                        basis = payload.get("operator_basis_message_ids")
                        if not isinstance(basis, list) or not basis or not all(isinstance(v, str) and v for v in basis):
                            raise ValueError("assistant_lane_decision_basis_required")
                        conn.execute(
                            "UPDATE v2_assistant_composite_lanes SET phase=?,version=version+1,updated_at=? WHERE lane_id=?",
                            (requested_to, _routing_iso_now(), audit_lane_id),
                        )
                        if transition == "reopen":
                            conn.execute("UPDATE v2_assistant_composite_lanes SET completion_report_id=NULL WHERE lane_id=?",
                                         (audit_lane_id,))
                        next_phase = requested_to
                    else:
                        if (
                            prior_phase != "completed"
                            or not str(payload.get("completion_message_id") or "")
                            or str(payload.get("completion_disposition") or "") != "accepted"
                        ):
                            raise ValueError("assistant_lane_close_state_invalid")
                        conn.execute(
                            "UPDATE v2_assistant_composite_lanes SET phase='closed',version=version+1,updated_at=? WHERE lane_id=?",
                            (_routing_iso_now(), audit_lane_id),
                        )
                        next_phase = "closed"
                elif operation in {"question.open", "question.cancel"}:
                    if not audit_lane_id:
                        raise ValueError("assistant_lane_id_required")
                    row = conn.execute(
                        "SELECT * FROM v2_assistant_composite_lanes WHERE lane_id=? AND stream_id=?",
                        (audit_lane_id, stream_id),
                    ).fetchone()
                    if row is None or str(row["phase"]) not in {"discussion", "execution", "waiting"}:
                        raise ValueError("assistant_question_lane_not_current")
                    require_no_question_bridge(row)
                    require_lane_version(row)
                    prior_phase = next_phase = str(row["phase"])
                    bridge = conn.execute(
                        "SELECT * FROM v2_assistant_composite_question_bridges WHERE operation_id=?",
                        (operation_id,),
                    ).fetchone()
                    if bridge is None or str(bridge["state"] or "") != "prepared":
                        raise ValueError("assistant_question_bridge_missing")
                    if (
                        str(bridge["stream_id"] or ""), str(bridge["lane_id"] or ""),
                        str(bridge["operation"] or ""), str(bridge["payload_digest"] or ""),
                    ) != (stream_id, audit_lane_id, operation, digest):
                        raise ValueError("assistant_question_bridge_conflict")
                    if operation == "question.open":
                        if not question_id:
                            raise ValueError("assistant_question_id_required")
                        conn.execute(
                            """UPDATE v2_assistant_composite_lanes
                               SET pending_question_id=?,question_bridge_operation_id=NULL,
                                   version=version+1,updated_at=? WHERE stream_id=? AND lane_id=?""",
                            (question_id, _routing_iso_now(), stream_id, audit_lane_id),
                        )
                    else:
                        requested_question_id = str(payload.get("question_id") or "")
                        if requested_question_id and requested_question_id != str(row["pending_question_id"] or ""):
                            raise ValueError("assistant_question_binding_unverified")
                        conn.execute(
                            """UPDATE v2_assistant_composite_lanes
                               SET pending_question_id=NULL,question_bridge_operation_id=NULL,
                                   version=version+1,updated_at=? WHERE stream_id=? AND lane_id=?""",
                            (_routing_iso_now(), stream_id, audit_lane_id),
                        )
                    conn.execute(
                        """UPDATE v2_assistant_composite_question_bridges
                           SET question_id=?,state='committed',updated_at=? WHERE operation_id=?""",
                        (question_id if operation == "question.open" else requested_question_id or None,
                         _routing_iso_now(), operation_id),
                    )
                elif operation == "route.resolve":
                    resolved_route_id = str(route_id or "")
                    target = str(route_target or "")
                    resolved_dispatch_id = str(route_dispatch_id or "")
                    if not resolved_route_id:
                        raise ValueError("assistant_route_resolve_invalid")
                    existing_route = conn.execute(
                        "SELECT route_json FROM v2_assistant_composite_routes WHERE route_id=? AND stream_id=?",
                        (resolved_route_id, stream_id),
                    ).fetchone()
                    try:
                        fallback_payload = json.loads(str((existing_route or {})["route_json"] or "{}"))
                    except (TypeError, ValueError, KeyError):
                        fallback_payload = {}
                    persisted_route_payload = dict(payload)
                    if isinstance(fallback_payload, dict) and isinstance(
                        fallback_payload.get("routing_context"), dict,
                    ):
                        persisted_route_payload["backend_context"] = fallback_payload["routing_context"]
                    persisted_route_json = json.dumps(
                        persisted_route_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                    )
                    if route_defer_dependency:
                        updated = conn.execute(
                            """UPDATE v2_assistant_composite_routes
                               SET routing_state='deferred',delivery_state=NULL,route_json=?,
                                   depends_on_message_id=?,updated_at=?,lease_owner=NULL,lease_until=NULL
                               WHERE route_id=? AND stream_id=? AND routing_state='fallback_dispatched'""",
                            (persisted_route_json, route_defer_dependency, _routing_iso_now(), resolved_route_id, stream_id),
                        )
                    else:
                        if not target or not resolved_dispatch_id:
                            raise ValueError("assistant_route_resolve_invalid")
                        updated = conn.execute(
                            """UPDATE v2_assistant_composite_routes
                               SET routing_state='resolved',delivery_state='intent',dispatch_id=?,
                                   route_target=?,route_target_generation=?,route_json=?,updated_at=?,lease_owner=NULL,lease_until=NULL
                               WHERE route_id=? AND stream_id=? AND routing_state='fallback_dispatched'""",
                            (resolved_dispatch_id, target, route_target_generation, persisted_route_json, _routing_iso_now(), resolved_route_id, stream_id),
                        )
                    if updated.rowcount != 1:
                        raise ValueError("assistant_route_resolve_state_invalid")
                    # Only an explicitly causal deferred input resumes.  A
                    # busy unrelated lane never turns ordinary follow-up into
                    # an accidental held inbox.
                    conn.execute(
                        """UPDATE v2_assistant_composite_routes
                           SET routing_state='queued',depends_on_message_id=NULL,updated_at=?
                           WHERE stream_id=? AND routing_state='deferred'
                             AND depends_on_message_id=(
                                SELECT input_identity FROM v2_assistant_composite_routes WHERE route_id=?
                             )""",
                        (_routing_iso_now(), stream_id, resolved_route_id),
                    )
                # A decision made by the currently bound lead is a gate, not
                # ordinary progress.  Persist its one authority wake in the
                # existing outbox in this same authority-operation transaction.
                # Replays return the operation receipt before reaching here, so
                # one operation key can never create a second wake.
                if operation == "lane.decision" and authority_wake_recipient and authority_wake_tell_id:
                    authority_context = _assistant_authority_context_conn(conn, stream_id, audit_lane_id, authority_wake_recipient)
                    transition = str(payload.get("transition") or "")
                    _insert_outbound_notice_conn(
                        conn,
                        notice_id=authority_wake_tell_id,
                        kind="assistant_composite_authority",
                        dedupe_key=authority_wake_tell_id,
                        recipient_stream_id=authority_wake_recipient,
                        tell_id=authority_wake_tell_id,
                        source_stream_id=stream_id,
                        body=(
                            "[assistant composite authority decision]\n"
                            f"lane_id={audit_lane_id}\noperation_id={operation_id}\n"
                            f"transition={transition}\n"
                            f"authority_context={json.dumps(authority_context, sort_keys=True)}\n"
                            "Inspect the committed lane decision and its operator basis. "
                            "Do not infer accepted closure."
                        ),
                        metadata={
                            "lane_id": audit_lane_id,
                            "operation_id": operation_id,
                            "transition": transition,
                            "authority_context": authority_context,
                        },
                    )
                conn.execute(
                    """INSERT INTO v2_assistant_composite_operations(
                        operation_id,stream_id,dispatch_id,lane_id,reply_to_message_id,operation,payload_digest,payload_json,
                        evidence_refs_json,expected_lane_version,actor_stream_id,prior_phase,next_phase,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        operation_id, stream_id, dispatch_id, audit_lane_id, reply_to_message_id, operation, digest, payload_json,
                        evidence_json, expected_lane_version, actor_stream_id, prior_phase, next_phase, _routing_iso_now(),
                    ),
                )
                result = {
                    "operation_id": operation_id, "operation": operation, "lane_id": audit_lane_id,
                    "prior_phase": prior_phase, "next_phase": next_phase, "duplicate": False,
                }
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise

        return await self.submit(_op)

    async def complete_assistant_composite_lane(
        self, *, stream_id: str, lane_id: str, dispatch_id: str,
        actor_stream_id: str, actor_generation: str, report_id: str,
        authority_wake_recipient: str | None = None,
    ) -> dict[str, Any] | None:
        """A current backend's terminal report is the only completed transition."""
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            conn.execute("BEGIN IMMEDIATE")
            try:
                route = _assistant_dispatch_conn(conn, stream_id, dispatch_id, actor_stream_id, actor_generation)
                if route["routing_state"] != "resolved" or not _assistant_route_lane_conn(conn, stream_id, route, lane_id):
                    raise ValueError("assistant_terminal_report_scope_unverified")
                prior = conn.execute(
                    "SELECT * FROM v2_assistant_composite_terminal_reports WHERE report_id=?", (report_id,),
                ).fetchone()
                if prior is not None:
                    if (
                        prior["stream_id"], prior["lane_id"], prior["dispatch_id"], prior["actor_stream_id"]
                    ) != (stream_id, lane_id, dispatch_id, actor_stream_id):
                        raise ValueError("assistant_terminal_report_idempotency_conflict")
                    row = conn.execute(
                        "SELECT * FROM v2_assistant_composite_lanes WHERE stream_id=? AND lane_id=?",
                        (stream_id, lane_id),
                    ).fetchone()
                    conn.commit()
                    return dict(row) if row is not None else None
                row = conn.execute(
                    """SELECT * FROM v2_assistant_composite_lanes
                       WHERE stream_id=? AND lane_id=?
                         AND phase IN ('execution','waiting')""",
                    (stream_id, lane_id),
                ).fetchone()
                if row is None:
                    conn.commit()
                    return None
                current = _assistant_actor_conn(conn, actor_stream_id, actor_generation)
                if ((row["bound_stream_id"], row["bound_generation"]) != (actor_stream_id, actor_generation)
                        and current["handoff_from_stream_id"] != row["bound_stream_id"]):
                    raise ValueError("assistant_terminal_report_bound_lead_required")
                if row["completion_report_id"] and row["completion_report_id"] != report_id:
                    raise ValueError("assistant_lane_completion_conflict")
                conn.execute(
                    """UPDATE v2_assistant_composite_lanes
                       SET phase='completed',completion_report_id=?,version=version+1,updated_at=? WHERE lane_id=?""",
                    (report_id, _routing_iso_now(), row["lane_id"]),
                )
                conn.execute(
                    """INSERT INTO v2_assistant_composite_terminal_reports(
                        report_id,stream_id,lane_id,dispatch_id,actor_stream_id,actor_generation,payload_digest,created_at
                    ) VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        report_id, stream_id, lane_id, dispatch_id, actor_stream_id, actor_generation,
                        hashlib.sha256((stream_id + "\0" + lane_id + "\0" + dispatch_id + "\0" + report_id).encode()).hexdigest(),
                        _routing_iso_now(),
                    ),
                )
                # A submitted terminal report moves the lane only to
                # ``completed``.  Its separately durable notice wakes the
                # authority once to accept/reopen/close; it never claims that
                # the report itself was accepted as closure.
                if authority_wake_recipient:
                    authority_context = _assistant_authority_context_conn(conn, stream_id, lane_id, authority_wake_recipient)
                    tell_id = f"assistant-terminal:{row['lane_id']}:{report_id}"
                    _insert_outbound_notice_conn(
                        conn,
                        notice_id=tell_id,
                        kind="assistant_composite_authority",
                        dedupe_key=tell_id,
                        recipient_stream_id=authority_wake_recipient,
                        tell_id=tell_id,
                        source_stream_id=stream_id,
                        body=(
                            "[assistant composite terminal report]\n"
                            f"lane_id={row['lane_id']}\ncompletion_report_id={report_id}\n"
                            f"authority_context={json.dumps(authority_context, sort_keys=True)}\n"
                            "The bound lead submitted a terminal report. Inspect its evidence; "
                            "submit lane.close only after accepted completion."
                        ),
                        metadata={
                            "lane_id": row["lane_id"],
                            "completion_report_id": report_id,
                            "authority_context": authority_context,
                        },
                    )
                completed = dict(conn.execute(
                    "SELECT * FROM v2_assistant_composite_lanes WHERE lane_id=?", (row["lane_id"],),
                ).fetchone())
                conn.commit()
                return completed
            except BaseException:
                conn.rollback()
                raise
        return await self.submit(_op)

    async def get_assistant_composite_lane(
        self, *, stream_id: str, lane_id: str,
    ) -> dict[str, Any] | None:
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT * FROM v2_assistant_composite_lanes WHERE stream_id=? AND lane_id=?",
                (stream_id, lane_id),
            ).fetchone()
            return dict(row) if row is not None else None
        return await self.submit(_op)

    async def get_assistant_composite_operation(self, operation_id: str) -> dict[str, Any] | None:
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT * FROM v2_assistant_composite_operations WHERE operation_id=?", (operation_id,),
            ).fetchone()
            return dict(row) if row is not None else None

        return await self.submit(_op)

    async def list_assistant_composite_open_lanes(
        self, *, stream_id: str, limit: int = 16,
    ) -> list[dict[str, Any]]:
        """Small routing-context projection; closed/cancelled lanes are absent."""
        effective_limit = max(0, min(int(limit), 16))
        if not effective_limit:
            return []

        def _op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                """SELECT lane_id,phase,bound_stream_id,bound_generation,bound_backend_kind,
                          pending_question_id,summary,version,updated_at
                   FROM v2_assistant_composite_lanes
                   WHERE stream_id=? AND phase IN ('discussion','execution','waiting')
                   ORDER BY updated_at DESC,lane_id DESC LIMIT ?""",
                (stream_id, effective_limit),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

        return await self.submit(_op)

    async def set_assistant_composite_lane_question(
        self, *, stream_id: str, lane_id: str, question_id: str | None,
    ) -> None:
        def _op(conn: sqlite3.Connection) -> None:
            changed = conn.execute(
                """UPDATE v2_assistant_composite_lanes
                   SET pending_question_id=?,version=version+1,updated_at=?
                   WHERE stream_id=? AND lane_id=? AND phase IN ('discussion','execution','waiting')""",
                (question_id, _routing_iso_now(), stream_id, lane_id),
            ).rowcount
            if changed != 1:
                raise ValueError("assistant_question_lane_not_current")
            conn.commit()

        await self.submit(_op)

    async def get_assistant_composite_lane_for_question(
        self, *, stream_id: str, question_id: str,
    ) -> dict[str, Any] | None:
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                """SELECT * FROM v2_assistant_composite_lanes
                   WHERE stream_id=? AND pending_question_id=?
                     AND phase IN ('discussion','execution','waiting')""",
                (stream_id, question_id),
            ).fetchone()
            return dict(row) if row is not None else None

        return await self.submit(_op)

    def routing_integrity_lifecycle_lock(self, stream_id: str) -> _RoutingIntegrityLifecycleGuard:
        """Serialize close/reopen with routing notification delivery.

        The returned guard retains the dictionary entry before it can suspend
        on acquisition, which makes closed-stream cache eviction race-safe.
        """
        entry = self._routing_lifecycle_locks.get(stream_id)
        if entry is None:
            entry = _RoutingIntegrityLifecycleEntry(self, stream_id)
            self._routing_lifecycle_locks[stream_id] = entry
        return _RoutingIntegrityLifecycleGuard(entry)

    def _retire_routing_integrity_lifecycle_lock(self, stream_id: str) -> None:
        entry = self._routing_lifecycle_locks.get(stream_id)
        if entry is None:
            return
        entry.retire_requested = True
        self._maybe_retire_routing_integrity_lifecycle_lock(entry)

    def _maybe_retire_routing_integrity_lifecycle_lock(
        self, entry: _RoutingIntegrityLifecycleEntry
    ) -> None:
        if (
            entry.retire_requested
            and entry.references == 0
            and not entry.lock.locked()
            and self._routing_lifecycle_locks.get(entry.stream_id) is entry
        ):
            self._routing_lifecycle_locks.pop(entry.stream_id, None)

    async def apply_routing_integrity(
        self,
        host: str,
        session_name: str,
        *,
        provider: str,
        requested_model: str,
        requested_effort: str,
        effective_model: str | None,
        effective_effort: str | None,
        integrity: str,
        reason: str | None,
        observed_at: str | None = None,
        expected_generation: str | None = None,
    ) -> dict[str, Any] | None:
        """Atomically stamp an observation and maintain its current episode."""
        observed = observed_at or _routing_iso_now()
        stream_id = f"{host}:{session_name}"

        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            conn.execute("BEGIN IMMEDIATE")
            try:
                session_row = conn.execute(
                    "SELECT * FROM sessions WHERE host=? AND session_name=?",
                    (host, session_name),
                ).fetchone()
                if session_row is None:
                    conn.commit()
                    return None
                session = dict(session_row)
                session_generation = str(session.get("created_at") or "")
                if expected_generation is not None and session_generation != expected_generation:
                    conn.commit()
                    return None
                episode: dict[str, Any] | None = None
                if integrity == "mismatch":
                    current_row = conn.execute(
                        "SELECT * FROM v2_routing_integrity WHERE stream_id=?",
                        (stream_id,),
                    ).fetchone()
                    current = dict(current_row) if current_row is not None else None
                    incoming_tuple = (
                        provider,
                        requested_model,
                        requested_effort,
                        str(effective_model or ""),
                        str(effective_effort or ""),
                    )
                    current_tuple = None if current is None else (
                        str(current.get("provider") or ""),
                        str(current.get("requested_model") or ""),
                        str(current.get("requested_effort") or ""),
                        str(current.get("effective_model") or ""),
                        str(current.get("effective_effort") or ""),
                    )
                    if current is None:
                        episode_id = _routing_episode_id(
                            conn,
                            stream_id=stream_id,
                            session_generation=session_generation,
                            provider=provider,
                            requested_model=requested_model,
                            requested_effort=requested_effort,
                            effective_model=effective_model,
                            effective_effort=effective_effort,
                            first_observed=observed,
                        )
                        first_observed = observed
                        conn.execute(
                            """INSERT INTO v2_routing_integrity (
                                stream_id, host, session_name, provider, episode_id,
                                requested_model, requested_effort, effective_model,
                                effective_effort, reason, first_observed_at, updated_at,
                                parent_stream_id, notified_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                stream_id,
                                host,
                                session_name,
                                provider,
                                episode_id,
                                requested_model,
                                requested_effort,
                                effective_model,
                                effective_effort,
                                reason,
                                first_observed,
                                observed,
                                session.get("parent_stream_id"),
                                None,
                            ),
                        )
                        conn.execute(
                            """INSERT OR IGNORE INTO v2_routing_integrity_audit (
                                audit_id, stream_id, episode_id, event_type, actor,
                                reason, metadata, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                "routing-drift-" + episode_id,
                                stream_id,
                                episode_id,
                                "routing_drift",
                                "daemon:routing_integrity",
                                reason,
                                json.dumps(
                                    {
                                        "provider": provider,
                                        "requested_model": requested_model,
                                        "requested_effort": requested_effort,
                                        "effective_model": effective_model,
                                        "effective_effort": effective_effort,
                                    },
                                    separators=(",", ":"),
                                ),
                                observed,
                            ),
                        )
                    elif current_tuple != incoming_tuple:
                        old_episode_id = str(current["episode_id"])
                        supersede_material = "\x1f".join(
                            (old_episode_id, *incoming_tuple, observed)
                        )
                        conn.execute(
                            """INSERT OR IGNORE INTO v2_routing_integrity_audit (
                                audit_id, stream_id, episode_id, event_type, actor,
                                reason, metadata, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                "routing-superseded-"
                                + hashlib.sha256(supersede_material.encode("utf-8")).hexdigest()[:24],
                                stream_id,
                                old_episode_id,
                                "routing_drift_superseded",
                                "daemon:routing_integrity",
                                reason,
                                json.dumps(
                                    {
                                        "old_episode_id": old_episode_id,
                                        "new_tuple": incoming_tuple,
                                    },
                                    separators=(",", ":"),
                                ),
                                observed,
                            ),
                        )
                        conn.execute(
                            "DELETE FROM v2_routing_integrity WHERE stream_id=?",
                            (stream_id,),
                        )
                        episode_id = _routing_episode_id(
                            conn,
                            stream_id=stream_id,
                            session_generation=session_generation,
                            provider=provider,
                            requested_model=requested_model,
                            requested_effort=requested_effort,
                            effective_model=effective_model,
                            effective_effort=effective_effort,
                            first_observed=observed,
                        )
                        conn.execute(
                            """INSERT INTO v2_routing_integrity (
                                stream_id, host, session_name, provider, episode_id,
                                requested_model, requested_effort, effective_model,
                                effective_effort, reason, first_observed_at, updated_at,
                                parent_stream_id, notified_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                stream_id,
                                host,
                                session_name,
                                provider,
                                episode_id,
                                requested_model,
                                requested_effort,
                                effective_model,
                                effective_effort,
                                reason,
                                observed,
                                observed,
                                session.get("parent_stream_id"),
                                None,
                            ),
                        )
                        conn.execute(
                            """INSERT OR IGNORE INTO v2_routing_integrity_audit (
                                audit_id, stream_id, episode_id, event_type, actor,
                                reason, metadata, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                "routing-drift-" + episode_id,
                                stream_id,
                                episode_id,
                                "routing_drift",
                                "daemon:routing_integrity",
                                reason,
                                json.dumps(
                                    {
                                        "provider": provider,
                                        "requested_model": requested_model,
                                        "requested_effort": requested_effort,
                                        "effective_model": effective_model,
                                        "effective_effort": effective_effort,
                                    },
                                    separators=(",", ":"),
                                ),
                                observed,
                            ),
                        )
                    else:
                        episode_id = str(current["episode_id"])
                        conn.execute(
                            """UPDATE v2_routing_integrity
                               SET provider=?, requested_model=?, requested_effort=?,
                                   effective_model=?, effective_effort=?, reason=?,
                                   updated_at=?, parent_stream_id=?
                             WHERE stream_id=?""",
                            (
                                provider,
                                requested_model,
                                requested_effort,
                                effective_model,
                                effective_effort,
                                reason,
                                observed,
                                session.get("parent_stream_id"),
                                stream_id,
                            ),
                        )
                    episode_row = conn.execute(
                        "SELECT * FROM v2_routing_integrity WHERE stream_id=?",
                        (stream_id,),
                    ).fetchone()
                    episode = dict(episode_row) if episode_row is not None else None
                    if episode is not None:
                        episode["session_generation"] = session_generation
                    event_id = episode.get("episode_id") if episode is not None else None
                else:
                    prior = conn.execute(
                        "SELECT episode_id FROM v2_routing_integrity WHERE stream_id=?",
                        (stream_id,),
                    ).fetchone()
                    if prior is not None:
                        conn.execute(
                            """INSERT OR IGNORE INTO v2_routing_integrity_audit (
                                audit_id, stream_id, episode_id, event_type, actor,
                                reason, metadata, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                "routing-resolved-" + str(prior["episode_id"]),
                                stream_id,
                                str(prior["episode_id"]),
                                "routing_drift_resolved",
                                "daemon:routing_integrity",
                                reason,
                                "{}",
                                observed,
                            ),
                        )
                        conn.execute("DELETE FROM v2_routing_integrity WHERE stream_id=?", (stream_id,))
                    event_id = None
                conn.execute(
                    """UPDATE sessions
                       SET effective_model=?, effective_effort=?, routing_integrity=NULL,
                           routing_integrity_reason=NULL, routing_integrity_updated_at=NULL
                     WHERE host=? AND session_name=?""",
                    (
                        effective_model,
                        effective_effort,
                        host,
                        session_name,
                    ),
                )
                conn.commit()
                session = _session_row(conn, conn.execute(
                    "SELECT * FROM sessions WHERE host=? AND session_name=?",
                    (host, session_name),
                ).fetchone()) or session
                return {
                    "session": session,
                    "episode": episode,
                    "should_notify": bool(episode and not episode.get("notified_at")),
                }
            except Exception:
                conn.rollback()
                raise

        return await self.submit(_op)

    async def routing_integrity_episode(self, stream_id: str) -> dict[str, Any] | None:
        """Return the current durable routing-drift episode, if any."""
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT * FROM v2_routing_integrity WHERE stream_id=?",
                (stream_id,),
            ).fetchone()
            return None if row is None else dict(row)

        return await self.submit(_op)

    async def mark_routing_integrity_notified(
        self,
        stream_id: str,
        episode_id: str,
        *,
        expected_generation: str | None = None,
    ) -> bool:
        """Durably close the one-notice window for an episode."""
        stamp = _routing_iso_now()

        def _op(conn: sqlite3.Connection) -> bool:
            if expected_generation is not None:
                generation_host, separator, generation_name = stream_id.partition(":")
                if not separator:
                    conn.commit()
                    return False
                session = conn.execute(
                    "SELECT created_at FROM sessions WHERE host=? AND session_name=?",
                    (generation_host, generation_name),
                ).fetchone()
                if session is None or str(session[0] or "") != expected_generation:
                    conn.commit()
                    return False
            cur = conn.execute(
                """UPDATE v2_routing_integrity SET notified_at=?
                   WHERE stream_id=? AND episode_id=? AND notified_at IS NULL""",
                (stamp, stream_id, episode_id),
            )
            conn.commit()
            return cur.rowcount == 1

        return await self.submit(_op)

    async def enqueue_outbound_notice(
        self,
        *,
        notice_id: str,
        kind: str,
        dedupe_key: str,
        recipient_stream_id: str,
        tell_id: str,
        body: str,
        source_stream_id: str | None = None,
        episode_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: str | None = None,
        watch_fact: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert the notice and satisfy subscriptions in the same transaction."""
        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            from store_watch_wake import coalesce_notice_conn
            with conn:
                notice = dict(notice_id=notice_id, kind=kind, dedupe_key=dedupe_key,
                    recipient_stream_id=recipient_stream_id, tell_id=tell_id, body=body,
                    source_stream_id=source_stream_id, episode_id=episode_id,
                    metadata=metadata, created_at=created_at)
                return coalesce_notice_conn(conn, notice, watch_fact)
        return await self.submit(_op)

    async def list_outbound_notice_ids(
        self,
        *,
        limit: int = 32,
        now: float | None = None,
        kinds: set[str] | None = None,
        force: bool = False,
    ) -> list[str]:
        stamp = time.time() if now is None else float(now)

        def _op(conn: sqlite3.Connection) -> list[str]:
            clauses = [
                "delivered_at IS NULL",
                "terminal_at IS NULL",
                "(lease_until IS NULL OR lease_until <= ?)",
            ]
            params: list[Any] = [stamp]
            if not force:
                clauses.append("next_attempt_at <= ?")
                params.append(stamp)
            if kinds:
                marks = ",".join("?" for _ in kinds)
                clauses.append(f"kind IN ({marks})")
                params.extend(sorted(kinds))
            params.append(max(1, int(limit)))
            rows = conn.execute(
                "SELECT notice_id FROM v2_outbound_notices WHERE "
                + " AND ".join(clauses)
                + " ORDER BY next_attempt_at ASC, created_at ASC LIMIT ?",
                params,
            ).fetchall()
            return [str(row[0]) for row in rows]

        return await self.submit(_op)

    async def claim_outbound_notice(
        self,
        notice_id: str,
        *,
        owner: str,
        lease_s: float,
        force: bool = False,
    ) -> dict[str, Any] | None:
        now = time.time()
        lease_until = now + max(0.1, float(lease_s))

        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            due_clause = "" if force else " AND next_attempt_at <= ?"
            params: list[Any] = [owner, lease_until, notice_id, now]
            if not force:
                params.append(now)
            cur = conn.execute(
                """UPDATE v2_outbound_notices
                   SET lease_owner=?, lease_until=?, attempts=attempts+1
                   WHERE notice_id=? AND delivered_at IS NULL AND terminal_at IS NULL
                     AND (lease_until IS NULL OR lease_until <= ?)""" + due_clause,
                params,
            )
            if cur.rowcount != 1:
                conn.commit()
                return None
            conn.commit()
            row = conn.execute(
                "SELECT * FROM v2_outbound_notices WHERE notice_id=?", (notice_id,)
            ).fetchone()
            return None if row is None else dict(row)

        return await self.submit(_op)

    async def fail_outbound_notice(
        self,
        notice_id: str,
        *,
        owner: str,
        error: str,
        next_attempt_at: float,
        next_action: str,
    ) -> bool:
        def _op(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                """UPDATE v2_outbound_notices
                   SET last_error=?, next_attempt_at=?, next_action=?,
                       lease_owner=NULL, lease_until=NULL
                   WHERE notice_id=? AND lease_owner=?
                     AND delivered_at IS NULL AND terminal_at IS NULL""",
                (error[:400], float(next_attempt_at), next_action[:400], notice_id, owner),
            )
            conn.commit()
            return cur.rowcount == 1

        return await self.submit(_op)

    async def complete_outbound_notice(self, notice_id: str, *, owner: str) -> bool:
        stamp = _routing_iso_now()

        def _op(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                """UPDATE v2_outbound_notices
                   SET delivered_at=?, last_error=NULL, next_action=NULL,
                       lease_owner=NULL, lease_until=NULL
                   WHERE notice_id=? AND lease_owner=?
                     AND delivered_at IS NULL AND terminal_at IS NULL""",
                (stamp, notice_id, owner),
            )
            conn.commit()
            return cur.rowcount == 1

        return await self.submit(_op)

    async def terminal_outbound_notice(
        self,
        notice_id: str,
        *,
        owner: str,
        reason: str,
        next_action: str,
    ) -> bool:
        stamp = _routing_iso_now()

        def _op(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                """UPDATE v2_outbound_notices
                   SET terminal_at=?, terminal_reason=?, last_error=?, next_action=?,
                       lease_owner=NULL, lease_until=NULL
                   WHERE notice_id=? AND lease_owner=?
                     AND delivered_at IS NULL AND terminal_at IS NULL""",
                (stamp, reason[:400], reason[:400], next_action[:400], notice_id, owner),
            )
            conn.commit()
            return cur.rowcount == 1

        return await self.submit(_op)

    async def complete_answer_notice_from_proof(self, notice_id: str) -> bool:
        """Settle one bounded-unconfirmed answer from its same-intent USER proof."""
        def _op(conn: sqlite3.Connection) -> bool:
            with conn:
                row = conn.execute("SELECT * FROM v2_outbound_notices WHERE notice_id=?", (notice_id,)).fetchone()
                if row is None or row["kind"] != "notification_answer" or row["terminal_reason"] != "unconfirmed_after_bound":
                    return False
                tell = conn.execute("SELECT reply FROM v2_tell_deliveries WHERE tell_id=?", (row["tell_id"],)).fetchone()
                if tell is None:
                    return False
                envelope = json.loads(tell["reply"])
                delivery, reply = envelope.get("delivery") or {}, envelope.get("reply") or {}
                metadata = json.loads(row["metadata"])
                if (reply.get("delivery_status") != "delivered" or delivery.get("delivery_status") != "delivered"
                        or delivery.get("proof_state") != "proven" or not delivery.get("proof_event_id")
                        or delivery.get("text") != row["body"]
                        or delivery.get("to_stream_id") != row["recipient_stream_id"]
                        or delivery.get("notification_answer_generation") != metadata.get("producer_session_generation")):
                    return False
                conn.execute("UPDATE v2_outbound_notices SET delivered_at=?, terminal_at=NULL, terminal_reason=NULL, "
                             "last_error=NULL, next_action=NULL WHERE notice_id=?", (_routing_iso_now(), notice_id))
                return True
        return await self.submit(_op)

def _routing_iso_now() -> str:
    """UTC stamp with sub-second precision for recurring observations."""
    now = time.time()
    whole = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
    return f"{whole}.{int(now * 1_000_000) % 1_000_000:06d}Z"

def _outbound_notice_digest(
    kind: str,
    dedupe_key: str,
    recipient_stream_id: str,
    tell_id: str,
    body: str,
    metadata_json: str,
    source_stream_id: str,
    episode_id: str,
) -> str:
    payload = "\x00".join(
        (
            kind,
            dedupe_key,
            recipient_stream_id,
            tell_id,
            body,
            metadata_json,
            source_stream_id,
            episode_id,
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _routing_episode_id(
    conn: sqlite3.Connection,
    *,
    stream_id: str,
    session_generation: str,
    provider: str,
    requested_model: str,
    requested_effort: str,
    effective_model: str | None,
    effective_effort: str | None,
    first_observed: str,
) -> str:
    """Derive a replay-stable episode id, suffixing only a real collision."""
    material = "\x1f".join(
        (
            stream_id,
            session_generation,
            provider,
            requested_model,
            requested_effort,
            str(effective_model or ""),
            str(effective_effort or ""),
            first_observed,
        )
    )
    base = "routing-drift-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    candidate = base
    suffix = 2
    while conn.execute(
        "SELECT 1 FROM v2_routing_integrity_audit WHERE episode_id=? LIMIT 1",
        (candidate,),
    ).fetchone():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate
