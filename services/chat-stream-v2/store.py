"""store.py — the ONLY module that touches SQLite.

Contract (v2_design.md module table):
  owns: SQLite in one worker thread, WAL, write queue.
  notes: **only** module touching the DB; the daemon's bounded retention job
         archives terminal rows and vacuums on a cadence so the hot set stays
         O(open).

The retention job lives in `retention.py` and runs its passes through `submit()`
like any other caller, one bounded batch per trip. Its loop-rule knobs are
documented on `retention.RetentionConfig`; the store's only obligations to it
are `path` (to locate the archive DB beside the hot one) and `pending()` (so a
VACUUM can stand down when requests are waiting).

Architecture constraint 2 (spec): zero blocking I/O on the event loop. The
asyncio side NEVER calls sqlite3 directly — it submits a callable to the queue
and awaits a future resolved by the worker thread.

Schema contract: v2 opens the SAME `sessions.db` as v1 (PK `(host,
session_name)`; `stream_id` is derived as `f"{host}:{session_name}"`, never
stored). v2-only auxiliary tables are prefixed `v2_` and are invisible to v1.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
from typing import Any, Callable
import uuid

import store_exchange
from store_exchange import ExchangeStoreMixin

from store_routing import (
    OUTBOUND_NOTICE_DDL,
    OUTBOUND_NOTICE_INDEX_DDL,
    ROUTING_INTEGRITY_AUDIT_DDL,
    ROUTING_INTEGRITY_DDL,
    ROUTING_INTEGRITY_NOTICE_DDL,
    _RoutingStoreMixin,
    _routing_iso_now,
)
from store_specs import (
    JSON_COLUMNS,
    SPEC_BINDING_PROVENANCE_KINDS,
    SPEC_JSON_COLUMNS,
    _SpecPersistenceMixin,
    _enc,
    _normalize_session_fields,
    _row,
    _session_row,
    normalize_spec_binding_provenance,
    normalize_spec_ids,
)
from claude_jsonl_norm import strip_peer_delivery_envelope
from submission_events import COMMITTED_PENDING_PROOF_STATUSES
from v2_runtime import iso_now


log = logging.getLogger("chat_streamd_v2.store")

EVENT_PUSH_TARGET_SHA_KEY = "event_push.target_sha"
EVENT_PUSH_TARGET_SHA_PREVIOUS_KEY = "event_push.target_sha.previous"
_FULL_GIT_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
_RECEIPT_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_SEND_REQUEST_ID_RE = re.compile(r"send-[A-Za-z0-9_-]{1,123}\Z")
_PROVENANCE_ID_MAX = 256
SCHEMA_VERSION = 1


class _EntryDropped:
    """One entry whose lifecycle predicate missed inside a batch CAS.

    Distinct from ``None`` (nothing new was stored — a durable replay, or a
    superseded Claude binding) so a caller can report the miss for that one
    entry instead of rejecting every entry the same host pushed alongside it.
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "ENTRY_DROPPED"


#: Sentinel returned in place of a sequence for a per-entry predicate miss.
ENTRY_DROPPED = _EntryDropped()


def _send_wire_digest(value: str) -> str:
    """Hash the normalized injected prompt without retaining staged paths."""
    cleaned = _RECEIPT_ANSI_RE.sub("", value or "").replace("\r", "").replace("\u00a0", " ")
    normalized = "\n".join(line.rstrip() for line in cleaned.split("\n")).strip()
    if not normalized:
        return ""
    normalized = re.sub(r"\s+", " ", normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _safe_provenance_id(value: object) -> str | None:
    """Keep actor claims bounded and metadata-only in durable projections."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:_PROVENANCE_ID_MAX]


def _decode_spawn_outcome(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Decode the optional structured delivery receipt once at the store edge."""
    if row and isinstance(row.get("delivery_receipt"), str):
        try:
            decoded = json.loads(row["delivery_receipt"])
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, dict):
            row["delivery_receipt"] = decoded
    return row


def _spawn_cancelled(conn: sqlite3.Connection, host: str, name: str, request_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM v2_spawn_cancellations WHERE host=? "
        "AND COALESCE(request_id, '')=? AND (request_id != '' OR session_name=?)",
        (host, request_id, name),
    ).fetchone() is not None or conn.execute(
        "SELECT 1 FROM v2_spawn_outcomes WHERE host=? AND session_name=? "
        "AND COALESCE(request_id, '')=? AND state='cancelled'",
        (host, name, request_id),
    ).fetchone() is not None


def _spawn_reservation(conn: sqlite3.Connection, host: str, name: str,
                       request_id: str, nonce: str | None = None) -> dict[str, Any] | None:
    row = _row(conn.execute(
        "SELECT * FROM v2_stream_reservations WHERE host=? AND session_name=? "
        "AND COALESCE(request_id, '')=?", (host, name, request_id),
    ).fetchone())
    if row is None or (nonce is not None and str(row.get("nonce") or "") != nonce):
        return None
    return None if _spawn_cancelled(conn, host, name, request_id) else row


def _admission_held(conn: sqlite3.Connection, host: str, now: float) -> bool:
    """Is a spawn admission freeze active for `host` right now? Read inside the
    admitting store txn (QA cycle-3 astra-[3]) so the hold check and the claim
    are ONE serialized operation -- a freeze set between a separate pre-check
    and the claim can no longer let a new admission slip through."""
    row = conn.execute(
        "SELECT held_until FROM v2_spawn_admission_hold WHERE host=?", (host,)
    ).fetchone()
    return row is not None and float(row["held_until"]) >= now


def _insert_stream_reservation(
    conn: sqlite3.Connection, *, host: str, session_name: str, request_id: str,
    expires_at: float, nonce: str, owner_instance_id: str,
    idempotency_key: str, request_payload_hash: str,
) -> None:
    """Write the one canonical stream-reservation row for both claim paths."""
    if _spawn_cancelled(conn, host, session_name, request_id):
        raise sqlite3.IntegrityError("spawn request was cancelled")
    conn.execute(
        "INSERT INTO v2_stream_reservations "
        "(host, session_name, request_id, expires_at, nonce, owner_instance_id, "
        "idempotency_key, request_payload_hash) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (host, session_name, request_id, expires_at, nonce or None,
         owner_instance_id or None, idempotency_key or None,
         request_payload_hash or None),
    )


def _upsert_spawn_outcome(
    conn: sqlite3.Connection, host: str, session_name: str, state: str, **fields: Any
) -> bool:
    """Write one `v2_spawn_outcomes` row inside the CALLER's transaction (no
    commit). Factored out of `set_spawn_outcome` so an atomic multi-write op
    (e.g. `cancel_reservation_fenced`) can terminalize an outcome and mutate the
    reservation in the SAME single-writer txn."""
    request_id = str(fields.get("request_id") or "")
    if _spawn_cancelled(conn, host, session_name, request_id):
        return False
    prior = conn.execute(
        "SELECT state FROM v2_spawn_outcomes WHERE host=? AND session_name=?", (host, session_name),
    ).fetchone()
    if prior is not None and prior["state"] == "cancelled" and not _spawn_reservation(
        conn, host, session_name, request_id,
    ):
        return False
    conn.execute(
        "INSERT INTO v2_spawn_outcomes (host, session_name, request_id, state, reason,"
        " effective_model, effective_effort, delivery_evidence, delivery_receipt, updated_at,"
        " idempotency_key, request_payload_hash, readiness_timed_out)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(host, session_name) DO UPDATE SET request_id=excluded.request_id,"
        " state=excluded.state, reason=excluded.reason, effective_model=excluded.effective_model,"
        " effective_effort=excluded.effective_effort,"
        " delivery_evidence=excluded.delivery_evidence, delivery_receipt=excluded.delivery_receipt,"
        " updated_at=excluded.updated_at,"
        " idempotency_key=COALESCE(excluded.idempotency_key, v2_spawn_outcomes.idempotency_key),"
        " request_payload_hash=COALESCE(excluded.request_payload_hash, v2_spawn_outcomes.request_payload_hash),"
        " readiness_timed_out=excluded.readiness_timed_out"
        " WHERE v2_spawn_outcomes.state != 'cancelled' "
        " OR COALESCE(v2_spawn_outcomes.request_id, '') != COALESCE(excluded.request_id, '')",
        (
            host, session_name, fields.get("request_id"), state, fields.get("reason"),
            fields.get("effective_model"), fields.get("effective_effort"),
            fields.get("delivery_evidence"),
            json.dumps(fields["delivery_receipt"], separators=(",", ":"))
            if isinstance(fields.get("delivery_receipt"), dict) else fields.get("delivery_receipt"),
            time.time(),
            fields.get("idempotency_key"), fields.get("request_payload_hash"),
            1 if fields.get("readiness_timed_out") else 0,
        ),
    )
    if state == "cancelled":
        # Persist the immutable request tombstone, carrying the creation nonce
        # (QA cycle-3 luna-[2]) so cleanup/identity can survive name reuse even
        # holding only the tombstone. Explicit columns: the cancellation table
        # has the extra `nonce`, so `SELECT *` no longer aligns.
        conn.execute(
            f"INSERT OR IGNORE INTO v2_spawn_cancellations ({_SPAWN_OUTCOME_COLUMNS}, nonce) "
            f"SELECT {_SPAWN_OUTCOME_COLUMNS}, ? FROM v2_spawn_outcomes "
            "WHERE host=? AND session_name=? AND state='cancelled'",
            (fields.get("nonce"), host, session_name),
        )
    return True


def _send_receipt_row(
    row: sqlite3.Row, *, include_attachments: bool = False, include_display_text: bool = False,
    include_rowid: bool = False,
) -> dict[str, Any]:
    """Project a receipt without leaking staged paths, wire text, or bytes."""
    try:
        attachments = json.loads(str(row["attachments_json"] or "[]"))
    except (TypeError, ValueError):
        attachments = []
    if not isinstance(attachments, list):
        attachments = []
    result: dict[str, Any] = {
        "receipt_id": str(row["receipt_id"]),
        "request_id": str(row["request_id"]),
        "to_stream_id": str(row["to_stream_id"]),
        "state": str(row["state"]),
        "content_kind": str(row["content_kind"]),
        "attachment_count": len(attachments),
        "delivery": str(row["delivery"]),
        "submission_confirmed": bool(row["submission_confirmed"]),
        "created_at": str(row["created_at"]),
    }
    if include_rowid:
        result["receipt_rowid"] = int(row["receipt_rowid"])
    columns = set(row.keys())
    if "from_stream_id" in columns and row["from_stream_id"]:
        result["from_stream_id"] = str(row["from_stream_id"])
    if "actor_stream_id" in columns and row["actor_stream_id"]:
        result["actor_stream_id"] = str(row["actor_stream_id"])
    if "actor_trusted" in columns:
        result["actor_trusted"] = bool(row["actor_trusted"])
    if row["optimistic_id"]:
        result["optimistic_id"] = str(row["optimistic_id"])
    if row["reason"]:
        result["reason"] = str(row["reason"])
    if row["attempts"] is not None:
        result["attempts"] = int(row["attempts"])
    if include_attachments:
        result["attachments"] = [dict(item) for item in attachments if isinstance(item, dict)]
    # Only the existing USER event projection needs the already-safe display
    # text.  The durable query stays metadata-only and never returns the
    # injected attachment wire text (or a staged path).
    if include_display_text:
        result["display_text"] = str(row["display_text"] or "")
    return result


# v1's `sessions` DDL plus v2's nullable title. Existing databases receive the
# same column through the additive startup seam below, with no row rewrite.
SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    host TEXT NOT NULL,
    session_name TEXT NOT NULL,
    parent_stream_id TEXT,
    objective TEXT,
    objective_source TEXT,
    role TEXT,
    phase TEXT,
    visibility TEXT NOT NULL,
    created_at TEXT NOT NULL, handoff_from_stream_id TEXT, closed_at TEXT,
    status TEXT NOT NULL DEFAULT 'open', spec_id TEXT, spec_resolution TEXT,
    offline_since_ts INTEGER, self_close_on_completion INTEGER NOT NULL DEFAULT 0,
    token_hash TEXT, token_hash_version TEXT, harness_run_id TEXT,
    opened_by_host_id TEXT, jsonl_path TEXT, claude_session_id TEXT, claude_session_lineage TEXT,
    spec_ids TEXT, presumed_dead_at TEXT, status_card TEXT,
    context_tokens INTEGER, model_context_window INTEGER, context_updated_at TEXT,
    context_level TEXT, pane_pid TEXT, observer_binding TEXT,
    pane_status TEXT NOT NULL DEFAULT 'pane_unknown',
    requested_model TEXT, requested_effort TEXT, effective_model TEXT,
    effective_effort TEXT, routing_integrity TEXT, routing_integrity_reason TEXT,
    routing_integrity_updated_at TEXT, provider TEXT, presumed_dead_fired_at TEXT,
    qualified_spec_ids TEXT, spec_binding_provenance TEXT, dead_open_closed_at TEXT,
    close_kind TEXT, bootstrap_state TEXT, title TEXT,
    PRIMARY KEY (host, session_name)
)
"""

# v2-only. Ledger req 4: spawn reserves a stream id ATOMICALLY with a TTL; a
# failed spawn releases it; a reserved id can never be adopted by an unrelated
# live session. Prefixed `v2_` so v1 never sees it.
#
# The row doubles as the crash-safe SPAWN INTENT (QA #5). `payload` is written
# before `tmux new-session`, `tmux_created` immediately after it, so a daemon
# death anywhere in that window still leaves a discoverable record that boot
# reconciliation can adopt (pane alive) or release (pane gone). Normal adoption
# never kills; if the fenced session-row registration itself fails, reconciliation
# rolls back only after confirming pane death. Without the intent, a live pane can
# become an orphan no row points at.
RESERVATIONS_DDL = """
CREATE TABLE IF NOT EXISTS v2_stream_reservations (
    host TEXT NOT NULL,
    session_name TEXT NOT NULL,
    request_id TEXT,
    expires_at REAL NOT NULL,
    payload TEXT,
    tmux_created INTEGER NOT NULL DEFAULT 0,
    nonce TEXT,
    owner_instance_id TEXT,
    pane_pid INTEGER,
    pane_started_at TEXT,
    idempotency_key TEXT,
    request_payload_hash TEXT,
    PRIMARY KEY (host, session_name)
)
"""
# In-flight dedupe marker: a same-key spawn arriving while a reservation still
# holds resolves to that in-flight session instead of minting a second pane
# (rpc_delivery_determinism lane). Content-hash lets a same-key/different-payload
# retry be rejected as a conflict rather than silently deduped.
STREAM_RESERVATION_IDEMPOTENCY_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS v2_stream_reservations_idempotency_key "
    "ON v2_stream_reservations (host, idempotency_key)"
)

# v2-only. `tell_id` is the caller's idempotency key (QA #6): a retried tell
# must not inject twice, so the durable delivery envelope is remembered and
# replayed (including a Codex ``pasted_unsubmitted`` outcome).
# Bounded by TELL_RETENTION rows — this is a dedupe window, not a ledger.
TELL_DELIVERY_DDL = """
CREATE TABLE IF NOT EXISTS v2_tell_deliveries (
    tell_id TEXT PRIMARY KEY,
    reply TEXT NOT NULL,
    created_at REAL NOT NULL
)
"""

# v2-only. Post-hoc role provenance. `role` itself stays on the sessions row —
# window_schedule authority reads `sessions.role` directly — so a `role set`
# needs no sessions-column migration. This aux row only records that the role
# was set on a LIVE seat via `role set` (vs spawn/handoff), plus the acting
# principal and timestamp, so inspect/list can project `role_source`. A NEW
# table (not a sessions column) is deliberate: `CREATE TABLE IF NOT EXISTS`
# lands it on the existing production DB at the next boot, while store boot
# never reshapes the `sessions` table (see test_store_schema_floor).
SESSION_ROLE_DDL = """
CREATE TABLE IF NOT EXISTS v2_session_role (
    host TEXT NOT NULL,
    session_name TEXT NOT NULL,
    role_source TEXT NOT NULL,
    actor TEXT,
    changed_at TEXT,
    PRIMARY KEY (host, session_name)
)
"""

TELL_RETENTION = 1000


def role_source_for(row: Any, stored: str | None) -> str | None:
    """Project a session's role provenance. A stored marker (written by a
    post-hoc `role set`) wins; otherwise derive from the row — a handoff
    successor is ``handoff``, any other roled seat is ``spawn``, a roleless
    seat is ``None``. Migration-safe: a session with no aux row reads as its
    derived source, so legacy rows need no backfill."""
    if stored:
        return stored
    row = row if isinstance(row, dict) else {}
    if str(row.get("handoff_from_stream_id") or "").strip():
        return "handoff"
    if str(row.get("role") or "").strip():
        return "spawn"
    return None


# v2-only. Restart-safe per-session nudge cooldown (the pinned dead-nudge-spam
# class, D2). v1 kept the last-nudge time in memory, so a daemon restart or a
# host sleep/wake flap re-minted it and the "please title yourself" reminder
# re-fired every pass. Persisting `(stream_id, kind) -> last_nudged_at` here
# means a mid-cooldown restart still honors the cooldown. `basis` records the
# state that was stale when we nudged (the status card's `updated_at`); a later
# card write moves past it, which resets the episode so a genuinely-updated
# session is free to be nudged again once it later goes stale. Bounded to the
# open-session set — closed rows are pruned each pass.
NUDGE_STATE_DDL = """
CREATE TABLE IF NOT EXISTS v2_nudge_state (
    stream_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    last_nudged_at REAL NOT NULL,
    basis TEXT,
    PRIMARY KEY (stream_id, kind)
)
"""

# Durable close/reap readback.  The v1 contract keeps this outside the stable
# sessions row; v2 uses the same child-table shape while retaining additive
# schema migration for databases that predate this lane.
SESSION_REAP_DDL = """
CREATE TABLE IF NOT EXISTS session_reap (
    stream_id TEXT PRIMARY KEY,
    reap_status TEXT NOT NULL CHECK (reap_status IN ('reaped', 'survivors', 'unknown')),
    survivors TEXT NOT NULL DEFAULT '[]',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    exhausted_at TEXT,
    updated_at TEXT NOT NULL
)
"""
SESSION_REAP_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_session_reap_status_next "
    "ON session_reap (reap_status, next_attempt_at)"
)
MAX_SESSION_REAP_SURVIVORS = 256

# v2-only.  `created_at` is a display/ordering field and is only second
# resolution in the shared sessions contract.  Reconciliation needs a durable
# per-open-generation identity so a close/reopen in one second cannot let an
# old death episode CAS the successor row.  Keeping it in a v2 table preserves
# the v1 sessions schema and makes the migration additive.
SESSION_GENERATIONS_DDL = """
CREATE TABLE IF NOT EXISTS v2_session_generations (
    host TEXT NOT NULL,
    session_name TEXT NOT NULL,
    generation TEXT NOT NULL,
    PRIMARY KEY (host, session_name)
)
"""

# v1's `session_event_tail` DDL, VERBATIM (not `v2_`-prefixed — this is a v1
# table both daemons share, so pre-cutover rows render after the swap and v1's
# retention/archive tool keeps working). Chat transcript events (B13 ingest)
# land here as one row per normalized event; `request_stream_events` serves the
# newest N. `identity` is the source-independent durable dedup key (live tail vs
# turn-end resync describe one record differently), NULLABLE so pre-migration
# rows are untouched. Created IF NOT EXISTS so the live DB (which already has it)
# is a no-op and a fresh test DB gets it.
SESSION_EVENT_TAIL_DDL = """
CREATE TABLE IF NOT EXISTS session_event_tail (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id TEXT NOT NULL,
    session_created_at TEXT NOT NULL DEFAULT '',
    event_key TEXT NOT NULL,
    event_json TEXT NOT NULL,
    event_ts TEXT,
    recorded_at REAL NOT NULL,
    identity TEXT,
    UNIQUE(stream_id, session_created_at, event_key)
)
"""
SESSION_EVENT_TAIL_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_session_event_tail_stream_event "
    "ON session_event_tail(stream_id, session_created_at, event_id DESC)"
)
SESSION_EVENT_TAIL_IDENTITY_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_session_event_tail_identity "
    "ON session_event_tail(stream_id, session_created_at, identity)"
)
SESSION_EVENT_TAIL_RECORDED_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_session_event_tail_recorded "
    "ON session_event_tail(recorded_at)"
)
# Shared event vocabulary retained by normalizer tests and callers.
INBOUND_TURN_KINDS = ("USER", "TELL")

# v2-only. The receipt lane deliberately owns one append-only log and no
# retention worker, cache, or retry machinery. SQLite's implicit rowid is the
# projection order: callers read the newest physical row for one
# (to_stream_id, request_id) key.
SEND_RECEIPT_DDL = """
CREATE TABLE IF NOT EXISTS v2_send_receipts (
    to_stream_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('accepted', 'landed', 'not_landed')),
    optimistic_id TEXT,
    wire_digest TEXT NOT NULL DEFAULT '',
    display_text TEXT NOT NULL DEFAULT '',
    content_kind TEXT NOT NULL,
    attachments_json TEXT NOT NULL DEFAULT '[]',
    delivery TEXT NOT NULL,
    submission_confirmed INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    attempts INTEGER,
    created_at TEXT NOT NULL,
    from_stream_id TEXT,
    actor_stream_id TEXT,
    actor_trusted INTEGER NOT NULL DEFAULT 0
)
"""

# Durable attribution for every close (spec: unaudited operator close of a live
# seat).  v2 has no lifecycle_audit subsystem, so this append-only, v2-prefixed
# table is the equivalent durable store: one row per close disposition
# (`closed`/`deferred`/`fenced`/`presumed_dead`) naming WHO closed the seat, with
# what auth, why, and under which request.  Patterned on v2_routing_integrity_audit.
CLOSE_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS v2_close_audit (
    audit_id TEXT PRIMARY KEY,
    stream_id TEXT NOT NULL,
    host TEXT NOT NULL,
    session_name TEXT NOT NULL,
    close_kind TEXT,
    disposition TEXT NOT NULL,
    actor_kind TEXT,
    closed_by TEXT,
    auth_kind TEXT,
    peer TEXT,
    reason TEXT,
    request_id TEXT,
    defer_if_working INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
)
"""
CLOSE_AUDIT_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS ix_v2_close_audit_stream "
    "ON v2_close_audit (stream_id, created_at)"
)

