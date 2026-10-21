"""Durable scheduling through caller-supplied store and spawn adapters.

Authentication arrives through the server's verified request context. Host,
provider, model, and specification policy belong to the supplied adapters; this
module contains no credentials or installation-specific configuration. Inline
prompts are caller-provided data. External prompt archives and installation
attestations are unsupported and fail closed, including on recovered rows.
"""

from __future__ import annotations

from _shared.spawn_objective import objective_error, resolve_objective

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

import recurrence
from sessions import VerbError
from store_specs import effective_assignment, normalize_spec_ids


RESOURCE_RE = re.compile(r"[a-z0-9][a-z0-9._:/-]{0,127}\Z")
TARGET_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
SCHEDULE_TERMINAL = frozenset({"fired", "cancelled", "failed", "indeterminate", "expired"})
RECEIPT_RETENTION_DAYS = 30
INVALID_ATTESTATION_AUDIT_TOKEN = "invalid"
RECURRING_LATE_FIRE_GRACE_S = 900
RECURRING_DUE_LIMIT = 8
RECURRING_OWNER = "recurring"
RECURRING_OPEN_STATES = frozenset({"pending", "retry_pending", "firing"})
# Spawn-template fields; changing any of them re-runs spawn admission.
RECURRING_TEMPLATE_KEYS = frozenset({
    "provider", "model", "effort", "host", "target_host", "role", "phase", "visibility",
    "parent", "spec_id", "spec_ids", "objective", "initial_prompt", "self_close_on_completion",
})
RECURRING_SCHEDULE_KEYS = ("every", "at_time", "days", "tz")
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


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _from_iso(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _recurrence_error(exc: recurrence.RecurrenceError) -> VerbError:
    return VerbError(exc.code, str(exc))


def _project_recurring(row: dict[str, Any] | None, *, include_prompt: bool = False) -> dict[str, Any] | None:
    """Project a rule for the wire: recurrence in its zone, never the raw prompt."""
    if row is None:
        return None
    value = dict(row)
    rule = _decode(value.pop("recurrence_json", "{}"), {})
    value["recurrence"] = rule
    value["tz"] = rule.get("tz")
    value["recurrence_text"] = recurrence.describe(rule) if rule else ""
    try:
        value["next_fire_local"] = _from_iso(value["next_fire_at_utc"]).astimezone(
            recurrence.zone(str(rule.get("tz")))
        ).isoformat(timespec="seconds")
    except (KeyError, ValueError, recurrence.RecurrenceError):
        value["next_fire_local"] = None
    value["owner_spec_ids"] = _decode(value.pop("owner_spec_ids_json", "[]"), [])
    value["owner_spec_provenance"] = _decode(value.pop("owner_spec_provenance_json", "[]"), [])
    if value.get("self_close_on_completion") is not None:
        value["self_close_on_completion"] = bool(value["self_close_on_completion"])
    value["provider"] = value.get("resolved_provider")
    value["model"] = value.get("resolved_model")
    value["effort"] = value.get("resolved_effort")
    encoded = value.pop("prompt_b64", None)
    try:
        prompt = base64.b64decode(encoded or "").decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        prompt = ""
    value["prompt_preview"] = WindowSchedule._prompt_preview(prompt)
    if include_prompt:
        value["initial_prompt_b64"] = encoded
    fired, missed = value.get("last_fired_at_utc"), value.get("last_missed_at")
    if missed and (not fired or missed > fired):
        value.update(last_result="missed", last_result_at=missed, last_result_reason=value.get("last_missed_reason"))
    elif fired:
        value.update(last_result="fired", last_result_at=fired, last_result_reason=None)
    else:
        value.update(last_result=None, last_result_at=None, last_result_reason=None)
    return value


_RECURRING_SELECT = (
    "SELECT r.*,s.state AS last_schedule_state,d.child_stream_id AS last_child_stream_id "
    "FROM v2_recurring_schedules r "
    "LEFT JOIN v2_schedules s ON s.schedule_id=r.last_schedule_id "
    "LEFT JOIN v2_schedule_dispatches d ON d.schedule_id=s.schedule_id AND d.generation=s.generation "
)


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
        # Resolves the assistant alias to its currently bound seat id (or None).
        self.assistant_binding: Callable[[], Awaitable[str | None]] | None = None

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
            "schedule.recurring.create": self.recurring_create,
            "schedule.recurring.list": self.recurring_list,
            "schedule.recurring.get": self.recurring_get,
            "schedule.recurring.update": self.recurring_update,
            "schedule.recurring.pause": self.recurring_pause,
            "schedule.recurring.resume": self.recurring_resume,
            "schedule.recurring.run": self.recurring_run,
            "schedule.recurring.delete": self.recurring_delete,
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
        # Objective resolution is lineage-aware and needs the parent, computed below.
        request_id = _valid_uuid(msg.get("request_id"))
        kind, actor, seat = await self._actor(msg)
        auth = msg.get("_auth_context") or {}
        owner_generation = str(auth.get("session_generation") or "")
        if kind != "seat" or owner_generation != str((seat or {}).get("session_generation") or ""):
            owner_generation = ""
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
        # Mirror live-spawn admission: a parented scheduled child requires an
        # objective; a top-level/handoff scheduled spawn derives one from its
        # brief so the stored row carries a concrete objective for fire.
        objective, objective_source, error = resolve_objective(
            msg.get("objective"), objective_supported=msg.get("objective_supported"),
            parent_stream_id=parent_stream_id,
            brief=str(msg.get("initial_prompt") or ""), title=msg.get("title"),
        )
        if error:
            raise VerbError(error, error)
        msg = {**msg, "objective": objective, "objective_source": objective_source}
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
        link_refusal = getattr(self.spawnctl, "_work_link_refusal", None)
        if kind == "seat" and not handoff and link_refusal is not None:
            # R4: a scheduled new commission is checked here, at insert; the fire never refuses.
            # The assignment leads the stored ids, so the fire binds it instead of a portfolio tag.
            assignment = requested_spec_ids[0] if requested_spec_ids else effective_assignment(seat)
            refusal = await link_refusal(
                {**msg, "parent_stream_id": msg.get("parent_stream_id") or actor},
                {"assigned_spec_id": normalize_spec_ids(spec_id=assignment)[0] if assignment else None})
            if refusal is not None:
                raise refusal
            canonical = normalize_spec_ids(spec_id=assignment)[0]
            admission_spec_ids = [assignment, *[value for value in admission_spec_ids
                                                if normalize_spec_ids(spec_id=value) != [canonical]]]
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
                        "target_sha,attestation_json,reparent_children,self_close_on_completion,objective,objective_source,no_watch) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',1,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                         msg["objective"], msg.get("objective_source"), int(bool(msg.get("no_watch")))),
                    )
                    conn.execute("UPDATE v2_schedules SET qa_spec_id=?,qa_surface=?,qa_cycle=?,qa_owner_generation=? WHERE schedule_id=?",
                        (msg.get("qa_spec_id"), msg.get("qa_surface"), msg.get("qa_cycle"),
                         admission.get("qa_owner_generation"), schedule_id))
                    prior = _row(conn.execute("SELECT * FROM v2_schedules WHERE schedule_id=?", (schedule_id,)).fetchone())
                result = {
                    "type": "schedule.insert.ok", "schedule": _project_schedule(prior),
                    "receipt_command": self._receipt_command("schedule", request_id, "row_committed"),
                }
                self._insert_receipt_tx(
                    conn, request_id=request_id, phase="row_committed", surface="schedule",
                    verb=verb, actor_kind=kind, actor_id=actor, payload_sha=digest,
                    target_id=str(prior["schedule_id"]), measured={"state": prior["state"], "generation": prior["generation"],
                        "owner_session_generation": owner_generation or None},
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
        return (
            kind in {"operator", "nexus"} or row.get("owner_stream_id") == actor
            or row.get("owner_service_actor") == actor
            # The creating seat of a recurring rule sees the occurrences it owns.
            or (row.get("owner_service_actor") == RECURRING_OWNER
                and row.get("created_by_stream_id") == actor)
        )

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
                # A properly-inserted schedule always carries a resolved objective
                # (parented required, top-level derived at insert); a blank one is a
                # legacy row to retire, so this re-validation stays presence-only.
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
        # Retain the admitting generation rather than resolving today's owner.
        # Protected handoffs without that binding fail before dispatch.
        admission_receipt = await self._get_receipt(schedule["request_id"], "row_committed")
        owner_generation = None
        if (admission_receipt and admission_receipt["surface"] == "schedule"
                and admission_receipt["verb"] == "schedule.insert"
                and admission_receipt["actor_kind"] == "seat"
                and admission_receipt["target_id"] == schedule_id):
            original_generation = _decode(admission_receipt.get("measured_state_json"), {}).get("owner_session_generation")
            effective_owner, effective_generation = await self.store.transfer_successor(admission_receipt['actor_id'], original_generation)
            if effective_owner == schedule.get('owner_stream_id'):
                owner_generation = effective_generation
        source_id = str(schedule.get("handoff_from_stream_id") or "")
        policy = getattr(self.sessions, "assistant", None)
        source = (await self.store.fetch_session(*source_id.split(":", 1))
                  if policy and ":" in source_id else None)
        if (source_id and policy and (policy.protects(source)
                or (policy.role and schedule.get("role") == policy.role))):
            if (not owner_generation or source_id != schedule.get("owner_stream_id")
                    or not source or source.get("status") != "open"
                    or source.get("session_generation") != owner_generation):
                oracle_error = "stale_owner_generation"
        spawn_msg = {
            # The durable schedule was owner-authorized at admission; this
            # internal identity is never accepted from a client payload.
            "_auth_context": {
                "service_authenticated": True, "service_actor": "daemon:scheduler",
                "token_verified": bool(schedule.get("owner_stream_id")),
                "stream_id": schedule.get("owner_stream_id"),
                "session_generation": owner_generation,
            },
            "type": "spawn", "request_id": dispatch["spawn_request_id"],
            "idempotency_key": dispatch["spawn_key"], "host": schedule["target_host"],
            "provider": schedule["resolved_provider"], "model": schedule["resolved_model"],
            "effort": schedule["resolved_effort"], "role": schedule.get("role"),
            "phase": schedule.get("phase"), "visibility": schedule.get("visibility"),
            "objective": schedule.get("objective"),
            "objective_source": schedule.get("objective_source"),
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
            "qa_spec_id": schedule.get("qa_spec_id"),
            "qa_surface": schedule.get("qa_surface"),
            "qa_cycle": schedule.get("qa_cycle"),
            "_qa_owner_generation": schedule.get("qa_owner_generation"),
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
            # Post-fix inserts always store a resolved objective (parented required,
            # top-level derived), so a NULL objective marks a pre-objective legacy
            # row that must not fire; retire it regardless of lineage.
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
        # Same evaluation as the periodic pass, applied to every due active rule.
        while await self.materialise_due_recurring() >= RECURRING_DUE_LIMIT:
            pass

    # ---- recurring rules -------------------------------------------------

    async def recurring_inventory(self) -> list[dict[str, Any]]:
        """Every rule, projected for the authenticated operator snapshot."""
        if self.schema_health != "ok":
            return []

        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            return [dict(row) for row in conn.execute(
                _RECURRING_SELECT + "ORDER BY r.created_at,r.recurring_id"
            ).fetchall()]

        return [_project_recurring(row) or {} for row in await self.store.submit(op)]

    async def _recurring_row(self, recurring_id: str) -> dict[str, Any] | None:
        def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            return _row(conn.execute(
                _RECURRING_SELECT + "WHERE r.recurring_id=?", (recurring_id,),
            ).fetchone())
        return await self.store.submit(op)

    async def _recurring_target(self, msg: dict[str, Any], kind: str, actor: str) -> dict[str, Any]:
        row = await self._recurring_row(str(msg.get("recurring_id") or ""))
        # A seat reaches only the rules it created; absence and ownership look the same.
        if row is None or (kind == "seat" and row.get("created_by_stream_id") != actor):
            raise VerbError("not_found", "recurring schedule not found")
        return row

    async def _broadcast_recurring(
        self, event: str, recurring_id: str, *, row: dict[str, Any] | None = None,
    ) -> None:
        if self.broadcast is None:
            return
        try:
            row = row or await self._recurring_row(recurring_id)
            if row is None:
                return
            await self.broadcast({
                "type": "schedule.lifecycle", "event": event,
                "recurring": _project_recurring(row),
            })
        except Exception:  # a committed mutation must not become a false RPC failure
            log.exception("recurring lifecycle broadcast failed recurring_id=%s event=%s", recurring_id, event)

    async def _assistant_parent(self) -> str | None:
        """The open seat the assistant alias is bound to right now, else None."""
        if self.assistant_binding is None:
            return None
        try:
            stream_id = str(await self.assistant_binding() or "").strip()
        except Exception:
            log.exception("assistant binding lookup failed")
            return None
        seat = await self._session(stream_id) if stream_id else None
        if seat is None or str(seat.get("status") or "open") != "open":
            return None
        return stream_id

    async def _recurring_template(
        self, msg: dict[str, Any], fields: dict[str, Any], *, kind: str, actor: str,
        seat: dict[str, Any] | None, admission_name: str,
    ) -> dict[str, Any]:
        """Admit the spawn template once, mirroring ``schedule_insert`` admission.

        ``fields`` is the complete template (create: the request; update: the
        stored template overlaid with the request). Returns the columns to store.
        """
        if msg.get("agent_orch_attestation") is not None or fields.get("initial_prompt_blob_sha"):
            raise VerbError("unsupported_configuration", "use an inline prompt")
        if any(msg.get(key) for key in ("handoff", "handoff_from_stream_id")):
            raise VerbError("unsupported_configuration", "recurring rules cannot hand off")
        if msg.get("resume_session_id") is not None or msg.get("resume") is not None:
            raise VerbError("resume_not_schedulable", "resume cannot be scheduled")
        if msg.get("confirm_model_change"):
            raise VerbError("handoff_only_flag", "confirm_model_change applies only to handoff")
        if "idempotency_key" in msg:
            raise VerbError("idempotency_key_not_schedulable", "recurring dispatch identity is rule-owned")
        prompt_text = fields.get("initial_prompt")
        if not isinstance(prompt_text, str) or not prompt_text.strip():
            raise VerbError("invalid_request", "initial_prompt is required")
        parent_mode = str(fields.get("parent") or "assistant").strip()
        parent_stream_id = None
        if parent_mode not in {"assistant", "none"}:
            if ":" not in parent_mode or await self._session(parent_mode) is None:
                raise VerbError("parent_not_found", "recurring parent must exist at create")
            parent_stream_id = parent_mode
        self_close = fields.get("self_close_on_completion")
        if self_close is not None and not isinstance(self_close, bool):
            raise VerbError("invalid_request", "self_close_on_completion must be boolean when specified")
        if self_close is not None and parent_mode == "none":
            raise VerbError("self_close_requires_parent", "self_close_on_completion requires parent lineage")
        objective, objective_source, error = resolve_objective(
            fields.get("objective"), objective_supported=True,
            parent_stream_id=None if parent_mode == "none" else (parent_stream_id or "assistant"),
            brief=prompt_text, title=fields.get("name"),
        )
        if error:
            raise VerbError(error, error)
        owner_spec_ids = [
            str(value) for value in (seat or {}).get("qualified_spec_ids") or [] if str(value)
        ]
        requested_spec_ids = list(
            fields.get("spec_ids") or ([fields["spec_id"]] if fields.get("spec_id") else [])
        )
        admission_spec_ids = requested_spec_ids or owner_spec_ids
        if not admission_spec_ids:
            raise VerbError("not_authorized", "recurring owner must be spec-bound with provenance")
        link_refusal = getattr(self.spawnctl, "_work_link_refusal", None)
        if kind == "seat" and link_refusal is not None:
            # Admission-time only, like a one-shot insert; materialisation never refuses on it.
            assignment = requested_spec_ids[0] if requested_spec_ids else effective_assignment(seat)
            refusal = await link_refusal(
                {**msg, "parent_stream_id": parent_stream_id or actor},
                {"assigned_spec_id": normalize_spec_ids(spec_id=assignment)[0] if assignment else None})
            if refusal is not None:
                raise refusal
            canonical = normalize_spec_ids(spec_id=assignment)[0]
            admission_spec_ids = [assignment, *[value for value in admission_spec_ids
                                                if normalize_spec_ids(spec_id=value) != [canonical]]]
        requested = {
            key: str((fields[f"requested_{key}"] if f"requested_{key}" in fields else fields.get(key)) or "").strip()
            for key in ("provider", "model", "effort")
        }
        host = str(fields.get("target_host") or fields.get("host") or self.local_host)
        admission_msg = {
            **msg, "host": host, "provider": requested["provider"] or msg.get("provider"),
            "model": requested["model"] or None, "effort": requested["effort"] or None,
            "spec_ids": admission_spec_ids, "target_sha": None, "objective": objective,
            "role": fields.get("role"), "phase": fields.get("phase"),
            "visibility": fields.get("visibility"), "initial_prompt": prompt_text,
        }
        admission_msg.pop("spec_id", None)
        if parent_stream_id is not None:
            admission_msg["parent_stream_id"] = parent_stream_id
        else:
            admission_msg.pop("parent_stream_id", None)
        admission = await self.spawnctl.admit_schedule(
            admission_msg, self.local_host, admission_name=admission_name,
        )
        binding = admission.get("spec_binding")
        if not isinstance(binding, dict):
            raise VerbError("not_authorized", "recurring admission returned no canonical spec binding")
        spec_ids = [str(value) for value in binding.get("qualified_spec_ids") or [] if str(value)]
        canonical_provenance = binding.get("spec_binding_provenance")
        provenance = [
            dict(entry) for entry in canonical_provenance
            if isinstance(entry, dict) and str(entry.get("spec_id") or "") in spec_ids
        ] if isinstance(canonical_provenance, list) else []
        if (
            not spec_ids or len(set(spec_ids)) != len(spec_ids)
            or {str(entry.get("spec_id") or "") for entry in provenance} != set(spec_ids)
            or len(provenance) != len(spec_ids)
        ):
            raise VerbError("not_authorized", "canonical recurring admission binding lacks complete spec provenance")
        if kind == "seat" and any(spec_id not in owner_spec_ids for spec_id in spec_ids):
            raise VerbError("not_authorized", "requested spec selection is outside the owner's qualified binding")
        resolved = {
            key: str(admission.get(f"resolved_{key}") or "").strip()
            for key in ("provider", "model", "effort")
        }
        if not all(resolved.values()):
            raise VerbError("invalid_request", "recurring spawn resolution is incomplete")
        raw = prompt_text.encode("utf-8")
        return {
            "parent_mode": parent_mode,
            "target_host": str(admission.get("host") or self.local_host),
            "requested_provider": requested["provider"], "requested_model": requested["model"],
            "requested_effort": requested["effort"],
            "resolved_provider": resolved["provider"], "resolved_model": resolved["model"],
            "resolved_effort": resolved["effort"],
            "role": admission.get("role"), "phase": fields.get("phase"),
            "visibility": fields.get("visibility"),
            "objective": objective, "objective_source": objective_source,
            "prompt_sha256": hashlib.sha256(raw).hexdigest(),
            "prompt_b64": base64.b64encode(raw).decode("ascii"),
            "owner_spec_ids_json": _compact(spec_ids),
            "owner_spec_provenance_json": _compact(provenance),
            "self_close_on_completion": None if self_close is None else int(self_close),
        }

    @staticmethod
    def _recurring_name(value: Any) -> str:
        name = str(value or "").strip()
        if not name or len(name) > 120 or len(name.splitlines()) != 1:
            raise VerbError("invalid_request", "name must be one line of 1-120 characters")
        return name

    @staticmethod
    def _recurring_rule(values: dict[str, Any]) -> dict[str, Any]:
        try:
            return recurrence.normalize(values)
        except recurrence.RecurrenceError as exc:
            raise _recurrence_error(exc) from exc

    @staticmethod
    def _next_fire(rule: dict[str, Any], after: datetime) -> str:
        try:
            return _iso(recurrence.next_occurrence(rule, after))
        except recurrence.RecurrenceError as exc:
            raise _recurrence_error(exc) from exc

    async def recurring_create(self, msg: dict[str, Any]) -> dict[str, Any]:
        request_id = _valid_uuid(msg.get("request_id"))
        kind, actor, seat = await self._actor(msg)
        if kind not in {"seat", "service"}:
            raise VerbError("not_authorized", "recurring owner must be a seat or service actor")
        replay = await self._mutation_replay(
            msg, phase="row_committed", surface="schedule", actor_kind=kind, actor_id=actor,
        )
        if replay is not None:
            return replay
        name = self._recurring_name(msg.get("name"))
        rule = self._recurring_rule(msg)
        creator = actor if kind == "seat" else None
        recurring_id = str(uuid.uuid4())
        template = await self._recurring_template(
            msg, {**msg, "name": name}, kind=kind, actor=actor, seat=seat,
            admission_name=f"recurring-{recurring_id[-12:]}",
        )
        now = datetime.now(timezone.utc)
        timestamp = _iso(now)
        next_fire = self._next_fire(rule, now)
        digest = _payload_sha(msg)
        columns = {
            "recurring_id": recurring_id, "name": name, "state": "active",
            "recurrence_json": _compact(rule), "next_fire_at_utc": next_fire,
            "created_by_stream_id": creator, "generation": 1,
            "created_at": timestamp, "updated_at": timestamp, **template,
        }

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute("BEGIN IMMEDIATE")
            try:
                marks = ",".join("?" for _ in columns)
                conn.execute(
                    f"INSERT INTO v2_recurring_schedules ({','.join(columns)}) VALUES ({marks})",
                    tuple(columns.values()),
                )
                current = _row(conn.execute(
                    _RECURRING_SELECT + "WHERE r.recurring_id=?", (recurring_id,),
                ).fetchone())
                result = {
                    "type": "schedule.recurring.create.ok", "recurring": _project_recurring(current),
                    "receipt_command": self._receipt_command("schedule", request_id, "row_committed"),
                }
                self._insert_receipt_tx(
                    conn, request_id=request_id, phase="row_committed", surface="schedule",
                    verb="schedule.recurring.create", actor_kind=kind, actor_id=actor,
                    payload_sha=digest, target_id=recurring_id,
                    measured={"state": "active", "generation": 1}, result=result, measured_at=timestamp,
                )
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

        result = await self.store.submit(op)
        await self._broadcast_recurring("recurring_created", recurring_id)
        return result

    async def recurring_list(self, msg: dict[str, Any]) -> dict[str, Any]:
        kind, actor, _ = await self._actor(msg)
        rows = await self.recurring_inventory()
        return {"type": "schedule.recurring.list.ok", "recurring": [
            row for row in rows
            if kind != "seat" or row.get("created_by_stream_id") == actor
        ]}

    async def recurring_get(self, msg: dict[str, Any]) -> dict[str, Any]:
        kind, actor, _ = await self._actor(msg)
        row = await self._recurring_target(msg, kind, actor)
        return {"type": "schedule.recurring.get.ok", "recurring": _project_recurring(row, include_prompt=True)}

    async def _recurring_write(
        self, rule_row: dict[str, Any], assignments: dict[str, Any], *, expected_generation: Any,
        recompute: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One CAS-guarded UPDATE: bumps ``generation`` and, with ``recompute``, re-arms from now."""
        recurring_id = str(rule_row["recurring_id"])

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = _row(conn.execute(
                    "SELECT * FROM v2_recurring_schedules WHERE recurring_id=?", (recurring_id,),
                ).fetchone())
                if current is None:
                    raise VerbError("not_found", "recurring schedule not found")
                if expected_generation is not None and int(expected_generation) != int(current["generation"]):
                    raise VerbError("generation_conflict", "recurring schedule changed; re-read and retry")
                now = datetime.now(timezone.utc)
                values = dict(assignments)
                if recompute is not None:
                    values["next_fire_at_utc"] = self._next_fire(recompute, now)
                values.update(updated_at=_iso(now), generation=int(current["generation"]) + 1)
                conn.execute(
                    "UPDATE v2_recurring_schedules SET "
                    + ",".join(f"{key}=?" for key in values) + " WHERE recurring_id=? AND generation=?",
                    (*values.values(), recurring_id, current["generation"]),
                )
                conn.commit()
                return {}
            except Exception:
                conn.rollback()
                raise

        await self.store.submit(op)
        return await self._recurring_row(recurring_id) or {}

    async def recurring_update(self, msg: dict[str, Any]) -> dict[str, Any]:
        kind, actor, seat = await self._actor(msg)
        row = await self._recurring_target(msg, kind, actor)
        expected = msg.get("expected_generation")
        assignments: dict[str, Any] = {}
        recompute = None
        if "name" in msg:
            assignments["name"] = self._recurring_name(msg.get("name"))
        if any(key in msg for key in RECURRING_SCHEDULE_KEYS):
            current = _decode(row["recurrence_json"], {})
            merged = {**current, **{key: msg[key] for key in RECURRING_SCHEDULE_KEYS if key in msg}}
            if "every" in msg and "days" not in msg and merged["every"] != "weekly":
                merged["days"] = []
            recompute = self._recurring_rule(merged)
            assignments["recurrence_json"] = _compact(recompute)
        if RECURRING_TEMPLATE_KEYS & set(msg):
            # Template edits re-run spawn admission as the creating seat (operator-created
            # rules have no seat); plain name/schedule/pause edits never reach this branch.
            creator = row.get("created_by_stream_id")
            creator_row = seat if kind == "seat" else (await self._session(creator) if creator else None)
            prior = {
                "provider": row["requested_provider"], "model": row["requested_model"],
                "effort": row["requested_effort"], "target_host": row["target_host"],
                "role": row.get("role"), "phase": row.get("phase"), "visibility": row.get("visibility"),
                "parent": row["parent_mode"], "spec_ids": _decode(row["owner_spec_ids_json"], []),
                "objective": row["objective"] if row.get("objective_source") == "explicit" else None,
                "initial_prompt": base64.b64decode(row["prompt_b64"]).decode("utf-8"),
                "self_close_on_completion": (
                    None if row.get("self_close_on_completion") is None
                    else bool(row["self_close_on_completion"])
                ),
                "name": assignments.get("name", row["name"]),
            }
            fields = {**prior, **{key: msg[key] for key in RECURRING_TEMPLATE_KEYS if key in msg}}
            if "spec_id" in msg and "spec_ids" not in msg:
                fields["spec_ids"] = []
            assignments.update(await self._recurring_template(
                msg, fields, kind="seat" if creator_row else "operator",
                actor=str(creator or actor), seat=creator_row,
                admission_name=f"recurring-{str(row['recurring_id'])[-12:]}",
            ))
        if not assignments:
            raise VerbError("invalid_request", "nothing to update")
        updated = await self._recurring_write(row, assignments, expected_generation=expected, recompute=recompute)
        await self._broadcast_recurring("recurring_updated", str(row["recurring_id"]))
        return {"type": "schedule.recurring.update.ok", "recurring": _project_recurring(updated)}

    async def recurring_pause(self, msg: dict[str, Any]) -> dict[str, Any]:
        kind, actor, _ = await self._actor(msg)
        row = await self._recurring_target(msg, kind, actor)
        if row["state"] == "paused":
            return {"type": "schedule.recurring.pause.ok", "recurring": _project_recurring(row)}
        updated = await self._recurring_write(
            row, {"state": "paused"}, expected_generation=msg.get("expected_generation"),
        )
        await self._broadcast_recurring("recurring_paused", str(row["recurring_id"]))
        return {"type": "schedule.recurring.pause.ok", "recurring": _project_recurring(updated)}

    async def recurring_resume(self, msg: dict[str, Any]) -> dict[str, Any]:
        kind, actor, _ = await self._actor(msg)
        row = await self._recurring_target(msg, kind, actor)
        if row["state"] == "active":
            return {"type": "schedule.recurring.resume.ok", "recurring": _project_recurring(row)}
        # Re-arms from now: a paused interval is neither replayed nor counted as missed.
        updated = await self._recurring_write(
            row, {"state": "active"}, expected_generation=msg.get("expected_generation"),
            recompute=_decode(row["recurrence_json"], {}),
        )
        await self._broadcast_recurring("recurring_resumed", str(row["recurring_id"]))
        return {"type": "schedule.recurring.resume.ok", "recurring": _project_recurring(updated)}

    async def recurring_delete(self, msg: dict[str, Any]) -> dict[str, Any]:
        kind, actor, _ = await self._actor(msg)
        row = await self._recurring_target(msg, kind, actor)
        recurring_id = str(row["recurring_id"])

        def op(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM v2_recurring_schedules WHERE recurring_id=?", (recurring_id,))
            conn.commit()

        await self.store.submit(op)
        await self._broadcast_recurring("recurring_deleted", recurring_id, row=row)
        return {"type": "schedule.recurring.delete.ok", "recurring_id": recurring_id}

    async def recurring_run(self, msg: dict[str, Any]) -> dict[str, Any]:
        kind, actor, _ = await self._actor(msg)
        row = await self._recurring_target(msg, kind, actor)
        recurring_id = str(row["recurring_id"])
        await self._broadcast_recurring("recurring_run_requested", recurring_id, row=row)
        parent = await self._assistant_parent() if row["parent_mode"] == "assistant" else None
        now = datetime.now(timezone.utc)
        outcome = await self.store.submit(lambda conn: self._materialise_tx(
            conn, recurring_id, now=now, parent_stream=parent, manual=True,
        ))
        if outcome["outcome"] != "materialised":
            reason = outcome.get("reason") or "not_found"
            raise VerbError(reason, f"recurring run refused: {reason}")
        await self._announce_materialised(outcome)
        updated = await self._recurring_row(recurring_id)
        return {
            "type": "schedule.recurring.run.ok", "recurring": _project_recurring(updated),
            "schedule_id": outcome["schedule_id"],
        }

    def _previous_run_open_tx(self, conn: sqlite3.Connection, rule: dict[str, Any]) -> bool:
        """True while the rule's last materialised row is non-terminal or its child seat is open."""
        last = rule.get("last_schedule_id")
        if not last:
            return False
        row = conn.execute(
            "SELECT s.state,d.child_stream_id FROM v2_schedules s "
            "LEFT JOIN v2_schedule_dispatches d ON d.schedule_id=s.schedule_id AND d.generation=s.generation "
            "WHERE s.schedule_id=?", (last,),
        ).fetchone()
        if row is None:
            return False
        if row["state"] in RECURRING_OPEN_STATES:
            return True
        child = str(row["child_stream_id"] or "")
        if ":" not in child:
            return False
        host, name = child.split(":", 1)
        seat = conn.execute(
            "SELECT status FROM sessions WHERE host=? AND session_name=?", (host, name),
        ).fetchone()
        return seat is not None and seat["status"] == "open"

    def _materialise_tx(
        self, conn: sqlite3.Connection, recurring_id: str, *, now: datetime,
        parent_stream: str | None, manual: bool,
    ) -> dict[str, Any]:
        """One rule, one transaction: decide (miss/overlap/unbound/fire), insert, advance.

        The occurrence row is copied straight from the admitted template with a
        deterministic ``request_id`` (UNIQUE), so no restart can produce two rows
        for one occurrence; it never re-runs admission or the spec-link check.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            rule = _row(conn.execute(
                "SELECT * FROM v2_recurring_schedules WHERE recurring_id=?", (recurring_id,),
            ).fetchone())
            timestamp = _iso(now)
            if rule is None or (not manual and rule["state"] != "active"):
                conn.rollback()
                return {"outcome": "noop", "recurring_id": recurring_id}
            schedule_rule = _decode(rule["recurrence_json"], {})
            occurrence = timestamp if manual else str(rule["next_fire_at_utc"])
            if not manual and _from_iso(occurrence) > now:
                conn.rollback()
                return {"outcome": "noop", "recurring_id": recurring_id}
            reason = None
            if not manual and (now - _from_iso(occurrence)).total_seconds() > RECURRING_LATE_FIRE_GRACE_S:
                reason = "late"
            elif self._previous_run_open_tx(conn, rule):
                reason = "previous_run_open"
            elif rule["parent_mode"] == "assistant" and parent_stream is None:
                reason = "parent_unbound"
            if reason is not None:
                if manual:
                    conn.rollback()
                    return {"outcome": "refused", "reason": reason, "recurring_id": recurring_id}
                conn.execute(
                    "UPDATE v2_recurring_schedules SET missed_count=missed_count+1,last_missed_at=?,"
                    "last_missed_reason=?,next_fire_at_utc=?,generation=generation+1,updated_at=? "
                    "WHERE recurring_id=?",
                    (timestamp, reason, self._next_fire(schedule_rule, now), timestamp, recurring_id),
                )
                conn.commit()
                return {"outcome": "missed", "reason": reason, "recurring_id": recurring_id}
            request_id = str(uuid.uuid5(
                uuid.UUID(recurring_id), f"manual:{timestamp}" if manual else occurrence,
            ))
            existing = conn.execute(
                "SELECT schedule_id FROM v2_schedules WHERE request_id=?", (request_id,),
            ).fetchone()
            schedule_id = existing["schedule_id"] if existing else f"sched-{uuid.uuid4().hex}"
            parent = None if rule["parent_mode"] == "none" else (
                parent_stream if rule["parent_mode"] == "assistant" else rule["parent_mode"]
            )
            if existing is None:
                conn.execute(
                    "INSERT INTO v2_schedules "
                    "(schedule_id,request_id,owner_service_actor,owner_spec_ids_json,owner_spec_provenance_json,"
                    "parent_stream_id,created_by_stream_id,target_host,role,phase,visibility,"
                    "requested_provider,requested_model,requested_effort,resolved_provider,resolved_model,"
                    "resolved_effort,fires_at_utc,state,generation,prompt_sha256,prompt_b64,created_at,updated_at,"
                    "self_close_on_completion,objective,objective_source) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',1,?,?,?,?,?,?,?)",
                    (schedule_id, request_id, RECURRING_OWNER, rule["owner_spec_ids_json"],
                     rule["owner_spec_provenance_json"], parent, rule["created_by_stream_id"],
                     rule["target_host"], rule["role"], rule["phase"], rule["visibility"],
                     rule["requested_provider"], rule["requested_model"], rule["requested_effort"],
                     rule["resolved_provider"], rule["resolved_model"], rule["resolved_effort"],
                     occurrence, rule["prompt_sha256"], rule["prompt_b64"], timestamp, timestamp,
                     rule["self_close_on_completion"], rule["objective"], rule["objective_source"]),
                )
                self._insert_receipt_tx(
                    conn, request_id=request_id, phase="row_committed", surface="schedule",
                    verb="schedule.recurring.materialise", actor_kind="service", actor_id=RECURRING_OWNER,
                    payload_sha=hashlib.sha256(_compact(
                        {"recurring_id": recurring_id, "occurrence": occurrence}).encode()).hexdigest(),
                    target_id=schedule_id,
                    measured={"state": "pending", "generation": 1, "recurring_id": recurring_id},
                    result={"type": "schedule.recurring.materialise.ok",
                            "schedule_id": schedule_id, "recurring_id": recurring_id},
                    measured_at=timestamp,
                )
            advance = (
                {} if manual else {"next_fire_at_utc": self._next_fire(schedule_rule, now)}
            )
            values = {"last_fired_at_utc": occurrence, "last_schedule_id": schedule_id, **advance,
                      "updated_at": timestamp}
            conn.execute(
                "UPDATE v2_recurring_schedules SET "
                + ",".join(f"{key}=?" for key in values) + ",generation=generation+1 WHERE recurring_id=?",
                (*values.values(), recurring_id),
            )
            conn.commit()
            return {"outcome": "materialised", "schedule_id": schedule_id,
                    "recurring_id": recurring_id, "created": existing is None}
        except Exception:
            conn.rollback()
            raise

    async def _announce_materialised(self, outcome: dict[str, Any]) -> None:
        if outcome.get("created", True):
            await self._broadcast_schedule_lifecycle("created", str(outcome["schedule_id"]))
        await self._broadcast_recurring("recurring_materialised", str(outcome["recurring_id"]))

    async def materialise_due_recurring(self, now: datetime | None = None) -> int:
        """One pass over due active rules; returns how many were due, or 0 when every one raised."""
        if self.schema_health != "ok":
            return 0
        moment = now or datetime.now(timezone.utc)
        horizon = _iso(moment)

        def due(conn: sqlite3.Connection) -> list[tuple[str, str]]:
            return [(str(row[0]), str(row[1])) for row in conn.execute(
                "SELECT recurring_id,parent_mode FROM v2_recurring_schedules "
                "WHERE state='active' AND next_fire_at_utc<=? ORDER BY next_fire_at_utc,recurring_id LIMIT ?",
                (horizon, RECURRING_DUE_LIMIT),
            ).fetchall()]

        rules = await self.store.submit(due)
        parent = await self._assistant_parent() if any(mode == "assistant" for _, mode in rules) else None
        failed = 0
        for recurring_id, _mode in rules:
            try:
                outcome = await self.store.submit(lambda conn, rid=recurring_id: self._materialise_tx(
                    conn, rid, now=moment, parent_stream=parent, manual=False,
                ))
                if outcome["outcome"] == "materialised":
                    await self._announce_materialised(outcome)
                elif outcome["outcome"] == "missed":
                    await self._broadcast_recurring("recurring_missed", recurring_id)
            except Exception:
                failed += 1
                log.exception("recurring materialise failed recurring_id=%s", recurring_id)
        return len(rules) if failed < len(rules) else 0

    async def run_forever(self) -> None:
        await self.recover()
        while True:
            try:
                await self.materialise_due_recurring()
            except Exception:
                log.exception("recurring materialise pass failed")
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
