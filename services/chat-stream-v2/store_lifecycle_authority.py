"""Operator-designated fleet lifecycle authority (Store worker only).

One durable manager grant names an existing session generation. Only an
authenticated operator designates, replaces or revokes it; only the current
holder, proving its own current generation, transfers it. Eligibility is
re-read inside the same transaction as the mutation. Every recognized attempt
is audited with the server-verified actor, never a caller claim.

Protected-assistant handoffs also record their source identity here so a
retired source can read back exactly its own receipt without new admission.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from typing import Any

ACTIONS = ("designate", "transfer", "revoke")
MAX_REASON = 1024
MAX_REQUEST_ID = 128
# A long mixed-case alphanumeric run is treated as a possible credential and
# never persisted; ids, hex generations and prose are unaffected.
_SECRET_SHAPED = re.compile(r"[A-Za-z0-9_+/=-]{20,}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _secret_shaped(run: str) -> bool:
    return any(c.isupper() for c in run) and any(c.islower() for c in run) and any(c.isdigit() for c in run)


def scrub(text: Any, limit: int) -> str | None:
    """Bounded, control-free caller text with credential-shaped runs redacted."""
    value = _CONTROL.sub(" ", str(text or "")).strip()[:limit]
    value = _SECRET_SHAPED.sub(lambda m: "[redacted]" if _secret_shaped(m.group(0)) else m.group(0), value)
    return value or None


class AuthorityError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def initialize(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS v2_lifecycle_manager (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        stream_id TEXT, session_generation TEXT,
        revision INTEGER NOT NULL, updated_at REAL NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS v2_lifecycle_authority_receipts (
        actor_identity TEXT NOT NULL, request_id TEXT NOT NULL,
        payload_hash TEXT NOT NULL, receipt TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY (actor_identity, request_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS v2_lifecycle_authority_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT NOT NULL, actor_kind TEXT NOT NULL, actor_identity TEXT,
        actor_generation TEXT, target_stream_id TEXT, target_generation TEXT,
        old_revision INTEGER, new_revision INTEGER, prior_stream_id TEXT,
        prior_generation TEXT, reason TEXT, request_id TEXT,
        result TEXT NOT NULL, refusal_code TEXT, created_at REAL NOT NULL)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS ix_v2_lifecycle_authority_audit_target
        ON v2_lifecycle_authority_audit (target_stream_id, created_at)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS v2_assistant_handoff_receipts (
        host TEXT NOT NULL, idempotency_key TEXT NOT NULL,
        source_stream_id TEXT NOT NULL, source_generation TEXT NOT NULL,
        source_token_hash TEXT NOT NULL, logical_payload_hash TEXT NOT NULL,
        successor_name TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY (host, idempotency_key))""")


def logical_payload_hash(msg: dict[str, Any]) -> str:
    """Hash only caller wire input: auth/transport state cannot change identity."""
    volatile = {"request_id", "idempotency_key", "objective_supported",
                "objective_source", "stream_token", "from_stream_id", "token"}
    canonical = {k: msg[k] for k in sorted(msg) if k not in volatile and not str(k).startswith("_")}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def _generation(conn: sqlite3.Connection, stream_id: str) -> str | None:
    host, _, name = stream_id.partition(":")
    row = conn.execute("SELECT generation FROM v2_session_generations WHERE host=? AND session_name=?",
                       (host, name)).fetchone()
    return str(row[0]) if row else None


def _session(conn: sqlite3.Connection, stream_id: str) -> dict[str, Any] | None:
    host, _, name = stream_id.partition(":")
    cur = conn.execute("SELECT * FROM sessions WHERE host=? AND session_name=?", (host, name))
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip([c[0] for c in cur.description], row))