# When no rich caller attribution is threaded (internal close paths), the actor
# kind is derived from the close_kind so every audit row still names a who.
_CLOSE_ACTOR_BY_KIND = {
    "operator_close": "operator",
    "idle_reap": "reaper",
    "spawn_rollback": "spawnctl",
    "session_close": "self_or_parent",
    "reconciler_dead": "reconciler_dead",
}
_CLOSE_AUDIT_COLUMNS = (
    "audit_id, stream_id, host, session_name, close_kind, disposition, "
    "actor_kind, closed_by, auth_kind, peer, reason, request_id, "
    "defer_if_working, created_at"
)


def _close_audit_payload(
    *,
    host: str,
    session_name: str,
    close_kind: str | None,
    disposition: str,
    attribution: dict[str, Any] | None,
    reason: str | None,
) -> dict[str, Any]:
    attribution = attribution or {}
    actor_kind = str(attribution.get("actor_kind") or "").strip() or (
        _CLOSE_ACTOR_BY_KIND.get(str(close_kind or ""), "unknown")
    )

    def _clean(value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    return {
        "audit_id": uuid.uuid4().hex,
        "stream_id": f"{host}:{session_name}",
        "host": host,
        "session_name": session_name,
        "close_kind": close_kind,
        "disposition": disposition,
        "actor_kind": actor_kind,
        "closed_by": _clean(attribution.get("closed_by")),
        "auth_kind": _clean(attribution.get("auth_kind")),
        "peer": _clean(attribution.get("peer")),
        "reason": _clean(reason),
        "request_id": _clean(attribution.get("request_id")),
        "defer_if_working": 1 if attribution.get("defer_if_working") else 0,
        "created_at": _routing_iso_now(),
    }


def _insert_close_audit(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    conn.execute(
        f"INSERT OR IGNORE INTO v2_close_audit ({_CLOSE_AUDIT_COLUMNS}) VALUES ("
        ":audit_id, :stream_id, :host, :session_name, :close_kind, :disposition, "
        ":actor_kind, :closed_by, :auth_kind, :peer, :reason, :request_id, "
        ":defer_if_working, :created_at)",
        payload,
    )

# First-class v2 scheduled-spawn state and its operation receipts.  Every object
# is additive and v2-prefixed so a v1 rollback ignores it while a later forward
# redeploy can still reconcile terminal rows and receipts.
SCHEDULE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS v2_operation_receipts (
      receipt_id TEXT PRIMARY KEY,
      request_id TEXT NOT NULL,
      phase TEXT NOT NULL,
      surface TEXT NOT NULL CHECK(surface IN ('schedule')),
      verb TEXT NOT NULL,
      actor_kind TEXT NOT NULL CHECK(actor_kind IN ('seat','nexus','operator','service')),
      actor_id TEXT NOT NULL,
      canonical_payload_sha256 TEXT NOT NULL CHECK(length(canonical_payload_sha256)=64),
      target_id TEXT,
      measured_state_json TEXT NOT NULL CHECK(json_valid(measured_state_json)),
      result_json TEXT NOT NULL CHECK(json_valid(result_json)),
      measured_at TEXT NOT NULL,
      retain_until TEXT NOT NULL,
      UNIQUE(request_id,phase)
    )
    """,
    """CREATE INDEX IF NOT EXISTS ix_v2_receipt_target
       ON v2_operation_receipts(target_id)""",
    """CREATE INDEX IF NOT EXISTS ix_v2_receipt_retain
       ON v2_operation_receipts(retain_until)""",
    """
    CREATE TABLE IF NOT EXISTS v2_schedules (
      schedule_id TEXT PRIMARY KEY,
      request_id TEXT NOT NULL UNIQUE,
      owner_stream_id TEXT,
      owner_service_actor TEXT,
      owner_spec_ids_json TEXT NOT NULL CHECK(json_valid(owner_spec_ids_json)),
      owner_spec_provenance_json TEXT NOT NULL CHECK(json_valid(owner_spec_provenance_json)),
      parent_stream_id TEXT,
      objective TEXT,
      handoff_from_stream_id TEXT,
      created_by_stream_id TEXT,
      target_host TEXT NOT NULL,
      role TEXT,
      phase TEXT,
      visibility TEXT,
      requested_provider TEXT NOT NULL,
      requested_model TEXT NOT NULL,
      requested_effort TEXT NOT NULL,
      resolved_provider TEXT NOT NULL,
      resolved_model TEXT NOT NULL,
      resolved_effort TEXT NOT NULL,
      disposition_waived_reason TEXT,
      confirm_model_change INTEGER NOT NULL DEFAULT 0 CHECK(confirm_model_change IN (0,1)),
      fires_at_utc TEXT NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('pending','retry_pending','firing','fired','cancelled','failed','indeterminate','expired')),
      generation INTEGER NOT NULL DEFAULT 1 CHECK(generation >= 1),
      prompt_sha256 TEXT,
      prompt_b64 TEXT,
      prompt_blob_id TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      terminal_at TEXT,
      last_error_code TEXT,
      target_sha TEXT,
      attestation_json TEXT CHECK(attestation_json IS NULL OR json_valid(attestation_json)),
      reparent_children INTEGER CHECK(reparent_children IS NULL OR reparent_children IN (0,1)),
      self_close_on_completion INTEGER CHECK(self_close_on_completion IS NULL OR self_close_on_completion IN (0,1)),
      CHECK(owner_stream_id IS NOT NULL OR owner_service_actor IS NOT NULL),
      CHECK(prompt_b64 IS NULL OR prompt_blob_id IS NULL),
      CHECK(prompt_sha256 IS NOT NULL OR (prompt_b64 IS NULL AND prompt_blob_id IS NULL))
    )
    """,
    """CREATE INDEX IF NOT EXISTS ix_v2_schedule_due
       ON v2_schedules(state,fires_at_utc)""",
    """CREATE INDEX IF NOT EXISTS ix_v2_schedule_owner_state
       ON v2_schedules(owner_stream_id,state)""",
    """
    CREATE TABLE IF NOT EXISTS v2_schedule_dispatches (
      schedule_id TEXT NOT NULL REFERENCES v2_schedules(schedule_id),
      generation INTEGER NOT NULL CHECK(generation >= 1),
      spawn_key TEXT NOT NULL UNIQUE,
      phase TEXT NOT NULL CHECK(phase IN ('prepared','dispatch_claimed','dispatch_transmitted','spawn_delivered','failed','indeterminate')),
      spawn_request_id TEXT NOT NULL,
      prepared_at TEXT NOT NULL,
      claimed_at TEXT,
      transmitted_at TEXT,
      outcome_at TEXT,
      spawn_outcome_id TEXT,
      child_stream_id TEXT,
      prompt_delivery_status TEXT,
      error_code TEXT,
      evidence_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(evidence_json)),
      PRIMARY KEY(schedule_id,generation)
    )
    """,
    """CREATE INDEX IF NOT EXISTS ix_v2_dispatch_phase
       ON v2_schedule_dispatches(phase)""",
)

#: v1 `STREAM_TOKEN_HASH_VERSION`. grant_token persists only the hash.
STREAM_TOKEN_HASH_VERSION = "sha256:v1"

# v2-only. Durable spawn outcome so `await_spawn` answers from the store and is
# never indeterminate once the row exists (design § spawn / B9).
SPAWN_OUTCOME_DDL = """
CREATE TABLE IF NOT EXISTS v2_spawn_outcomes (
    host TEXT NOT NULL,
    session_name TEXT NOT NULL,
    request_id TEXT,
    state TEXT NOT NULL,
    reason TEXT,
    effective_model TEXT,
    effective_effort TEXT,
    delivery_evidence TEXT,
    delivery_receipt TEXT,
    updated_at REAL NOT NULL,
    idempotency_key TEXT,
    request_payload_hash TEXT,
    readiness_timed_out INTEGER,
    PRIMARY KEY (host, session_name)
)
"""

# Request-keyed, insert-only terminal records survive cleanup and name reuse.
# An additive table keeps old reservation/outcome rows and the v1 schema readable.
# It carries an extra `nonce` column (QA cycle-3 luna-[2]) so a recovery/identity
# path holding ONLY the tombstone can reconstruct the pane's creation identity
# across name reuse -- the reservation (the other nonce source) is deleted at
# cancel. Backfilled/legacy tombstones have a NULL nonce.
SPAWN_CANCELLATION_DDL = SPAWN_OUTCOME_DDL.replace(
    "v2_spawn_outcomes", "v2_spawn_cancellations"
).replace(
    "PRIMARY KEY (host, session_name)",
    "nonce TEXT,\n    PRIMARY KEY (host, session_name, request_id)",
)
# Explicit column list for the outcome->cancellation backfill: the cancellation
# table has one extra column (nonce), so `SELECT *` no longer aligns.
_SPAWN_OUTCOME_COLUMNS = (
    "host, session_name, request_id, state, reason, effective_model, effective_effort, "
    "delivery_evidence, delivery_receipt, updated_at, idempotency_key, request_payload_hash, "
    "readiness_timed_out"
)

# Daemon-side admission freeze: one row per host. A deploy window sets a TTL'd
# hold so new spawns are refused (`spawn_frozen`) while the daemon is being
# cut over; the deployer clears it in its finally. `held_until` is an absolute
# epoch so an abandoned hold self-expires instead of wedging admission forever.
SPAWN_ADMISSION_HOLD_DDL = """
CREATE TABLE IF NOT EXISTS v2_spawn_admission_hold (
    host TEXT PRIMARY KEY,
    held_until REAL NOT NULL,
    reason TEXT,
    set_at REAL NOT NULL
)
"""
# readiness_timed_out (spawn_outcomes coverage/semantics spec, Defect 2): 1 when
# `state='failed'` came from the bounded readiness poll timing out
# (`VerbError` code `boot_not_ready`) rather than a genuine boot/registration
# failure. A readiness timeout does NOT mean the seat is dead -- admission
# (the `sessions` row) always precedes this poll, so `readiness_timed_out=1`
# implies admitted-and-possibly-live, not admitted-and-dead. 0/NULL for every
# other `failed` reason. Consumers must branch on this column, not parse
# `reason` prose, to tell a readiness timeout apart from a real boot failure.
SPAWN_OUTCOME_REQUEST_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS v2_spawn_outcomes_request_id "
    "ON v2_spawn_outcomes (request_id, updated_at DESC)"
)
# Durable key->outcome index: a same-key retry after the first spawn already
# succeeded (and released its reservation) replays the recorded outcome instead
# of minting a duplicate pane (rpc_delivery_determinism lane AC1).
SPAWN_OUTCOME_IDEMPOTENCY_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS v2_spawn_outcomes_idempotency_key "
    "ON v2_spawn_outcomes (host, idempotency_key, updated_at DESC)"
)

# v2-only. THE durable report ledger (B9): `await_report` answers from this
# table and never from transport, so a report ingested while nobody is awaiting
# is still returned by a later await. Every validated payload field gets its own
# typed column — the v1 class where `findings` arrived NULL while `summary`
# survived came from persisting a subset and reconstructing the rest.
REPORTS_DDL = """
CREATE TABLE IF NOT EXISTS v2_reports (
    ledger_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT NOT NULL UNIQUE,
    from_stream_id TEXT NOT NULL,
    msg_id INTEGER NOT NULL DEFAULT 0,
    session_generation TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    summary TEXT,
    findings TEXT,
    next_action TEXT,
    details TEXT,
    extras TEXT,
    reason TEXT,
    completion_kind TEXT,
    qa_verdict TEXT,
    target_sha TEXT,
    qa_attestation TEXT,
    ac_claim TEXT,
    claim_verified TEXT,
    ac_claim_mismatch TEXT,
    agent_orch_attestation TEXT,
    qa_attestation_validation TEXT,
    to_stream_id TEXT,
    request_payload_hash TEXT,
    effective_model TEXT,
    effective_effort TEXT,
    provenance_version TEXT,
    provenance_generation TEXT,
    provenance_qualified_spec_ids TEXT,
    provenance_routing_integrity TEXT,
    provenance_requested_model TEXT,
    provenance_requested_effort TEXT,
    provenance_effective_model TEXT,
    provenance_effective_effort TEXT,
    ingested_at TEXT NOT NULL,
    created_at REAL NOT NULL
)
"""
REPORTS_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS v2_reports_by_stream ON v2_reports (from_stream_id, msg_id)"
)

# v2-only. An await request is durable before its in-memory future is parked.
# The row is shared by identical `(stream_id, msg_id)` requests and survives a
# daemon restart, so a confirmed-dead close can settle a re-issued await
# without relying on the old process's asyncio state. `-1` represents a
# stream-only await (`msg_id` omitted).
AWAITERS_DDL = """
CREATE TABLE IF NOT EXISTS v2_awaiters (
    awaiter_id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id TEXT NOT NULL,
    session_generation TEXT NOT NULL DEFAULT '',
    target_msg_id INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('pending', 'report', 'closed_without_report')),
    report_id TEXT,
    ledger_row_id INTEGER,
    reason TEXT,
    requested_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(stream_id, session_generation, target_msg_id)
)
"""
AWAITERS_PENDING_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS v2_awaiters_pending "
    "ON v2_awaiters (outcome, stream_id, session_generation, target_msg_id)"
)

# v2-only (pop2). A schema-rejected TERMINAL report from a
# `self_close_on_completion` seat never becomes a `v2_reports` row (the daemon
# raises before persisting), so a finished seat that botched its report payload
# left NO durable completion signal and the self-close backlog sweep — which
# requires a valid terminal report — could never reap it. This table is that
# missing trace: the reason plus the seat/generation, so the sweep can close the
# finished seat and the coordinator can read WHY the report was refused.
REPORT_REJECTIONS_DDL = """
CREATE TABLE IF NOT EXISTS v2_report_rejections (
    rejection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id TEXT NOT NULL,
    session_generation TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    reason TEXT,
    created_at REAL NOT NULL
)
"""
REPORT_REJECTIONS_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS v2_report_rejections_by_stream "
    "ON v2_report_rejections (stream_id, session_generation)"
)

REPORT_COLUMNS = (
    "report_id", "from_stream_id", "msg_id", "session_generation", "status", "summary", "findings",
    "next_action", "details", "extras", "reason", "completion_kind", "qa_verdict", "target_sha",
    "qa_attestation", "ac_claim", "claim_verified", "ac_claim_mismatch", "agent_orch_attestation",
    "qa_attestation_validation", "to_stream_id", "request_payload_hash",
    "effective_model", "effective_effort", "ingested_at",
    "provenance_version", "provenance_generation", "provenance_qualified_spec_ids",
    "provenance_routing_integrity", "provenance_requested_model", "provenance_requested_effort",
    "provenance_effective_model", "provenance_effective_effort",
)
REPORT_IDENTITY_COLUMNS = (
    "report_id", "from_stream_id", "msg_id", "status", "summary", "findings",
    "next_action", "details", "extras", "reason", "completion_kind", "qa_verdict",
    "target_sha", "qa_attestation", "agent_orch_attestation",
    "ac_claim",
)
#: Report columns holding JSON, decoded on read so a caller never sees a blob.
REPORT_JSON_COLUMNS = (
    "findings", "details", "extras", "qa_attestation", "ac_claim", "ac_claim_mismatch",
    "agent_orch_attestation", "qa_attestation_validation",
    "provenance_qualified_spec_ids",
)

QA_ATTESTATION_REASON_ORDER = (
    "missing_attestation",
    "self_reference",
    "unknown_qa_stream",
    "spec_binding_mismatch",
    "unknown_qa_report",
    "qa_report_stream_mismatch",
    "qa_report_not_done",
    "qa_verdict_not_accept",
    "qa_report_lower_trust",
)

_REPORT_PROVENANCE_VALUE_FIELDS = (
    "requested_model",
    "requested_effort",
    "effective_model",
    "effective_effort",
    "routing_integrity",
)


def _complete_report_provenance(
    snapshot: dict[str, Any] | None, fields: dict[str, Any],
) -> bool:
    """Accept only the complete snapshot derived for this exact report source."""
    if not isinstance(snapshot, dict):
        return False
    if snapshot.get("stream_id") != str(fields.get("from_stream_id") or ""):
        return False
    if not str(snapshot.get("generation") or ""):
        return False
    if not str(snapshot.get("session_generation") or ""):
        return False
    if not isinstance(snapshot.get("qualified_spec_ids"), list):
        return False
    return all(field in snapshot for field in _REPORT_PROVENANCE_VALUE_FIELDS)


class ReportProvenanceUnavailable(RuntimeError):
    """No complete source-backed provenance snapshot is available for a report."""

    def __init__(self, snapshot: dict[str, Any] | None) -> None:
        self.snapshot = snapshot
        super().__init__("report provenance is unavailable")


class ReportProvenanceChanged(RuntimeError):
    """The source session changed after its report provenance was observed."""

    def __init__(self, expected: dict[str, Any], current: dict[str, Any] | None) -> None:
        self.expected = expected
        self.current = current
        super().__init__("report provenance changed during write")


class ReportReplayConflict(RuntimeError):
    """A durable report id was reused with different request material."""

    def __init__(self, report_id: str) -> None:
        super().__init__("report_id_replay_conflict")
        self.report_id = report_id


class QAAttestationUnverified(RuntimeError):
    """An enforce-mode READY report failed durable attestation validation."""

    def __init__(self, validation: dict[str, Any]) -> None:
        super().__init__("qa_attestation_unverified")
        self.validation = validation


from store_watch_wake import _WatchWakeStoreMixin, WATCH_WAKE_DDL, install_default_conn, lifecycle_watch_conn, report_watch_conn


class Store(ExchangeStoreMixin, _RoutingStoreMixin, _SpecPersistenceMixin, _WatchWakeStoreMixin):
    """SQLite owned by exactly one worker thread; async callers use await."""

    def __init__(self, path: str = ":memory:", *, max_pending: int = 10_000) -> None:
        self._path = path
        self._spec_identity_resolver: Callable[[str | None], str | None] | None = None
        self.schedule_schema_health = "initializing"
        # Bounded (QA #8): an unbounded write queue is exactly the growth that
        # helped wedge v1 — a stalled store thread let callers pile work up
        # without limit. Past the cap `submit` fails fast rather than growing.
        self._queue: queue.Queue[tuple[Callable[[sqlite3.Connection], Any], asyncio.Future, asyncio.AbstractEventLoop] | None] = queue.Queue(maxsize=max_pending)
        self._thread: threading.Thread | None = None
        # Serializes the start/stop transition against `submit` so no callable is
        # ever enqueued after the stop sentinel — such a future would never run
        # (QA #8). Held only for the enqueue/transition, never across I/O.
        self._lock = threading.Lock()
        self._closing = False
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._columns: set[str] = set()
        self._init_routing_integrity_state()

    @property
    def path(self) -> str:
        """The DB file this store owns. `:memory:` for the ephemeral default."""
        return self._path

    def pending(self) -> int:
        """Callables queued but not yet run. Read from the store thread by the
        retention job, which skips its VACUUM whenever anything is waiting: the
        thread is single-threaded, so a VACUUM is a hard stall for every queued
        request, not merely slow I/O."""
        return self._queue.qsize()

    # -- lifecycle ---------------------------------------------------------

    def set_spec_identity_resolver(
        self, resolver: Callable[[str | None], str | None] | None,
    ) -> None:
        """Configure resolver-proven identity lookup for attestation reads."""
        self._spec_identity_resolver = resolver

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="store", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=10)
        if self._start_error is not None:
            raise self._start_error

    def stop(self) -> None:
        with self._lock:
            if self._thread is None:
                return
            # From here `submit` refuses, so nothing lands after the sentinel.
            self._closing = True
            thread = self._thread
        self._queue.put(None)
        thread.join(timeout=5)
        with self._lock:
            self._thread = None
            self._closing = False

    def _fail_pending(self, why: str) -> None:
        """Resolve every still-queued callable with an error so no `submit`
        awaiter hangs after the worker exits. Runs on the worker thread as it
        tears down; the futures are completed on their own event loops."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            _fn, fut, loop = item
            loop.call_soon_threadsafe(_set_exception, fut, RuntimeError(why))

    # -- worker thread -----------------------------------------------------

    def _run(self) -> None:
        try:
            conn = sqlite3.connect(self._path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("BEGIN")
            conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
            conn.execute(SESSIONS_DDL)
            if "no_watch" not in {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}:
                conn.execute("ALTER TABLE sessions ADD COLUMN no_watch INTEGER NOT NULL DEFAULT 0")
            for ddl in WATCH_WAKE_DDL:
                conn.execute(ddl)
            conn.execute(RESERVATIONS_DDL)
            conn.execute(SPAWN_OUTCOME_DDL)
            conn.execute(SPAWN_CANCELLATION_DDL)
            # Additive migration (QA cycle-3 luna-[2]): an older table created
            # before the nonce column gains it here; a fresh DDL already has it.
            if "nonce" not in {
                r[1] for r in conn.execute("PRAGMA table_info(v2_spawn_cancellations)")
            }:
                conn.execute("ALTER TABLE v2_spawn_cancellations ADD COLUMN nonce TEXT")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS v2_spawn_cancellations_identity "
                "ON v2_spawn_cancellations (host, session_name, COALESCE(request_id, ''))"
            )
            conn.execute(
                f"INSERT OR IGNORE INTO v2_spawn_cancellations ({_SPAWN_OUTCOME_COLUMNS}, nonce) "
                f"SELECT {_SPAWN_OUTCOME_COLUMNS}, NULL FROM v2_spawn_outcomes "
                "WHERE state='cancelled'"
            )
            conn.execute(SPAWN_ADMISSION_HOLD_DDL)
            conn.execute(SPAWN_OUTCOME_REQUEST_INDEX_DDL)
            conn.execute(TELL_DELIVERY_DDL)
            conn.execute(SESSION_ROLE_DDL)
            conn.execute(NUDGE_STATE_DDL)
            conn.execute(SESSION_REAP_DDL)
            conn.execute(SESSION_REAP_INDEX_DDL)
            conn.execute(SESSION_GENERATIONS_DDL)
            conn.execute(ROUTING_INTEGRITY_DDL)
            conn.execute(ROUTING_INTEGRITY_AUDIT_DDL)
            conn.execute(OUTBOUND_NOTICE_DDL)
            conn.execute(OUTBOUND_NOTICE_INDEX_DDL)
            conn.execute(REPORTS_DDL)
            conn.execute(REPORTS_INDEX_DDL)
            conn.execute(AWAITERS_DDL)
            conn.execute(AWAITERS_PENDING_INDEX_DDL)
            conn.execute(REPORT_REJECTIONS_DDL)
            conn.execute(REPORT_REJECTIONS_INDEX_DDL)
            conn.execute(SESSION_EVENT_TAIL_DDL)
            conn.execute(SESSION_EVENT_TAIL_INDEX_DDL)
            conn.execute(SESSION_EVENT_TAIL_IDENTITY_DDL)
            conn.execute(SESSION_EVENT_TAIL_RECORDED_DDL)
            conn.execute(SEND_RECEIPT_DDL)
            conn.execute(CLOSE_AUDIT_DDL)
            conn.execute(CLOSE_AUDIT_INDEX_DDL)
            for ddl in SCHEDULE_DDL:
                conn.execute(ddl)
            if "no_watch" not in {r[1] for r in conn.execute("PRAGMA table_info(v2_schedules)")}:
                conn.execute("ALTER TABLE v2_schedules ADD COLUMN no_watch INTEGER NOT NULL DEFAULT 0")
            conn.execute(SPAWN_OUTCOME_IDEMPOTENCY_INDEX_DDL)
            conn.execute(STREAM_RESERVATION_IDEMPOTENCY_INDEX_DDL)
            if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            for table in ("sessions", "v2_schedules"):
                if "objective" not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN objective TEXT")
            store_exchange.initialize(conn)
            if "objective_source" not in {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}:
                conn.execute("ALTER TABLE sessions ADD COLUMN objective_source TEXT")
                conn.execute("UPDATE sessions SET objective_source='explicit' WHERE objective IS NOT NULL")
            if "exchange_json" not in {r[1] for r in conn.execute("PRAGMA table_info(v2_reports)")}:
                conn.execute("ALTER TABLE v2_reports ADD COLUMN exchange_json TEXT")
            if "observer_binding" not in {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}:
                conn.execute("ALTER TABLE sessions ADD COLUMN observer_binding TEXT")
            store_exchange.repair(conn)
            self.schedule_schema_health = "ok"
            conn.commit()
            self._columns = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        except BaseException as exc:  # pragma: no cover - startup failure path
            self._start_error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                fn, fut, loop = item
                try:
                    result = fn(conn)
                    loop.call_soon_threadsafe(_set_result, fut, result)
                except BaseException as exc:
                    loop.call_soon_threadsafe(_set_exception, fut, exc)
        finally:
            conn.close()
            # Belt-and-suspenders (QA #8): resolve anything still queued so no
            # awaiter hangs forever. With the stop lock nothing should land here
            # after the sentinel, but a future that never runs must never be
            # left pending.
            self._fail_pending("store stopped before this callable ran")

    # -- async API ---------------------------------------------------------

    async def submit(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        """Run `fn(conn)` on the store thread; await its result."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        with self._lock:
            if self._thread is None or self._closing:
                # Nothing will ever drain this future, so it would hang for the
                # life of the process (QA #8): fail loudly instead. The lock
                # makes the check-and-enqueue atomic against `stop`, so a
                # callable can never be queued behind the stop sentinel.
                raise RuntimeError("store is not running")
            try:
                # Never block the event loop: past the cap fail fast rather than
                # wait for the worker to drain (QA #8).
                self._queue.put_nowait((fn, fut, loop))
            except queue.Full as exc:
                raise RuntimeError("store overloaded: pending write queue is full") from exc
        return await fut

    async def put(self, key: str, value: str) -> None:
        def _op(conn: sqlite3.Connection) -> None:
            conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (key, value))
            conn.commit()

        await self.submit(_op)

    async def get(self, key: str) -> str | None:
        def _op(conn: sqlite3.Connection) -> str | None:
            row = conn.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
            return None if row is None else row[0]

        return await self.submit(_op)

    # -- event.push target pin ------------------------------------------------

    @staticmethod
    def _require_full_git_sha(value: object, *, label: str) -> str:
        if not isinstance(value, str) or _FULL_GIT_SHA.fullmatch(value) is None:
            raise ValueError(f"{label} must be an exact 40-character hexadecimal git SHA")
        return value

    async def stage_event_push_target_sha(self, target_sha: str) -> str | None:
        """Durably record the current pin, stage ``target_sha``, then read it back.

        This is the sole manual deploy-window writer for ``event_push.target_sha``.
        It deliberately has no deploy/restart behavior: the Nexus-serialized window
        owns invocation and rollback timing.  An empty/missing old pin is recorded
        as an empty string, which preserves the reader's existing no-pin semantics.
        """
        target = self._require_full_git_sha(target_sha, label="target_sha")
        previous = await self.get(EVENT_PUSH_TARGET_SHA_KEY)
        if previous:
            self._require_full_git_sha(previous, label="existing event.push target")

        # Persist this before changing the live key.  Calls share this Store's
        # serialized queue; the external deploy window supplies cross-process
        # serialization with the daemon's single store owner.
        await self.put(EVENT_PUSH_TARGET_SHA_PREVIOUS_KEY, previous or "")
        await self.put(EVENT_PUSH_TARGET_SHA_KEY, target)
        readback = await self.get(EVENT_PUSH_TARGET_SHA_KEY)
        if readback != target:
            raise RuntimeError(
                "event.push target pin readback mismatch after stage: "
                f"expected {target}, got {readback!r}"
            )
        return previous

    async def rollback_event_push_target_sha(self) -> str | None:
        """Restore the preceding pin captured by :meth:`stage_event_push_target_sha`."""
        previous = await self.get(EVENT_PUSH_TARGET_SHA_PREVIOUS_KEY)
        if previous is None:
            raise RuntimeError("no captured previous event.push target pin to restore")
        if previous:
            self._require_full_git_sha(previous, label="captured previous event.push target")

        await self.put(EVENT_PUSH_TARGET_SHA_KEY, previous)
        readback = await self.get(EVENT_PUSH_TARGET_SHA_KEY)
        if readback != previous:
            raise RuntimeError(
                "event.push target pin readback mismatch after rollback: "
                f"expected {previous!r}, got {readback!r}"
            )
        return previous or None

    # -- sessions table ----------------------------------------------------

    async def open_session(
        self, host: str, session_name: str, *, spawn_request_id: str | None = None, **fields: Any,
    ) -> dict[str, Any] | None:
        """Insert (or re-open) a session row. Returns the stored row."""
        fields = _normalize_session_fields(fields)
        generation = str(fields.pop("session_generation", "") or uuid.uuid4().hex)
        cols = self._known(fields)
        cols.setdefault("visibility", "default")
        cols.setdefault("created_at", iso_now())
        cols["status"] = "open"
        cols["closed_at"] = None

        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            if spawn_request_id is not None:
                reservation = _spawn_reservation(conn, host, session_name, spawn_request_id)
                if reservation is None:
                    return None
                intent = json.loads(reservation.get("payload") or "{}")
                expected = (intent.get("open_fields") or {}).get("session_generation")
                if expected and expected != generation:
                    return None
                observer = cols.get("observer_binding")
                if isinstance(observer, dict):
                    cols["observer_binding"] = {
                        "executable": observer.get("executable", ""),
                        "pane_pid": str(reservation.get("pane_pid") or ""),
                        "pane_started_at": str(reservation.get("pane_started_at") or ""),
                        "generation": generation,
                    }
            stream_id = f"{host}:{session_name}"
            existing = conn.execute(
                "SELECT status, jsonl_path FROM sessions WHERE host=? AND session_name=?",
                (host, session_name),
            ).fetchone()
            prior_generation = conn.execute(
                "SELECT generation FROM v2_session_generations WHERE host=? AND session_name=?",
                (host, session_name),
            ).fetchone()
            generation_changed = (
                prior_generation is None
                or str(prior_generation[0] or "") != generation
            )
            if not generation_changed and {"objective", "objective_source"}.intersection(cols):
                prior_objective = conn.execute("SELECT objective,objective_source FROM sessions WHERE host=? AND session_name=?", (host, session_name)).fetchone()
                if prior_objective and any(key in cols and prior_objective[index] != cols[key]
                                           for index, key in enumerate(("objective", "objective_source"))):
                    raise ValueError("objective and objective_source are immutable within a session generation")
            if generation_changed:
                observer = cols.get("observer_binding")
                if isinstance(observer, dict):
                    cols["observer_binding"] = {
                        key: observer.get(key, "")
                        for key in ("executable", "pane_pid", "pane_started_at")
                    } | {"generation": generation}
                    cols["observer_binding"]["pane_pid"] = str(
                        cols["observer_binding"].get("pane_pid") or cols.get("pane_pid") or "")
                else:
                    cols["observer_binding"] = None
            reopened = existing is not None and str(existing[0] or "") == "closed"
            if reopened:
                # A host/session name is reusable, but a routing quarantine is
                # bound to the prior lifecycle. Retire its active delivery state
                # before the caller seeds the new row.
                stamp = _routing_iso_now()
                conn.execute(
                    """UPDATE v2_outbound_notices
                       SET terminal_at=?, last_error=?
                       WHERE source_stream_id=? AND kind='routing_integrity'
                         AND terminal_at IS NULL""",
                    (stamp, "session_reopened", stream_id),
                )
                conn.execute(
                    "DELETE FROM v2_routing_integrity WHERE stream_id=?",
                    (stream_id,),
                )
                # Post-hoc role provenance is bound to the prior lifecycle too:
                # drop it so a reused name reopened as a fresh spawn derives its
                # role_source (spawn/handoff) instead of inheriting a stale
                # `role_set` marker from the closed generation.
                conn.execute(
                    "DELETE FROM v2_session_role WHERE host=? AND session_name=?",
                    (host, session_name),
                )
                # `created_at` is v2's existing durable lifecycle generation.
                # Always mint it on a closed-name reopen, even if a stale row
                # was forwarded as input, so late observations from the prior
                # worker cannot pass the generation CAS.
                cols["created_at"] = stamp
                reopen_defaults = {
                    "objective": None,
                    "objective_source": None,
                    "parent_stream_id": None,
                    "role": None,
                    "phase": None,
                    "handoff_from_stream_id": None,
                    "spec_id": None,
                    "spec_resolution": None,
                    "offline_since_ts": None,
                    "self_close_on_completion": 0,
                    "no_watch": 0,
                    "token_hash": None,
                    "token_hash_version": None,
                    "harness_run_id": None,
                    "opened_by_host_id": None,
                    "jsonl_path": None,
                    "observer_binding": None,
                    "claude_session_id": None,
                    "claude_session_lineage": None,
                    "spec_ids": None,
                    "presumed_dead_at": None,
                    "status_card": None,
                    "context_tokens": None,
                    "model_context_window": None,
                    "context_updated_at": None,
                    "context_level": None,
                    "pane_pid": None,
                    "pane_status": "pane_unknown",
                    "requested_model": None,
                    "requested_effort": None,
                    "effective_model": None,
                    "effective_effort": None,
                    "routing_integrity": None,
                    "routing_integrity_reason": None,
                    "routing_integrity_updated_at": None,
                    "provider": None,
                    "presumed_dead_fired_at": None,
                    "qualified_spec_ids": None,
                    "spec_binding_provenance": None,
                    "title": None,
                    "dead_open_closed_at": None,
                    # A fresh generation has not closed; never inherit a close class.
                    "close_kind": None,
                    "bootstrap_state": None,
                }
                for name, default in reopen_defaults.items():
                    if name not in cols:
                        cols[name] = default
                # These read-model fields are daemon-owned and must never be
                # accepted from a caller forwarding a closed row. A new spawn
                # seeds effective_* explicitly; jsonl_path is intentionally
                # preserved when the new lifecycle supplies one.
                cols["routing_integrity"] = None
                cols["routing_integrity_reason"] = None
                cols["routing_integrity_updated_at"] = None
            names = ["host", "session_name", *cols]
            values = [host, session_name, *(_enc(k, v) for k, v in cols.items())]
            updates = ", ".join(f"{k}=excluded.{k}" for k in cols)
            conn.execute(
                f"INSERT INTO sessions ({','.join(names)}) VALUES ({','.join('?' * len(names))}) "
                f"ON CONFLICT(host, session_name) DO UPDATE SET {updates}",
                values,
            )
            conn.execute(
                "INSERT INTO v2_session_generations (host, session_name, generation) "
                "VALUES (?,?,?) ON CONFLICT(host, session_name) DO UPDATE SET generation=excluded.generation",
                (host, session_name, generation),
            )
            if spawn_request_id is not None:
                reservation = _spawn_reservation(conn, host, session_name, spawn_request_id)
                intent = json.loads(reservation.get("payload") or "{}")
                if "exchange_binding" not in intent:
                    intent["exchange_binding"] = store_exchange.direct_pair(conn, cols.get("parent_stream_id"), stream_id)
                    conn.execute("UPDATE v2_stream_reservations SET payload=? WHERE host=? AND session_name=?", (json.dumps(intent), host, session_name))
            if generation_changed:
                # A new lifecycle must not inherit either kind's cooldown or
                # stale basis from the predecessor.  This delete is inside the
                # same store transaction as the open + generation update, so a
                # crash cannot expose a successor with predecessor nudge state.
                conn.execute(
                    "DELETE FROM v2_nudge_state WHERE stream_id=?",
                    (stream_id,),
                )
            try:
                lifecycle_watch_conn(conn, time.time())
                install_default_conn(conn, f"{host}:{session_name}", time.time())
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
            return _session_row(conn, conn.execute(
                "SELECT * FROM sessions WHERE host=? AND session_name=?", (host, session_name)
            ).fetchone())

        lifecycle_lock = self.routing_integrity_lifecycle_lock(f"{host}:{session_name}")
        async with lifecycle_lock:
            return await self.submit(_op)

    async def update_session(
        self,
        host: str,
        session_name: str,
        *,
        expected_generation: str | None = None,
        **fields: Any,
    ) -> dict[str, Any] | None:
        """Patch named columns on one row. Returns the row, or None if absent."""
        if {"objective", "objective_source"}.intersection(fields):
            raise ValueError("objective and objective_source are immutable within a session generation")
        fields = _normalize_session_fields(fields)
        cols = self._known(fields)
        if not cols:
            row = await self.fetch_session(host, session_name)
            if (
                expected_generation is not None
                and isinstance(row, dict)
                and str(row.get("created_at") or "") != expected_generation
            ):
                return None
            return row
        closing = str(cols.get("status") or "") == "closed"

        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            if expected_generation is not None:
                current = conn.execute(
                    "SELECT created_at FROM sessions WHERE host=? AND session_name=?",
                    (host, session_name),
                ).fetchone()
                if current is None or str(current[0] or "") != expected_generation:
                    conn.commit()
                    return None
            assign = ", ".join(f"{k}=?" for k in cols)
            cur = conn.execute(
                f"UPDATE sessions SET {assign} WHERE host=? AND session_name=?",
                [*(_enc(k, v) for k, v in cols.items()), host, session_name],
            )
            if cur.rowcount == 0:
                conn.commit()
                return None
            if closing:
                stream_id = f"{host}:{session_name}"
                stamp = _routing_iso_now()
                # Closing is a lifecycle boundary. Retire active routing
                # delivery/quarantine state before a later reopen seeds a fresh row.
                conn.execute(
                    """UPDATE v2_outbound_notices
                       SET terminal_at=?, last_error=?
                       WHERE source_stream_id=? AND kind='routing_integrity'
                         AND terminal_at IS NULL""",
                    (stamp, "session_closed", stream_id),
                )
                conn.execute(
                    "DELETE FROM v2_routing_integrity WHERE stream_id=?",
                    (stream_id,),
                )
            try:
                lifecycle_watch_conn(conn, time.time())
                install_default_conn(conn, f"{host}:{session_name}", time.time())
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
            return _session_row(conn, conn.execute(
                "SELECT * FROM sessions WHERE host=? AND session_name=?", (host, session_name)
            ).fetchone())

        if closing or "parent_stream_id" in cols:
            stream_id = f"{host}:{session_name}"
            lifecycle_lock = self.routing_integrity_lifecycle_lock(stream_id)
            try:
                async with lifecycle_lock:
                    result = await self.submit(_op)
            finally:
                self._retire_routing_integrity_lifecycle_lock(stream_id)
            return result
        return await self.submit(_op)

    async def set_session_role_source(
        self, host: str, session_name: str, *,
        role_source: str, actor: str | None, changed_at: str | None,
    ) -> None:
        """Upsert the post-hoc role provenance for one seat (`role set`)."""
        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO v2_session_role "
                "(host, session_name, role_source, actor, changed_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(host, session_name) DO UPDATE SET "
                "role_source=excluded.role_source, actor=excluded.actor, "
                "changed_at=excluded.changed_at",
                (host, session_name, role_source, actor, changed_at),
            )
            conn.commit()
        await self.submit(_op)

    async def fetch_role_source(self, host: str, session_name: str) -> dict[str, Any] | None:
        """The stored role provenance for one seat, or None if never set post hoc."""
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT role_source, actor, changed_at FROM v2_session_role "
                "WHERE host=? AND session_name=?",
                (host, session_name),
            ).fetchone()
            return dict(row) if row is not None else None
        return await self.submit(_op)

    async def all_role_sources(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Every stored role provenance keyed by (host, session_name) for the
        list projection — one bulk read, never a per-row round trip."""
        def _op(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
            return {
                (str(r["host"]), str(r["session_name"])): {
                    "role_source": r["role_source"],
                    "actor": r["actor"],
                    "changed_at": r["changed_at"],
                }
                for r in conn.execute(
                    "SELECT host, session_name, role_source, actor, changed_at "
                    "FROM v2_session_role"
                )
            }
        return await self.submit(_op)

    async def mark_closed(
        self,
        host: str,
        session_name: str,
        *,
        closed_at: str,
        pane_status: str,
        expected_generation: str | None = None,
        close_kind: str | None = None,
        reason: str | None = None,
        attribution: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Close one open generation with a durable compare-and-set.

        A same-name reopen replaces the generation token while the old close
        operation may still be awaiting tmux. The token predicate makes that
        stale close a no-op instead of closing the replacement row.

        `close_kind` (spawn-admission rollback-truth spec, AC5) records the
        close origin (e.g. ``spawn_rollback`` vs a live/self/operator close) so
        a failed-boot rollback is queryably distinct from a live-session death.

        The CAS's ``status='open'`` predicate already refuses to re-close a row
        that is already closed. AC1: that drop is now AUDIBLE — a stale
        spawn-admission retry landing on an already-closed row emits one WARN
        naming the stream, generation, and dropped reason instead of returning
        None silently (the silent-drop was indistinguishable from a benign
        generation-mismatch no-op).
        """
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            set_cols = "status='closed', closed_at=?, pane_status=?"
            params: list[Any] = [closed_at, pane_status]
            if close_kind is not None:
                set_cols += ", close_kind=?"
                params.append(close_kind)
            params += [host, session_name]
            predicate = "host=? AND session_name=? AND status='open'"
            if expected_generation:
                predicate += " AND EXISTS ("
                predicate += (
                    "SELECT 1 FROM v2_session_generations g "
                    "WHERE g.host=sessions.host AND g.session_name=sessions.session_name "
                    "AND g.generation=?"
                )
                predicate += ")"
                params.append(expected_generation)
            cur = conn.execute(
                f"UPDATE sessions SET {set_cols} WHERE {predicate}",
                params,
            )
            if cur.rowcount == 0:
                # AC1: distinguish an already-closed drop (a stale/duplicate
                # spawn-admission retry trying to falsify a legitimate close)
                # from a benign generation-mismatch or absent-row no-op, and
                # make it audible so the falsification attempt is nameable in
                # the daemon log even though the CAS correctly refused it.
                existing = conn.execute(
                    "SELECT status, closed_at FROM sessions WHERE host=? AND session_name=?",
                    (host, session_name),
                ).fetchone()
                conn.commit()
                if existing is not None and str(existing[0] or "") == "closed":
                    log.warning(
                        "mark_closed dropped: row already closed "
                        "stream=%s generation=%s dropped_kind=%s dropped_reason=%s "
                        "existing_closed_at=%s (stale spawn-admission retry refused)",
                        f"{host}:{session_name}",
                        expected_generation or "(current)",
                        close_kind or "(none)",
                        reason or "(none)",
                        existing[1],
                    )
                return None
            stream_id = f"{host}:{session_name}"
            stamp = _routing_iso_now()
            conn.execute(
                """UPDATE v2_outbound_notices
                   SET terminal_at=?, last_error=?
                   WHERE source_stream_id=? AND kind='routing_integrity'
                     AND terminal_at IS NULL""",
                (stamp, "session_closed", stream_id),
            )
            conn.execute("DELETE FROM v2_routing_integrity WHERE stream_id=?", (stream_id,))
            # Attribute the close in the SAME transaction as the CAS UPDATE, so a
            # closed row can never exist without its audit row.
            _insert_close_audit(conn, _close_audit_payload(
                host=host, session_name=session_name, close_kind=close_kind,
                disposition="closed", attribution=attribution, reason=reason,
            ))
            with conn:
                lifecycle_watch_conn(conn, time.time())
            conn.commit()
            return _session_row(conn, conn.execute(
                "SELECT * FROM sessions WHERE host=? AND session_name=?", (host, session_name)
            ).fetchone())

        stream_id = f"{host}:{session_name}"
        lifecycle_lock = self.routing_integrity_lifecycle_lock(stream_id)
        try:
            async with lifecycle_lock:
                return await self.submit(_op)
        finally:
            self._retire_routing_integrity_lifecycle_lock(stream_id)

    async def mark_reconciled_dead(
        self,
        host: str,
        session_name: str,
        *,
        expected_generation: str,
        presumed_dead_at: str,
        closed_at: str,
    ) -> dict[str, Any] | None:
        """CAS the reconciler's death transition to one unreserved row generation."""
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            cur = conn.execute(
                "UPDATE sessions SET status='closed', closed_at=?, pane_status='pane_dead', "
                "presumed_dead_at=?, presumed_dead_fired_at=?, dead_open_closed_at=? "
                "WHERE host=? AND session_name=? AND status='open' AND EXISTS ("
                "SELECT 1 FROM v2_session_generations g "
                "WHERE g.host=sessions.host AND g.session_name=sessions.session_name "
                "AND g.generation=?) AND NOT EXISTS ("
                "SELECT 1 FROM v2_stream_reservations r "
                "WHERE r.host=sessions.host AND r.session_name=sessions.session_name "
                "AND r.expires_at >= ?)",
                (
                    closed_at, presumed_dead_at, closed_at, closed_at,
                    host, session_name, expected_generation, time.time(),
                ),
            )
            if cur.rowcount == 0:
                conn.commit()
                return None
            stream_id = f"{host}:{session_name}"
            stamp = _routing_iso_now()
            conn.execute(
                """UPDATE v2_outbound_notices
                   SET terminal_at=?, last_error=?
                   WHERE source_stream_id=? AND kind='routing_integrity'
                     AND terminal_at IS NULL""",
                (stamp, "session_closed", stream_id),
            )
            conn.execute("DELETE FROM v2_routing_integrity WHERE stream_id=?", (stream_id,))
            # The presumed-dead reconciler transition bypasses mark_closed but is
            # still an affirmative close — attribute it in the same transaction.
            _insert_close_audit(conn, _close_audit_payload(
                host=host, session_name=session_name, close_kind="reconciler_dead",
                disposition="presumed_dead", attribution=None,
                reason="reconciler_confirmed_dead",
            ))
            with conn:
                lifecycle_watch_conn(conn, time.time())
            conn.commit()
            return _session_row(conn, conn.execute(
                "SELECT * FROM sessions WHERE host=? AND session_name=?", (host, session_name)
            ).fetchone())

        stream_id = f"{host}:{session_name}"
        lifecycle_lock = self.routing_integrity_lifecycle_lock(stream_id)
        try:
            async with lifecycle_lock:
                return await self.submit(_op)
        finally:
            self._retire_routing_integrity_lifecycle_lock(stream_id)

    async def record_close_audit(
        self,
        *,
        host: str,
        session_name: str,
        close_kind: str | None,
        disposition: str,
        attribution: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Append one close-attribution row for a disposition that does NOT reach
        mark_closed (``deferred``/``fenced`` refusals). ``closed`` and
        ``presumed_dead`` are written in their own close transactions instead."""
        payload = _close_audit_payload(
            host=host, session_name=session_name, close_kind=close_kind,
            disposition=disposition, attribution=attribution, reason=reason,
        )

        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            _insert_close_audit(conn, payload)
            conn.commit()
            return payload

        return await self.submit(_op)

    async def latest_close_audit(self, stream_id: str) -> dict[str, Any] | None:
        """The most recent close-attribution row for a stream, or None."""
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                f"SELECT {_CLOSE_AUDIT_COLUMNS} FROM v2_close_audit "
                "WHERE stream_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (stream_id,),
            ).fetchone()
            if row is None:
                return None
            keys = [c.strip() for c in _CLOSE_AUDIT_COLUMNS.split(",")]
            return dict(zip(keys, row))

        return await self.submit(_op)

    async def fetch_session(self, host: str, session_name: str) -> dict[str, Any] | None:
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            return _session_row(conn, conn.execute(
                "SELECT * FROM sessions WHERE host=? AND session_name=?", (host, session_name)
            ).fetchone())

        return await self.submit(_op)

    async def list_sessions(self, status: str | None = "open") -> list[dict[str, Any]]:
        """Rows by status. The hot path never calls this — `sessions.Registry`
        keeps an in-memory inventory and refreshes from here (design: the
        `list_sessions` verb is UI polling and must be O(open))."""

        def _op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            if status is None:
                rows = conn.execute("SELECT * FROM sessions").fetchall()
            else:
                rows = conn.execute("SELECT * FROM sessions WHERE status=?", (status,)).fetchall()
            return [_session_row(conn, r) for r in rows]

        return await self.submit(_op)

    # -- durable close/reap state -----------------------------------------

    async def upsert_session_reap(
        self,
        stream_id: str,
        *,
        reap_status: str,
        survivors: list[dict[str, Any]] | None = None,
        attempts: int = 0,
        next_attempt_at: str | None = None,
        exhausted_at: str | None = None,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        if reap_status not in {"reaped", "survivors", "unknown"}:
            raise ValueError("invalid session reap status")
        survivor_json = json.dumps(_sanitize_session_reap_survivors(survivors), separators=(",", ":"))
        timestamp = updated_at or iso_now()

        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute(
                "INSERT INTO session_reap "
                "(stream_id,reap_status,survivors,attempts,next_attempt_at,exhausted_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(stream_id) DO UPDATE SET "
                "reap_status=excluded.reap_status,survivors=excluded.survivors,"
                "attempts=excluded.attempts,next_attempt_at=excluded.next_attempt_at,"
                "exhausted_at=excluded.exhausted_at,updated_at=excluded.updated_at",
                (stream_id, reap_status, survivor_json, int(attempts), next_attempt_at, exhausted_at, timestamp),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM session_reap WHERE stream_id=?", (stream_id,)).fetchone()
            return _session_reap_row(row)

        return await self.submit(_op)

    async def get_session_reap(self, stream_id: str) -> dict[str, Any] | None:
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            return _session_reap_row(
                conn.execute("SELECT * FROM session_reap WHERE stream_id=?", (stream_id,)).fetchone()
            )

        return await self.submit(_op)

    async def list_session_reap_unreaped(
        self, *, lookback_h: int = 48, now: str | None = None
    ) -> list[dict[str, Any]]:
        """Return pending survivor rows in the bounded closed-row lookback."""
        now_value = now or iso_now()
        try:
            parsed = datetime.fromisoformat(now_value.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.now(timezone.utc)
        cutoff = (parsed - timedelta(hours=int(lookback_h))).isoformat().replace("+00:00", "Z")

        def _op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT r.* FROM session_reap r "
                "JOIN sessions s ON s.host || ':' || s.session_name = r.stream_id "
                "WHERE r.reap_status != 'reaped' AND s.status = 'closed' "
                "AND s.closed_at IS NOT NULL AND s.closed_at >= ? "
                "AND (r.next_attempt_at IS NULL OR r.next_attempt_at <= ?) "
                "ORDER BY r.updated_at",
                (cutoff, now_value),
            ).fetchall()
            return [_session_reap_row(row) for row in rows]

        return await self.submit(_op)

    async def list_session_reap_all(self) -> dict[str, dict[str, Any]]:
        """Read the child table in one trip for the read-only status surface."""
        def _op(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
            rows = conn.execute("SELECT * FROM session_reap").fetchall()
            return {
                str(item["stream_id"]): item
                for item in (_session_reap_row(row) for row in rows)
                if item is not None
            }

        return await self.submit(_op)

    async def count_closed_tree_alive(self, *, lookback_h: int = 48, now: str | None = None) -> int:
        now_value = now or iso_now()
        try:
            parsed = datetime.fromisoformat(now_value.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.now(timezone.utc)
        cutoff = (parsed - timedelta(hours=int(lookback_h))).isoformat().replace("+00:00", "Z")

        def _op(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM sessions s "
                "LEFT JOIN session_reap r ON r.stream_id=s.host || ':' || s.session_name "
                "WHERE s.status='closed' AND s.closed_at IS NOT NULL AND s.closed_at >= ? "
                "AND (s.pane_status='pane_alive' OR (r.reap_status IS NOT NULL AND r.reap_status != 'reaped'))",
                (cutoff,),
            ).fetchone()
            return int(row["n"] if row else 0)

        return await self.submit(_op)

    async def find_successor(self, stream_id: str) -> str | None:
        """The session handed off FROM `stream_id`, if any — the forward link for
        handoff-lineage tell routing.

        A live successor is preferred (a direct hop wins), but a CLOSED successor
        is still returned so `resolve_route_target` can follow the chain THROUGH
        an already-retired intermediate to the live tail — v1 progeny-follow
        parity, pinned by the lifted route-reliability regression. The caller's
        loop guard bounds depth and breaks cycles."""

        def _op(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT host, session_name FROM sessions WHERE handoff_from_stream_id=?"
                " ORDER BY (status='open') DESC, created_at DESC LIMIT 1",
                (stream_id,),
            ).fetchone()
            return None if row is None else f"{row['host']}:{row['session_name']}"

        return await self.submit(_op)

    # -- nudge cooldown (dead-nudge-spam class, D2) ------------------------

    async def nudge_states(self, keep_stream_ids: set[str] | None = None) -> dict[tuple[str, str], dict[str, Any]]:
        """Every recorded nudge episode, keyed `(stream_id, kind)`. When
        `keep_stream_ids` is given, rows for any stream not in that set are first
        deleted — the cooldown table follows the open-session set and never
        accumulates closed rows (D2 hygiene). One store-thread trip per pass."""

        def _op(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
            if keep_stream_ids is not None:
                live = {str(s) for s in keep_stream_ids}
                stale = [
                    r[0] for r in conn.execute("SELECT DISTINCT stream_id FROM v2_nudge_state").fetchall()
                    if r[0] not in live
                ]
                if stale:
                    conn.executemany("DELETE FROM v2_nudge_state WHERE stream_id=?", [(s,) for s in stale])
                    conn.commit()
            out: dict[tuple[str, str], dict[str, Any]] = {}
            for r in conn.execute("SELECT stream_id, kind, last_nudged_at, basis FROM v2_nudge_state").fetchall():
                out[(r["stream_id"], r["kind"])] = dict(r)
            return out

        return await self.submit(_op)

    async def record_nudge(self, stream_id: str, kind: str, at: float, basis: str = "") -> None:
        """Stamp `(stream_id, kind)` with the time it was last nudged. Written on
        every attempt — success OR failure — so a flapping pane that never echoes
        the receipt is still paced by the cooldown rather than nudged every pass."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO v2_nudge_state (stream_id, kind, last_nudged_at, basis) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(stream_id, kind) DO UPDATE SET last_nudged_at=excluded.last_nudged_at, "
                "basis=excluded.basis",
                (stream_id, kind, float(at), basis),
            )
            conn.commit()

        await self.submit(_op)

    async def record_nudges(self, records: list[tuple[str, str, float, str]]) -> None:
        """Record multiple kinds in one transaction.

        Combined title/card tells use this boundary so success and failure both
        leave an all-or-neither cooldown episode.  Explicit rollback matters
        when a later row fails: SQLite otherwise leaves the earlier write in
        the connection's open transaction for a future callable to commit.
        """
        if not records:
            return

        def _op(conn: sqlite3.Connection) -> None:
            try:
                conn.executemany(
                    "INSERT INTO v2_nudge_state (stream_id, kind, last_nudged_at, basis) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(stream_id, kind) DO UPDATE SET "
                    "last_nudged_at=excluded.last_nudged_at, basis=excluded.basis",
                    [(stream_id, kind, float(at), basis) for stream_id, kind, at, basis in records],
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        await self.submit(_op)

    # -- reserved stream-id fencing (ledger req 4) -------------------------

    async def children_of(self, parent_stream_id: str) -> list[dict[str, Any]]:
        """Open sessions whose direct parent is `parent_stream_id` — the handoff
        reparent set (v1 Pin 7: enumerate by `parent_stream_id ==` plainly, no
        visibility/online filter)."""

        def _op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE parent_stream_id=? AND status='open'",
                (parent_stream_id,),
            ).fetchall()
            return [_session_row(conn, r) for r in rows]

        return await self.submit(_op)

    async def reserve_stream_id(
        self, host: str, session_name: str, *, ttl_s: float, request_id: str = "",
        nonce: str = "", owner_instance_id: str = "",
        idempotency_key: str = "", request_payload_hash: str = "",
        refuse_if_held: bool = False,
    ) -> bool:
        """Atomically claim `host:session_name`. False if a live session holds
        it, an unexpired reservation exists, OR (with `refuse_if_held`, the
        keyless NEW-admission path) a spawn admission hold is active -- the hold
        check and claim run in ONE serialized txn (QA cycle-3 astra-[3]) so a
        freeze set after a separate pre-check can no longer slip a claim
        through. The caller re-reads the sticky hold only to pick the error.

        `nonce` (spec §D1) is the creation nonce injected into the pane's
        environment; `owner_instance_id` (spec §D3/INV-2) is the durable
        ownership token of the daemon instance that owns this in-flight spawn.
        Both are written in the SAME atomic INSERT that claims the id, so no
        reservation ever exists without its identity."""

        def _op(conn: sqlite3.Connection) -> bool:
            now = time.time()
            # The keyless path is always a NEW admission (no replay), so a live
            # hold refuses it in this same serialized txn.
            if refuse_if_held and _admission_held(conn, host, now):
                conn.commit()
                return False
            # INV-6 (intent-aware expiry): sweep only reservations that are BOTH
            # pane-less (`tmux_created = 0`) AND intent-less (`payload IS NULL`).
            # A payload is a persisted spawn intent — a recovery handle for a
            # pane that may already be live in the F1 crash window — so it is
            # TTL-exempt until reconciliation settles it (never deleted before
            # adoption). Bounded terminalization retires stale intents instead.
            conn.execute(
                "DELETE FROM v2_stream_reservations "
                "WHERE expires_at < ? AND tmux_created = 0 AND payload IS NULL", (now,)
            )
            live = conn.execute(
                "SELECT 1 FROM sessions WHERE host=? AND session_name=? AND status='open'",
                (host, session_name),
            ).fetchone()
            if live is not None:
                conn.commit()
                return False
            try:
                _insert_stream_reservation(
                    conn, host=host, session_name=session_name, request_id=request_id,
                    expires_at=now + ttl_s, nonce=nonce,
                    owner_instance_id=owner_instance_id,
                    idempotency_key=idempotency_key,
                    request_payload_hash=request_payload_hash,
                )
            except sqlite3.IntegrityError:
                conn.commit()
                return False
            conn.commit()
            return True

        return await self.submit(_op)

    async def release_stream_id_fenced(
        self, host: str, session_name: str, request_id: str, *, cleanup_confirmed: bool = False,
    ) -> bool:
        def _op(conn: sqlite3.Connection) -> bool:
            if not cleanup_confirmed and _spawn_cancelled(conn, host, session_name, request_id):
                held = conn.execute(
                    "SELECT 1 FROM v2_stream_reservations WHERE host=? AND session_name=? "
                    "AND payload IS NOT NULL", (host, session_name),
                ).fetchone()
                if held is not None:
                    return False
            cursor = conn.execute(
                "DELETE FROM v2_stream_reservations "
                "WHERE host=? AND session_name=? AND COALESCE(request_id, '')=?",
                (host, session_name, request_id),
            )
            conn.commit()
            return cursor.rowcount > 0

        return await self.submit(_op)

    async def claim_spawn_intent(
        self, host: str, session_name: str, *, request_id: str,
        prior_owner: str, owner_instance_id: str,
    ) -> bool:
        """CAS one existing reservation onto the reconciling daemon instance."""
        def _op(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE v2_stream_reservations SET owner_instance_id=? "
                "WHERE host=? AND session_name=? AND COALESCE(request_id, '')=? "
                "AND COALESCE(owner_instance_id, '')=?",
                (owner_instance_id, host, session_name, request_id, prior_owner),
            )
            conn.commit()
            return cursor.rowcount == 1

        return await self.submit(_op)

    async def restore_spawn_intent_owner(
        self, host: str, session_name: str, *, request_id: str,
        owner_instance_id: str, prior_owner: str,
    ) -> bool:
        """Release a reconcile claim only when this instance still owns it."""
        def _op(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE v2_stream_reservations SET owner_instance_id=? "
                "WHERE host=? AND session_name=? AND COALESCE(request_id, '')=? "
                "AND COALESCE(owner_instance_id, '')=?",
                (prior_owner or None, host, session_name, request_id, owner_instance_id),
            )
            conn.commit()
            return cursor.rowcount == 1

        return await self.submit(_op)

    async def reservations(self, *, include_expired: bool = False) -> list[dict[str, Any]]:
        """Live reservations; `include_expired` is for boot reconciliation,
        which must see intents whose TTL lapsed while the daemon was down."""

        def _op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            if include_expired:
                rows = conn.execute("SELECT * FROM v2_stream_reservations").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM v2_stream_reservations WHERE expires_at >= ?", (time.time(),)
                ).fetchall()
            return [_row(r) for r in rows]

        return await self.submit(_op)

    async def record_spawn_intent(
        self, host: str, session_name: str, payload: dict[str, Any], *,
        request_id: str | None = None, nonce: str | None = None,
    ) -> bool:
        """Persist what it would take to finish this spawn's row, BEFORE the
        pane exists. Written to the reservation the spawn already holds."""

        def _op(conn: sqlite3.Connection) -> bool:
            held = conn.execute(
                "SELECT request_id FROM v2_stream_reservations WHERE host=? AND session_name=?",
                (host, session_name),
            ).fetchone()
            identity = request_id if request_id is not None else str((held[0] if held else "") or "")
            res = _spawn_reservation(conn, host, session_name, identity, nonce)
            if not res:
                return False
            # Local TTL fence (QA cycle-3 luna-[1]): a payload-LESS reservation
            # whose TTL already lapsed is not a valid pre-pane creation handle.
            # Fail closed here so the spawn aborts BEFORE new_session instead of
            # creating a pane with no recoverable intent. Scoped to this pre-pane
            # record CAS: an intent-bearing reservation stays TTL-exempt (INV-6),
            # and record_spawn_intent runs before payload exists.
            if res.get("payload") is None and float(res.get("expires_at") or 0.0) < time.time():
                return False
            prior_intent = json.loads(res.get("payload") or "{}")
            if "exchange_binding" in prior_intent:
                prior_intent = {**payload, "exchange_binding": prior_intent["exchange_binding"]}
            else:
                prior_intent = payload
            cursor = conn.execute(
                "UPDATE v2_stream_reservations SET payload=? WHERE host=? AND session_name=?",
                (json.dumps(prior_intent, separators=(",", ":")), host, session_name),
            )
            conn.commit()
            return cursor.rowcount == 1

        return await self.submit(_op)

    async def mark_tmux_created(
        self, host: str, session_name: str, *,
        request_id: str | None = None, nonce: str | None = None,
        pane_pid: str = "", pane_started_at: str = "",
    ) -> None:
        """The pane now exists. From here the reservation is orphan evidence.

        Persist the observed pane identity (spec §D1) when known: it is the
        corroborating adoption signal once the pane is marked, and the ONLY
        adoption key for a legacy nonce-less row (spec A2). Absent in the F1
        crash window (the pane died before this write), where the nonce carried
        in the pane's environment is the primary signal instead. Existing
        identity is never clobbered by a later mark with no new values."""

        def _op(conn: sqlite3.Connection) -> None:
            held = conn.execute(
                "SELECT request_id FROM v2_stream_reservations WHERE host=? AND session_name=?",
                (host, session_name),
            ).fetchone()
            identity = request_id if request_id is not None else str((held[0] if held else "") or "")
            if not _spawn_reservation(conn, host, session_name, identity, nonce):
                return
            conn.execute(
                "UPDATE v2_stream_reservations "
                "SET tmux_created=1, "
                "    pane_pid=COALESCE(?, pane_pid), "
                "    pane_started_at=COALESCE(?, pane_started_at) "
                "WHERE host=? AND session_name=?",
                (pane_pid or None, pane_started_at or None, host, session_name),
            )
            conn.commit()

        await self.submit(_op)

    # -- tell idempotency (QA #6) ------------------------------------------

    async def get_tell_delivery(self, tell_id: str) -> dict[str, Any] | None:
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT rowid AS ledger_row_id, reply, created_at "
                "FROM v2_tell_deliveries WHERE tell_id=?", (tell_id,)
            ).fetchone()
            return _decode_tell_envelope(row)

        return await self.submit(_op)

    async def get_tell_delivery_by_ledger_row_id(self, ledger_row_id: int) -> dict[str, Any] | None:
        """Read one completed tell by its durable row identity.

        This is a SELECT-only companion to ``get_tell_delivery``. The row id
        is exposed in ``tell.ok`` so an audit can bind a recipient's frame to
        the exact stored delivery record without touching delivery.
        """

        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT rowid AS ledger_row_id, reply, created_at "
                "FROM v2_tell_deliveries WHERE rowid=?", (ledger_row_id,)
            ).fetchone()
            return _decode_tell_envelope(row)

        return await self.submit(_op)

    async def list_tell_deliveries(
        self, to_stream_id: str, *, limit: int,
    ) -> list[dict[str, Any]]:
        """Return durable delivery envelopes for one recipient, newest first.

        ``v2_tell_deliveries`` is the durable record used for tell idempotency.
        The audit view filters that bounded record set; it does not create a
        recipient inbox, pending state, or another copy of an undelivered
        message.
        """
        if not to_stream_id or limit <= 0:
            return []
        effective_limit = min(int(limit), TELL_RETENTION)

        def _op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT rowid AS ledger_row_id, reply, created_at "
                "FROM v2_tell_deliveries ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (TELL_RETENTION,),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                envelope = _decode_tell_envelope(row)
                if envelope is None:
                    continue
                delivery = envelope.get("delivery")
                reply = envelope.get("reply")
                target = (
                    delivery.get("to_stream_id")
                    if isinstance(delivery, dict) else None
                ) or (reply.get("to_stream_id") if isinstance(reply, dict) else None)
                if str(target or "") != to_stream_id:
                    continue
                result.append(envelope)
                if len(result) >= effective_limit:
                    break
            return result

        return await self.submit(_op)

    async def list_delivered_tell_ids(self, *, prefix: str, limit: int) -> list[str]:
        """List v2's own durably delivered tell IDs under one fixed namespace.

        The answer-card startup cleanup uses this as its sole ownership proof:
        it never scans shared question rows first.  A row is eligible only when
        this v2-local ledger contains a structurally complete, delivered record
        for its deterministic tell ID.  Old/partial and ``pasted_unsubmitted``
        records intentionally fail closed.
        """
        if not prefix or limit <= 0:
            return []
        effective_limit = min(int(limit), TELL_RETENTION)

        def _op(conn: sqlite3.Connection) -> list[str]:
            rows = conn.execute(
                "SELECT rowid AS ledger_row_id, tell_id, reply, created_at FROM v2_tell_deliveries "
                "WHERE tell_id LIKE ? ORDER BY created_at ASC, rowid ASC LIMIT ?",
                (f"{prefix}%", TELL_RETENTION),
            ).fetchall()
            result: list[str] = []
            for row in rows:
                tell_id = str(row["tell_id"] or "")
                envelope = _decode_tell_envelope(row)
                if envelope is None:
                    continue
                delivery = envelope.get("delivery")
                reply = envelope.get("reply")
                if (
                    not isinstance(delivery, dict)
                    or not isinstance(reply, dict)
                    or delivery.get("tell_id") != tell_id
                    or reply.get("tell_id") != tell_id
                    or delivery.get("delivery_status") != "delivered"
                    or reply.get("delivery_status") != "delivered"
                ):
                    continue
                result.append(tell_id)
                if len(result) >= effective_limit:
                    break
            return result

        return await self.submit(_op)

    async def put_tell_delivery(self, tell_id: str, envelope: dict[str, Any]) -> int:
        """Remember a tell delivery envelope so a retry replays it instead of
        injecting again, and return its ledger row id — the row's `rowid`, which
        `tell.ok` surfaces as v1's `ledger_row_id` (QA #10). The id is stamped
        back into the stored envelope's `reply` so a replayed `tell.ok`
        reproduces it. A provider-proven failure is not a transport exception;
        it remains as `pasted_unsubmitted` on this same idempotency row.

        `envelope` is comms' own record: `{"payload_digest", "reply", "delivery"}`.
        The optional ``delivery`` object is the exact text and route metadata
        written after the unconditional paste. Only row identities are stamped
        here; the digest and delivered text are opaque to the store."""

        def _op(conn: sqlite3.Connection) -> int:
            conn.execute(
                "INSERT INTO v2_tell_deliveries (tell_id, reply, created_at) VALUES (?,?,?)"
                " ON CONFLICT(tell_id) DO NOTHING",
                (tell_id, json.dumps(envelope, separators=(",", ":")), time.time()),
            )
            stored = conn.execute(
                "SELECT rowid AS ledger_row_id, reply FROM v2_tell_deliveries WHERE tell_id=?",
                (tell_id,),
            ).fetchone()
            row_id = int(stored["ledger_row_id"])
            payload = json.loads(stored["reply"])
            changed = False
            reply = payload.get("reply")
            if isinstance(reply, dict) and reply.get("ledger_row_id") != row_id:
                reply["ledger_row_id"] = row_id
                changed = True
            delivery = payload.get("delivery")
            if isinstance(delivery, dict) and delivery.get("ledger_row_id") != row_id:
                delivery["ledger_row_id"] = row_id
                changed = True
            if changed:
                conn.execute(
                    "UPDATE v2_tell_deliveries SET reply=? WHERE tell_id=?",
                    (json.dumps(payload, separators=(",", ":")), tell_id),
                )
            if isinstance(delivery, dict):
                store_exchange.append(conn, delivery.get("exchange"))
                conn.execute("UPDATE v2_tell_deliveries SET reply=? WHERE tell_id=?", (json.dumps(payload, separators=(",", ":")), tell_id))
            conn.execute(
                "DELETE FROM v2_tell_deliveries WHERE NOT EXISTS (SELECT 1 FROM v2_child_exchange e WHERE e.kind='tell' AND e.ref_id=v2_tell_deliveries.tell_id) AND tell_id NOT IN"
                " (SELECT tell_id FROM v2_tell_deliveries ORDER BY created_at DESC LIMIT ?)",
                (TELL_RETENTION,),
            )
            conn.commit()
            return row_id

        return await self.submit(_op)

    async def promote_tell_delivery(
        self, tell_id: str, payload_digest: str, *, proof: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Atomically promote one matching proof-pending tell.

        This is the only later state transition for a tell row.  It is
        deliberately same-ID and same-digest constrained so an evidence-only
        outbound sweep cannot rewrite another payload or create a second row.
        ``None`` means the row was absent, had a different digest, or was
        already in a different state.
        """

        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT rowid AS ledger_row_id, reply, created_at "
                    "FROM v2_tell_deliveries WHERE tell_id=?",
                    (tell_id,),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return None
                try:
                    payload = json.loads(row["reply"])
                except (TypeError, ValueError, KeyError):
                    conn.rollback()
                    return None
                if not isinstance(payload, dict) or payload.get("payload_digest") != payload_digest:
                    conn.rollback()
                    return None
                reply = payload.get("reply")
                delivery = payload.get("delivery")
                if not isinstance(reply, dict) or not isinstance(delivery, dict):
                    conn.rollback()
                    return None
                if (
                    reply.get("delivery_status") not in COMMITTED_PENDING_PROOF_STATUSES
                    or delivery.get("delivery_status") not in COMMITTED_PENDING_PROOF_STATUSES
                ):
                    conn.rollback()
                    return None

                acknowledged_at = iso_now()
                reply["delivery_status"] = "delivered"
                reply["submission_confirmed"] = True
                reply["delivery_ack_at"] = acknowledged_at
                reply.pop("reason", None)
                delivery["delivery_status"] = "delivered"
                delivery["submission_confirmed"] = True
                delivery["delivered_at"] = acknowledged_at
                delivery["delivery_ack_at"] = acknowledged_at
                if isinstance(proof, dict):
                    reply.update(proof)
                    delivery.update(proof)
                conn.execute(
                    "UPDATE v2_tell_deliveries SET reply=? WHERE tell_id=?",
                    (json.dumps(payload, separators=(",", ":")), tell_id),
                )
                conn.commit()
                updated = conn.execute(
                    "SELECT rowid AS ledger_row_id, reply, created_at "
                    "FROM v2_tell_deliveries WHERE tell_id=?",
                    (tell_id,),
                ).fetchone()
                return _decode_tell_envelope(updated)
            except Exception:
                conn.rollback()
                raise

        return await self.submit(_op)


    # -- durable spawn outcome (await_spawn answers from here) -------------

    async def set_spawn_outcome(self, host: str, session_name: str, state: str, **fields: Any) -> bool:
        def _op(conn: sqlite3.Connection) -> bool:
            source_fields = dict(fields)
            prior = _decode_spawn_outcome(_row(conn.execute("SELECT * FROM v2_spawn_outcomes WHERE host=? AND session_name=?", (host, session_name)).fetchone()))
            prior_exchange = ((prior or {}).get("delivery_receipt") or {}).get("exchange")
            if prior_exchange and (prior or {}).get("request_id") == fields.get("request_id"):
                source_fields["delivery_receipt"] = {**(fields.get("delivery_receipt") or {}), "exchange": prior_exchange}
            if state == "delivered" and not prior_exchange:
                reservation = _spawn_reservation(conn, host, session_name, str(fields.get("request_id") or ""))
                intent = json.loads((reservation or {}).get("payload") or "{}")
                receipt = dict(fields.get("delivery_receipt") or {})
                if intent.get("brief") and intent.get("exchange_binding"):
                    receipt["exchange"] = store_exchange.source(intent["exchange_binding"], "brief", fields["request_id"], intent["brief"], receipt.get("delivery_ack_at") or receipt.get("delivered_at") or iso_now())
                    source_fields["delivery_receipt"] = receipt
            written = _upsert_spawn_outcome(conn, host, session_name, state, **source_fields)
            if written:
                store_exchange.append(conn, (source_fields.get("delivery_receipt") or {}).get("exchange"))
                if source_fields.get("delivery_receipt"):
                    conn.execute("UPDATE v2_spawn_outcomes SET delivery_receipt=? WHERE host=? AND session_name=?", (json.dumps(source_fields["delivery_receipt"]), host, session_name))
            conn.commit()
            return written

        return await self.submit(_op)

    async def cancel_reservation_fenced(
        self, host: str, session_name: str, request_id: str,
    ) -> dict[str, Any]:
        """Cancel and persist an immutable request tombstone in the bind transaction.

        Intent-bearing reservations survive as cleanup obligations; an intent-less
        reservation can be released because recording intent now fails closed.
        Bound requests return already_bound, including a concurrent row release."""
        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            if _spawn_cancelled(conn, host, session_name, request_id):
                return {"status": "cancelled"}
            row = _row(conn.execute(
                "SELECT tmux_created, request_id, idempotency_key, request_payload_hash, payload, nonce "
                "FROM v2_stream_reservations WHERE host=? AND session_name=? AND COALESCE(request_id, '')=?",
                (host, session_name, request_id),
            ).fetchone())
            if row is None:
                conn.commit()
                bound = conn.execute(
                    "SELECT 1 FROM sessions WHERE host=? AND session_name=? AND status='open'",
                    (host, session_name),
                ).fetchone()
                if bound is not None:
                    return {"status": "already_bound"}
                return {"status": "no_reservation"}
            if row.get("tmux_created"):
                conn.commit()
                return {"status": "already_bound"}
            conn.execute(
                "DELETE FROM v2_stream_reservations "
                "WHERE host=? AND session_name=? AND COALESCE(request_id, '')=? AND payload IS NULL",
                (host, session_name, request_id),
            )
            _upsert_spawn_outcome(
                conn, host, session_name, "cancelled",
                request_id=row.get("request_id"),
                idempotency_key=row.get("idempotency_key"),
                request_payload_hash=row.get("request_payload_hash"),
                nonce=row.get("nonce"),
                reason="cancelled_before_bind",
            )
            conn.commit()
            return {
                "status": "cancelled",
                "idempotency_key": row.get("idempotency_key"),
                "request_payload_hash": row.get("request_payload_hash"),
            }

        return await self.submit(_op)

    async def commit_tmux_created_fenced(
        self, host: str, session_name: str, *,
        request_id: str, nonce: str | None = None,
        pane_pid: str = "", pane_started_at: str = "",
    ) -> bool:
        """Bind only the owning request and creation nonce, unless cancellation won.

        Absence or a replaced reservation always fails closed. The same CAS is
        used by normal creation and restart adoption before admitting a seat."""
        def _op(conn: sqlite3.Connection) -> bool:
            if not _spawn_reservation(conn, host, session_name, request_id, nonce):
                return False
            conn.execute(
                "UPDATE v2_stream_reservations "
                "SET tmux_created=1, "
                "    pane_pid=COALESCE(?, pane_pid), "
                "    pane_started_at=COALESCE(?, pane_started_at) "
                "WHERE host=? AND session_name=?",
                (pane_pid or None, pane_started_at or None, host, session_name),
            )
            conn.commit()
            return True

        return await self.submit(_op)

    async def get_spawn_outcome(self, host: str, session_name: str) -> dict[str, Any] | None:
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            value = _row(conn.execute(
                "SELECT * FROM v2_spawn_outcomes WHERE host=? AND session_name=?", (host, session_name)
            ).fetchone())
            return _decode_spawn_outcome(value)

        return await self.submit(_op)

    async def get_spawn_outcome_by_request_id(
        self, request_id: str, *, host: str | None = None
    ) -> dict[str, Any] | None:
        """Find the terminal spawn outcome when the requester lost its stream id.

        The request id is the durable identity printed in a spawn
        ``reconcile_command``. It must be sufficient to resolve an abandoned or
        indeterminate spawn without first reconstructing ``host:session_name``.

        `host` scopes the lookup: a request id is only per-host unique, so a
        cancel/status on host X must never import another host's row that
        happens to share the id (QA cycle-3 finding [2]). Callers that already
        know the host MUST pass it; the hostless form stays for legacy audit."""
        request_id = str(request_id or "").strip()
        if not request_id:
            return None

        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            # Explicit columns: the cancellation table has an extra `nonce`, so
            # `SELECT *` no longer aligns across the UNION (QA cycle-3 luna-[2]).
            sql = (
                f"SELECT * FROM (SELECT {_SPAWN_OUTCOME_COLUMNS} FROM v2_spawn_outcomes UNION ALL "
                f"SELECT {_SPAWN_OUTCOME_COLUMNS} FROM v2_spawn_cancellations) WHERE request_id=?"
            )
            params: tuple[Any, ...] = (request_id,)
            if host is not None:
                sql += " AND host=?"
                params = (request_id, host)
            sql += " ORDER BY updated_at DESC LIMIT 1"
            value = _row(conn.execute(sql, params).fetchone())
            return _decode_spawn_outcome(value)

        return await self.submit(_op)

    async def spawn_cancellations(self, host: str, target: str) -> list[dict[str, Any]]:
        def _op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            return [_decode_spawn_outcome(dict(row)) for row in conn.execute(
                "SELECT * FROM v2_spawn_cancellations WHERE host=? "
                "AND (COALESCE(request_id, '')=? OR idempotency_key=?)",
                (host, target, target),
            )]
        return await self.submit(_op)

    async def spawn_cancelled(self, host: str, name: str, request_id: str) -> bool:
        return await self.submit(lambda conn: _spawn_cancelled(conn, host, name, request_id))

    async def owns_spawn_intent(
        self, host: str, name: str, request_id: str, nonce: str,
    ) -> bool:
        return await self.submit(lambda conn: bool(
            (row := _spawn_reservation(conn, host, name, request_id, nonce))
            and row.get("payload") is not None
        ))

    async def admitted_session_names_for_key(
        self, host: str, idempotency_key: str
    ) -> list[str]:
        """Distinct session names admitted for an idempotency key across
        reservations + outcomes (AC9 truthful enumeration). rpc_delivery_
        determinism lane."""
        idempotency_key = str(idempotency_key or "").strip()
        if not idempotency_key:
            return []

        def _op(conn: sqlite3.Connection) -> list[str]:
            names: list[str] = []
            seen: set[str] = set()
            for tbl in ("v2_spawn_outcomes", "v2_stream_reservations", "v2_spawn_cancellations"):
                for r in conn.execute(
                    f"SELECT session_name FROM {tbl} WHERE host=? AND idempotency_key=?",
                    (host, idempotency_key),
                ).fetchall():
                    name = str(r[0])
                    if name not in seen:
                        seen.add(name)
                        names.append(name)
            return sorted(names)

        return await self.submit(_op)

    async def set_spawn_admission_hold(
        self, host: str, *, held_until: float, reason: str = ""
    ) -> None:
        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO v2_spawn_admission_hold (host, held_until, reason, set_at)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(host) DO UPDATE SET held_until=excluded.held_until,"
                " reason=excluded.reason, set_at=excluded.set_at",
                (host, float(held_until), reason or None, time.time()),
            )
            conn.commit()

        await self.submit(_op)

    async def clear_spawn_admission_hold(self, host: str) -> None:
        def _op(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM v2_spawn_admission_hold WHERE host=?", (host,))
            conn.commit()

        await self.submit(_op)

    async def get_spawn_admission_hold(self, host: str) -> dict[str, Any] | None:
        """Active hold for `host`, or None. An expired hold is swept so a
        forgotten freeze never wedges admission."""
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = _row(conn.execute(
                "SELECT * FROM v2_spawn_admission_hold WHERE host=?", (host,)
            ).fetchone())
            if row is None:
                return None
            if float(row.get("held_until") or 0) < time.time():
                conn.execute("DELETE FROM v2_spawn_admission_hold WHERE host=?", (host,))
                conn.commit()
                return None
            return row

        return await self.submit(_op)

    async def atomic_claim_or_replay(
        self, host: str, session_name: str, *, idempotency_key: str,
        request_payload_hash: str, ttl_s: float, request_id: str = "",
        nonce: str = "", owner_instance_id: str = "", refuse_if_held: bool = False,
    ) -> dict[str, Any]:
        """Atomically resolve a keyed spawn in ONE single-writer op — closing the
        lookup-then-reserve race that let concurrent / re-fired same-key spawns
        each mint a pane (Multi-fire #4). Returns one of:

        - ``{"status": "replay", "kind": "terminal"|"in_flight", "row": <row>}``
          — this key already resolved (or is in flight); the caller replays that
          session instead of creating a second one.
        - ``{"status": "conflict"}`` — the key is bound to a different payload.
        - ``{"status": "claimed"}`` — this caller won the claim and reserved
          ``session_name``; it proceeds to create.
        - ``{"status": "unavailable"}`` — name live or already reserved.

        Because the whole check+claim runs in the single store thread, N
        concurrent same-key callers see exactly one ``claimed`` and the rest
        ``replay``. rpc_delivery_determinism lane."""
        idempotency_key = str(idempotency_key or "").strip()

        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            now = time.time()
            if idempotency_key:
                outcome = _row(conn.execute(
                    f"SELECT * FROM (SELECT {_SPAWN_OUTCOME_COLUMNS} FROM v2_spawn_outcomes UNION ALL "
                    f"SELECT {_SPAWN_OUTCOME_COLUMNS} FROM v2_spawn_cancellations) "
                    "WHERE host=? AND idempotency_key=? ORDER BY updated_at DESC LIMIT 1",
                    (host, idempotency_key),
                ).fetchone())
                existing = outcome
                kind = "terminal"
                if existing is None:
                    reservation = _row(conn.execute(
                        "SELECT * FROM v2_stream_reservations WHERE host=? AND idempotency_key=? "
                        "AND (expires_at >= ? OR tmux_created=1 OR payload IS NOT NULL) LIMIT 1",
                        (host, idempotency_key, now),
                    ).fetchone())
                    existing = reservation
                    kind = "in_flight"
                if existing is not None:
                    recorded_hash = str(existing.get("request_payload_hash") or "")
                    if recorded_hash and request_payload_hash and recorded_hash != request_payload_hash:
                        conn.commit()
                        return {"status": "conflict"}
                    if kind == "terminal":
                        existing = _decode_spawn_outcome(existing) or {}
                    conn.commit()
                    return {"status": "replay", "kind": kind, "row": existing}
            # A NEW keyed admission during an active hold is refused in the SAME
            # serialized txn that would otherwise claim it (QA cycle-3 astra-[3]);
            # the already-admitted-key replay resolved above is unaffected, so an
            # interrupted caller's retry still gets its seat during a deploy hold.
            if refuse_if_held and _admission_held(conn, host, now):
                conn.commit()
                return {"status": "frozen"}
            # No prior claim for this key (or keyless): reserve exactly as
            # reserve_stream_id does, atomically in this same op.
            conn.execute(
                "DELETE FROM v2_stream_reservations "
                "WHERE expires_at < ? AND tmux_created = 0 AND payload IS NULL", (now,)
            )
            live = conn.execute(
                "SELECT 1 FROM sessions WHERE host=? AND session_name=? AND status='open'",
                (host, session_name),
            ).fetchone()
            if live is not None:
                conn.commit()
                return {"status": "unavailable"}
            try:
                _insert_stream_reservation(
                    conn, host=host, session_name=session_name, request_id=request_id,
                    expires_at=now + ttl_s, nonce=nonce,
                    owner_instance_id=owner_instance_id,
                    idempotency_key=idempotency_key,
                    request_payload_hash=request_payload_hash,
                )
            except sqlite3.IntegrityError:
                conn.commit()
                return {"status": "unavailable"}
            conn.commit()
            return {"status": "claimed"}

        return await self.submit(_op)

    # -- durable report ledger (B9) ----------------------------------------

    async def put_report(
        self,
        fields: dict[str, Any],
        *,
        routing_snapshot: dict[str, Any] | None = None,
        qa_attestation_mode: str = "warn",
    ) -> dict[str, Any]:
        """Persist one validated report and return the stored row.

        Ingest is idempotent on `report_id`: a retried `report` returns the row
        already written rather than a second ledger row. The row is the answer
        `await_report` will give, so it is written whole — the caller has
        already validated it and nothing here is allowed to drop a field.

        ``routing_snapshot`` is the complete source snapshot observed immediately
        before this write. The worker re-reads that source in the same serialized
        store boundary and refuses a changed source before inserting anything.
        """

        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            existing = conn.execute(
                "SELECT * FROM v2_reports WHERE report_id=?", (fields["report_id"],)
            ).fetchone()
            if existing is not None:
                stored = _report_row(existing)
                if stored is not None:
                    if not _report_replay_matches(stored, fields):
                        raise ReportReplayConflict(str(fields["report_id"]))
                    stored["_report_inserted"] = False
                return stored
            if not _complete_report_provenance(routing_snapshot, fields):
                raise ReportProvenanceUnavailable(routing_snapshot)
            stream_id = str(fields.get("from_stream_id") or "")
            host, _, session_name = stream_id.partition(":")
            current_row = None
            if host and session_name:
                current_row = conn.execute(
                    "SELECT s.created_at, s.requested_model, s.requested_effort, "
                    "s.effective_model, s.effective_effort, s.qualified_spec_ids, "
                    "g.generation AS session_generation "
                    "FROM sessions s LEFT JOIN v2_session_generations g "
                    "ON g.host=s.host AND g.session_name=s.session_name "
                    "WHERE s.host=? AND s.session_name=?",
                    (host, session_name),
                ).fetchone()
            current = dict(current_row) if current_row is not None else None
            tuple_fields = (
                "requested_model", "requested_effort", "effective_model", "effective_effort",
            )
            changed = current is None or (
                str((current or {}).get("created_at") or "")
                != str(routing_snapshot.get("generation") or "")
            ) or (
                str((current or {}).get("session_generation") or "")
                != str(routing_snapshot.get("session_generation") or "")
            ) or any(
                str((current or {}).get(field) or "")
                != str(routing_snapshot.get(field) or "")
                for field in tuple_fields
            ) or (
                normalize_spec_ids((current or {}).get("qualified_spec_ids"))
                != normalize_spec_ids(routing_snapshot.get("qualified_spec_ids"))
            )
            if changed:
                raise ReportProvenanceChanged(routing_snapshot, current)
            stored_fields = dict(fields)
            stored_fields.update({
                "session_generation": str(routing_snapshot["session_generation"]),
                "effective_model": routing_snapshot.get("effective_model"),
                "effective_effort": routing_snapshot.get("effective_effort"),
                "provenance_version": "v1",
                "provenance_generation": str(routing_snapshot["session_generation"]),
                "provenance_qualified_spec_ids": normalize_spec_ids(
                    routing_snapshot.get("qualified_spec_ids"),
                ),
                "provenance_routing_integrity": routing_snapshot.get("routing_integrity"),
                "provenance_requested_model": routing_snapshot.get("requested_model"),
                "provenance_requested_effort": routing_snapshot.get("requested_effort"),
                "provenance_effective_model": routing_snapshot.get("effective_model"),
                "provenance_effective_effort": routing_snapshot.get("effective_effort"),
            })
            validation = _qa_attestation_validation_conn(
                conn,
                reporting_stream_id=str(stored_fields.get("from_stream_id") or ""),
                completion_kind=stored_fields.get("completion_kind"),
                attestation=stored_fields.get("qa_attestation"),
                mode=qa_attestation_mode,
                canonical_spec_identity=self._spec_identity_resolver,
            )
            if qa_attestation_mode == "enforce" and validation and validation["state"] == "unverified":
                raise QAAttestationUnverified(validation)
            stored_fields["qa_attestation_validation"] = validation
            values = [_enc_report(col, stored_fields.get(col)) for col in REPORT_COLUMNS]
            inserted = conn.execute(
                f"INSERT INTO v2_reports ({','.join(REPORT_COLUMNS)}, created_at)"
                f" VALUES ({','.join('?' * len(REPORT_COLUMNS))}, ?)"
                " ON CONFLICT(report_id) DO NOTHING",
                [*values, time.time()],
            ).rowcount == 1
            stored = _report_row(conn.execute(
                "SELECT * FROM v2_reports WHERE report_id=?", (stored_fields["report_id"],)
            ).fetchone())
            if stored is not None and not inserted and not _report_replay_matches(stored, fields):
                raise ReportReplayConflict(str(fields["report_id"]))
            if inserted and stored is not None:
                binding = store_exchange.direct_pair(conn, stream_id, stored.get("to_stream_id"))
                text = "\n".join(str(part) for part in (stored.get("summary"), *(finding.get(key) for finding in stored.get("findings") or [] for key in ("where", "issue", "suggested_fix"))) if part)
                observation = store_exchange.source(binding, "report", stored["report_id"], text, stored["ingested_at"])
                store_exchange.append(conn, observation)
                conn.execute("UPDATE v2_reports SET exchange_json=? WHERE report_id=?", (json.dumps(observation), stored["report_id"]))
                live = store_exchange.session(conn, stream_id)
                if live and live["status"] == "open" and live["generation"] == stored["session_generation"]:
                    conn.execute("INSERT INTO v2_agent_report_state VALUES (?,?,?,?) ON CONFLICT(stream_id) DO UPDATE SET generation=excluded.generation,status=excluded.status,ts=CASE WHEN v2_agent_report_state.generation=excluded.generation AND (v2_agent_report_state.status=excluded.status OR (v2_agent_report_state.status IN ('done','aborted') AND excluded.status IN ('done','aborted'))) THEN v2_agent_report_state.ts ELSE excluded.ts END", (stream_id, stored["session_generation"], stored["status"], stored["ingested_at"]))
            if inserted and stored is not None and stored["status"] in {"done", "error", "aborted"}:
                # A real report has higher trust than a close outcome. Keep the
                # report insert and await-result promotion in one store-thread
                # transaction so a close/report race has one durable winner.
                _resolve_awaiters_for_report_conn(conn, stored)
            # D2 composes the accepted-report transaction; D1 append stays before this hook.
            with conn:
                if inserted and stored is not None:
                    stored["_watch_notice_created"] = report_watch_conn(conn, stored)
            conn.commit()
            if stored is not None:
                stored["_report_inserted"] = inserted
            return stored

        return await self.submit(_op)

    # -- durable awaiters (C5) --------------------------------------------

    async def register_awaiter(
        self, stream_id: str, msg_id: int | None,
    ) -> dict[str, Any]:
        """Record an await before its caller parks an asyncio future.

        Registration is also the late-awaiter recovery path: a terminal report
        or a confirmed-closed target is converted to a durable result in the
        same transaction that observes it. A reopened host/session name gets a
        fresh generation key and cannot inherit the predecessor's close result.
        """
        target_msg_id = _awaiter_target_msg_id(msg_id)

        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            generation, terminal = _awaiter_session_state_conn(conn, stream_id)
            existing = conn.execute(
                "SELECT * FROM v2_awaiters "
                "WHERE stream_id=? AND session_generation=? AND target_msg_id=?",
                (stream_id, generation, target_msg_id),
            ).fetchone()
            report = _report_for_awaiter_conn(conn, stream_id, target_msg_id)
            now = iso_now()
            if report is not None:
                _write_awaiter_report_conn(
                    conn, stream_id, generation, target_msg_id, report, now,
                )
            elif existing is None:
                outcome = "closed_without_report" if terminal else "pending"
                conn.execute(
                    "INSERT INTO v2_awaiters "
                    "(stream_id,session_generation,target_msg_id,outcome,reason,requested_at,resolved_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        stream_id, generation, target_msg_id, outcome,
                        "target_closed_without_report" if terminal else None,
                        now, now if terminal else None,
                    ),
                )
            elif existing["outcome"] == "pending" and terminal:
                conn.execute(
                    "UPDATE v2_awaiters SET outcome='closed_without_report', "
                    "reason=?, resolved_at=? WHERE awaiter_id=?",
                    ("target_closed_without_report", now, existing["awaiter_id"]),
                )
            row = conn.execute(
                "SELECT * FROM v2_awaiters "
                "WHERE stream_id=? AND session_generation=? AND target_msg_id=?",
                (stream_id, generation, target_msg_id),
            ).fetchone()
            conn.commit()
            return _awaiter_row(conn, row)

        return await self.submit(_op)

    async def get_awaiter_result(
        self, stream_id: str, msg_id: int | None,
    ) -> dict[str, Any] | None:
        """Read the current-generation durable await result, if registered."""
        target_msg_id = _awaiter_target_msg_id(msg_id)

        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            generation, _terminal = _awaiter_session_state_conn(conn, stream_id)
            row = conn.execute(
                "SELECT * FROM v2_awaiters "
                "WHERE stream_id=? AND session_generation=? AND target_msg_id=?",
                (stream_id, generation, target_msg_id),
            ).fetchone()
            return _awaiter_row(conn, row)

        return await self.submit(_op)

    async def resolve_awaiters_on_close(
        self, stream_id: str, *, session_generation: str | None = None,
        reason: str = "confirmed_dead_close",
    ) -> list[dict[str, Any]]:
        """Resolve pending awaiters after a confirmed-dead lifecycle close.

        The report lookup is inside the same SQLite transaction as the
        close-result write. If a terminal report was durably ingested first it
        wins; otherwise the one await row becomes `closed_without_report`.
        """
        def _op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            generation = session_generation
            if generation is None:
                generation, _terminal = _awaiter_session_state_conn(conn, stream_id)
            rows = conn.execute(
                "SELECT * FROM v2_awaiters WHERE stream_id=? AND outcome='pending' "
                "AND session_generation IN (?, '') ORDER BY awaiter_id",
                (stream_id, generation),
            ).fetchall()
            outcomes: list[dict[str, Any]] = []
            now = iso_now()
            for row in rows:
                target_msg_id = int(row["target_msg_id"])
                report = _report_for_awaiter_conn(conn, stream_id, target_msg_id)
                if report is not None:
                    _write_awaiter_report_conn(
                        conn, stream_id, str(row["session_generation"] or generation),
                        target_msg_id, report, now,
                    )
                else:
                    conn.execute(
                        "UPDATE v2_awaiters SET outcome='closed_without_report', "
                        "reason=?, resolved_at=? WHERE awaiter_id=? AND outcome='pending'",
                        (reason, now, row["awaiter_id"]),
                    )
                updated = conn.execute(
                    "SELECT * FROM v2_awaiters WHERE awaiter_id=?",
                    (row["awaiter_id"],),
                ).fetchone()
                outcomes.append(_awaiter_row(conn, updated))
            conn.commit()
            return outcomes

        return await self.submit(_op)

    async def resolve_closed_awaiters(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Re-drive pending rows whose child is durably terminal/dead.

        An explicitly open session always wins over stale reap metadata, which
        is the live-worker exclusion. The join also lets a restart recover a
        closed row before an in-memory callback can run.
        """
        cap = max(1, int(limit))

        def _op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT a.* FROM v2_awaiters a "
                "LEFT JOIN sessions s ON s.host || ':' || s.session_name = a.stream_id "
                "LEFT JOIN session_reap r ON r.stream_id = a.stream_id "
                "LEFT JOIN v2_session_generations g "
                "ON g.host=s.host AND g.session_name=s.session_name "
                "WHERE a.outcome='pending' "
                "AND ("
                "  (s.status IS NOT NULL AND s.status <> 'open' "
                "   AND a.session_generation IN (COALESCE(g.generation, ''), '')) "
                "  OR (s.status IS NULL AND r.reap_status='reaped')"
                ") ORDER BY a.awaiter_id LIMIT ?",
                (cap,),
            ).fetchall()
            outcomes: list[dict[str, Any]] = []
            now = iso_now()
            for row in rows:
                target_msg_id = int(row["target_msg_id"])
                report = _report_for_awaiter_conn(conn, row["stream_id"], target_msg_id)
                if report is not None:
                    _write_awaiter_report_conn(
                        conn, row["stream_id"], row["session_generation"],
                        target_msg_id, report, now,
                    )
                else:
                    conn.execute(
                        "UPDATE v2_awaiters SET outcome='closed_without_report', "
                        "reason='terminal_session_without_report', resolved_at=? "
                        "WHERE awaiter_id=? AND outcome='pending'",
                        (now, row["awaiter_id"]),
                    )
                updated = conn.execute(
                    "SELECT * FROM v2_awaiters WHERE awaiter_id=?",
                    (row["awaiter_id"],),
                ).fetchone()
                outcomes.append(_awaiter_row(conn, updated))
            conn.commit()
            return outcomes

        return await self.submit(_op)

    async def find_report(
        self, from_stream_id: str, msg_id: int | None = None, *, statuses: tuple[str, ...] = (), session_generation: str | None = None
    ) -> dict[str, Any] | None:
        """The report an `await` is asking for: newest row for the stream,
        narrowed to one `msg_id` when the caller named one. `statuses` filters
        to terminal statuses so a `progress` report never settles an await."""

        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            sql = "SELECT * FROM v2_reports WHERE from_stream_id=?"
            args: list[Any] = [from_stream_id]
            if session_generation is not None:
                sql += " AND session_generation=?"; args.append(session_generation)
            if msg_id is not None:
                sql += " AND msg_id=?"
                args.append(msg_id)
            if statuses:
                sql += f" AND status IN ({','.join('?' * len(statuses))})"
                args.extend(statuses)
            sql += " ORDER BY ledger_row_id DESC LIMIT 1"
            return _report_row(conn.execute(sql, args).fetchone())

        return await self.submit(_op)

    async def record_report_rejection(
        self, stream_id: str, *, session_generation: str, status: str, reason: str,
    ) -> None:
        """Durably record a REJECTED terminal-report attempt (pop2). Keyed to the
        seat's current generation so a resumed seat is never reaped on a stale
        rejection. Append-only; the sweep reads the newest."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO v2_report_rejections "
                "(stream_id, session_generation, status, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (stream_id, session_generation, status, reason, time.time()),
            )
            conn.commit()

        await self.submit(_op)

    async def find_report_rejection(
        self, stream_id: str, *, session_generation: str | None = None,
        statuses: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        """Newest recorded terminal-report rejection for the stream, mirroring
        `find_report`: generation- and status-scoped so the self-close sweep
        matches only a rejection of the row's CURRENT generation."""

        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            sql = (
                "SELECT stream_id, session_generation, status, reason, created_at "
                "FROM v2_report_rejections WHERE stream_id=?"
            )
            args: list[Any] = [stream_id]
            if session_generation is not None:
                sql += " AND session_generation=?"; args.append(session_generation)
            if statuses:
                sql += f" AND status IN ({','.join('?' * len(statuses))})"
                args.extend(statuses)
            sql += " ORDER BY rejection_id DESC LIMIT 1"
            row = conn.execute(sql, args).fetchone()
            if row is None:
                return None
            return {
                "stream_id": row[0], "session_generation": row[1],
                "status": row[2], "reason": row[3], "created_at": row[4],
            }

        return await self.submit(_op)

    async def get_report(self, report_id: str) -> dict[str, Any] | None:
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            return _report_row(conn.execute(
                "SELECT * FROM v2_reports WHERE report_id=?", (report_id,)
            ).fetchone())

        return await self.submit(_op)

    # -- grant_token / register_push (UI-sent verbs) -----------------------

    async def grant_stream_token(self, host: str, session_name: str, token_hash: str, version: str) -> str:
        """Bootstrap-once, race-safe (v1 `set_token_hash_if_unset`): set the hash
        only on an OPEN row that has none. Returns `ok` / `stream_unknown` /
        `token_already_set` so the caller maps v1's grant_token vocabulary."""

        def _op(conn: sqlite3.Connection) -> str:
            row = conn.execute(
                "SELECT token_hash FROM sessions WHERE host=? AND session_name=? AND status='open'",
                (host, session_name),
            ).fetchone()
            if row is None:
                return "stream_unknown"
            if row[0]:
                return "token_already_set"
            cur = conn.execute(
                "UPDATE sessions SET token_hash=?, token_hash_version=? WHERE host=? AND session_name=?"
                " AND status='open' AND (token_hash IS NULL OR token_hash='')",
                (token_hash, version, host, session_name),
            )
            conn.commit()
            return "ok" if cur.rowcount == 1 else "token_already_set"

        return await self.submit(_op)

    async def stream_id_for_token_hash(self, token_hash: str) -> str | None:
        """The OPEN stream whose granted token hashes to `token_hash`, or None.

        The daemon's proof of a report/close caller's authenticated identity: a
        presented `stream_token` is hashed and matched here, so a spoofed
        `from_stream_id` can be rejected against the token-owner. Empty hash
        never matches (a tokenless caller has no authenticated identity)."""
        if not token_hash:
            return None

        def _op(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT host, session_name FROM sessions"
                " WHERE token_hash=? AND token_hash_version=? AND status='open'",
                (token_hash, STREAM_TOKEN_HASH_VERSION),
            ).fetchone()
            return f"{row[0]}:{row[1]}" if row is not None else None

        return await self.submit(_op)

    async def stream_token_state(self, token_hash: str) -> dict[str, str] | None:
        """Return the lifecycle state for a token hash without exposing it.

        Verification telemetry needs to distinguish a token that belonged to a
        closed seat (expired) from a token that belongs to another open seat
        (wrong-seat). The hash itself never leaves this store method.
        """
        if not token_hash:
            return None

        def _op(conn: sqlite3.Connection) -> dict[str, str] | None:
            row = conn.execute(
                """SELECT host, session_name, status, token_hash_version
                   FROM sessions
                   WHERE token_hash=?
                   ORDER BY CASE WHEN status='open' THEN 0 ELSE 1 END, rowid DESC
                   LIMIT 1""",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            return {
                "stream_id": f"{row['host']}:{row['session_name']}",
                "status": str(row["status"] or ""),
                "token_hash_version": str(row["token_hash_version"] or ""),
            }

        return await self.submit(_op)

    # -- helpers -----------------------------------------------------------

    # -- session_event_tail: chat transcript events (B13 ingest + backfill) --

    async def fetch_open_session_lifecycle(
        self,
        stream_id: str,
        *,
        pane_pid: str,
    ) -> dict[str, str] | None:
        """Return the current open lifecycle proven by one observed pane PID.

        This is an admission snapshot only.  Callers still must use
        :meth:`append_session_events_lifecycle_cas`, whose transaction repeats
        every predicate before writing; a snapshot alone is never authority to
        append after a close/reopen or pane replacement.
        """
        host, separator, session_name = str(stream_id or "").partition(":")
        expected_pid = str(pane_pid or "").strip()
        if not separator or not host or not session_name or not expected_pid:
            return None

        def _op(conn: sqlite3.Connection) -> dict[str, str] | None:
            row = conn.execute(
                """SELECT s.created_at AS session_created_at, g.generation, s.pane_pid
                   FROM sessions s
                   JOIN v2_session_generations g
                     ON g.host=s.host AND g.session_name=s.session_name
                   WHERE s.host=? AND s.session_name=? AND s.status='open'
                     AND s.pane_pid IS NOT NULL
                     AND CAST(s.pane_pid AS TEXT)=?
                """,
                (host, session_name, expected_pid),
            ).fetchone()
            if row is None:
                return None
            lifecycle = str(row["session_created_at"] or "").strip()
            generation = str(row["generation"] or "").strip()
            actual_pid = str(row["pane_pid"] or "").strip()
            if not lifecycle or not generation or not actual_pid:
                return None
            return {
                "pane_pid": actual_pid,
                "session_created_at": lifecycle,
                "generation": generation,
            }

        return await self.submit(_op)

    async def append_session_events_lifecycle_cas(
        self,
        entries: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[int | None | _EntryDropped] | None:
        """Append one submitted batch, fencing each lifecycle-bound row.

        An entry may carry ``lifecycle`` with the previously admitted
        ``pane_pid``, ``session_created_at``, and ``generation``, or a
        ``claude_binding`` fenced against an open row.  Each such append
        repeats its predicate inside this one transaction.  A miss is that
        ONE entry's verdict: it yields :data:`ENTRY_DROPPED` and the rest of
        the batch still commits, because a batch spans every session on a
        host and one stale pane must never stop the others from ingesting.
        Every predicate for an entry is evaluated before any write for it, so
        a drop leaves nothing partial behind.  Storage faults still fail the
        whole batch closed.  Entries without a lifecycle or binding retain the
        existing non-Codex ingest behaviour.
        """
        if limit <= 0:
            return None
        prepared: list[tuple[str, str, str, str | None, float, dict[str, str] | None, str | None]] = []
        for entry in entries:
            stream_id = str(entry.get("stream_id") or "")
            event = entry.get("event")
            if not stream_id or not isinstance(event, dict):
                return None
            event_json = json.dumps(event, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
            event_key = hashlib.sha256(event_json.encode("utf-8")).hexdigest()
            identity = entry.get("identity")
            lifecycle = entry.get("lifecycle")
            if lifecycle is not None:
                if not isinstance(lifecycle, dict):
                    return None
                lifecycle = {
                    "pane_pid": str(lifecycle.get("pane_pid") or "").strip(),
                    "session_created_at": str(lifecycle.get("session_created_at") or "").strip(),
                    "generation": str(lifecycle.get("generation") or "").strip(),
                }
                if not all(lifecycle.values()):
                    return None
            prepared.append((
                stream_id,
                event_json,
                event_key,
                None if identity is None else str(identity),
                time.time(),
                lifecycle,
                str(entry.get("claude_binding") or "").strip() or None,
            ))
        if not prepared:
            return []

        def _op(conn: sqlite3.Connection) -> list[int | None | _EntryDropped] | None:
            conn.execute("BEGIN")
            try:
                inserted: list[int | None | _EntryDropped] = []
                for stream_id, event_json, event_key, identity, recorded_at, lifecycle, binding in prepared:
                    host, separator, session_name = stream_id.partition(":")
                    rebind: tuple[str, str] | None = None
                    if binding:
                        row = conn.execute(
                            "SELECT claude_session_id, claude_session_lineage FROM sessions WHERE host=? AND session_name=? AND status='open'",
                            (host, session_name),
                        ).fetchone()
                        if row is None:
                            inserted.append(ENTRY_DROPPED)
                            continue
                        current = str(row["claude_session_id"] or "")
                        lineage = str(row["claude_session_lineage"] or "").split(",") if row["claude_session_lineage"] else []
                        if binding != current:
                            if binding in lineage:
                                inserted.append(None)
                                continue
                            # Deferred until every predicate below has passed:
                            # a drop must not leave this rebind committed.
                            rebind = (binding, ",".join(dict.fromkeys([*lineage, current])))
                    if lifecycle is not None:
                        if not separator or not host or not session_name:
                            inserted.append(ENTRY_DROPPED)
                            continue
                        current = conn.execute(
                            """SELECT s.created_at
                               FROM sessions s
                               JOIN v2_session_generations g
                                 ON g.host=s.host AND g.session_name=s.session_name
                               WHERE s.host=? AND s.session_name=? AND s.status='open'
                                 AND s.pane_pid IS NOT NULL
                                 AND CAST(s.pane_pid AS TEXT)=?
                                 AND s.created_at=? AND g.generation=?
                            """,
                            (
                                host, session_name, lifecycle["pane_pid"],
                                lifecycle["session_created_at"], lifecycle["generation"],
                            ),
                        ).fetchone()
                        if current is None:
                            inserted.append(ENTRY_DROPPED)
                            continue
                        session_created_at = lifecycle["session_created_at"]
                    else:
                        session_created_at = ""
                        if separator:
                            row = conn.execute(
                                "SELECT created_at FROM sessions WHERE host=? AND session_name=?",
                                (host, session_name),
                            ).fetchone()
                            session_created_at = str(row["created_at"] or "") if row else ""
                    if rebind is not None:
                        conn.execute(
                            "UPDATE sessions SET claude_session_id=?, claude_session_lineage=? WHERE host=? AND session_name=?",
                            (*rebind, host, session_name),
                        )
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO session_event_tail(
                            stream_id, session_created_at, event_key, event_json, event_ts,
                            recorded_at, identity
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            stream_id, session_created_at, event_key, event_json,
                            json.loads(event_json).get("timestamp"), recorded_at, identity,
                        ),
                    )
                    inserted.append(int(cur.lastrowid) if cur.rowcount > 0 else None)
                conn.commit()
                return inserted
            except BaseException:
                conn.rollback()
                raise

        return await self.submit(_op)

    # -- durable send receipts -------------------------------------------

    async def append_send_receipt(
        self,
        *,
        to_stream_id: str,
        request_id: str,
        receipt_id: str,
        state: str,
        wire_text: str,
        display_text: str,
        attachments: list[dict[str, Any]],
        delivery: str,
        submission_confirmed: bool,
        optimistic_id: str | None = None,
        reason: str | None = None,
        attempts: int | None = None,
        content_kind: str | None = None,
        created_at: str | None = None,
        from_stream_id: str | None = None,
        actor_stream_id: str | None = None,
        actor_trusted: bool = False,
    ) -> dict[str, Any]:
        """Append one immutable send receipt and return its safe wire shape.

        This intentionally has no upsert, uniqueness constraint, or payload
        comparison. A same-key retry is another physical attempt; the one-row
        reader below gives clients its highest-rowid projection.
        """
        if state not in {"accepted", "landed", "not_landed"}:
            raise ValueError("invalid_send_receipt_state")
        target = str(to_stream_id or "").strip()
        request = str(request_id or "").strip()
        receipt = str(receipt_id or "").strip()
        if not target or not request or not receipt:
            raise ValueError("invalid_send_receipt_key")
        normalized_attachments = [dict(item) for item in attachments if isinstance(item, dict)]
        kind = content_kind or (
            "image_and_text" if normalized_attachments and display_text else
            "attachment_only" if normalized_attachments else "text"
        )
        encoded_attachments = json.dumps(
            normalized_attachments, separators=(",", ":"), sort_keys=True,
        )
        timestamp = created_at or iso_now()

        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            cur = conn.execute(
                """INSERT INTO v2_send_receipts (
                    to_stream_id, request_id, receipt_id, state, optimistic_id,
                    wire_digest, display_text, content_kind, attachments_json,
                    delivery, submission_confirmed, reason, attempts, created_at,
                    from_stream_id, actor_stream_id, actor_trusted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    target, request, receipt, state, optimistic_id or None,
                    # A peer send is pasted with a `[from …]` envelope but the
                    # normalizer stores only the header-stripped payload as the
                    # event text; hash the stripped basis so both the operator
                    # (unstamped) and peer (stamped→TELL) events correlate here.
                    _send_wire_digest(strip_peer_delivery_envelope(str(wire_text or ""))),
                    str(display_text or ""), str(kind),
                    encoded_attachments, str(delivery or state),
                    1 if submission_confirmed else 0, reason or None, attempts, timestamp,
                    _safe_provenance_id(from_stream_id),
                    _safe_provenance_id(actor_stream_id),
                    1 if actor_trusted else 0,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT rowid AS receipt_rowid, * FROM v2_send_receipts WHERE rowid=?",
                (int(cur.lastrowid),),
            ).fetchone()
            assert row is not None
            return _send_receipt_row(row, include_rowid=True)

        return await self.submit(_op)

    async def get_send_receipt(self, to_stream_id: str, request_id: str) -> dict[str, Any] | None:
        """Return only the highest-rowid receipt for one lookup key."""
        target = str(to_stream_id or "").strip()
        request = str(request_id or "").strip()
        if not target or not request:
            return None

        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                """SELECT rowid AS receipt_rowid, * FROM v2_send_receipts
                   WHERE to_stream_id=? AND request_id=?
                   ORDER BY rowid DESC LIMIT 1""",
                (target, request),
            ).fetchone()
            return _send_receipt_row(row) if row is not None else None

        return await self.submit(_op)

    async def stamp_event_with_send_receipt(self, event: dict[str, Any]) -> dict[str, Any]:
        """Stamp one existing USER or peer-TELL event from its send-receipt projection.

        The event remains on the established ingest/broadcast route. This is a
        read at event acceptance time, not an observation row or a receipt push.
        A peer send now normalizes as TELL, and its receipt still carries the
        clean display_text/attachments; correlate it too so the card never shows
        the raw staged attachment path.
        """
        payload = dict(event)
        kind = str(payload.get("kind") or "")
        stream_id = str(payload.get("stream_id") or "").strip()
        text = str(payload.get("text") or "")
        request_id = str(payload.get("request_id") or "").strip()
        # A peer delivery (tell/send) carries its receipt identity as the
        # envelope anchor (raw.tell_id). Peer traffic correlates ONLY by that
        # anchor; the text-digest fallback below must never see it, or two
        # distinct peer sends with identical text would collapse onto one
        # receipt (re-QA reject #2, 2026-09-07).
        raw = payload.get("raw")
        peer_anchor = (
            str(raw.get("tell_id") or "").strip()
            if kind == "TELL" and isinstance(raw, dict) else ""
        )
        if kind not in ("USER", "TELL") or not stream_id or not text:
            return payload

        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            # Peer deliveries correlate solely by their envelope anchor, never
            # by text digest; an anchorless peer TELL correlates to nothing.
            if kind == "TELL":
                if not peer_anchor:
                    return None
                anchored = conn.execute(
                    """SELECT rowid AS receipt_rowid, * FROM v2_send_receipts
                       WHERE to_stream_id=? AND request_id=?
                       ORDER BY rowid DESC LIMIT 1""",
                    (stream_id, peer_anchor),
                ).fetchone()
                if (
                    anchored is not None
                    and str(anchored["wire_digest"] or "") == _send_wire_digest(text)
                ):
                    return _send_receipt_row(
                        anchored, include_attachments=True, include_display_text=True,
                    )
                return None

            # An event already stamped with a client-minted request ID must
            # stay bound to that durable attempt when it is replayed.
            if _SEND_REQUEST_ID_RE.fullmatch(request_id):
                exact = conn.execute(
                    """SELECT rowid AS receipt_rowid, * FROM v2_send_receipts
                       WHERE to_stream_id=? AND request_id=?
                       ORDER BY rowid DESC LIMIT 1""",
                    (stream_id, request_id),
                ).fetchone()
                if (
                    exact is not None
                    and str(exact["wire_digest"] or "") == _send_wire_digest(text)
                ):
                    return _send_receipt_row(
                        exact, include_attachments=True, include_display_text=True,
                    )
                return None

            # Legacy events without a valid client request key can only use the
            # prior text-digest projection, preserving compatibility.
            rows = conn.execute(
                """SELECT r.rowid AS receipt_rowid, r.*
                   FROM v2_send_receipts AS r
                   JOIN (
                       SELECT request_id, MAX(rowid) AS receipt_rowid
                       FROM v2_send_receipts
                       WHERE to_stream_id=?
                       GROUP BY request_id
                   ) AS current ON current.receipt_rowid=r.rowid
                   ORDER BY r.rowid DESC""",
                (stream_id,),
            ).fetchall()
            for row in rows:
                if str(row["wire_digest"] or "") and str(row["wire_digest"]) == _send_wire_digest(text):
                    return _send_receipt_row(
                        row, include_attachments=True, include_display_text=True,
                    )
            return None

        receipt = await self.submit(_op)
        if receipt is None:
            return payload
        # The transcript's USER record includes the literal staged attachment
        # path used for injection.  Its raw identity was captured by the caller
        # before this projection, so replacing that presentation text preserves
        # dedupe while preventing the path from entering public chat history.
        display_text = str(receipt.pop("display_text", ""))
        payload["text"] = display_text
        # A peer TELL renders from raw.peer_payload; restore the clean display
        # text there too so an attachment send's staged path never surfaces.
        if kind == "TELL" and isinstance(payload.get("raw"), dict):
            payload["raw"] = {**payload["raw"], "peer_payload": display_text}
        payload.update({
            "receipt_id": receipt["receipt_id"],
            "request_id": receipt["request_id"],
            "receipt_state": receipt["state"],
            "receipt_delivery": receipt["delivery"],
            "attachment_count": receipt["attachment_count"],
        })
        if receipt.get("optimistic_id"):
            payload["optimistic_id"] = receipt["optimistic_id"]
        attachments = receipt.pop("attachments", [])
        if attachments:
            payload["attachments"] = attachments
        return payload

    async def append_session_event(
        self, stream_id: str, event: dict[str, Any], *, identity: str | None, limit: int,
    ) -> int | None:
        """Durably record one normalized transcript event; return its `event_id`
        (the durable per-daemon `daemon_seq`) iff it was newly inserted, else None
        (a same-event/same-identity durable replay). The truthy id lets the caller
        stamp the live `chat.event` broadcast with the SAME value the backfill
        (`fetch_session_event_tail`) later serves for that row — the identity the
        shared chat-core reducer's dedupe needs across a fetch/live overlap.

        Simplified lift of v1's `append_session_event`: the INSERT OR IGNORE keyed
        on `event_key` (sha256 of the canonical JSON) AND the `identity` unique
        index is the exactly-once floor B13 needs — a replay-from-start after a
        rediscovery re-appends nothing. v1's suppression_frontier / send-correlation
        machinery is NOT lifted (D3 removed in v2). The caller broadcasts
        `chat.event` iff this returns True, so a duplicate never re-broadcasts."""
        if not stream_id or limit <= 0:
            return False
        event_json = json.dumps(event, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
        event_key = hashlib.sha256(event_json.encode("utf-8")).hexdigest()
        recorded_at = time.time()

        def _op(conn: sqlite3.Connection) -> int | None:
            session_created_at = ""
            if ":" in stream_id:
                host, session_name = stream_id.split(":", 1)
                row = conn.execute(
                    "SELECT created_at FROM sessions WHERE host = ? AND session_name = ?",
                    (host, session_name),
                ).fetchone()
                session_created_at = str(row["created_at"] or "") if row else ""
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO session_event_tail(
                    stream_id, session_created_at, event_key, event_json, event_ts,
                    recorded_at, identity
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (stream_id, session_created_at, event_key, event_json,
                 event.get("timestamp"), recorded_at, identity),
            )
            conn.commit()
            # lastrowid is the AUTOINCREMENT event_id only on a real insert; an
            # IGNORE'd duplicate leaves it stale, so gate on rowcount.
            return int(cur.lastrowid) if cur.rowcount > 0 else None

        return await self.submit(_op)

    async def fetch_session_event_page(
        self,
        stream_id: str,
        *,
        before_daemon_seq: int | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Fetch one newest-first cursor page, returned oldest-first.

        The SQL limit is the page bound, not the requested history length. A
        caller can therefore walk a permanently large tail without loading the
        whole stream into one Python list or frame.
        """
        if not stream_id or limit <= 0:
            return []

        host, separator, session_name = str(stream_id or "").partition(":")
        if not separator or not host or not session_name:
            return []

        def _op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            where = "t.stream_id = ? AND t.session_created_at = s.created_at"
            params: list[Any] = [host, session_name, stream_id]
            if before_daemon_seq is not None:
                where += " AND t.event_id < ?"
                params.append(int(before_daemon_seq))
            params.append(int(limit))
            rows = conn.execute(
                f"""
                SELECT t.event_id, t.event_json FROM session_event_tail t
                JOIN sessions s ON s.host=? AND s.session_name=?
                WHERE {where}
                ORDER BY t.event_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            newest_first: list[dict[str, Any]] = []
            for row in rows:
                try:
                    event = json.loads(row["event_json"])
                except (TypeError, ValueError):
                    continue
                if isinstance(event, dict):
                    # Stamp the durable per-daemon `daemon_seq` the reducer keys on.
                    # event_id is NOT persisted inside event_json (it's the row id,
                    # assigned post-serialize), so the same row carries the same
                    # value here as on its live broadcast (append_session_event).
                    event["daemon_seq"] = int(row["event_id"])
                    newest_first.append(event)
            return list(reversed(newest_first))

        return await self.submit(_op)

    async def fetch_session_event_tail(self, stream_id: str, *, limit: int) -> list[dict[str, Any]]:
        """The newest `limit` events for a stream, returned oldest-first."""
        return await self.fetch_session_event_page(
            stream_id, before_daemon_seq=None, limit=limit,
        )

    async def list_open_sessions_with_event_summary(self) -> list[dict[str, Any]]:
        """Open rows plus lifecycle-matched event state from one DB snapshot."""

        def _op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                """
                SELECT s.*,
                       EXISTS (
                           SELECT 1 FROM session_event_tail t
                           WHERE t.stream_id = s.host || ':' || s.session_name
                             AND t.session_created_at = s.created_at
                       ) AS _bootstrap_event_seen,
                       COALESCE((
                           SELECT t.event_ts FROM session_event_tail t
                           WHERE t.stream_id = s.host || ':' || s.session_name
                             AND t.session_created_at = s.created_at
                             AND julianday(t.event_ts) IS NOT NULL
                           ORDER BY julianday(t.event_ts) DESC, t.event_id DESC
                           LIMIT 1
                       ), '') AS last_event_at
                FROM sessions s
                WHERE s.status = 'open'
                """
            ).fetchall()
            return [_session_row(conn, row) for row in rows]

        return await self.submit(_op)

    async def bind_observer_transcript(
        self, stream_id: str, *, generation: str, pane_pid: str,
        expected: dict[str, Any], transcript: dict[str, Any],
    ) -> bool:
        """Retain the first proven file under the same open provider birth."""
        host, _, name = stream_id.partition(":")
        def op(conn: sqlite3.Connection) -> bool:
            row = _session_row(conn, conn.execute(
                "SELECT * FROM sessions WHERE host=? AND session_name=? AND status='open'",
                (host, name),
            ).fetchone())
            if not row or row.get("session_generation") != generation or str(row.get("pane_pid") or "") != pane_pid:
                return False
            binding = row.get("observer_binding")
            if not isinstance(binding, dict) or binding != expected or binding.get("generation") != generation:
                return False
            prior = binding.get("transcript")
            if prior is not None and prior != transcript:
                return False
            conn.execute("UPDATE sessions SET observer_binding=? WHERE host=? AND session_name=?",
                         (json.dumps({**binding, "transcript": transcript}, separators=(",", ":")), host, name))
            conn.commit()
            return True
        return await self.submit(op)

    async def count_session_events(
        self,
        stream_id: str,
        *,
        kind: str,
        minimum: int | None = None,
        session_created_at: str | None = None,
        provider: str | None = None,
        exclude_sidechain: bool = False,
        recorded_before: float | None = None,
        operator_only: bool = False,
    ) -> int:
        """Count matching normalized events without crossing lifecycle bounds.

        ``session_created_at`` is the durable v2 lifecycle boundary copied onto
        each tail row. Callers that need lifecycle-scoped evidence must provide
        it; an empty supplied boundary fails closed because legacy rows without
        a boundary cannot be proven to belong to the live generation. Optional
        provider and sidechain filters are applied to the normalized JSON.
        When ``recorded_before`` is supplied, only rows durably recorded at or
        before that wall-clock cutoff are considered.
        When ``minimum`` is supplied, stop after reaching that threshold while
        iterating the cursor rather than materializing the complete tail.
        """
        if not stream_id or not kind:
            return 0
        threshold = None if minimum is None else max(0, int(minimum))
        if threshold == 0:
            return 0
        lifecycle = None if session_created_at is None else str(session_created_at or "").strip()
        if lifecycle == "":
            return 0
        provider_name = None if provider is None else str(provider or "").strip().lower()
        if provider is not None and not provider_name:
            return 0
        host, separator, session_name = str(stream_id or "").partition(":")
        if not separator or not host or not session_name:
            return 0

        def _op(conn: sqlite3.Connection) -> int:
            clauses = ["t.stream_id = ?", "t.session_created_at = s.created_at"]
            params: list[Any] = [host, session_name, stream_id]
            if lifecycle is not None:
                clauses.append("t.session_created_at = ?")
                params.append(lifecycle)
            if recorded_before is not None:
                clauses.append("t.recorded_at <= ?")
                params.append(float(recorded_before))
            rows = conn.execute(
                "SELECT t.event_json FROM session_event_tail t "
                "JOIN sessions s ON s.host=? AND s.session_name=? WHERE "
                + " AND ".join(clauses)
                + " ORDER BY t.event_id DESC",
                params,
            )
            count = 0
            for row in rows:
                try:
                    event = json.loads(row["event_json"])
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, dict) or str(event.get("kind") or "") != kind:
                    continue
                if provider_name is not None and str(event.get("provider") or "").lower() != provider_name:
                    continue
                if exclude_sidechain:
                    raw = event.get("raw")
                    if isinstance(raw, dict) and bool(raw.get("is_sidechain", raw.get("isSidechain", False))):
                        continue
                if operator_only:
                    from sessions import operator_user_epoch
                    if operator_user_epoch(event, lifecycle) is None:
                        continue
                count += 1
                if threshold is not None and count >= threshold:
                    return count
            return count

        return await self.submit(_op)

    def _known(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Drop keys that are not real `sessions` columns — callers pass
        presentation-only fields such as `stream_id` alongside durable columns."""
        return {k: v for k, v in fields.items() if k in self._columns and k not in ("host", "session_name")}


def _enc_report(column: str, value: Any) -> Any:
    if value is None:
        return None
    return json.dumps(value, separators=(",", ":")) if column in REPORT_JSON_COLUMNS else value


def _decode_tell_envelope(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Decode a durable tell envelope without changing its state."""
    if row is None:
        return None
    try:
        payload = json.loads(row["reply"])
    except (TypeError, ValueError, KeyError):
        return None
    if not isinstance(payload, dict):
        return None
    payload.setdefault("ledger_row_id", int(row["ledger_row_id"]))
    payload.setdefault("created_at", row["created_at"])
    return payload


def _report_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out.pop("exchange_json", None)
    for col in REPORT_JSON_COLUMNS:
        raw = out.get(col)
        out[col] = json.loads(raw) if isinstance(raw, str) and raw else None
    return out


def _report_replay_matches(stored: dict[str, Any], incoming: dict[str, Any]) -> bool:
    recorded_hash = str(stored.get("request_payload_hash") or "")
    incoming_hash = str(incoming.get("request_payload_hash") or "")
    if recorded_hash and incoming_hash:
        return recorded_hash == incoming_hash
    return all(stored.get(column) == incoming.get(column) for column in REPORT_IDENTITY_COLUMNS)


def _session_for_stream_conn(conn: sqlite3.Connection, stream_id: str) -> dict[str, Any] | None:
    host, separator, session_name = str(stream_id or "").partition(":")
    if not separator or not host or not session_name:
        return None
    return _session_row(conn, conn.execute(
        "SELECT * FROM sessions WHERE host=? AND session_name=?", (host, session_name)
    ).fetchone())


def _attestation_spec_identities(
    session: dict[str, Any] | None,
    canonical_spec_identity: Callable[[str | None], str | None] | None,
) -> set[str]:
    """Return only resolver-proven canonical IDs for QA attestation.

    Stored raw spellings remain readable for historical compatibility, but no
    raw value may authorize READY when catalog resolution is unavailable,
    ambiguous, missing, or errors.
    """
    if not callable(canonical_spec_identity):
        return set()
    source = (session or {}).get("qualified_spec_ids") or (session or {}).get("spec_ids")
    identities: set[str] = set()
    for spec_id in normalize_spec_ids(source, (session or {}).get("spec_id")):
        try:
            resolved = canonical_spec_identity(spec_id)
        except Exception:
            continue
        identity = str(resolved or "").strip()
        if identity:
            identities.add(identity)
    return identities


def _qa_attestation_validation_conn(
    conn: sqlite3.Connection,
    *,
    reporting_stream_id: str,
    completion_kind: object,
    attestation: object,
    mode: str,
    canonical_spec_identity: Callable[[str | None], str | None] | None = None,
) -> dict[str, Any] | None:
    """Derive READY authority only from the serialized v2 durable ledger."""
    if completion_kind != "implementation_ready" or mode == "off":
        return None

    qa_stream_id = attestation.get("stream_id") if isinstance(attestation, dict) else None
    qa_report_id = attestation.get("report_id") if isinstance(attestation, dict) else None
    reasons: list[str] = []
    matched_spec_ids: list[str] = []
    if attestation is None:
        reasons.append("missing_attestation")
    else:
        if qa_stream_id == reporting_stream_id:
            reasons.append("self_reference")
        reporter = _session_for_stream_conn(conn, reporting_stream_id)
        qa_session = _session_for_stream_conn(conn, str(qa_stream_id or ""))
        if qa_session is None:
            reasons.append("unknown_qa_stream")
        reporter_specs = _attestation_spec_identities(reporter, canonical_spec_identity)
        qa_specs = _attestation_spec_identities(qa_session, canonical_spec_identity)
        matched_spec_ids = sorted(reporter_specs & qa_specs)
        if not matched_spec_ids:
            reasons.append("spec_binding_mismatch")
        qa_report = _report_row(conn.execute(
            "SELECT * FROM v2_reports WHERE report_id=?", (qa_report_id,)
        ).fetchone()) if qa_report_id else None
        if qa_report is None:
            reasons.append("unknown_qa_report")
        else:
            if qa_report.get("from_stream_id") != qa_stream_id:
                reasons.append("qa_report_stream_mismatch")
            if qa_report.get("status") != "done":
                reasons.append("qa_report_not_done")
            if qa_report.get("qa_verdict") != "accept":
                reasons.append("qa_verdict_not_accept")

    ordered_reasons = [reason for reason in QA_ATTESTATION_REASON_ORDER if reason in reasons]
    return {
        "state": "verified" if not ordered_reasons else "unverified",
        "reasons": ordered_reasons,
        "matched_spec_ids": matched_spec_ids,
        "qa_stream_id": qa_stream_id,
        "qa_report_id": qa_report_id,
    }


def _awaiter_target_msg_id(msg_id: int | None) -> int:
    """Use a non-NULL key for the stream-only await form."""
    return -1 if msg_id is None else int(msg_id)


def _awaiter_session_state_conn(
    conn: sqlite3.Connection, stream_id: str,
) -> tuple[str, bool]:
    """Return `(current_generation, terminal)` for one target stream."""
    host, separator, session_name = str(stream_id or "").partition(":")
    if not separator or not host or not session_name:
        return "", False
    row = conn.execute(
        "SELECT s.status, COALESCE(g.generation, '') AS generation "
        "FROM sessions s LEFT JOIN v2_session_generations g "
        "ON g.host=s.host AND g.session_name=s.session_name "
        "WHERE s.host=? AND s.session_name=?",
        (host, session_name),
    ).fetchone()
    if row is not None:
        return str(row["generation"] or ""), str(row["status"] or "") != "open"
    reap = conn.execute(
        "SELECT reap_status FROM session_reap WHERE stream_id=?",
        (stream_id,),
    ).fetchone()
    return "", reap is not None and str(reap["reap_status"] or "") == "reaped"


def _report_for_awaiter_conn(
    conn: sqlite3.Connection, stream_id: str, target_msg_id: int,
) -> dict[str, Any] | None:
    sql = "SELECT * FROM v2_reports WHERE from_stream_id=? "
    args: list[Any] = [stream_id]
    if target_msg_id >= 0:
        sql += "AND msg_id=? "
        args.append(target_msg_id)
    sql += "AND status IN ('done','error','aborted') "
    sql += "ORDER BY ledger_row_id DESC LIMIT 1"
    return _report_row(conn.execute(sql, args).fetchone())


def _write_awaiter_report_conn(
    conn: sqlite3.Connection,
    stream_id: str,
    session_generation: str,
    target_msg_id: int,
    report: dict[str, Any],
    resolved_at: str,
) -> None:
    conn.execute(
        "INSERT INTO v2_awaiters "
        "(stream_id,session_generation,target_msg_id,outcome,report_id,ledger_row_id,reason,requested_at,resolved_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(stream_id,session_generation,target_msg_id) DO UPDATE SET "
        "outcome='report',report_id=excluded.report_id,ledger_row_id=excluded.ledger_row_id, "
        "reason='report_ingested',resolved_at=excluded.resolved_at",
        (
            stream_id, session_generation, target_msg_id, "report",
            report["report_id"], report["ledger_row_id"], "report_ingested",
            resolved_at, resolved_at,
        ),
    )


def _resolve_awaiters_for_report_conn(
    conn: sqlite3.Connection, report: dict[str, Any],
) -> None:
    """Promote pending or lower-trust close outcomes to one real report."""
    conn.execute(
        "UPDATE v2_awaiters SET outcome='report', report_id=?, ledger_row_id=?, "
        "reason='report_ingested', resolved_at=? "
        "WHERE stream_id=? AND outcome IN ('pending','closed_without_report') "
        "AND (target_msg_id=? OR target_msg_id=-1)",
        (
            report["report_id"], report["ledger_row_id"], iso_now(),
            report["from_stream_id"], report["msg_id"],
        ),
    )


def _awaiter_row(conn: sqlite3.Connection, row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    target_msg_id = int(out.get("target_msg_id", -1))
    out["msg_id"] = None if target_msg_id < 0 else target_msg_id
    out["result_kind"] = out.get("outcome")
    out["report"] = None
    if out.get("report_id"):
        out["report"] = _report_row(conn.execute(
            "SELECT * FROM v2_reports WHERE report_id=?", (out["report_id"],)
        ).fetchone())
    return out


def _session_reap_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    raw = out.get("survivors")
    if isinstance(raw, str):
        try:
            out["survivors"] = json.loads(raw)
        except (TypeError, ValueError):
            out["survivors"] = []
    out["survivors"] = _sanitize_session_reap_survivors(out.get("survivors"))
    return out


def _sanitize_session_reap_survivors(value: object) -> list[dict[str, Any]]:
    """Keep close/reconcile readback a bounded array of safe records."""
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    overflow = False
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("pid") is None:
            continue
        try:
            pid = int(item.get("pid"))
        except (TypeError, ValueError):
            pid = -1
        if pid == 0 or pid < -1:
            continue
        proof = item.get("ownership_proof_v2")
        clean_proof: dict[str, Any] | None = None
        if isinstance(proof, dict):
            # Preserve the complete proof needed for generation-safe reaping,
            # while bounding every field that came from a process command or
            # remote inventory.
            clean_proof = {
                "version": proof.get("version"),
                "host": str(proof.get("host") or "")[:128],
                "uid": proof.get("uid"),
                "boot_id": str(proof.get("boot_id") or "")[:256],
                "pid": proof.get("pid"),
                "start_id": str(proof.get("start_id") or "")[:128],
                "ppid": proof.get("ppid"),
                "pgid": proof.get("pgid"),
                "sid": proof.get("sid"),
                "tmux_socket": str(proof.get("tmux_socket") or "")[:512],
                "tmux_session": str(proof.get("tmux_session") or "")[:256],
                "tmux_pane": str(proof.get("tmux_pane") or "")[:128],
                "tty": str(proof.get("tty") or "")[:256],
                "captured_at": str(proof.get("captured_at") or "")[:64],
                "command_fingerprint": str(proof.get("command_fingerprint") or "")[:128],
            }
        clean_item: dict[str, Any] = {
            "pid": pid,
            "command": str(item.get("command") or "")[:512],
            "first_seen": str(item.get("first_seen") or "")[:64],
        }
        if item.get("reap_reason") is not None:
            clean_item["reap_reason"] = str(item.get("reap_reason") or "signal_unconfirmed")[:64]
        if clean_proof is not None:
            clean_item["ownership_proof_v2"] = clean_proof
        if len(result) >= MAX_SESSION_REAP_SURVIVORS - 1:
            overflow = True
            continue
        result.append(clean_item)
    if overflow:
        result.append({
            "pid": -1,
            "command": "",
            "first_seen": "",
            "reap_reason": "survivor_limit_exceeded",
        })
    return result


def _set_result(fut: asyncio.Future, value: Any) -> None:
    if not fut.done():
        fut.set_result(value)


def _set_exception(fut: asyncio.Future, exc: BaseException) -> None:
    if not fut.done():
        fut.set_exception(exc)
