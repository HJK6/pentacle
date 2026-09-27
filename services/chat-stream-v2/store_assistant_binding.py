"""Durable, generation-bound direct assistant binding and rebind receipts."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any


ASSISTANT_BINDING_DDL = """
CREATE TABLE IF NOT EXISTS v2_assistant_direct_binding (
    id INTEGER PRIMARY KEY CHECK(id=1),
    stream_id TEXT,
    generation TEXT,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL
)
"""
ASSISTANT_REBIND_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS v2_assistant_rebind_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    actor_stream_id TEXT NOT NULL,
    actor_generation TEXT NOT NULL,
    old_binding_json TEXT NOT NULL,
    new_binding_json TEXT,
    outcome TEXT NOT NULL,
    receipt_json TEXT,
    created_at TEXT NOT NULL
)
"""
ASSISTANT_REBIND_AUDIT_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_v2_assistant_rebind_request "
    "ON v2_assistant_rebind_audit(request_id, audit_id)"
)
ASSISTANT_HANDOFF_PROOF_DDL = """
CREATE TABLE IF NOT EXISTS v2_assistant_direct_handoff_proofs (
    successor_stream_id TEXT NOT NULL,
    successor_generation TEXT NOT NULL,
    predecessor_stream_id TEXT NOT NULL,
    predecessor_generation TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(successor_stream_id, successor_generation)
)
"""
PORTFOLIO_SPEC = "spec_pentacle__bart_portfolio_coordination_2026_09"
_STREAM_RE = re.compile(r"[a-z][a-z0-9_-]*:[A-Za-z0-9_.:-]+\Z")
_GENERATION_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _binding_conn(
    conn: sqlite3.Connection, env_binding: dict[str, str], *, include_target: bool = True,
) -> dict[str, Any]:
    # ``include_target`` resolves the bound seat row for diagnostic
    # provider/model/effort fields.  Per-event callers on the ingest hot path
    # (the prose mirror) pass ``False`` to skip that extra sessions JOIN when
    # they only need the effective stream/generation/source.
    row = conn.execute(
        "SELECT stream_id,generation,revision FROM v2_assistant_direct_binding WHERE id=1"
    ).fetchone()
    revision = int(row["revision"]) if row else 0
    if row and bool(row["stream_id"]) != bool(row["generation"]):
        raise ValueError("assistant_binding_corrupt")
    if row and row["stream_id"]:
        pair = {"stream_id": row["stream_id"], "generation": row["generation"]}
        source = "durable"
    else:
        pair = dict(env_binding)
        source = "env" if pair.get("stream_id") and pair.get("generation") else "unconfigured"
    if source != "unconfigured" and (
        not _STREAM_RE.fullmatch(str(pair.get("stream_id") or ""))
        or not _GENERATION_RE.fullmatch(str(pair.get("generation") or ""))
    ):
        raise ValueError("assistant_binding_corrupt")
    target = (_seat_conn(conn, str(pair.get("stream_id") or ""))
              if include_target and source != "unconfigured" else None)
    return {"source": source, "stream_id": pair.get("stream_id") or "",
            "generation": pair.get("generation") or "", "revision": revision,
            "effective_provider": (target or {}).get("provider"),
            "effective_model": (target or {}).get("effective_model"),
            "effective_effort": (target or {}).get("effective_effort")}


def _seat_conn(conn: sqlite3.Connection, stream_id: str) -> dict[str, Any] | None:
    host, sep, name = stream_id.partition(":")
    if not sep:
        return None
    row = conn.execute(
        "SELECT s.*,g.generation AS session_generation FROM sessions s "
        "LEFT JOIN v2_session_generations g ON g.host=s.host AND g.session_name=s.session_name "
        "WHERE s.host=? AND s.session_name=?", (host, name),
    ).fetchone()
    return dict(row) if row else None