def current(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT stream_id, session_generation, revision FROM v2_lifecycle_manager WHERE id=1").fetchone()
    if row is None:
        return {"stream_id": None, "session_generation": None, "revision": 0}
    return {"stream_id": row[0], "session_generation": row[1], "revision": int(row[2])}


def holder_is(conn: sqlite3.Connection, stream_id: str, generation: str | None) -> bool:
    """True only for the exact current holder generation of an open session."""
    grant = current(conn)
    if not stream_id or not generation or grant["stream_id"] != stream_id or grant["session_generation"] != generation:
        return False
    row = _session(conn, stream_id)
    return bool(row and row.get("status") == "open" and _generation(conn, stream_id) == generation)


def eligible(conn: sqlite3.Connection, stream_id: str, generation: str, protected_role: str) -> str | None:
    """Refusal code for a recipient, or None when it may hold authority."""
    row = _session(conn, stream_id)
    if row is None or row.get("status") != "open":
        return "authority_target_unavailable"
    if _generation(conn, stream_id) != generation:
        return "authority_target_generation_mismatch"
    # Readiness must be positively established; unknown state fails closed.
    if (str(row.get("pane_status") or "") != "pane_alive"
            or str(row.get("bootstrap_state") or "") != "ready"
            or row.get("offline_since_ts") or row.get("presumed_dead_at")):
        return "authority_target_not_ready"
    role = str(row.get("role") or "")
    if role != "lead" and not (protected_role and role == protected_role):
        return "authority_target_ineligible"
    return None


def target_readback(conn: sqlite3.Connection, stream_id: str, protected_role: str) -> dict[str, Any]:
    """Current generation and recipient eligibility; advisory, re-checked at mutation."""
    generation = _generation(conn, stream_id)
    row = _session(conn, stream_id)
    code = eligible(conn, stream_id, generation or "", protected_role) if generation else "authority_target_unavailable"
    return {"stream_id": stream_id, "session_generation": generation,
            "role": (row or {}).get("role"), "eligible": code is None, "refusal_code": code}


def audit(conn: sqlite3.Connection, **fields: Any) -> None:
    """Append one row. Caller text is scrubbed; an unverified caller's free
    text (reason, request id) is not persisted at all."""
    verified = fields.get("actor_kind") not in {None, "unauthenticated"}
    fields["reason"] = scrub(fields.get("reason"), MAX_REASON) if verified else None
    fields["request_id"] = scrub(fields.get("request_id"), MAX_REQUEST_ID) if verified else None
    cols = ("action", "actor_kind", "actor_identity", "actor_generation", "target_stream_id",
            "target_generation", "old_revision", "new_revision", "prior_stream_id",
            "prior_generation", "reason", "request_id", "result", "refusal_code")
    values = [fields.get(c) for c in cols]
    conn.execute(f"INSERT INTO v2_lifecycle_authority_audit ({','.join(cols)}, created_at) "
                 f"VALUES ({','.join('?' * len(cols))}, ?)", (*values, time.time()))


def _actor(auth: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """(actor_kind, identity, generation) from the server-derived context only.

    A seat token wins over the connection's operator bit so a seat can never
    act as the operator; an operator has no session generation.
    """
    if auth.get("token_verified") and auth.get("stream_id"):
        return "seat", str(auth["stream_id"]), str(auth.get("session_generation") or "") or None
    principal = str(auth.get("operator_principal") or "")
    if auth.get("operator_authenticated") and principal.startswith("operator:"):
        return "operator", principal, None
    return "unauthenticated", None, None


def mutate(conn: sqlite3.Connection, msg: dict[str, Any], auth: dict[str, Any],
           protected_role: str) -> dict[str, Any]:
    """Apply one designate/transfer/revoke atomically; raise AuthorityError on refusal.

    The caller commits; on refusal the audit row is committed without mutation.
    """
    action = str(msg.get("action") or "")
    request_id = str(msg.get("request_id") or "").strip()
    reason = str(msg.get("reason") or "").strip()
    target = str(msg.get("target_stream_id") or "").strip()
    target_generation = str(msg.get("target_generation") or "").strip()
    actor_kind, identity, actor_generation = _actor(auth)
    grant = current(conn)
    if actor_kind == "seat" and holder_is(conn, identity or "", actor_generation):
        actor_kind = "manager"
    # Retry identity is the verified principal (operator credential, or seat
    # stream plus generation), independent of whether it still holds the grant.
    replay_key = str(identity) if actor_kind == "operator" else f"seat:{identity}:{actor_generation or ''}"
    base = dict(action=action, actor_kind=actor_kind, actor_identity=identity,
                actor_generation=actor_generation, target_stream_id=target or None,
                target_generation=target_generation or None, old_revision=grant["revision"],
                prior_stream_id=grant["stream_id"], prior_generation=grant["session_generation"],
                reason=reason[:MAX_REASON] or None, request_id=request_id or None)

    def refuse(code: str) -> None:
        audit(conn, **base, result="refused", refusal_code=code)
        raise AuthorityError(code)

    if not request_id or len(request_id) > MAX_REQUEST_ID:
        refuse("authority_request_id_required")
    if not reason or len(reason) > MAX_REASON:
        refuse("authority_reason_required")
    payload = {k: msg.get(k) for k in ("action", "target_stream_id", "target_generation", "reason",
                                       "expected_revision")}
    payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    if identity:
        prior = conn.execute("SELECT payload_hash, receipt FROM v2_lifecycle_authority_receipts "
                             "WHERE actor_identity=? AND request_id=?", (replay_key, request_id)).fetchone()
        if prior is not None:
            if prior[0] != payload_hash:
                refuse("authority_request_conflict")
            return {**json.loads(prior[1]), "replayed": True}
    if action in {"designate", "revoke"} and actor_kind != "operator":
        refuse("authority_operator_required")
    if action == "transfer" and actor_kind != "manager":
        refuse("authority_holder_required")
    expected = msg.get("expected_revision")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected != grant["revision"]:
        refuse("authority_revision_conflict")
    if action in {"designate", "transfer"}:
        if not target or not target_generation:
            refuse("authority_target_required")
        if action == "transfer" and target == identity:
            refuse("authority_target_is_holder")
        code = eligible(conn, target, target_generation, protected_role)
        if code:
            refuse(code)
        new_holder: tuple[str | None, str | None] = (target, target_generation)
    else:
        if grant["stream_id"] is None:
            refuse("authority_not_held")
        new_holder = (None, None)
    new_revision = grant["revision"] + 1
    conn.execute("INSERT INTO v2_lifecycle_manager (id, stream_id, session_generation, revision, updated_at) "
                 "VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET stream_id=excluded.stream_id, "
                 "session_generation=excluded.session_generation, revision=excluded.revision, "
                 "updated_at=excluded.updated_at", (*new_holder, new_revision, time.time()))
    audit(conn, **base, new_revision=new_revision, result="applied")
    receipt = {"action": action, "revision": new_revision, "holder_stream_id": new_holder[0],
               "holder_generation": new_holder[1], "prior_stream_id": grant["stream_id"],
               "prior_generation": grant["session_generation"], "actor_kind": actor_kind,
               "actor_identity": identity, "actor_generation": actor_generation,
               "request_id": request_id}
    conn.execute("INSERT INTO v2_lifecycle_authority_receipts VALUES (?, ?, ?, ?, ?)",
                 (replay_key, request_id, payload_hash,
                  json.dumps(receipt, sort_keys=True), time.time()))
    return receipt


def carry_on_handoff(conn: sqlite3.Connection, source: str, source_generation: str,
                     successor: str, successor_generation: str, protected_role: str) -> dict[str, Any] | None:
    """A completed protected handoff moves authority only from the exact holder."""
    grant = current(conn)
    if grant["stream_id"] != source or grant["session_generation"] != source_generation:
        return None
    base = dict(action="handoff_transfer", actor_kind="manager", actor_identity=source,
                actor_generation=source_generation, target_stream_id=successor,
                target_generation=successor_generation, old_revision=grant["revision"],
                prior_stream_id=source, prior_generation=source_generation,
                reason="protected assistant handoff", request_id=None)
    code = eligible(conn, successor, successor_generation, protected_role)
    if code:
        audit(conn, **base, result="refused", refusal_code=code)
        return None
    revision = grant["revision"] + 1
    conn.execute("UPDATE v2_lifecycle_manager SET stream_id=?, session_generation=?, revision=?, updated_at=? "
                 "WHERE id=1", (successor, successor_generation, revision, time.time()))
    audit(conn, **base, new_revision=revision, result="applied")
    return {"revision": revision, "holder_stream_id": successor, "holder_generation": successor_generation}


def record_handoff_receipt(conn: sqlite3.Connection, host: str, key: str, source: str,
                           source_generation: str, token_hash: str, payload_hash: str,
                           successor_name: str) -> None:
    conn.execute("INSERT OR IGNORE INTO v2_assistant_handoff_receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (host, key, source, source_generation, token_hash, payload_hash, successor_name, time.time()))


def retired_handoff_receipt(conn: sqlite3.Connection, host: str, key: str, owner: dict[str, Any],
                            payload_hash: str) -> dict[str, Any]:
    """The retired owner's own receipt row, or AuthorityError. Never admits."""
    row = conn.execute("SELECT source_stream_id, source_generation, source_token_hash, "
                       "logical_payload_hash, successor_name FROM v2_assistant_handoff_receipts "
                       "WHERE host=? AND idempotency_key=?", (host, key)).fetchone()
    if row is None:
        raise AuthorityError("assistant_handoff_receipt_unavailable")
    source, generation, token_hash, recorded_hash, successor = row
    live = _session(conn, source)
    if (owner.get("stream_id") != source or owner.get("session_generation") != generation
            or owner.get("token_hash") != token_hash or live is None
            or live.get("token_hash") != token_hash or _generation(conn, source) != generation):
        raise AuthorityError("assistant_handoff_replay_unauthorized")
    if recorded_hash != payload_hash:
        raise AuthorityError("idempotency_key_conflict")
    return {"successor_name": successor, "source_stream_id": source, "source_generation": generation}
