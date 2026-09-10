"""Durable scheduling through caller-supplied store and spawn adapters.

Authentication arrives through the server's verified request context. Host,
provider, model, and specification policy belong to the supplied adapters; this
module contains no credentials or installation-specific configuration. Inline
prompts are caller-provided data. External prompt archives and installation
attestations are unsupported and fail closed, including on recovered rows.
"""

from __future__ import annotations

from _shared.spawn_objective import objective_error

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import re
import sqlite3
import uuid
from typing import Any, Awaitable, Callable

from sessions import VerbError


RESOURCE_RE = re.compile(r"[a-z0-9][a-z0-9._:/-]{0,127}\Z")
TARGET_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
SCHEDULE_TERMINAL = frozenset({"fired", "cancelled", "failed", "indeterminate", "expired"})
RECEIPT_RETENTION_DAYS = 30
INVALID_ATTESTATION_AUDIT_TOKEN = "invalid"
log = logging.getLogger("chat_streamd_v2.window_schedule")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _decode(value: Any, fallback: Any) -> Any:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError):
        return fallback
    return decoded


def _row(value: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(value) if value is not None else None


def _unproven_spawn_delivery_proof(
    outcome: dict[str, Any] | None,
    admitted: list[str] | None,
    *,
    target_host: str,
    prompt_required: bool,
) -> tuple[str, str] | None:
    """Accept a durable delivery receipt only for exactly one matching child.

    A later readiness check can fail after delivery succeeds. An ambiguous
    admission set or a reservation without delivery evidence remains unproved.
    """
    if not isinstance(outcome, dict) or outcome.get("state") != "indeterminate":
        return None
    host = str(outcome.get("host") or "")
    session_name = str(outcome.get("session_name") or "")
    if host != target_host or not session_name or admitted != [session_name]:
        return None
    receipt = outcome.get("delivery_receipt")
    if not isinstance(receipt, dict):
        return None
    prompt_status = str(receipt.get("delivery_status") or receipt.get("state") or "")
    proven = (
        prompt_status == "delivered"
        if prompt_required
        else prompt_status in {"not_requested", "delivered"}
    )
    if not proven:
        return None
    return f"{host}:{session_name}", prompt_status


def _b64_component(value: str) -> tuple[int, str]:
    raw = value.encode("utf-8")
    return len(raw), base64.urlsafe_b64encode(raw).decode("ascii")


def receipt_id(request_id: str, phase: str) -> str:
    phase_len, phase_b64 = _b64_component(phase)
    request_len, request_b64 = _b64_component(request_id)
    return f"receipt-v1:{phase_len}:{phase_b64}:{request_len}:{request_b64}"


def _payload_sha(msg: dict[str, Any]) -> str:
    safe = {
        str(key): value
        for key, value in msg.items()
        if not str(key).startswith("_") and key != "stream_token"
    }
    return hashlib.sha256(_compact(safe).encode("utf-8")).hexdigest()


def _valid_uuid(value: Any) -> str:
    raw = str(value or "")
    try:
        uuid.UUID(raw)
    except (ValueError, AttributeError) as exc:
        raise VerbError("invalid_request_id", "request_id must be a UUID") from exc
    return raw


def _parse_time(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerbError("invalid_fire_time", "fires_at_utc must be ISO-8601 with timezone") from exc
    if parsed.tzinfo is None:
        raise VerbError("invalid_fire_time", "fires_at_utc must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _project_schedule(row: dict[str, Any] | None, *, include_prompt: bool = False) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    value["owner_spec_ids"] = _decode(value.pop("owner_spec_ids_json", "[]"), [])
    value["owner_spec_provenance"] = _decode(
        value.pop("owner_spec_provenance_json", "[]"), []
    )
    value["confirm_model_change"] = bool(value.get("confirm_model_change"))
    for field in ("reparent_children", "self_close_on_completion", "no_watch"):
        if value.get(field) is not None:
            value[field] = bool(value[field])
    value.pop("attestation_json", None)
    value["provider"] = value.get("resolved_provider")
    value["model"] = value.get("resolved_model")
    value["effort"] = value.get("resolved_effort")
    if not include_prompt:
        value.pop("prompt_b64", None)
        value.pop("prompt_blob_id", None)
    else:
        value["initial_prompt_b64"] = value.pop("prompt_b64", None)
        value["initial_prompt_blob_sha"] = value.pop("prompt_blob_id", None)
    return value


class WindowSchedule:
    def __init__(
        self,
        store: Any,
        sessions: Any,
        comms: Any,
        spawnctl: Any,
        *,
        local_host: str,
        broadcast: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
        poll_interval_s: float = 0.5,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.comms = comms
        self.spawnctl = spawnctl
        self.local_host = local_host
        self.broadcast = broadcast
        self.poll_interval_s = poll_interval_s
        self.schema_health = "initializing"

    def mark_store_ready(self) -> None:
        self.schema_health = "ok"

    def mark_store_failed(self) -> None:
        self.schema_health = "failed"

    def _require_store(self) -> None:
        if self.schema_health != "ok":
            raise VerbError("store_unavailable", "window/schedule schema is not healthy")

    @staticmethod
    def _prompt_preview(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()[:160]

    async def _schedule_transport_row(
        self, row: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Project a bounded prompt preview for authenticated operator clients."""
        if row is None:
            return None
        projected = _project_schedule(row) or {}
        projected["prompt_preview"] = self._prompt_preview(base64.b64decode(row.get("prompt_b64") or "").decode("utf-8"))
        return projected

    async def schedule_inventory(self) -> list[dict[str, Any]]:
        """Return the complete schedule snapshot for an authenticated UI."""
        if self.schema_health != "ok":
            return []

        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            return [dict(row) for row in conn.execute(
                "SELECT s.*,d.child_stream_id "
                "FROM v2_schedules s LEFT JOIN v2_schedule_dispatches d "
                "ON d.schedule_id=s.schedule_id AND d.generation=s.generation "
                "ORDER BY s.fires_at_utc,s.schedule_id"
            ).fetchall()]

        rows = await self.store.submit(op)
        projected = await asyncio.gather(*(self._schedule_transport_row(row) for row in rows))
        return [row for row in projected if row is not None]

    async def _broadcast_schedule_lifecycle(self, event: str, schedule_id: str) -> None:
        if self.broadcast is None:
            return

        def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            return _row(conn.execute(
                "SELECT s.*,d.child_stream_id "
                "FROM v2_schedules s LEFT JOIN v2_schedule_dispatches d "
                "ON d.schedule_id=s.schedule_id AND d.generation=s.generation "
                "WHERE s.schedule_id=?",
                (schedule_id,),
            ).fetchone())

        try:
            row = await self._schedule_transport_row(await self.store.submit(op))
            if row is None:
                return
            await self.broadcast({
                "type": "schedule.lifecycle",
                "event": event,
                "schedule": row,
            })
        except Exception:  # a committed mutation must not become a false RPC failure
            log.exception(
                "schedule lifecycle broadcast failed schedule_id=%s event=%s",
                schedule_id,
                event,
            )

    def wire_handlers(self) -> dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]]:
        return {
            "schedule.insert": self.schedule_insert,
            "schedule.list": self.schedule_list,
            "schedule.get": self.schedule_get,
            "schedule.cancel": self.schedule_cancel,
            "schedule.reschedule": self.schedule_reschedule,
            "schedule.run": self.schedule_run,
            "schedule.receipt": self.schedule_receipt,
        }

    async def _session(self, stream_id: str) -> dict[str, Any] | None:
        if ":" not in stream_id:
            return None
        current = self.sessions.get(stream_id)
        if current is not None:
            return dict(current)
        host, name = stream_id.split(":", 1)
        return await self.store.fetch_session(host, name)

    async def _seat(self, msg: dict[str, Any], *, live: bool = True) -> tuple[str, dict[str, Any]]:
        self._require_store()
        auth = msg.get("_auth_context") if isinstance(msg.get("_auth_context"), dict) else {}
        if not auth.get("token_verified"):
            raise VerbError("stream_ownership_unverified", "exact stream token required")
        actor = str(auth.get("stream_id") or "").strip()
        claimed = str(msg.get("from_stream_id") or "").strip()
        if not actor or claimed != actor:
            raise VerbError("stream_ownership_unverified", "token owner does not match from_stream_id")
        row = await self._session(actor)
        if row is None or (live and str(row.get("status") or "open") != "open"):
            raise VerbError("actor_not_live", "actor is not a live registered seat")
        return actor, row

    async def _actor(self, msg: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None]:
        self._require_store()
        auth = msg.get("_auth_context") if isinstance(msg.get("_auth_context"), dict) else {}
        if auth.get("operator_authenticated"):
            return "operator", str(auth.get("operator_principal") or "operator"), None
        if auth.get("service_authenticated"):
            actor = str(auth.get("service_actor") or msg.get("from_stream_id") or "").strip()
            if not actor:
                raise VerbError("stream_ownership_unverified", "service actor is missing")
            return "service", actor, None
        actor, row = await self._seat(msg)
        return ("nexus" if row.get("role") == "nexus" else "seat"), actor, row

    @staticmethod
    def _receipt_command(surface: str, request_id: str, phase: str) -> str:
        return f"agent-orch {surface} receipt {request_id} --phase {phase}"

    async def _get_receipt(self, request_id: str, phase: str) -> dict[str, Any] | None:
        def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            return _row(conn.execute(
                "SELECT * FROM v2_operation_receipts WHERE request_id=? AND phase=?",
                (request_id, phase),
            ).fetchone())
        return await self.store.submit(op)

    async def _mutation_replay(
        self, msg: dict[str, Any], *, phase: str, surface: str,
        actor_kind: str, actor_id: str,
    ) -> dict[str, Any] | None:
        request_id = _valid_uuid(msg.get("request_id"))
        stored = await self._get_receipt(request_id, phase)
        if stored is None:
            return None
        self._check_receipt(
            stored, surface=surface, verb=str(msg.get("type") or ""),
            actor_kind=actor_kind, actor_id=actor_id, payload_sha=_payload_sha(msg),
        )
        return _decode(stored.get("result_json"), {})

    @staticmethod
    def _check_receipt(
        stored: dict[str, Any], *, surface: str, verb: str, actor_kind: str,
        actor_id: str, payload_sha: str,
    ) -> None:
        expected = (surface, verb, actor_kind, actor_id, payload_sha)
        current = tuple(stored.get(key) for key in (
            "surface", "verb", "actor_kind", "actor_id", "canonical_payload_sha256",
        ))
        if current != expected:
            raise VerbError("request_phase_conflict", "request_id/phase is bound to different operation fields")

    @staticmethod
    def _insert_receipt_tx(
        conn: sqlite3.Connection, *, request_id: str, phase: str, surface: str,
        verb: str, actor_kind: str, actor_id: str, payload_sha: str,
        target_id: str | None, measured: dict[str, Any], result: dict[str, Any],
        measured_at: str,
    ) -> dict[str, Any]:
        existing = _row(conn.execute(
            "SELECT * FROM v2_operation_receipts WHERE request_id=? AND phase=?",
            (request_id, phase),
        ).fetchone())
        if existing is not None:
            WindowSchedule._check_receipt(
                existing, surface=surface, verb=verb, actor_kind=actor_kind,
                actor_id=actor_id, payload_sha=payload_sha,
            )
            return existing
        retain_until = (
            datetime.now(timezone.utc) + timedelta(days=RECEIPT_RETENTION_DAYS)
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")
        rid = receipt_id(request_id, phase)
        conn.execute(
            "INSERT INTO v2_operation_receipts "
            "(receipt_id,request_id,phase,surface,verb,actor_kind,actor_id,canonical_payload_sha256,"
            "target_id,measured_state_json,result_json,measured_at,retain_until) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, request_id, phase, surface, verb, actor_kind, actor_id, payload_sha,
             target_id, _compact(measured), _compact(result), measured_at, retain_until),
        )
        return _row(conn.execute("SELECT * FROM v2_operation_receipts WHERE receipt_id=?", (rid,)).fetchone()) or {}

    @staticmethod
    def _pin_schedule_receipts_tx(
        conn: sqlite3.Connection, schedule_id: str, terminal_at: str,
    ) -> str:
        parsed = datetime.fromisoformat(terminal_at.replace("Z", "+00:00"))
        retain_until = (
            parsed.astimezone(timezone.utc) + timedelta(days=RECEIPT_RETENTION_DAYS)
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")
        conn.execute(
            "UPDATE v2_operation_receipts SET retain_until=? "
            "WHERE surface='schedule' AND target_id=?",
            (retain_until, schedule_id),
        )
        return retain_until

    async def _receipt_read(self, msg: dict[str, Any], surface: str) -> dict[str, Any]:
        kind, actor, _ = await self._actor(msg)
        operation_id = str(msg.get("operation_request_id") or msg.get("target_request_id") or "").strip()
        phase = str(msg.get("phase") or "").strip()
        if not operation_id or not phase:
            raise VerbError("invalid_request", "operation_request_id and phase are required")
        stored = await self._get_receipt(operation_id, phase)
        if stored is None:
            raise VerbError("not_found", "receipt not found")
        if stored.get("surface") != surface:
            raise VerbError("not_found", "receipt not found")
        target_id = str(stored.get("target_id") or "")
        target = await self._schedule_target(target_id)
        visible = self._schedule_visible(target, kind, actor)
        if not visible:
            raise VerbError("not_found", "receipt not found")
        return {
            "type": "schedule.receipt.ok",
            "receipt": {
                **stored,
                "measured_state": _decode(stored.get("measured_state_json"), {}),
                "phase_result": _decode(stored.get("result_json"), {}),
            },
        }

    async def schedule_receipt(self, msg: dict[str, Any]) -> dict[str, Any]:
        return await self._receipt_read(msg, "schedule")

    async def schedule_insert(self, msg: dict[str, Any]) -> dict[str, Any]:
        if error := objective_error(msg.get("objective")):
            raise VerbError(error, error)
        request_id = _valid_uuid(msg.get("request_id"))
        kind, actor, seat = await self._actor(msg)
        if msg.get("agent_orch_attestation") is not None:
            raise VerbError("unsupported_configuration", "installation attestations are unsupported")
        if msg.get("initial_prompt_blob_sha"):
            raise VerbError("unsupported_configuration", "use an inline prompt")
        if kind not in {"seat", "service"}:
            raise VerbError("not_authorized", "schedule owner must be a seat or service actor")
        handoff = bool(msg.get("handoff") or msg.get("handoff_from_stream_id"))
        if not handoff:
            if msg.get("resume_session_id") is not None or msg.get("resume") is not None:
                raise VerbError("resume_not_schedulable", "resume cannot be scheduled")
            if msg.get("confirm_model_change"):
                raise VerbError("handoff_only_flag", "confirm_model_change applies only to handoff")
            if "idempotency_key" in msg:
                raise VerbError(
                    "idempotency_key_not_schedulable",
                    "schedule dispatch identity is generation-owned",
                )
        if "self_close_on_completion" in msg and not (
            handoff or msg.get("parent_stream_id")
        ):
            raise VerbError(
                "self_close_requires_parent",
                "self_close_on_completion requires parent lineage",
            )
        for lineage_field in ("handoff_from_stream_id", "created_by_stream_id"):
            lineage_owner = str(msg.get(lineage_field) or "").strip()
            if lineage_owner and lineage_owner != actor:
                raise VerbError("not_authorized", "schedule lineage is not owner-authorized")
        if kind == "seat" and str(msg.get("created_by_stream_id") or actor) != actor:
            raise VerbError("not_authorized", "schedule creator does not match token owner")
        parent_stream_id = str(msg.get("parent_stream_id") or "").strip() or None
        if parent_stream_id is not None and await self._session(parent_stream_id) is None:
            raise VerbError("parent_not_found", "scheduled parent must exist at create")
        for field in ("reparent_children", "self_close_on_completion", "no_watch"):
            if field in msg and not isinstance(msg.get(field), bool):
                raise VerbError("invalid_request", f"{field} must be boolean when specified")
        replay = await self._mutation_replay(
            msg, phase="row_committed", surface="schedule", actor_kind=kind, actor_id=actor,
        )
        if replay is not None:
            return replay
        owner_spec_ids = [
            str(value) for value in (seat or {}).get("qualified_spec_ids") or []
            if str(value)
        ]
        requested_spec_ids = list(msg.get("spec_ids") or ([msg["spec_id"]] if msg.get("spec_id") else []))
        admission_spec_ids = requested_spec_ids or owner_spec_ids
        if not admission_spec_ids:
            raise VerbError("not_authorized", "schedule owner must be spec-bound with provenance")
        fires_at = _parse_time(msg.get("fires_at_utc"))
        requested_provider = str(
            (msg.get("requested_provider") if "requested_provider" in msg else msg.get("provider")) or ""
        ).strip()
        requested_model = str(
            (msg.get("requested_model") if "requested_model" in msg else msg.get("model")) or ""
        ).strip()
        requested_effort = str(
            (msg.get("requested_effort") if "requested_effort" in msg else msg.get("effort")) or ""
        ).strip()
        prompt_text = msg.get("initial_prompt")
        prompt_blob = str(msg.get("initial_prompt_blob_sha") or "").strip() or None
        if prompt_text is not None and prompt_blob is not None:
            raise VerbError("invalid_request", "prompt inline/blob are mutually exclusive")
        prompt_b64 = None
        prompt_sha = prompt_blob
        if prompt_text is not None:
            raw = str(prompt_text).encode("utf-8")
            prompt_b64 = base64.b64encode(raw).decode("ascii")
            prompt_sha = hashlib.sha256(raw).hexdigest()
        timestamp = _now()
        attestation_json = None
        raw_target_sha = msg.get("target_sha")
        if raw_target_sha is None:
            target_sha = None
        elif not isinstance(raw_target_sha, str):
            raise VerbError("invalid_request", "target_sha must be a full 40-hex Git SHA")
        else:
            target_sha = raw_target_sha.strip()
            if not target_sha:
                target_sha = None
            elif TARGET_SHA_RE.fullmatch(target_sha) is None:
                raise VerbError("invalid_request", "target_sha must be a full 40-hex Git SHA")
        digest = _payload_sha(msg)
        schedule_id = f"sched-{uuid.uuid4().hex}"
        verb = "schedule.insert"
        admission_msg = dict(msg)
        admission_msg.update({
            "host": str(msg.get("target_host") or self.local_host),
            "provider": requested_provider or msg.get("provider"),
            "model": requested_model or None,
            "effort": requested_effort or None,
            "spec_ids": admission_spec_ids,
            "target_sha": target_sha,
        })
        admission = await self.spawnctl.admit_schedule(
            admission_msg, self.local_host, admission_name=f"schedule-{schedule_id[-12:]}",
        )
        binding = admission.get("spec_binding")
        if not isinstance(binding, dict):
            raise VerbError("not_authorized", "schedule admission returned no canonical spec binding")
        spec_ids = [
            str(value) for value in binding.get("qualified_spec_ids") or []
            if str(value)
        ]
        canonical_provenance = binding.get("spec_binding_provenance")
        provenance = [
            dict(entry) for entry in canonical_provenance
            if isinstance(entry, dict) and str(entry.get("spec_id") or "") in spec_ids
        ] if isinstance(canonical_provenance, list) else []
        provenance_ids = [str(entry.get("spec_id") or "") for entry in provenance]
        if (
            not spec_ids
            or len(set(spec_ids)) != len(spec_ids)
            or len(provenance) != len(spec_ids)
            or set(provenance_ids) != set(spec_ids)
        ):
            raise VerbError(
                "not_authorized",
                "canonical schedule admission binding lacks complete spec provenance",
            )
        if kind == "seat" and any(spec_id not in owner_spec_ids for spec_id in spec_ids):
            raise VerbError(
                "not_authorized",
                "requested spec selection is outside the owner's qualified binding",
            )
        resolved_provider = str(admission.get("resolved_provider") or "").strip()
        resolved_model = str(admission.get("resolved_model") or "").strip()
        resolved_effort = str(admission.get("resolved_effort") or "").strip()
        if not all((resolved_provider, resolved_model, resolved_effort)):
            raise VerbError("invalid_request", "scheduled spawn resolution is incomplete")

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute("BEGIN IMMEDIATE")
            try:
                prior = _row(conn.execute("SELECT * FROM v2_schedules WHERE request_id=?", (request_id,)).fetchone())
                if prior is None:
                    conn.execute(
                        "INSERT INTO v2_schedules "
                        "(schedule_id,request_id,owner_stream_id,owner_service_actor,owner_spec_ids_json,"
                        "owner_spec_provenance_json,parent_stream_id,handoff_from_stream_id,created_by_stream_id,"
                        "target_host,role,phase,visibility,requested_provider,requested_model,requested_effort,"
                        "resolved_provider,resolved_model,resolved_effort,disposition_waived_reason,confirm_model_change,"
                        "fires_at_utc,state,generation,prompt_sha256,prompt_b64,prompt_blob_id,created_at,updated_at,"
                        "target_sha,attestation_json,reparent_children,self_close_on_completion,objective,no_watch) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',1,?,?,?,?,?,?,?,?,?,?,?)",
                        (schedule_id, request_id, actor if kind == "seat" else None,
                         actor if kind == "service" else None, _compact(spec_ids), _compact(provenance),
                         parent_stream_id, msg.get("handoff_from_stream_id"),
                         actor if kind == "seat" else msg.get("created_by_stream_id"),
                         str(admission.get("host") or self.local_host), admission.get("role"), msg.get("phase"),
                         msg.get("visibility"), requested_provider, requested_model, requested_effort,
                         resolved_provider, resolved_model, resolved_effort,
                         msg.get("disposition_waived_reason"), 1 if msg.get("confirm_model_change") else 0,
                         fires_at, prompt_sha, prompt_b64, prompt_blob, timestamp, timestamp,
                         target_sha, attestation_json,
                         None if "reparent_children" not in msg else int(bool(msg["reparent_children"])),
                         None if "self_close_on_completion" not in msg else int(bool(msg["self_close_on_completion"])),
                         msg["objective"], int(bool(msg.get("no_watch")))),
                    )
                    prior = _row(conn.execute("SELECT * FROM v2_schedules WHERE schedule_id=?", (schedule_id,)).fetchone())
                result = {
                    "type": "schedule.insert.ok", "schedule": _project_schedule(prior),
                    "receipt_command": self._receipt_command("schedule", request_id, "row_committed"),
                }
                self._insert_receipt_tx(
                    conn, request_id=request_id, phase="row_committed", surface="schedule",
                    verb=verb, actor_kind=kind, actor_id=actor, payload_sha=digest,
                    target_id=str(prior["schedule_id"]), measured={"state": prior["state"], "generation": prior["generation"]},
                    result=result, measured_at=timestamp,
                )
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise
        result = await self.store.submit(op)
        await self._broadcast_schedule_lifecycle(
            "created", str(result["schedule"]["schedule_id"]),
        )
        return result

    async def _schedule_target(self, schedule_id: str) -> dict[str, Any]:
        def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            return _row(conn.execute("SELECT * FROM v2_schedules WHERE schedule_id=?", (schedule_id,)).fetchone())
        row = await self.store.submit(op)
        if row is None:
            raise VerbError("not_found", "schedule not found")
        return row

    @staticmethod
    def _schedule_visible(row: dict[str, Any], kind: str, actor: str) -> bool:
        return kind in {"operator", "nexus"} or row.get("owner_stream_id") == actor or row.get("owner_service_actor") == actor

    async def schedule_list(self, msg: dict[str, Any]) -> dict[str, Any]:
        kind, actor, _ = await self._actor(msg)
        state = str(msg.get("state") or "").strip()
        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            if state:
                return [dict(row) for row in conn.execute(
                    "SELECT * FROM v2_schedules WHERE state=? ORDER BY fires_at_utc,schedule_id", (state,)
                ).fetchall()]
            return [dict(row) for row in conn.execute(
                "SELECT * FROM v2_schedules ORDER BY fires_at_utc,schedule_id"
            ).fetchall()]
        rows = await self.store.submit(op)
        return {"type": "schedule.list.ok", "schedules": [
            _project_schedule(row) for row in rows if self._schedule_visible(row, kind, actor)
        ]}

    async def schedule_get(self, msg: dict[str, Any]) -> dict[str, Any]:
        kind, actor, _ = await self._actor(msg)
        row = await self._schedule_target(str(msg.get("schedule_id") or ""))
        if not self._schedule_visible(row, kind, actor):
            raise VerbError("not_found", "schedule not found")
        include_prompt = kind == "operator" or row.get("owner_stream_id") == actor or row.get("owner_service_actor") == actor
        return {"type": "schedule.get.ok", "schedule": _project_schedule(row, include_prompt=include_prompt)}

    async def _authorize_schedule_mutation(self, msg: dict[str, Any], row: dict[str, Any]) -> tuple[str, str]:
        kind, actor, _ = await self._actor(msg)
        if not self._schedule_visible(row, kind, actor):
            raise VerbError("not_found", "schedule not found")
        if row.get("owner_stream_id") and kind == "seat" and row.get("owner_stream_id") != actor:
            raise VerbError("not_found", "schedule not found")
        return kind, actor

    async def _schedule_mutation(
        self, msg: dict[str, Any], row: dict[str, Any], *, phase: str, kind: str,
        actor: str, update: str, values: tuple[Any, ...], result_type: str,
        expected_states: tuple[str, ...],
    ) -> dict[str, Any]:
        request_id = _valid_uuid(msg.get("request_id"))
        digest = _payload_sha(msg)
        timestamp = _now()
        verb = str(msg.get("type") or "")
        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = _row(conn.execute(
                    "SELECT * FROM v2_operation_receipts WHERE request_id=? AND phase=?", (request_id, phase)
                ).fetchone())
                if existing is not None:
                    self._check_receipt(existing, surface="schedule", verb=verb, actor_kind=kind,
                                        actor_id=actor, payload_sha=digest)
                    conn.rollback()
                    return _decode(existing["result_json"], {})
                placeholders = ",".join("?" for _ in expected_states)
                changed = conn.execute(
                    f"UPDATE v2_schedules SET {update} WHERE schedule_id=? AND generation=? "
                    f"AND state IN ({placeholders})",
                    (*values, row["schedule_id"], row["generation"], *expected_states),
                ).rowcount
                if changed != 1:
                    raise VerbError("invalid_transition", "schedule state/generation changed before commit")
                current = _row(conn.execute("SELECT * FROM v2_schedules WHERE schedule_id=?", (row["schedule_id"],)).fetchone()) or {}
                result = {
                    "type": result_type, "schedule": _project_schedule(current),
                    "receipt_command": self._receipt_command("schedule", request_id, phase),
                }
                self._insert_receipt_tx(
                    conn, request_id=request_id, phase=phase, surface="schedule", verb=verb,
                    actor_kind=kind, actor_id=actor, payload_sha=digest,
                    target_id=str(row["schedule_id"]), measured={"state": current["state"], "generation": current["generation"]},
                    result=result, measured_at=timestamp,
                )
                if current.get("state") in SCHEDULE_TERMINAL and current.get("terminal_at"):
                    self._pin_schedule_receipts_tx(
                        conn, str(row["schedule_id"]), str(current["terminal_at"]),
                    )
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise
        return await self.store.submit(op)

    async def schedule_cancel(self, msg: dict[str, Any]) -> dict[str, Any]:
        await self._actor(msg)
        row = await self._schedule_target(str(msg.get("schedule_id") or ""))
        kind, actor = await self._authorize_schedule_mutation(msg, row)
        replay = await self._mutation_replay(
            msg, phase="terminal", surface="schedule", actor_kind=kind, actor_id=actor,
        )
        if replay is not None:
            return replay
        if row.get("state") not in {"pending", "retry_pending"}:
            raise VerbError("invalid_transition", "cancel requires pending/retry_pending")
        timestamp = _now()
        result = await self._schedule_mutation(
            msg, row, phase="terminal", kind=kind, actor=actor,
            update="state='cancelled',terminal_at=?,updated_at=?", values=(timestamp, timestamp),
            result_type="schedule.cancel.ok",
            expected_states=("pending", "retry_pending"),
        )
        await self._broadcast_schedule_lifecycle(
            "cancelled", str(result["schedule"]["schedule_id"]),
        )
        return result

    async def schedule_reschedule(self, msg: dict[str, Any]) -> dict[str, Any]:
        await self._actor(msg)
        row = await self._schedule_target(str(msg.get("schedule_id") or ""))
        kind, actor = await self._authorize_schedule_mutation(msg, row)
        replay = await self._mutation_replay(
            msg, phase="row_committed", surface="schedule", actor_kind=kind, actor_id=actor,
        )
        if replay is not None:
            return replay
        if row.get("state") != "pending":
            raise VerbError("invalid_transition", "reschedule requires pending")
        fires_at = _parse_time(msg.get("fires_at_utc"))
        timestamp = _now()
        result = await self._schedule_mutation(
            msg, row, phase="row_committed", kind=kind, actor=actor,
            update="fires_at_utc=?,generation=generation+1,updated_at=?", values=(fires_at, timestamp),
            result_type="schedule.reschedule.ok",
            expected_states=("pending",),
        )
        await self._broadcast_schedule_lifecycle(
            "rescheduled", str(result["schedule"]["schedule_id"]),
        )
        return result

    async def schedule_run(self, msg: dict[str, Any]) -> dict[str, Any]:
        await self._actor(msg)
        row = await self._schedule_target(str(msg.get("schedule_id") or ""))
        kind, actor = await self._authorize_schedule_mutation(msg, row)
        for phase in ("spawn_delivered", "terminal"):
            replay = await self._mutation_replay(
                msg, phase=phase, surface="schedule", actor_kind=kind, actor_id=actor,
            )
            if replay is not None:
                return replay
        if row.get("state") not in {"pending", "retry_pending"}:
            raise VerbError("invalid_transition", "run requires pending/retry_pending")
        return await self._fire_schedule(
            str(row["schedule_id"]), operation_request_id=_valid_uuid(msg.get("request_id")),
            operation_msg=msg, operation_actor=(kind, actor),
        )

    def _classify_schedule_fire(
        self,
        schedule: dict[str, Any],
        persisted_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Refuse legacy installation data without accessing external systems."""
        unsupported = (
            schedule.get("attestation_json") is not None
            or bool(schedule.get("prompt_blob_id"))
        )
        return {
            "stored_attestation": None,
            "oracle_error": "unsupported_configuration" if unsupported else None,
        }

    async def _fire_schedule(
        self, schedule_id: str, *, operation_request_id: str | None = None,
        operation_msg: dict[str, Any] | None = None,
        operation_actor: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        timestamp = _now()
        operation_msg = operation_msg or {"type": "schedule.run", "request_id": operation_request_id or ""}
        digest = _payload_sha(operation_msg)

        def prepare(conn: sqlite3.Connection) -> tuple[dict[str, Any], dict[str, Any], str, str, str]:
            conn.execute("BEGIN IMMEDIATE")
            try:
                schedule = _row(conn.execute("SELECT * FROM v2_schedules WHERE schedule_id=?", (schedule_id,)).fetchone())
                if schedule is None:
                    raise VerbError("not_found", "schedule not found")
                if error := objective_error(schedule.get("objective")):
                    conn.execute("UPDATE v2_schedules SET state='failed',last_error_code=?,terminal_at=?,updated_at=? WHERE schedule_id=?", (error, timestamp, timestamp, schedule_id))
                    conn.commit()
                    raise VerbError(error, error)
                if schedule["state"] not in {"pending", "retry_pending"}:
                    raise VerbError("invalid_transition", "schedule already claimed")
                owner_kind = "service" if schedule.get("owner_service_actor") else "seat"
                owner_id = str(schedule.get("owner_service_actor") or schedule.get("owner_stream_id"))
                receipt_kind, receipt_actor = operation_actor or (owner_kind, owner_id)
                op_id = operation_request_id or str(schedule["request_id"])
                dispatch = _row(conn.execute(
                    "SELECT * FROM v2_schedule_dispatches WHERE schedule_id=? AND generation=?",
                    (schedule_id, schedule["generation"]),
                ).fetchone())
                if dispatch is None:
                    spawn_key = f"schedule:{schedule_id}:{schedule['generation']}"
                    spawn_request_id = f"schedule-spawn:{schedule_id}:{schedule['generation']}"
                    conn.execute(
                        "INSERT INTO v2_schedule_dispatches "
                        "(schedule_id,generation,spawn_key,phase,spawn_request_id,prepared_at,evidence_json) "
                        "VALUES (?,? ,?,'prepared',?,?, '{}')",
                        (schedule_id, schedule["generation"], spawn_key, spawn_request_id, timestamp),
                    )
                    dispatch = _row(conn.execute(
                        "SELECT * FROM v2_schedule_dispatches WHERE schedule_id=? AND generation=?",
                        (schedule_id, schedule["generation"]),
                    ).fetchone())
                elif dispatch["phase"] != "prepared":
                    raise VerbError("invalid_transition", "generation dispatch is already claimed")
                conn.execute("UPDATE v2_schedules SET state='firing',updated_at=? WHERE schedule_id=?", (timestamp, schedule_id))
                conn.commit()
                schedule["state"] = "firing"
                return schedule, dispatch or {}, op_id, receipt_kind, receipt_actor
            except Exception:
                conn.rollback()
                raise

        schedule, dispatch, op_id, receipt_kind, receipt_actor = await self.store.submit(prepare)
        await self._broadcast_schedule_lifecycle("firing", schedule_id)
        claimed_at = _now()
        def claim(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN IMMEDIATE")
            try:
                changed = conn.execute(
                    "UPDATE v2_schedule_dispatches SET phase='dispatch_claimed',claimed_at=? "
                    "WHERE schedule_id=? AND generation=? AND phase='prepared'",
                    (claimed_at, schedule_id, schedule["generation"]),
                ).rowcount
                if changed != 1:
                    raise VerbError("invalid_transition", "dispatch claim lost")
                self._insert_receipt_tx(
                    conn, request_id=op_id, phase="dispatch_claimed", surface="schedule",
                    verb="schedule.run", actor_kind=receipt_kind, actor_id=receipt_actor, payload_sha=digest,
                    target_id=schedule_id, measured={"state": "firing", "dispatch_phase": "dispatch_claimed"},
                    result={"type": "schedule.dispatch_claimed", "schedule_id": schedule_id}, measured_at=claimed_at,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        await self.store.submit(claim)
        classification = self._classify_schedule_fire(schedule)
        oracle_error = classification["oracle_error"]
        prompt = None
        if oracle_error is None and schedule.get("prompt_b64"):
            try:
                prompt = base64.b64decode(str(schedule["prompt_b64"]), validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                oracle_error = "invalid_prompt"
        spawn_msg = {
            "type": "spawn", "request_id": dispatch["spawn_request_id"],
            "idempotency_key": dispatch["spawn_key"], "host": schedule["target_host"],
            "provider": schedule["resolved_provider"], "model": schedule["resolved_model"],
            "effort": schedule["resolved_effort"], "role": schedule.get("role"),
            "phase": schedule.get("phase"), "visibility": schedule.get("visibility"),
            "objective": schedule.get("objective"),
            "parent_stream_id": schedule.get("parent_stream_id"),
            "no_watch": bool(schedule.get("no_watch")),
            "handoff_from_stream_id": schedule.get("handoff_from_stream_id"),
            "handoff": bool(schedule.get("handoff_from_stream_id")),
            "spec_ids": _decode(schedule.get("owner_spec_ids_json"), []),
            "initial_prompt": prompt,
            "confirm_model_change": bool(schedule.get("confirm_model_change")),
            "disposition_waived_reason": schedule.get("disposition_waived_reason"),
            "reparent_children": (
                None if schedule.get("reparent_children") is None
                else bool(schedule.get("reparent_children"))
            ),
            "self_close_on_completion": (
                None if schedule.get("self_close_on_completion") is None
                else bool(schedule.get("self_close_on_completion"))
            ),
            "target_sha": schedule.get("target_sha"),
        }
        spawn_msg = {key: value for key, value in spawn_msg.items() if value is not None}
        if oracle_error is not None:
            response = {
                "type": "spawn.error",
                "error_code": oracle_error,
            }
        else:
            try:
                response = await self.spawnctl.spawn(spawn_msg, self.local_host)
            except VerbError as exc:
                response = {"type": "spawn.error", "error_code": exc.code, **getattr(exc, "extra", {})}
            except Exception as exc:  # after claim: absence is unproved
                response = {"type": "spawn.error", "error_code": type(exc).__name__}
        transmitted_at = _now()
        child = str(response.get("stream_id") or (response.get("session") or {}).get("stream_id") or "")
        prompt_receipt = response.get("initial_prompt_delivery") if isinstance(response.get("initial_prompt_delivery"), dict) else {}
        prompt_status = str(prompt_receipt.get("delivery_status") or prompt_receipt.get("state") or "")
        prompt_required = bool(schedule.get("prompt_sha256"))
        delivered = response.get("type") == "spawn.ok" and bool(child) and (
            prompt_status == "delivered" if prompt_required else prompt_status in {"", "not_requested", "delivered"}
        )
        reply_delivered = delivered
        admitted: list[str] | None = [] if oracle_error is not None else None
        if oracle_error is None:
            try:
                admitted = await self.store.admitted_session_names_for_key(
                    str(schedule["target_host"]), str(dispatch["spawn_key"]),
                )
            except Exception:  # verification failure is uncertainty, never absence
                admitted = None
        durable_outcome = None
        if oracle_error is None and not delivered:
            try:
                durable_outcome = await self.store.get_spawn_outcome_by_request_id(
                    str(dispatch["spawn_request_id"])
                )
            except Exception:  # verification failure is uncertainty, never proof
                durable_outcome = None
            durable_proof = _unproven_spawn_delivery_proof(
                durable_outcome,
                admitted,
                target_host=str(schedule["target_host"]),
                prompt_required=prompt_required,
            )
            if durable_proof is not None:
                child, prompt_status = durable_proof
                delivered = True
        response["delivery_proof"] = (
            "spawn_reply" if reply_delivered
            else "durable_spawn_outcome" if delivered
            else "unproved"
        )
        if durable_outcome is not None:
            response["measured_spawn_outcome"] = durable_outcome
        response["measured_admitted_sessions"] = admitted
        durable_rejection = response.get("type") == "spawn.error" and admitted == []
        final_state = "fired" if delivered else ("failed" if durable_rejection else "indeterminate")
        dispatch_phase = "spawn_delivered" if delivered else final_state
        terminal_at = _now()
        error_code = None if delivered else str(response.get("error_code") or response.get("error") or "delivery_unproved")
        def finish(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE v2_schedule_dispatches SET phase='dispatch_transmitted',transmitted_at=? "
                    "WHERE schedule_id=? AND generation=? AND phase='dispatch_claimed'",
                    (transmitted_at, schedule_id, schedule["generation"]),
                )
                conn.execute(
                    "UPDATE v2_schedule_dispatches SET phase=?,outcome_at=?,"
                    "spawn_outcome_id=?,child_stream_id=?,prompt_delivery_status=?,error_code=?,evidence_json=? "
                    "WHERE schedule_id=? AND generation=? AND phase='dispatch_transmitted'",
                    (dispatch_phase, terminal_at, dispatch["spawn_request_id"], child or None,
                     prompt_status or ("not_requested" if not prompt_required else None), error_code,
                     _compact(response), schedule_id, schedule["generation"]),
                )
                conn.execute(
                    "UPDATE v2_schedules SET state=?,terminal_at=?,updated_at=?,last_error_code=? WHERE schedule_id=?",
                    (final_state, terminal_at, terminal_at, error_code, schedule_id),
                )
                current = _row(conn.execute("SELECT * FROM v2_schedules WHERE schedule_id=?", (schedule_id,)).fetchone()) or {}
                result = {
                    "type": "schedule.run.ok" if delivered else f"schedule.run.{final_state}",
                    "schedule": _project_schedule(current), "spawn": response,
                    "receipt_command": self._receipt_command(
                        "schedule", op_id, "spawn_delivered" if delivered else "terminal",
                    ),
                }
                final_phase = "spawn_delivered" if delivered else "terminal"
                self._insert_receipt_tx(
                    conn, request_id=op_id, phase=final_phase, surface="schedule",
                    verb="schedule.run", actor_kind=receipt_kind, actor_id=receipt_actor, payload_sha=digest,
                    target_id=schedule_id, measured={"state": final_state, "dispatch_phase": dispatch_phase, "child_stream_id": child or None},
                    result=result, measured_at=terminal_at,
                )
                self._pin_schedule_receipts_tx(conn, schedule_id, terminal_at)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise
        result = await self.store.submit(finish)
        await self._broadcast_schedule_lifecycle(final_state, schedule_id)
        if not delivered:
            raise VerbError(final_state, f"schedule dispatch {final_state}", schedule=result["schedule"], spawn=response,
                            receipt_command=self._receipt_command("schedule", op_id, "terminal"))
        return result

    async def recover(self) -> None:
        def retire_legacy(conn):
            conn.execute("UPDATE v2_schedules SET state='failed',last_error_code='objective_required',terminal_at=?,updated_at=? WHERE objective IS NULL AND state IN ('pending','retry_pending','firing')", (_now(), _now()))
            conn.commit()
        await self.store.submit(retire_legacy)
        def scan(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            return [dict(row) for row in conn.execute(
                "SELECT d.*,s.state AS schedule_state,s.target_host,s.prompt_sha256,"
                "s.attestation_json,s.prompt_blob_id,s.target_sha "
                "FROM v2_schedule_dispatches d "
                "JOIN v2_schedules s ON s.schedule_id=d.schedule_id "
                "WHERE s.state='firing'"
            ).fetchall()]
        rows = await self.store.submit(scan)
        for dispatch in rows:
            if dispatch["phase"] == "prepared":
                timestamp = _now()
                await self.store.submit(lambda conn, sid=dispatch["schedule_id"], ts=timestamp: (
                    conn.execute("UPDATE v2_schedules SET state='retry_pending',updated_at=? WHERE schedule_id=? AND state='firing'", (ts, sid)),
                    conn.commit(),
                ))
                await self._broadcast_schedule_lifecycle(
                    "retry_pending", str(dispatch["schedule_id"]),
                )
                continue
            persisted_evidence = _decode(dispatch.get("evidence_json"), {})
            if not isinstance(persisted_evidence, dict):
                persisted_evidence = {}
            classification = self._classify_schedule_fire(
                dispatch, persisted_evidence,
            )
            oracle_error = classification["oracle_error"]
            try:
                outcome = (
                    None
                    if oracle_error is not None
                    else await self.store.get_spawn_outcome_by_request_id(
                        str(dispatch["spawn_request_id"])
                    )
                )
            except Exception:  # failed verification is never proof of absence
                outcome = None
            try:
                admitted = (
                    []
                    if oracle_error is not None
                    else await self.store.admitted_session_names_for_key(
                        str(dispatch["target_host"]), str(dispatch["spawn_key"]),
                    )
                )
            except Exception:
                admitted = None
            state = "indeterminate"
            child = None
            if oracle_error is not None:
                state = "failed"
            elif outcome and outcome.get("state") == "delivered":
                receipt = outcome.get("delivery_receipt") or {}
                if receipt.get("state") in {"delivered", "not_requested"}:
                    state = "fired"
                    child = f"{outcome.get('host')}:{outcome.get('session_name')}"
            elif durable_proof := _unproven_spawn_delivery_proof(
                outcome,
                admitted,
                target_host=str(dispatch["target_host"]),
                prompt_required=bool(dispatch.get("prompt_sha256")),
            ):
                state = "fired"
                child = durable_proof[0]
            elif outcome and outcome.get("state") == "failed" and admitted == []:
                state = "failed"
            timestamp = _now()
            evidence = {
                "recovered": True, "spawn_outcome": outcome,
                "measured_admitted_sessions": admitted,
            }
            error_code = (
                None
                if state == "fired"
                else (
                    oracle_error
                    if oracle_error is not None
                    else "recovery_unproved"
                )
            )
            def settle(
                conn: sqlite3.Connection,
                sid=dispatch["schedule_id"],
                generation=dispatch["generation"],
                terminal=state,
                child_id=child,
                ts=timestamp,
                measured=evidence,
                terminal_error_code=error_code,
            ) -> None:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    claim_receipt = _row(conn.execute(
                        "SELECT * FROM v2_operation_receipts WHERE target_id=? "
                        "AND phase='dispatch_claimed' ORDER BY measured_at DESC LIMIT 1",
                        (sid,),
                    ).fetchone())
                    if claim_receipt is None:
                        raise RuntimeError("claimed dispatch is missing its operation receipt")
                    final_phase = "spawn_delivered" if terminal == "fired" else "terminal"
                    dispatch_phase = "spawn_delivered" if terminal == "fired" else terminal
                    conn.execute(
                        "UPDATE v2_schedule_dispatches SET phase=?,outcome_at=?,child_stream_id=?,"
                        "spawn_outcome_id=?,error_code=?,evidence_json=? "
                        "WHERE schedule_id=? AND generation=?",
                        (dispatch_phase, ts, child_id, dispatch["spawn_request_id"],
                         terminal_error_code, _compact(measured), sid, generation),
                    )
                    conn.execute(
                        "UPDATE v2_schedules SET state=?,terminal_at=?,updated_at=?,last_error_code=? "
                        "WHERE schedule_id=?",
                        (terminal, ts, ts, terminal_error_code, sid),
                    )
                    current = _row(conn.execute(
                        "SELECT * FROM v2_schedules WHERE schedule_id=?", (sid,),
                    ).fetchone()) or {}
                    result = {
                        "type": "schedule.run.ok" if terminal == "fired" else f"schedule.run.{terminal}",
                        "schedule": _project_schedule(current),
                        "spawn": measured.get("spawn_outcome"),
                        "recovered": True,
                        "receipt_command": self._receipt_command(
                            "schedule", str(claim_receipt["request_id"]), final_phase,
                        ),
                    }
                    self._insert_receipt_tx(
                        conn, request_id=str(claim_receipt["request_id"]), phase=final_phase,
                        surface="schedule", verb=str(claim_receipt["verb"]),
                        actor_kind=str(claim_receipt["actor_kind"]), actor_id=str(claim_receipt["actor_id"]),
                        payload_sha=str(claim_receipt["canonical_payload_sha256"]), target_id=sid,
                        measured={"state": terminal, "dispatch_phase": dispatch_phase,
                                  "child_stream_id": child_id, "recovered": True},
                        result=result, measured_at=ts,
                    )
                    self._pin_schedule_receipts_tx(conn, sid, ts)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            await self.store.submit(settle)
            await self._broadcast_schedule_lifecycle(state, str(dispatch["schedule_id"]))

    async def run_forever(self) -> None:
        await self.recover()
        while True:
            now = _now()
            def due(conn: sqlite3.Connection) -> list[str]:
                return [str(row[0]) for row in conn.execute(
                    "SELECT schedule_id FROM v2_schedules WHERE state IN ('pending','retry_pending') "
                    "AND fires_at_utc<=? ORDER BY fires_at_utc,schedule_id LIMIT 8", (now,)
                ).fetchall()]
            for schedule_id in await self.store.submit(due):
                try:
                    await self._fire_schedule(schedule_id)
                except VerbError:
                    pass
                except Exception:
                    log.exception("scheduled spawn pass failed schedule_id=%s", schedule_id)
            await asyncio.sleep(self.poll_interval_s)