def _live_seat(row: dict[str, Any] | None, *, target: bool = False) -> None:
    if row is None:
        raise ValueError("assistant_rebind_target_unknown" if target else "assistant_rebind_actor_unknown")
    if row["status"] != "open" or row["closed_at"] or row["pane_status"] != "pane_alive":
        raise ValueError("assistant_rebind_target_closed" if target else "assistant_rebind_actor_closed")
    if not row["session_generation"]:
        raise ValueError("assistant_rebind_generation_unavailable")


def _portfolio_authorized(row: dict[str, Any]) -> bool:
    if row["parent_stream_id"] or row["visibility"] != "default":
        return False
    try:
        qualified = json.loads(row["qualified_spec_ids"] or "[]")
        provenance = json.loads(row["spec_binding_provenance"] or "[]")
    except (TypeError, ValueError):
        return False
    return PORTFOLIO_SPEC in qualified and any(
        isinstance(item, dict) and item.get("spec_id") == PORTFOLIO_SPEC
        and item.get("provenance") in {"spawn_explicit", "drive_explicit", "handoff_inherited"}
        and item.get("granting_principal")
        for item in provenance
    )


class AssistantBindingStoreMixin:
    async def get_assistant_binding(self, *, env_binding: dict[str, str]) -> dict[str, Any]:
        return await self.submit(lambda conn: _binding_conn(conn, env_binding))

    async def rebind_assistant(
        self, *, env_binding: dict[str, str], actor_stream_id: str, actor_generation: str,
        target_stream_id: str | None, target_generation: str | None,
        request_id: str, expected_revision: int, clear: bool = False,
    ) -> dict[str, Any]:
        # The revision is a compare-and-swap observation, not caller intent.
        # A retry after a lost reply may observe the newly committed revision.
        payload = {"target": target_stream_id or "", "generation": target_generation or "",
                   "clear": clear}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute("BEGIN IMMEDIATE")
            old: dict[str, Any] = {}
            try:
                old = _binding_conn(conn, env_binding)
                prior = conn.execute(
                    "SELECT payload_digest,actor_stream_id,actor_generation,receipt_json,outcome "
                    "FROM v2_assistant_rebind_audit "
                    "WHERE request_id=? ORDER BY audit_id LIMIT 1", (request_id,),
                ).fetchone()
                if prior:
                    if (prior["payload_digest"] != digest
                            or prior["actor_stream_id"] != actor_stream_id
                            or prior["actor_generation"] != actor_generation):
                        raise ValueError("assistant_rebind_request_conflict")
                    receipt = json.loads(prior["receipt_json"] or "{}")
                    if prior["outcome"] != "ok":
                        raise ValueError(prior["outcome"])
                    conn.commit()
                    return {**receipt, "duplicate": True}
                if not isinstance(expected_revision, int) or expected_revision != old["revision"]:
                    raise ValueError("assistant_rebind_stale_revision")
                actor = _seat_conn(conn, actor_stream_id)
                _live_seat(actor)
                if actor["session_generation"] != actor_generation:
                    raise ValueError("assistant_rebind_actor_generation_mismatch")
                if actor_stream_id != old["stream_id"] or actor_generation != old["generation"]:
                    handoff = conn.execute(
                        "SELECT 1 FROM v2_assistant_direct_handoff_proofs "
                        "WHERE successor_stream_id=? AND successor_generation=? "
                        "AND predecessor_stream_id=? AND predecessor_generation=?",
                        (actor_stream_id, actor_generation, old["stream_id"], old["generation"]),
                    ).fetchone()
                    if handoff is None and not _portfolio_authorized(actor):
                        raise ValueError("assistant_rebind_unauthorized")
                if clear:
                    if old["source"] != "durable":
                        raise ValueError("assistant_rebind_clear_unavailable")
                    selected = dict(env_binding)
                    env_target = _seat_conn(conn, selected.get("stream_id") or "")
                    try:
                        _live_seat(env_target, target=True)
                    except ValueError as exc:
                        raise ValueError("assistant_rebind_clear_env_unusable") from exc
                    if env_target["session_generation"] != selected.get("generation"):
                        raise ValueError("assistant_rebind_clear_env_unusable")
                    new = {"stream_id": selected["stream_id"], "generation": selected["generation"],
                           "source": "env", "revision": old["revision"] + 1}
                    stored_stream = stored_generation = None
                else:
                    if not _STREAM_RE.fullmatch(str(target_stream_id or "")):
                        raise ValueError("assistant_rebind_target_invalid")
                    target = _seat_conn(conn, str(target_stream_id))
                    _live_seat(target, target=True)
                    if target["routing_integrity"] == "mismatch":
                        raise ValueError("assistant_rebind_target_integrity_mismatch")
                    if not target["provider"] or not target["effective_model"] or not target["effective_effort"]:
                        raise ValueError("assistant_rebind_target_tuple_unknown")
                    if target_generation and target_generation != target["session_generation"]:
                        raise ValueError("assistant_rebind_generation_mismatch")
                    stored_stream = str(target_stream_id)
                    stored_generation = str(target["session_generation"])
                    new = {"stream_id": stored_stream, "generation": stored_generation,
                           "source": "durable", "revision": old["revision"] + 1,
                           "effective_provider": target["provider"],
                           "effective_model": target["effective_model"],
                           "effective_effort": target["effective_effort"]}
                stamp = _stamp()
                conn.execute(
                    "INSERT INTO v2_assistant_direct_binding(id,stream_id,generation,revision,updated_at) "
                    "VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "stream_id=excluded.stream_id,generation=excluded.generation,"
                    "revision=excluded.revision,updated_at=excluded.updated_at",
                    (stored_stream, stored_generation, new["revision"], stamp),
                )
                receipt = {"type": "assistant.rebind.ok", "request_id": request_id,
                           "old_binding": old, "new_binding": new, "duplicate": False}
                conn.execute(
                    "INSERT INTO v2_assistant_rebind_audit(request_id,payload_digest,actor_stream_id,"
                    "actor_generation,old_binding_json,new_binding_json,outcome,receipt_json,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (request_id, digest, actor_stream_id, actor_generation, json.dumps(old),
                     json.dumps(new), "ok", json.dumps(receipt), stamp),
                )
                conn.commit()
                return receipt
            except ValueError as exc:
                code = str(exc)
                stamp = _stamp()
                conn.execute(
                    "INSERT INTO v2_assistant_rebind_audit(request_id,payload_digest,actor_stream_id,"
                    "actor_generation,old_binding_json,new_binding_json,outcome,receipt_json,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (request_id, digest, actor_stream_id, actor_generation, json.dumps(old),
                     None, code, json.dumps({"error_code": code}), stamp),
                )
                conn.commit()
                return {"error_code": code}
            except BaseException:
                conn.rollback()
                raise

        result = await self.submit(_op)
        if result.get("error_code"):
            raise ValueError(result["error_code"])
        return result

    async def fail_closed_assistant_routes(self, *, stream_id: str, target_stream_id: str,
                                           target_generation: str) -> int:
        def _op(conn: sqlite3.Connection) -> int:
            conn.execute("BEGIN IMMEDIATE")
            try:
                changed = conn.execute(
                    "UPDATE v2_assistant_composite_routes SET delivery_state='failed',"
                    "error_code='assistant_target_closed',updated_at=? "
                    "WHERE stream_id=? AND route_target=? AND route_target_generation=? "
                    "AND routing_state='resolved' AND COALESCE(delivery_state,'')!='failed' "
                    "AND NOT EXISTS (SELECT 1 FROM v2_assistant_composite_publications p "
                    "WHERE p.stream_id=v2_assistant_composite_routes.stream_id "
                    "AND p.reply_to_message_id=v2_assistant_composite_routes.input_identity "
                    "AND (p.publish_kind='result' OR json_extract(p.canonical_payload_json,'$.response_state')='final'))",
                    (_stamp(), stream_id, target_stream_id, target_generation),
                ).rowcount
                conn.commit()
                return changed
            except BaseException:
                conn.rollback()
                raise
        return await self.submit(_op)
