"""ledger.py - reports, assets, ledger queries, status cards, titles.

Contract (v2_design.md module table):
  owns: reports, assets, ledger queries, status cards, titles.

Binding requirements:
  - B9: report-then-close is atomic; `await` reads from the durable ledger,
    never from transport; typed report fields are validated at ingest.
  - Asset and other CRUD-shaped verbs are served by the one dispatch table in
    `server.py`, not by bespoke per-verb code.
  - Title/status-card reminder nudges are KEPT (operator 2026-08-04) but
    bounded: cadence-limited and only to live visible sessions (the
    dead-nudge-spam class is pinned).

Awaiter-resolution loop rules: cadence configurable; per-pass cap on durable
rows resolved; backoff on failure; kill switch `--disable-awaiter-resolution`.
Nudge loop rules: cadence configurable; per-pass cap on nudges emitted;
backoff on failure; kill switch `--disable-nudges`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sessions import TITLE_SOURCE_PLACEHOLDER, VerbError
from store import (
    QAAttestationUnverified,
    ReportReplayConflict,
    ReportProvenanceChanged,
    ReportProvenanceUnavailable,
    normalize_spec_ids,
)
from outbound_notices import OutboundNoticeQueue, NOTICE_KIND_REPORT, ensure_notice_marker

SERVICES_ROOT = str(Path(__file__).resolve().parents[1])
if SERVICES_ROOT not in sys.path:  # `_shared` is the fleet-wide schema, not a v2 copy
    sys.path.insert(0, SERVICES_ROOT)

from _shared.report_payload_v1 import (  # noqa: E402
    REPORT_PAYLOAD_FIELDS,
    SchemaError,
    unknown_report_message_violations,
    validate as validate_report_payload,
)
from v2_runtime import env_number, iso_now
log = logging.getLogger("chat_streamd_v2.ledger")

#: v1 `chat_streamd.py:242`. A report outside this set (`progress`) is durable
#: but never settles an await and never announces `child_report_ready`.
TERMINAL_REPORT_STATUSES = frozenset({"done", "error", "aborted"})
REPORT_STATUSES = frozenset({"done", "progress", "error", "aborted"})
#: v1 `chat_streamd.py:270` — the payload fields `_shared.report_payload_v1`
#: validates, and exactly the set persisted as typed columns.
INLINE_SCHEMA_FIELDS = tuple(sorted(REPORT_PAYLOAD_FIELDS))
AWAIT_TIMEOUT_DEFAULT_S = 30.0
AWAIT_TIMEOUT_MAX_S = 900.0
TELL_SUMMARY_MAX_CHARS = 1000
RESULT_BLOB_UNSUPPORTED_HINT = (
    "Use agent-orch report --result with an inline ReportPayloadV1; for oversized "
    "evidence, commit the artifact and include its path in the inline report."
)
QA_ATTESTATION_MODE_ENV = "PENTACLE_QA_ATTESTATION_MODE"
QA_ATTESTATION_MODES = frozenset({"off", "warn", "enforce"})
AUDIT_DEFAULT_LIMIT = 50
AUDIT_MAX_LIMIT = 500
AUDIT_MUTATION_FIELDS = frozenset({"defer", "hold", "queue", "pending", "drain"})

GOAL_MAX_CHARS = 500
UPDATE_MAX_CHARS = 300
STEP_TEXT_MAX_CHARS = 200
PLAN_MAX_STEPS = 20
UPDATES_MAX = 50
STEP_PENDING, STEP_ACTIVE, STEP_DONE = "pending", "active", "done"
_CARD_FIELDS = ("goal", "plan", "step_done", "update", "handoff_planned")
_AC_CHECKBOX_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[([ xX])\]")
_AC_CLAIM_TIMEOUT_S = 8.0


def _claim_wire_fields(
    claim_verified: str | None, mismatch: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project claim verification onto report/close replies without inventing it."""
    if claim_verified not in {"match", "mismatch", "unverifiable"}:
        return {}
    result: dict[str, Any] = {"ac_claim_verified": claim_verified}
    if claim_verified == "mismatch" and isinstance(mismatch, dict):
        result["ac_claim_mismatch"] = mismatch
    return result


def _checkbox_state(spec_text: str) -> list[dict[str, Any]]:
    """Return the authoritative, one-based checkbox state in source order."""
    actual: list[dict[str, Any]] = []
    for line in spec_text.splitlines():
        match = _AC_CHECKBOX_RE.match(line)
        if match is not None:
            actual.append({"index": len(actual) + 1, "checked": match.group(1).lower() == "x"})
    return actual


async def verify_ac_claim(ac_claim: dict[str, Any] | None) -> tuple[str | None, dict[str, Any] | None]:
    """Re-derive a report's claimed AC state from the memory git tree.

    The claim is intentionally a report assertion. Only the path at the exact
    source SHA can produce the comparison record; absent/unreadable source is
    therefore ``unverifiable`` rather than an accidental match.
    """
    if ac_claim is None:
        return None, None
    try:
        from _shared.specs_service import _resolve_specs_memory_root
    except ImportError as exc:
        raise RuntimeError(
            "close verification requires the bundled specs dependency "
            "(_shared.specs_service)"
        ) from exc
    try:
        source = ac_claim["spec_source"]
        root, _ = _resolve_specs_memory_root()
        root = root.expanduser().resolve()
        requested = Path(str(source["path"]))
        source_path = (requested if requested.is_absolute() else root / requested).resolve()
        source_path.relative_to(root)
        relative = source_path.relative_to(root).as_posix()
        sha = str(source["sha"])
        completed = await asyncio.wait_for(
            asyncio.to_thread(
                subprocess.run,
                ["git", "show", f"{sha}:{relative}"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=_AC_CLAIM_TIMEOUT_S,
                check=False,
            ),
            timeout=_AC_CLAIM_TIMEOUT_S + 1.0,
        )
        if completed.returncode != 0 or not completed.stdout:
            return "unverifiable", None
        actual = _checkbox_state(completed.stdout)
        claimed = [dict(item) for item in ac_claim["claims"]]
        actual_by_index = {int(item["index"]): bool(item["checked"]) for item in actual}
        claimed_by_index: dict[int, bool] = {}
        duplicate_indices: set[int] = set()
        for item in claimed:
            index = int(item["index"])
            if index in claimed_by_index:
                duplicate_indices.add(index)
            claimed_by_index[index] = bool(item["checked"])
        unverified = sorted(
            index
            for index in set(actual_by_index) | set(claimed_by_index) | duplicate_indices
            if index in duplicate_indices
            or actual_by_index.get(index) != claimed_by_index.get(index)
        )
        if not unverified and len(actual_by_index) == len(claimed_by_index):
            return "match", None
        return "mismatch", {
            "spec_sha": sha,
            "unverified_indices": unverified,
            "claimed": claimed,
            "actual": actual,
        }
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError, asyncio.TimeoutError):
        return "unverifiable", None


class StatusCardError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


def _qa_attestation_mode(value: object = None) -> str:
    raw = str(
        value if value is not None else os.environ.get(QA_ATTESTATION_MODE_ENV, "warn")
    ).strip().lower()
    return raw if raw in QA_ATTESTATION_MODES else "warn"


def _clean(value: object, *, field: str, max_chars: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise StatusCardError("empty_field", f"{field} must not be empty")
    if len(text) > max_chars:
        raise StatusCardError("too_long", f"{field} must be {max_chars} characters or fewer")
    return text


def _recompute_active(plan: list[dict]) -> list[dict]:
    active_assigned = False
    result = []
    for step in plan:
        status = STEP_DONE if step["status"] == STEP_DONE else STEP_PENDING
        if status != STEP_DONE and not active_assigned:
            status, active_assigned = STEP_ACTIVE, True
        result.append({"text": step["text"], "status": status})
    return result


def apply_status_card_update(current: dict | None, fields: dict, *, now_iso: str) -> dict:
    """New card = `current` + the subset of `fields` present. v1-compatible
    shape (goal / plan[{text,status}] / update + updates[] / handoff_planned).
    On error nothing mutates and no card is returned."""
    if not any(k in fields for k in _CARD_FIELDS):
        raise StatusCardError("no_fields", "at least one card field is required")
    card: dict[str, Any] = dict(current) if isinstance(current, dict) else {}

    if "plan" in fields:  # plan before step_done, so a combined call marks the new plan
        steps = fields["plan"]
        if not isinstance(steps, list) or not steps:
            raise StatusCardError("empty_field", "plan must be a non-empty list of steps")
        if len(steps) > PLAN_MAX_STEPS:
            raise StatusCardError("too_many_steps", f"plan must have <= {PLAN_MAX_STEPS} steps")
        card["plan"] = _recompute_active(
            [{"text": _clean(s, field="plan step", max_chars=STEP_TEXT_MAX_CHARS), "status": STEP_PENDING}
             for s in steps]
        )
    if "step_done" in fields:
        plan = card.get("plan")
        if not isinstance(plan, list) or not plan:
            raise StatusCardError("no_plan", "step-done requires a plan")
        n = fields["step_done"]
        if not isinstance(n, int) or isinstance(n, bool) or not (1 <= n <= len(plan)):
            raise StatusCardError("step_out_of_range", f"step-done must be an integer in 1..{len(plan)}")
        plan = [dict(s) for s in plan]
        plan[n - 1]["status"] = STEP_DONE
        card["plan"] = _recompute_active(plan)
    if "goal" in fields:
        card["goal"] = _clean(fields["goal"], field="goal", max_chars=GOAL_MAX_CHARS)
    if "update" in fields:
        text = _clean(fields["update"], field="update", max_chars=UPDATE_MAX_CHARS)
        card["update"] = text
        history = list(card["updates"]) if isinstance(card.get("updates"), list) else []
        history.append({"ts": now_iso, "text": text})
        card["updates"] = history[-UPDATES_MAX:]
    if "handoff_planned" in fields:
        if not isinstance(fields["handoff_planned"], bool):
            raise StatusCardError("empty_field", "handoff_planned must be a boolean")
        card["handoff_planned"] = fields["handoff_planned"]

    card["updated_at"] = now_iso
    return card


class Ledger:
    """The durable report ledger (B9).

    Three obligations, in order:

    1. **Validate then persist.** A report is typed at ingest against
       `_shared.report_payload_v1` — the same validator v1 uses — and rejected
       with v1's error vocabulary. A malformed report is never half-stored: the
       class of bug where `findings` landed NULL while `summary` survived is a
       partial write, so v2 writes every validated field as its own column or
       writes nothing at all.
    2. **Announce.** `child_report_ready` on BOTH paths (design L13): a
       top-level WS frame, and a pane tell to the parent that follows handoff
       lineage through `comms.tell` — the one injection path.
    3. **Answer from the table, never from transport.** `await_report` reads
       the row; a waiter exists only for a report that has not arrived yet. A
       report ingested with nobody listening is still returned by a later
       await — the durability contract v1 broke.
    """

    def __init__(
        self,
        store: Any,
        sessions: Any = None,
        comms: Any = None,
        broadcast: Any = None,
        routing_integrity: Any = None,
        outbound: OutboundNoticeQueue | None = None,
        alerts: Any = None,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.comms = comms
        self.routing_integrity = routing_integrity
        self.alerts = alerts
        self.qa_attestation_mode = _qa_attestation_mode()
        self.outbound = outbound or OutboundNoticeQueue(store, comms)
        #: async callable(frame) -> None, injected by `main.py` (server.broadcast).
        self._broadcast = broadcast
        #: Awaiters parked on a report that has not been ingested yet.
        self._waiters: list[tuple[str, int | None, asyncio.Future]] = []
        #: Pane tells in flight. Held so the loop cannot GC a live task, and so
        #: shutdown can see them; a tell must never block the ingest reply.
        self._tell_tasks: set[asyncio.Task] = set()
        if sessions is not None:
            set_resolver = getattr(sessions, "set_awaiter_resolver", None)
            if callable(set_resolver):
                set_resolver(self.resolve_awaiters_on_close)

    async def verify_ac_claim(
        self, ac_claim: dict[str, Any] | None,
    ) -> tuple[str | None, dict[str, Any] | None]:
        return await verify_ac_claim(ac_claim)

    def _alert_claim_result(
        self,
        state: str | None,
        mismatch: dict[str, Any] | None,
        *,
        report_id: str,
        from_stream_id: str,
    ) -> None:
        if state not in {"mismatch", "unverifiable"}:
            return
        alerts = self.alerts or getattr(self.sessions, "alerts", None)
        emit = getattr(alerts, "emit", None)
        if callable(emit):
            fields: dict[str, Any] = {
                "report_id": report_id,
                "from_stream_id": from_stream_id,
                "claim_verified": state,
            }
            if isinstance(mismatch, dict):
                fields.update({
                    "spec_sha": mismatch.get("spec_sha"),
                    "unverified_indices": mismatch.get("unverified_indices", []),
                })
            emit("close_claim_mismatch", **fields)

    # -- read-only delivery audit -----------------------------------------

    async def ledger_get(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Read one completed tell record without changing delivery state.

        ``tell_id`` is the normal lookup key and ``ledger_row_id`` is accepted
        as the durable identity echoed by ``tell.ok``. Both paths use the same
        completed-delivery table that powers tell idempotency; neither can
        enqueue, retry, acknowledge, or otherwise mutate a delivery.
        """
        tell_id = msg.get("tell_id")
        row_id = msg.get("ledger_row_id")
        if tell_id is not None and (not isinstance(tell_id, str) or not tell_id.strip()):
            raise VerbError("invalid_request", "tell_id must be a non-empty string")
        if row_id is not None and (
            not isinstance(row_id, int) or isinstance(row_id, bool) or row_id < 1
        ):
            raise VerbError("invalid_request", "ledger_row_id must be a positive integer")
        if tell_id is None and row_id is None:
            raise VerbError("invalid_request", "tell_id or ledger_row_id is required")

        record = (
            await self.store.get_tell_delivery(str(tell_id))
            if tell_id is not None
            else await self.store.get_tell_delivery_by_ledger_row_id(int(row_id))
        )
        if record is None:
            raise VerbError("tell_not_found", "completed tell delivery was not found")
        tell = _tell_audit_record(record)
        return {
            "type": "ledger_get.ok",
            "tell_id": tell["tell_id"],
            "ledger_row_id": tell["ledger_row_id"],
            "tell": tell,
            "read_only": True,
            "source": "v2_tell_deliveries",
        }

    async def inbound_audit(self, msg: dict[str, Any]) -> dict[str, Any]:
        """List already-completed tells addressed to one stream.

        This is a derived view over durable delivery records, not an inbound
        store. Mutation-looking flags are rejected so callers cannot mistake
        the audit request for a defer/hold operation; the always-submit tell
        path remains the only delivery path.
        """
        if any(bool(msg.get(field)) for field in AUDIT_MUTATION_FIELDS):
            raise VerbError(
                "invalid_request",
                "inbound_audit is read-only; delivery is always-submit and cannot be deferred",
            )
        if self.sessions is None:
            raise VerbError("internal_error", "session resolver is not configured")
        host, name = await self.sessions.resolve(msg)
        stream_id = f"{host}:{name}"
        raw_limit = msg.get("limit", AUDIT_DEFAULT_LIMIT)
        if raw_limit is None:
            raw_limit = AUDIT_DEFAULT_LIMIT
        if not isinstance(raw_limit, int) or isinstance(raw_limit, bool) or raw_limit < 0:
            raise VerbError("invalid_request", "limit must be a non-negative integer")
        limit = min(int(raw_limit), AUDIT_MAX_LIMIT)
        records = await self.store.list_tell_deliveries(stream_id, limit=limit)
        frames = [_tell_audit_record(record) for record in records]
        return {
            "type": "inbound_audit.ok",
            "stream_id": stream_id,
            "frames": frames,
            "complete": len(frames) < limit if limit else True,
            "limit": limit,
            "read_only": True,
            "source": "v2_tell_deliveries",
        }

    # -- report ingest -----------------------------------------------------


    async def _report_routing_snapshot(self, stream_id: str) -> dict[str, Any] | None:
        """Obtain a write-time tuple snapshot without consulting the sweep field."""
        observer = self.routing_integrity
        if observer is None and self.sessions is not None:
            from routing_integrity import RoutingIntegrity

            observer = RoutingIntegrity(self.store, self.sessions)
            self.routing_integrity = observer
        if observer is None:
            return None
        fresh_source: bool | None = None
        refresh = getattr(observer, "refresh_for_report", None)
        if callable(refresh):
            try:
                refreshed = await refresh(stream_id)
            except Exception:  # noqa: BLE001 - the verdict gate fails closed below
                log.exception("routing-integrity report refresh failed stream=%s", stream_id)
                refreshed = False
            if refreshed is not None:
                fresh_source = bool(refreshed)
        return await observer.report_write_snapshot(stream_id, fresh_source=fresh_source)

    async def _is_qa_grade_reporter(self, stream_id: str) -> bool:
        """Classify from daemon-owned session role/phase, never report prose."""
        try:
            host, name = self.sessions.split(stream_id)
            row = self.sessions.get(stream_id) or await self.store.fetch_session(host, name)
        except Exception:  # noqa: BLE001 - unavailable provenance fails later at filing
            return False
        if not isinstance(row, dict):
            return False
        return any(str(row.get(field) or "").lower() == "qa" for field in ("role", "phase"))

    @staticmethod
    def _routing_refusal(
        code: str,
        message: str,
        report_id: str,
        snapshot: dict[str, Any] | None,
    ) -> VerbError:
        snapshot = snapshot or {}
        return Ledger._error(
            code,
            message,
            report_id,
            requested_model=snapshot.get("requested_model"),
            requested_effort=snapshot.get("requested_effort"),
            effective_model=snapshot.get("effective_model"),
            effective_effort=snapshot.get("effective_effort"),
            routing_integrity=snapshot.get("routing_integrity"),
            routing_integrity_reason=snapshot.get("reason"),
            cycle_consuming=False,
            cycle_disposition="non_consuming",
        )

    async def report(self, msg: dict[str, Any]) -> dict[str, Any]:
        """`report`, with `--terminate` semantics.

        B9 atomicity: the row is durable BEFORE any close processing runs, so a
        `report --terminate` whose close fails (or whose daemon dies mid-close)
        still leaves the report retrievable. The close can be retried; a lost
        report cannot be reconstructed."""
        row = await self.ingest(msg)
        notice_delivery = row.pop("_notice_delivery", None)
        reply: dict[str, Any] = {
            "type": "report.ok",
            "report_id": row["report_id"],
            "ledger_row_id": row["ledger_row_id"],
            "durability_ack": True,
            "completion_kind": row.get("completion_kind"),
            "qa_verdict": row.get("qa_verdict"),
            "target_sha": row.get("target_sha"),
            "qa_attestation": row.get("qa_attestation"),
            "agent_orch_attestation": row.get("agent_orch_attestation"),
            "effective_model": row.get("effective_model"),
            "effective_effort": row.get("effective_effort"),
            "qa_attestation_validation": row.get("qa_attestation_validation"),
            "to_stream_id": row.get("to_stream_id"),
        }
        reply.update(_claim_wire_fields(row.get("claim_verified"), row.get("ac_claim_mismatch")))
        if notice_delivery is not None:
            reply["notice_delivery"] = notice_delivery
        validation = row.get("qa_attestation_validation")
        if isinstance(validation, dict) and validation.get("state") == "unverified":
            reply["warnings"] = [{
                "code": "qa_attestation_unverified",
                "reasons": list(validation.get("reasons") or []),
            }]
        auto_close = await self._self_close_on_completion(row)
        close_requested = bool(msg.get("terminate") or msg.get("close_on_ingest") or auto_close)
        if close_requested:
            close_msg = msg if not auto_close else {**msg, "close_on_ingest": True}
            refusal = await self._terminate_refusal(row, close_msg)
            if refusal is not None:
                reply.update(refusal)
            else:
                reply.update(await self._terminate_after_report(row))
        return reply

    async def _self_close_on_completion(self, row: dict[str, Any]) -> bool:
        """Honor the durable worker lifecycle bit for terminal reports."""
        if row.get("status") not in TERMINAL_REPORT_STATUSES or self.sessions is None:
            return False
        target = str(row.get("from_stream_id") or "")
        if not target:
            return False
        try:
            host, name = self.sessions.split(target)
            session = self.sessions.get(target) or await self.store.fetch_session(host, name)
        except Exception:  # noqa: BLE001 - report durability must remain primary
            return False
        return bool(isinstance(session, dict) and session.get("self_close_on_completion") is True)

    async def _self_close_generation(self, from_stream_id: str) -> str | None:
        """The current generation of a `self_close_on_completion` seat, else
        None. Reads the AUTHORITATIVE durable row (not the in-memory Sessions
        cache) and computes the reconciler's `_row_generation` formula against
        it, so a recorded rejection is keyed to the same generation the backlog
        sweep looks up (`reconciler._row_generation` reads the store row too)."""
        try:
            host, name = self.sessions.split(from_stream_id)
            session = await self.store.fetch_session(host, name)
            if session is None and self.sessions is not None:
                session = self.sessions.get(from_stream_id)
        except Exception:  # noqa: BLE001 - report durability must remain primary
            return None
        if not (isinstance(session, dict) and session.get("self_close_on_completion") is True):
            return None
        return str(session.get("session_generation") or f"created_at:{session.get('created_at') or ''}")

    async def _maybe_record_terminal_report_rejection(
        self, msg: dict[str, Any], reason: str
    ) -> None:
        """pop2: a schema-rejected TERMINAL report from a self-close seat leaves a
        durable trace so the finished seat can be reaped and the reason read.
        Safe to call at ANY pre-persist raise site (envelope or payload schema
        error): it re-derives identity and enforces the SAME attribution safety
        as the ingest gate — a caller may only attribute a rejection to itself,
        never to another seat — then records only for a terminal-status
        self-close seat. Best-effort; the raised schema rejection stays primary."""
        try:
            status = msg.get("status")
            if status not in TERMINAL_REPORT_STATUSES:
                return
            from_stream_id = str(msg.get("from_stream_id") or msg.get("stream_id") or "").strip()
            if not from_stream_id:
                return
            caller = await self._authenticated_caller(msg)
            if caller is not None and caller != from_stream_id:
                return  # cross-stream claim: never file a rejection against a victim
            generation = await self._self_close_generation(from_stream_id)
            if generation is None:
                return
            await self.store.record_report_rejection(
                from_stream_id, session_generation=generation, status=str(status), reason=reason,
            )
        except Exception:  # noqa: BLE001 - the raised schema rejection stays primary
            log.exception("record report rejection failed")

    async def _authenticated_caller(self, msg: dict[str, Any]) -> str | None:
        """The caller's proven identity, or None when the frame carries no
        identity signal (a legacy client — the mismatch gate cannot fire, so the
        top-level terminate guard is the backstop).

        A verified `stream_token` (hashed, matched to an open stream) is
        authoritative and outranks any claim. Absent a resolvable token, the
        env-derived `caller_stream_id` the CLI stamps from `AGENT_ORCH_STREAM_ID`
        is the honest-caller signal that catches the misuse trap. A claimed
        `caller_stream_id` is never authority OVER a resolved token: a token that
        resolves to a different stream than the claim wins (the claim is spoofable
        transport data; the token is not)."""
        token = str(msg.get("stream_token") or "").strip()
        if token:
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            resolved = await self.store.stream_id_for_token_hash(token_hash)
            if resolved:
                return resolved
        claim = str(msg.get("caller_stream_id") or "").strip()
        return claim or None

    async def _terminate_refusal(self, row: dict[str, Any], msg: dict[str, Any]) -> dict[str, Any] | None:
        """Refuse an auto-confirmed terminate of a VISIBLE TOP-LEVEL SEAT (parent
        null, visibility `default`) — the Nexus/operator-facing seats. Only an
        explicit operator confirmation on the frame may close one; the client's
        auto `close_on_ingest`/`terminate` classification must NEVER reap it
        (2026-08-05 root cause: `operator_confirm_auto=true` closed the Nexus).
        Returns a `closed: False` fragment on refusal — the report is already
        durable — or None to let the close proceed. A hidden/subagent seat or any
        seat with a parent self-terminates as before."""
        if bool(msg.get("operator_confirm")):
            return None
        target = row["from_stream_id"]
        host, name = self.sessions.split(target)
        session = self.sessions.get(target) or await self.store.fetch_session(host, name)
        if session is None:
            return None
        parent = str(session.get("parent_stream_id") or "").strip()
        visibility = str(session.get("visibility") or "default")
        if parent or visibility != "default":
            return None
        log.warning(
            "report --terminate refused: visible top-level seat %s requires operator confirm",
            target,
        )
        return {
            "closed": False,
            "close_error": "operator_confirm_required_for_top_level_seat",
        }

    async def ingest(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Validate, persist, announce, settle awaiters. Returns the stored row."""
        report_id = str(msg.get("report_id") or "").strip() or uuid.uuid4().hex
        envelope_violations = unknown_report_message_violations(msg)
        if envelope_violations:
            # pop2: an unknown-envelope-field rejection of a TERMINAL report (the
            # observed `bounce_required` case) also vanishes today. Persist it
            # before raising; the helper re-derives identity and enforces the
            # same caller-attribution safety as the gate below.
            await self._maybe_record_terminal_report_rejection(
                msg, "; ".join(str(item["detail"]) for item in envelope_violations),
            )
            raise self._error(
                "schema_error",
                "; ".join(str(item["detail"]) for item in envelope_violations),
                report_id,
                schema_violations=envelope_violations,
            )
        from_stream_id = str(msg.get("from_stream_id") or msg.get("stream_id") or "").strip()
        if not from_stream_id:
            raise self._error("bad_request", "from_stream_id is required", report_id)

        # Attribution is bound to the AUTHENTICATED caller, never to the claimed
        # `from_stream_id` alone (2026-08-05 Nexus-seat kill: a worker passed the
        # Nexus id as `--from-stream-id` and the daemon trusted the override,
        # writing — and terminating — as the Nexus). A caller may only attribute
        # a report to itself; a cross-stream claim is rejected HERE, before any
        # ledger write, idempotency claim, broadcast, or close side effect. No
        # silent reattribution: the report is refused, not rebound.
        caller = await self._authenticated_caller(msg)
        if caller is not None and caller != from_stream_id:
            raise self._error(
                "from_stream_id_forbidden",
                "from_stream_id does not match the authenticated caller; "
                "cross-stream attribution is not permitted",
                report_id,
                caller_stream_id=caller,
                claimed_from_stream_id=from_stream_id,
            )

        raw_msg_id = msg.get("msg_id", 0)
        if raw_msg_id is None:
            raw_msg_id = 0
        if not isinstance(raw_msg_id, int) or isinstance(raw_msg_id, bool) or raw_msg_id < 0:
            raise self._error("invalid_msg_id", "msg_id must be a non-negative integer", report_id)

        status = msg.get("status")
        if status not in REPORT_STATUSES:
            raise self._error("invalid_status", "status must be done, progress, error, or aborted", report_id)

        if msg.get("result_blob_sha"):
            # v2 has no blob store. Say so in the structured cutover vocabulary
            # rather than silently ingesting a report with no body.
            raise self._error(
                "unsupported_in_v2",
                "result_blob_sha reports are not implemented in v2",
                report_id,
                hint=RESULT_BLOB_UNSUPPORTED_HINT,
            )

        payload = {field: msg[field] for field in INLINE_SCHEMA_FIELDS if field in msg}
        qa_grade = await self._is_qa_grade_reporter(from_stream_id)
        try:
            validated = validate_report_payload(payload, str(status), qa_grade=qa_grade)
        except SchemaError as exc:
            # pop2: a schema-rejected TERMINAL report from a self-close seat must
            # not vanish. Persist the rejected attempt before raising so the
            # finished seat leaves a durable completion signal the backlog sweep
            # can reap. (Attribution is already validated above; the helper
            # re-checks it, which is harmless.)
            await self._maybe_record_terminal_report_rejection(msg, str(exc))
            raise self._error(exc.code, str(exc), report_id, schema_violations=exc.violations) from exc

        claim_verified, claim_mismatch = await self.verify_ac_claim(validated.ac_claim)

        routing_snapshot = await self._report_routing_snapshot(from_stream_id)
        durable_parent = await self._parent_of(from_stream_id)
        request_identity = {
            "report_id": report_id,
            "from_stream_id": from_stream_id,
            "msg_id": raw_msg_id,
            "status": validated.status,
            "summary": validated.summary,
            "findings": validated.findings,
            "next_action": validated.next_action,
            "details": validated.details,
            "extras": validated.extras,
            "reason": validated.reason,
            "completion_kind": validated.completion_kind,
            "qa_verdict": validated.qa_verdict,
            "target_sha": validated.target_sha,
            "qa_attestation": validated.qa_attestation,
            "ac_claim": validated.ac_claim,
            "agent_orch_attestation": msg.get("agent_orch_attestation"),
            "terminate": bool(msg.get("terminate")),
            "close_on_ingest": bool(msg.get("close_on_ingest")),
            "operator_confirm": bool(msg.get("operator_confirm")),
        }

        report_fields = {
            "report_id": report_id,
            "from_stream_id": from_stream_id,
            "msg_id": raw_msg_id,
            "status": validated.status,
            "summary": validated.summary,
            "findings": validated.findings,
            "next_action": validated.next_action,
            "details": validated.details,
            "extras": validated.extras,
            "reason": validated.reason,
            "completion_kind": validated.completion_kind,
            "qa_verdict": validated.qa_verdict,
            "target_sha": validated.target_sha,
            "qa_attestation": validated.qa_attestation,
            "ac_claim": validated.ac_claim,
            "claim_verified": claim_verified,
            "ac_claim_mismatch": claim_mismatch,
            "agent_orch_attestation": msg.get("agent_orch_attestation"),
            "to_stream_id": durable_parent or None,
            "request_payload_hash": hashlib.sha256(
                json.dumps(
                    request_identity, sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "effective_model": (
                routing_snapshot.get("effective_model") if routing_snapshot is not None else None
            ),
            "effective_effort": (
                routing_snapshot.get("effective_effort") if routing_snapshot is not None else None
            ),
            "ingested_at": iso_now(),
        }
        try:
            put_kwargs: dict[str, Any] = {}
            if validated.completion_kind == "implementation_ready":
                put_kwargs["qa_attestation_mode"] = self.qa_attestation_mode
            row = await self.store.put_report(
                report_fields,
                routing_snapshot=routing_snapshot,
                **put_kwargs,
            )
        except ReportProvenanceUnavailable as exc:
            raise self._routing_refusal(
                "report_provenance_unavailable",
                "report provenance source is unavailable",
                report_id,
                exc.snapshot,
            ) from exc
        except ReportReplayConflict as exc:
            raise self._error(
                "report_id_replay_conflict",
                "report_id is already bound to different report material",
                report_id,
            ) from exc
        except ReportProvenanceChanged as exc:
            raise self._routing_refusal(
                "report_provenance_changed",
                "report provenance changed before durable write",
                report_id,
                exc.expected,
            ) from exc
        except QAAttestationUnverified as exc:
            raise self._error(
                "qa_attestation_unverified",
                "implementation_ready report does not cite a verified QA accept",
                report_id,
                schema_violations=[{"reasons": exc.validation["reasons"]}],
            ) from exc
        # Durable first, announced second: everything below is best-effort and
        # may not fail the ingest the caller was already told to trust.
        inserted = bool(row.pop("_report_inserted", True))
        if inserted:
            if apply_state := getattr(self.sessions, "apply_report_state", None):
                try:
                    await apply_state(row)
                except Exception:
                    log.exception("report roster projection failed after durable ingest")
            self._alert_claim_result(
                row.get("claim_verified"), row.get("ac_claim_mismatch"),
                report_id=str(row.get("report_id") or report_id),
                from_stream_id=str(row.get("from_stream_id") or from_stream_id),
            )
        if row["status"] in TERMINAL_REPORT_STATUSES:
            if inserted:
                self._settle_waiters(row)
            row["_notice_delivery"] = await self._announce_child_report_ready(row)
        return row

    @staticmethod
    def _error(code: str, message: str, report_id: str, **extra: Any) -> VerbError:
        # v1's report.error shape: error_code + error_message (+ schema_violations).
        return VerbError(code, message, error_message=message, report_id=report_id, **extra)

    async def _terminate_after_report(self, row: dict[str, Any]) -> dict[str, Any]:
        """Close the reporting stream once its report is durable."""
        host, name = self.sessions.split(row["from_stream_id"])
        try:
            result = await self.sessions.close(host, name, "report_terminate")
        except Exception as exc:  # noqa: BLE001 - a failed close never voids the report
            log.exception("report --terminate close failed stream=%s", row["from_stream_id"])
            return {"closed": False, "close_error": str(exc)}
        # The close ladder can honestly leave the row OPEN (`close.failed`, no
        # deliverable signal). The report is already durable either way, but the
        # terminate result must not claim a close that did not happen.
        if result.get("failed"):
            return {"closed": False, "close_error": result.get("reason")}
        return {
            "closed": True,
            "already_closed": bool(result.get("already_closed")),
            "live_children": result.get("live_children", []),
            **_claim_wire_fields(row.get("claim_verified"), row.get("ac_claim_mismatch")),
        }

    # -- child_report_ready, both paths (design L13) -----------------------

    async def _announce_child_report_ready(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """Path 1 is the top-level WS frame (v1 `:19252`); path 2 is the pane
        tell routed down handoff lineage (v1 `:19139`). Both are addressed to
        the reporting stream's PARENT, so a report from a parentless stream
        announces nothing."""
        parent = str(row.get("to_stream_id") or "")
        if not parent:
            return None
        frame = {
            "type": "child_report_ready",
            "report_id": row["report_id"],
            "ledger_row_id": row["ledger_row_id"],
            "child_stream_id": row["from_stream_id"],
            "parent_stream_id": parent,
            "msg_id": row["msg_id"],
            "qa_attestation_validation": row.get("qa_attestation_validation"),
        }
        body = child_report_ready_text(row)
        legacy_id = legacy_child_report_ready_tell_id(row["report_id"])
        legacy_record = await self.store.get_tell_delivery(legacy_id)
        if isinstance(legacy_record, dict):
            legacy_audit = _tell_audit_record(legacy_record)
            legacy_receipt = _canonical_notice_delivery(legacy_record)
            legacy_text = legacy_audit.get("text")
            matching_text = legacy_text in {
                body,
                ensure_notice_marker(legacy_id, body),
            }
            if (
                legacy_audit.get("tell_id") == legacy_id
                and legacy_audit.get("from_stream_id") == row.get("from_stream_id")
                and matching_text
            ):
                if legacy_receipt.get("to_stream_id") != parent:
                    return _notice_delivery_failure(
                        to_stream_id=parent,
                        tell_id=legacy_id,
                        error_code="notice_receipt_target_mismatch",
                    )
                return legacy_receipt

        notice_id = child_report_ready_tell_id(row["report_id"])
        notice_row = await self.outbound.enqueue(
            kind=NOTICE_KIND_REPORT,
            dedupe_key=f"report:{row['report_id']}",
            recipient_stream_id=parent,
            tell_id=notice_id,
            body=body,
            watch_fact=row,
            source_stream_id=str(row.get("from_stream_id") or ""),
            metadata={
                "report_id": row.get("report_id"),
                "ledger_row_id": row.get("ledger_row_id"),
                "msg_id": row.get("msg_id"),
            },
        )
        if self._broadcast is not None and (notice_row.get("created") is True or row.get("_watch_notice_created") is True):
            for field in ("effective_model", "effective_effort"):
                if row.get(field) is not None:
                    frame[field] = row[field]
            await self._broadcast(frame)
        if self.outbound.comms is None:
            return _notice_delivery_failure(
                to_stream_id=parent,
                tell_id=notice_id,
                error_code="outbound_transport_unavailable",
            )
        notice_id = str(notice_row["notice_id"])
        try:
            await self.outbound.deliver_now(notice_id)
        except Exception as exc:  # noqa: BLE001 - the durable row remains authoritative
            return _notice_delivery_failure(
                to_stream_id=parent,
                tell_id=notice_id,
                error_code=str(exc) or "delivery_not_submitted",
            )
        record = await self.store.get_tell_delivery(notice_id)
        if not isinstance(record, dict):
            return _notice_delivery_failure(
                to_stream_id=parent,
                tell_id=notice_id,
                error_code="notice_receipt_missing",
            )
        receipt = _canonical_notice_delivery(record)
        if receipt.get("tell_id") != notice_id:
            return _notice_delivery_failure(
                to_stream_id=parent,
                tell_id=notice_id,
                error_code="notice_receipt_identity_mismatch",
            )
        if receipt.get("to_stream_id") != parent:
            return _notice_delivery_failure(
                to_stream_id=parent,
                tell_id=notice_id,
                error_code="notice_receipt_target_mismatch",
            )
        return receipt

    async def _parent_of(self, stream_id: str) -> str:
        if self.sessions is not None:
            host, name = self.sessions.split(stream_id)
            row = self.sessions.get(stream_id) or await self.store.fetch_session(host, name)
        else:
            host, separator, name = str(stream_id or "").partition(":")
            row = await self.store.fetch_session(host, name) if separator else None
        return str((row or {}).get("parent_stream_id") or "")

    # -- await_report ------------------------------------------------------

    async def resolve_awaiters_on_close(
        self, stream_id: str, *, session_generation: str | None = None,
        reason: str = "confirmed_dead_close",
    ) -> list[dict[str, Any]]:
        outcomes = await self.store.resolve_awaiters_on_close(
            stream_id,
            session_generation=session_generation,
            reason=reason,
        )
        self._settle_awaiter_results(outcomes)
        return outcomes

    async def sweep_awaited_unreported(self, *, limit: int = 500) -> dict[str, int]:
        """Resolve rows whose child became terminal while this process was down."""
        outcomes = await self.store.resolve_closed_awaiters(limit=limit)
        self._settle_awaiter_results(outcomes)
        return {
            "checked": len(outcomes),
            "resolved": len(outcomes),
            "reports": sum(item.get("outcome") == "report" for item in outcomes),
            "closed_without_report": sum(
                item.get("outcome") == "closed_without_report" for item in outcomes
            ),
        }

    async def await_report(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Answer from the durable row if it exists; otherwise park a bounded
        waiter. The durable await row is registered before the future is
        parked, so a close or daemon restart cannot strand the result."""
        host, name = await self.sessions.resolve(msg)
        stream_id = f"{host}:{name}"
        msg_id = msg.get("msg_id")
        if msg_id is not None and (not isinstance(msg_id, int) or isinstance(msg_id, bool) or msg_id < 0):
            raise VerbError("invalid_msg_id", "msg_id must be a non-negative integer")
        timeout = _await_timeout(msg.get("timeout"))

        registered = await self.store.register_awaiter(stream_id, msg_id)
        if registered.get("outcome") != "pending":
            return self._await_result_response(
                registered, stream_id, msg_id, source="ledger",
            )

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        entry = (stream_id, msg_id, fut)
        self._waiters.append(entry)
        try:
            # A report/close can win between durable registration and adding
            # the in-memory future. Re-read the durable row to close that
            # scheduling window before waiting.
            current = await self.store.get_awaiter_result(stream_id, msg_id)
            if current is not None and current.get("outcome") != "pending":
                fut.set_result(current)
            row = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return {
                "type": "await_report.timeout", "ok": False,
                "stream_id": stream_id, "msg_id": msg_id,
                "error": "await_timeout", "reason": "await_timeout",
                "message": f"await_report timed out after {timeout:.2f}s",
                "result_kind": "timeout",
                "consumption_semantics": "idempotent_durable_row",
            }
        finally:
            if entry in self._waiters:
                self._waiters.remove(entry)
        return self._await_result_response(row, stream_id, msg_id, source="broadcast")

    def _settle_waiters(self, row: dict[str, Any]) -> None:
        for entry in list(self._waiters):
            stream_id, msg_id, fut = entry
            if stream_id != row["from_stream_id"]:
                continue
            if msg_id is not None and msg_id != row["msg_id"]:
                continue
            if not fut.done():
                fut.set_result(row)

    def _settle_awaiter_results(self, outcomes: list[dict[str, Any]]) -> None:
        for outcome in outcomes:
            if outcome.get("outcome") == "report" and outcome.get("report") is not None:
                self._settle_waiters(outcome["report"])
            else:
                self._settle_waiters_result(outcome)

    def _settle_waiters_result(self, outcome: dict[str, Any]) -> None:
        for entry in list(self._waiters):
            stream_id, msg_id, fut = entry
            if stream_id != outcome.get("stream_id") or fut.done():
                continue
            if msg_id != outcome.get("msg_id"):
                continue
            fut.set_result(outcome)

    def _await_result_response(
        self, result: dict[str, Any], stream_id: str, msg_id: int | None, *, source: str,
    ) -> dict[str, Any]:
        if result.get("outcome") == "report" and result.get("report") is not None:
            return self._await_ok(result["report"], stream_id, msg_id, source=source)
        # A report ingested while a waiter is parked is delivered through the
        # existing in-memory broadcast path as the raw report row. Treat that
        # row as the same terminal result as the durable awaiter wrapper.
        if result.get("report_id") and result.get("status") in TERMINAL_REPORT_STATUSES:
            return self._await_ok(result, stream_id, msg_id, source=source)
        return self._await_closed_without_report(result, stream_id, msg_id, source=source)

    @staticmethod
    def _await_closed_without_report(
        result: dict[str, Any], stream_id: str, msg_id: int | None, *, source: str,
    ) -> dict[str, Any]:
        return {
            "type": "await_report.closed_without_report",
            "ok": False,
            "stream_id": stream_id,
            "msg_id": msg_id,
            "source": source,
            "result_kind": "closed_without_report",
            "error": "closed_without_report",
            "reason": "closed_without_report",
            "message": "target closed before a terminal report was durably ingested",
            "consumption_semantics": "idempotent_durable_row",
            "result": {
                "awaiter_id": result.get("awaiter_id"),
                "resolved_at": result.get("resolved_at"),
                "reason": result.get("reason"),
            },
        }

    @staticmethod
    def _await_ok(row: dict[str, Any], stream_id: str, msg_id: int | None, *, source: str) -> dict[str, Any]:
        """v1's `await_report.ok` field set for the fields v2 persists. `ok` is
        status-derived: an `error`/`aborted` report is a successful await of an
        unsuccessful run, and callers distinguish them by this flag."""
        return {
            "type": "await_report.ok",
            "ok": row["status"] == "done",
            "stream_id": stream_id,
            "msg_id": msg_id,
            "source": source,
            "result_kind": "report",
            "consumption_semantics": "idempotent_durable_row",
            "report": row,
            "report_id": row["report_id"],
            "ledger_row_id": row["ledger_row_id"],
            "status": row["status"],
            "summary": row["summary"],
            "findings": row["findings"],
            "next_action": row["next_action"],
            "reason": row["reason"],
            "target_sha": row.get("target_sha"),
            "agent_orch_attestation": row.get("agent_orch_attestation"),
            "ingested_at": row["ingested_at"],
            "effective_model": row.get("effective_model"),
            "effective_effort": row.get("effective_effort"),
            "qa_attestation_validation": row.get("qa_attestation_validation"),
        }


def _tell_audit_record(record: dict[str, Any]) -> dict[str, Any]:
    """Project one stored tell envelope into the public audit shape.

    Older v2 rows may predate the ``delivery`` envelope and therefore cannot
    supply text retroactively. They remain identifiable, but new rows always
    carry the exact post-validation text written by ``Comms``.
    """
    reply = record.get("reply") if isinstance(record.get("reply"), dict) else {}
    delivery = record.get("delivery") if isinstance(record.get("delivery"), dict) else {}
    tell_id = str(delivery.get("tell_id") or reply.get("tell_id") or "")
    row_id = record.get("ledger_row_id") or reply.get("ledger_row_id")
    text = delivery.get("text")
    route_hops = delivery.get("route_hops")
    if not isinstance(route_hops, list):
        route_hops = list(reply.get("hops") or []) if isinstance(reply.get("hops"), list) else []
    delivered_at = delivery.get("delivered_at")
    submission_confirmed = delivery.get("submission_confirmed")
    if submission_confirmed is None:
        submission_confirmed = reply.get("submission_confirmed")
    return {
        "ledger_row_id": int(row_id) if isinstance(row_id, int) and not isinstance(row_id, bool) else row_id,
        "tell_id": tell_id,
        "from_stream_id": str(delivery.get("from_stream_id") or ""),
        "to_stream_id": str(delivery.get("to_stream_id") or reply.get("to_stream_id") or ""),
        "original_to_stream_id": str(
            delivery.get("original_to_stream_id")
            or reply.get("original_target")
            or reply.get("to_stream_id")
            or ""
        ),
        "route_hops": route_hops,
        "text": text if isinstance(text, str) else None,
        "text_available": isinstance(text, str),
        "delivery_status": str(delivery.get("delivery_status") or reply.get("delivery_status") or "delivered"),
        "delivery_attempts": int(delivery.get("delivery_attempts") or 1),
        "submission_attempts": int(
            delivery.get("submission_attempts")
            or reply.get("submission_attempts")
            or delivery.get("delivery_attempts")
            or 1
        ),
        "submission_confirmed": bool(submission_confirmed),
        "enqueued_at": delivery.get("enqueued_at"),
        "delivered_at": delivered_at,
        "delivery_ack_at": delivery.get("delivery_ack_at"),
        "request_payload_hash": str(
            delivery.get("request_payload_hash") or record.get("payload_digest") or ""
        ),
        "recorded_at": record.get("created_at"),
    }


def _notice_delivery_failure(
    *, to_stream_id: str, error_code: str, tell_id: str | None = None,
) -> dict[str, Any]:
    return {
        "delivery_status": "failed",
        "to_stream_id": to_stream_id,
        **({"tell_id": tell_id} if tell_id else {}),
        "error_code": error_code,
    }


def _canonical_notice_delivery(record: dict[str, Any]) -> dict[str, Any]:
    reply = record.get("reply") if isinstance(record.get("reply"), dict) else {}
    delivery = record.get("delivery") if isinstance(record.get("delivery"), dict) else {}
    values = {
        "tell_id": delivery.get("tell_id") or reply.get("tell_id"),
        "ledger_row_id": record.get("ledger_row_id") or reply.get("ledger_row_id"),
        "delivery_status": delivery.get("delivery_status") or reply.get("delivery_status"),
        "delivery_ack_at": (
            delivery.get("delivery_ack_at") or delivery.get("delivered_at")
            or reply.get("delivery_ack_at")
        ),
        "to_stream_id": delivery.get("to_stream_id") or reply.get("to_stream_id"),
        "error_code": delivery.get("error_code") or reply.get("error_code"),
        # Committed-but-late-proof authority survives projection (see the CLI's
        # _notice_delivery_is_authoritative).
        "action_committed": delivery.get("action_committed") or reply.get("action_committed"),
        "do_not_resubmit": delivery.get("do_not_resubmit") or reply.get("do_not_resubmit"),
    }
    return {field: value for field, value in values.items() if value is not None}


def child_report_ready_tell_id(report_id: object) -> str:
    """Deterministic in the report row (v1 `_child_report_ready_tell_id`): the
    tell dedupe in `comms` is what makes a retried announcement safe, and it
    keys on this id."""
    digest = hashlib.sha256(str(report_id or "").encode("utf-8")).hexdigest()
    return f"child-report-ready-v2-{digest}"


def legacy_child_report_ready_tell_id(report_id: object) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(report_id or "")).strip("-")
    return f"child-report-ready-{safe or uuid.uuid4().hex}"


def child_report_ready_text(row: dict[str, Any]) -> str:
    summary = str(row.get("summary") or "").strip()
    if len(summary) > TELL_SUMMARY_MAX_CHARS:
        summary = summary[: TELL_SUMMARY_MAX_CHARS - 3] + "..."
    lines = [
        "[child_report_ready]",
        f"report_id={row.get('report_id')}",
        f"ledger_row_id={row.get('ledger_row_id')}",
        f"child_stream_id={row.get('from_stream_id')}",
        f"msg_id={row.get('msg_id')}",
        f"status={row.get('status')}",
    ]
    validation = row.get("qa_attestation_validation")
    if isinstance(validation, dict) and validation.get("state") == "unverified":
        lines.extend((
            "qa_attestation_state=unverified",
            "qa_attestation_reasons=" + ",".join(
                str(reason) for reason in validation.get("reasons") or []
            ),
        ))
    lines.append(f"summary={summary}")
    if row.get("effective_model") is not None:
        lines.append(f"effective_model={row['effective_model']}")
    if row.get("effective_effort") is not None:
        lines.append(f"effective_effort={row['effective_effort']}")
    return "\n".join(lines)


def _await_timeout(raw: object) -> float:
    try:
        timeout = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return AWAIT_TIMEOUT_DEFAULT_S
    if timeout <= 0:
        return AWAIT_TIMEOUT_DEFAULT_S
    return min(timeout, AWAIT_TIMEOUT_MAX_S)


# --------------------------------------------------------------------------- #
# durable awaiter-resolution sweep (C5)
# --------------------------------------------------------------------------- #

AWAITER_RESOLUTION_DEFAULT_INTERVAL_S = 30.0
AWAITER_RESOLUTION_DEFAULT_MAX_PER_PASS = 500
AWAITER_RESOLUTION_DEFAULT_BACKOFF_S = 5.0


@dataclass
class AwaiterResolutionConfig:
    """Loop-rule knobs for restart-safe awaited-unreported recovery."""

    interval_s: float = AWAITER_RESOLUTION_DEFAULT_INTERVAL_S
    max_per_pass: int = AWAITER_RESOLUTION_DEFAULT_MAX_PER_PASS
    error_backoff_s: float = AWAITER_RESOLUTION_DEFAULT_BACKOFF_S

    @classmethod
    def from_env(cls, env: dict | None = None) -> "AwaiterResolutionConfig":
        e = os.environ if env is None else env
        return cls(
            interval_s=env_number(
                e, "INTERVAL_S", cls.interval_s, float,
                prefix="PENTACLE_AWAITER_RESOLUTION_",
            ),
            max_per_pass=env_number(
                e, "MAX_PER_PASS", cls.max_per_pass, int,
                prefix="PENTACLE_AWAITER_RESOLUTION_",
            ),
            error_backoff_s=env_number(
                e, "ERROR_BACKOFF_S", cls.error_backoff_s, float,
                prefix="PENTACLE_AWAITER_RESOLUTION_",
            ),
        )


class AwaiterResolutionJob:
    """Periodic re-drive for pending await rows whose target is terminal."""

    def __init__(
        self, ledger: Ledger, config: AwaiterResolutionConfig | None = None,
    ) -> None:
        self.ledger = ledger
        self.config = config or AwaiterResolutionConfig.from_env()

    async def sweep_once(self) -> dict[str, int]:
        return await self.ledger.sweep_awaited_unreported(
            limit=self.config.max_per_pass,
        )

    async def run_forever(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - recovery stays supervised
                log.exception("awaiter resolution pass failed")
                await asyncio.sleep(self.config.error_backoff_s)
                continue
            await asyncio.sleep(self.config.interval_s)


# --------------------------------------------------------------------------- #
# Context crossings and title/status-card reminders
# --------------------------------------------------------------------------- #
#
# Lifted from v1's `_nudge_unnamed_top_level_sessions` +
# `_nudge_stale_status_cards` (`daemon_lifecycle.py`). v1's LOGIC — ping a
# top-level operator-facing session that never titled itself or whose status
# card is missing/stale — ports as-is; v1's I/O placement does not: every
# SQLite touch here goes through `store.py`'s worker thread, and the pane
# injection reuses `comms.tell` (the one receipt-backed injection path) rather
# than v1's queue.
#
# The pinned dead-nudge-spam class (D2) drove the adaptations, all here:
#   * title/card candidates are live AND visible (visibility != hidden)
#     top-level sessions; context notifications independently include hidden children;
#   * a bounded cadence with a per-pass cap and exponential backoff;
#   * a durable per-(stream, kind) cooldown (`v2_nudge_state`) so a restart or a
#     sleep/wake flap cannot re-fire — the exact v1 failure (in-memory episode
#     timestamp re-minted on restart);
#   * the `--disable-nudges` kill switch (main.py never constructs the job).
#
# Context nudges have independent eligibility and durable ingestion epochs.
# For title/card reminders, both providers require
# two admitted current-generation operator USER turns. Trusted local mirror or
# remote capture must prove an idle, visible, live top-level session. Only USER
# engagement rearms reminders; tool/assistant/reminder feedback cannot do so.

NUDGE_KIND_TITLE = "title"
NUDGE_KIND_CARD = "status_card"
NUDGE_KIND_CONTEXT_ADVISORY = "context_advisory"
NUDGE_KIND_CONTEXT_HANDOFF = "context_handoff"

NUDGE_TITLE_TEXT = 'Please run: agent-orch title "<succinct durable goal>" (2-7 words).'
NUDGE_CARD_TEXT = (
    'Please update your status card: agent-orch status --update "<one-line progress note>"'
    " [--step-done <N>]. Set --goal/--plan if unset; revise them if they have changed."
)

_NUDGE_ENV_PREFIX = "PENTACLE_NUDGE_"
NUDGE_DEFAULT_INTERVAL_S = 300.0
NUDGE_DEFAULT_MAX_PER_PASS = 10
NUDGE_DEFAULT_COOLDOWN_S = 3600.0
NUDGE_DEFAULT_STATUS_STALE_S = 1800.0
NUDGE_DEFAULT_ENGAGED_WINDOW_S = 1800.0
NUDGE_DEFAULT_BACKOFF_BASE_S = 30.0
NUDGE_DEFAULT_BACKOFF_MAX_S = 300.0
NUDGE_MIN_USER_EVENTS = 2

_NUDGE_VISIBLE = frozenset({"default", "visible"})
_NUDGE_PROVIDERS = frozenset({"claude", "codex"})


def _nudge_epoch(raw: object) -> float | None:
    """Parse an offset-bearing ISO stamp into a finite epoch.

    A naive ISO value is deliberately rejected.  The daemon must not interpret
    a card/activity stamp in its process-local timezone because that makes the
    nudge decision depend on where the daemon happens to be running.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        )
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        epoch = value.timestamp()
        return epoch if math.isfinite(epoch) else None
    except (ValueError, TypeError):
        return None


def _finite_epoch(raw: object) -> float | None:
    """Accept an already-materialized finite epoch or an offset ISO stamp."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if math.isfinite(value) else None
    return _nudge_epoch(raw)


@dataclass
class NudgeConfig:
    """Loop-rule knobs (v2_design.md § Event loop rules, rule 2).

    cadence      `interval_s`, default 5m, env `PENTACLE_NUDGE_INTERVAL_S`
    per-pass cap `max_per_pass` delivery attempts across context and title/card
    cooldown     `cooldown_s` per (stream, kind); restart-safe via `v2_nudge_state`
    staleness    `status_stale_s` — a card older than this is worth a nudge
    backoff      exponential from `backoff_base_s`, capped at `backoff_max_s`
    kill switch  `--disable-nudges` (main.py never constructs the job)
    """

    interval_s: float = NUDGE_DEFAULT_INTERVAL_S
    # None => wait a full interval before the first pass. Tests set a fraction of
    # a second; it is the "forced pass" trigger for a real daemon process, so no
    # test-only RPC verb has to exist on the wire (retention.py parity).
    first_delay_s: float | None = None
    max_per_pass: int = NUDGE_DEFAULT_MAX_PER_PASS
    cooldown_s: float = NUDGE_DEFAULT_COOLDOWN_S
    status_stale_s: float = NUDGE_DEFAULT_STATUS_STALE_S
    engaged_window_s: float = NUDGE_DEFAULT_ENGAGED_WINDOW_S
    backoff_base_s: float = NUDGE_DEFAULT_BACKOFF_BASE_S
    backoff_max_s: float = NUDGE_DEFAULT_BACKOFF_MAX_S

    @classmethod
    def from_env(cls, env: dict | None = None) -> "NudgeConfig":
        e = os.environ if env is None else env
        return cls(
            interval_s=env_number(
                e, "INTERVAL_S", NUDGE_DEFAULT_INTERVAL_S, float,
                prefix=_NUDGE_ENV_PREFIX,
            ),
            first_delay_s=env_number(
                e, "FIRST_DELAY_S", None, float,
                prefix=_NUDGE_ENV_PREFIX,
            ),
            max_per_pass=env_number(
                e, "MAX_PER_PASS", NUDGE_DEFAULT_MAX_PER_PASS, int,
                prefix=_NUDGE_ENV_PREFIX,
            ),
            cooldown_s=env_number(
                e, "COOLDOWN_S", NUDGE_DEFAULT_COOLDOWN_S, float,
                prefix=_NUDGE_ENV_PREFIX,
            ),
            status_stale_s=env_number(
                e, "STATUS_STALE_S", NUDGE_DEFAULT_STATUS_STALE_S, float,
                prefix=_NUDGE_ENV_PREFIX,
            ),
            engaged_window_s=env_number(
                e, "ENGAGED_WINDOW_S", NUDGE_DEFAULT_ENGAGED_WINDOW_S, float,
                prefix=_NUDGE_ENV_PREFIX,
            ),
        )


@dataclass
class NudgePassResult:
    attempted: int = 0
    pending: int = 0
    candidates: int = 0  # tell candidates eligible after the cooldown gate
    sent: int = 0
    errors: int = 0
    _capped: bool = field(default=False, repr=False)

    @property
    def capped(self) -> bool:
        """True when the per-pass cap was spent and candidates remained."""
        return self._capped


class NudgeJob:
    """One capped cadence for context crossings and title/card reminders.

    Context eligibility includes hidden/nested/working seats and uses durable
    ingestion episodes. Title/card reminders retain their existing visible,
    idle, operator-engaged eligibility. SQLite stays on the store worker and
    pane input stays on Comms; parentless context notices use Notify.
    """

    def __init__(self, sessions: Any, comms: Any, store: Any, config: NudgeConfig | None = None, *, notify: Any = None) -> None:
        self.sessions = sessions
        self.comms = comms
        self.store = store
        self.config = config or NudgeConfig()
        self.notify = notify
        self._last_tell_epoch_token: dict[str, int] = {}

    # -- eligibility (v1 candidate predicate, minus v1-only I/O machinery) --

    @staticmethod
    def _is_live(row: dict[str, Any]) -> bool:
        """Pane affirmatively alive. `pane_status` is the durable mirror column,
        `online` its live overlay; the mirror writes them together. An unknown
        pane (never observed) is NOT live — the safe default. A persisted
        `pane_alive` that is actually dead is not caught here but downstream: the
        tell's echo receipt fails (`delivery_failed`) and the cooldown paces the
        retry, so at worst one harmless nudge is attempted."""
        if row.get("pane_status") == "pane_dead":
            return False
        return bool(row.get("online")) or row.get("pane_status") == "pane_alive"

    @classmethod
    def _eligible(cls, row: dict[str, Any]) -> bool:
        if str(row.get("status") or "open") != "open":
            return False
        if not cls._is_live(row):
            return False
        if str(row.get("visibility") or "default") not in _NUDGE_VISIBLE:
            return False
        if str(row.get("parent_stream_id") or "").strip():
            return False  # top-level only (a child is its lead's concern)
        return str(row.get("provider") or "").lower() in _NUDGE_PROVIDERS

    @staticmethod
    def _known_local_mirror(row: dict[str, Any]) -> bool:
        """Require a current-generation local mirror snapshot.

        `online`/`pane_status` alone are not sufficient: remote presence rows
        and persisted rows can carry those values without a local capture that
        established the working state and activity baseline.
        """
        generation = str(row.get("session_generation") or "").strip()
        mirror_generation = str(
            row.get("mirror_generation") or row.get("local_mirror_generation") or ""
        ).strip()
        marker = row.get("local_mirror") is True or row.get("mirror_local") is True
        return bool(
            generation
            and marker
            and mirror_generation == generation
            and isinstance(row.get("working"), bool)
        )

    @classmethod
    def _known_capture(cls, row: dict[str, Any]) -> bool:
        if cls._known_local_mirror(row):
            return row.get("capture_liveness", "idle") == "idle"
        generation = str(row.get("session_generation") or "")
        return bool(generation and row.get("capture_generation") == generation
                    and row.get("capture_liveness") == "idle"
                    and isinstance(row.get("working"), bool))

    @staticmethod
    def _needs_title(row: dict[str, Any]) -> bool:
        # `list_open` overlays a "New Chat - <Host>" placeholder onto untitled
        # rows for display; the nudge must still see those as untitled or a
        # titleless session would never be nudged. The placeholder marks itself
        # with `title_source == "placeholder"`, so key off that rather than the
        # (now non-empty) title string.
        if str(row.get("title_source") or "") == TITLE_SOURCE_PLACEHOLDER:
            return True
        return not str(row.get("title") or "").strip()

    def _needs_card(self, row: dict[str, Any], *, now: float) -> bool:
        card = row.get("status_card")
        if not isinstance(card, dict):
            return True  # never carded
        updated = _nudge_epoch(card.get("updated_at"))
        if updated is None:
            return True
        return (now - updated) >= self.config.status_stale_s

    @classmethod
    def _activity_epoch(cls, row: dict[str, Any]) -> float | None:
        if not cls._known_capture(row):
            return None
        generation = str(row.get("session_generation") or "").strip()
        activity_generation = str(row.get("operator_activity_generation") or "").strip()
        if activity_generation != generation:
            return None
        return _finite_epoch(row.get("operator_activity_at"))

    def _has_activity_floor(self, row: dict[str, Any], *, now: float) -> bool:
        activity = self._activity_epoch(row)
        return activity is not None and now - activity >= 0

    def _status_engaged(
        self,
        row: dict[str, Any],
        state: dict[str, Any] | None,
        *,
        now: float,
    ) -> bool:
        """Require durable turn activity after the card and nudge grace."""
        activity = self._activity_epoch(row)
        if activity is None:
            return False
        age = now - activity
        if age < 0 or age > self.config.engaged_window_s:
            return False
        card = row.get("status_card")
        if isinstance(card, dict):
            card_updated = _nudge_epoch(card.get("updated_at"))
            if card_updated is None or activity <= card_updated:
                return False
        if state is not None:
            last_nudge = _finite_epoch(state.get("last_nudged_at"))
            if last_nudge is None or activity < last_nudge + self.config.cooldown_s:
                return False
        return True

    async def _passes_user_turn_grace(self, row: dict[str, Any]) -> bool:
        """Both providers get two admitted operator turns before a reminder."""
        stream_id = str(row.get("stream_id") or "")
        session_created_at = str(row.get("created_at") or "").strip()
        if not session_created_at:
            # Pre-cutover tail rows have no lifecycle boundary and cannot prove
            # that a USER event belongs to this live session generation.
            return False
        count = await self.store.count_session_events(
            stream_id,
            kind="USER",
            minimum=NUDGE_MIN_USER_EVENTS,
            session_created_at=session_created_at,
            provider=str(row.get("provider") or ""),
            exclude_sidechain=True,
            operator_only=True,
        )
        return count >= NUDGE_MIN_USER_EVENTS

    def _within_cooldown(
        self, row: dict[str, Any], kind: str, state: dict[str, Any] | None, *, now: float
    ) -> bool:
        """A nudge sent < cooldown ago suppresses a resend — EXCEPT, for the card
        nudge, when the card was (re)written since (compliance): that write moves
        past the recorded `basis`, opening a fresh episode so a later staleness
        is nudged again. v1 `_status_nudge_within_cooldown`, restart-safe here
        because the last-nudge time is durable, not in-memory."""
        if state is None:
            return False
        if (now - float(state.get("last_nudged_at") or 0.0)) >= self.config.cooldown_s:
            return False
        if kind == NUDGE_KIND_CARD:
            card = row.get("status_card")
            card_ep = _nudge_epoch(card.get("updated_at")) if isinstance(card, dict) else None
            basis_ep = _nudge_epoch(state.get("basis"))
            if card_ep is not None and (basis_ep is None or card_ep > basis_ep):
                return False  # card advanced past the nudged-at basis: new episode
        return True

    @staticmethod
    def _card_basis(row: dict[str, Any]) -> str:
        card = row.get("status_card")
        return str(card.get("updated_at") or "") if isinstance(card, dict) else ""

    def _tell_epoch(self, stream_id: str, now: float) -> int:
        # Comms deduplicates tell ids durably. Millisecond precision keeps a
        # close/reopen/new-generation combined nudge distinct when both passes
        # happen inside one wall-clock second, while remaining an epoch token.
        token = int(now * 1000)
        previous = self._last_tell_epoch_token.get(stream_id)
        if previous is not None and token <= previous:
            token = previous + 1
        self._last_tell_epoch_token[stream_id] = token
        return token

    @classmethod
    def _context_recipient_live(cls, row: dict[str, Any]) -> bool:
        return (row.get("status") == "open" and cls._is_live(row)
                and row.get("host_status") != "offline"
                and row.get("routing_integrity") != "mismatch")

    def _context_fresh(self, row: dict[str, Any], now: float) -> bool:
        stamp = _nudge_epoch(row.get("context_updated_at"))
        created = _nudge_epoch(row.get("created_at"))
        tokens, window = row.get("context_tokens"), row.get("model_context_window")
        return bool(
            self._context_recipient_live(row)
            and row.get("provider") in _NUDGE_PROVIDERS
            and row.get("context_level") in {"advisory", "handoff"}
            and isinstance(tokens, (int, float)) and not isinstance(tokens, bool)
            and math.isfinite(tokens) and tokens >= 0
            and isinstance(window, (int, float)) and not isinstance(window, bool)
            and math.isfinite(window) and window > 0
            and stamp is not None and created is not None
            and created <= stamp <= now and now - stamp <= 1800
        )

    @staticmethod
    def _context_delivery_outcome(reply: dict[str, Any]) -> str:
        status = str(reply.get("delivery_status") or "indeterminate")
        # Legacy Claude tells may label an unconfirmed paste "delivered".
        # A context advisory must not turn that into a receipt-backed success.
        if status == "delivered" and reply.get("submission_confirmed") is not True:
            return "committed_pending_proof"
        return status

    async def _context_pass(self, rows: list[dict[str, Any]], now: float) -> NudgePassResult:
        result = NudgePassResult()
        for initial in sorted(rows, key=lambda r: str(r.get("stream_id") or "")):
            sid = str(initial.get("stream_id") or "")
            if not sid:
                continue
            async with self.store.routing_integrity_lifecycle_lock(sid):
                row = self.sessions.get(sid) or {}
                if not self._context_fresh(row, now):
                    log.debug("subsystem=context_nudge bug_ref=context_notifications_handoff_proof "
                              "suppressed source=%s reason=ineligible_or_stale", sid)
                    continue
                kind = "context_" + row["context_level"]
                state = await self.store.nudge_state(sid, kind)
                basis = json.loads(state["basis"]) if state else {}
                if (not basis.get("active") or basis.get("superseded")
                        or basis.get("generation") != row.get("created_at")):
                    continue
                recipients = [(sid, str(row["created_at"]))]
                parent = str(row.get("parent_stream_id") or "")
                if parent:
                    route = await self.comms.resolve_route_target(parent)
                    target = str(route.get("final_target") or "")
                    parent_row = self.sessions.get(target) or {}
                    if route.get("ok") and target != sid and self._context_recipient_live(parent_row):
                        recipients.append((target, str(parent_row["created_at"])))
                    else:
                        log.debug("subsystem=context_nudge bug_ref=context_notifications_handoff_proof "
                                  "suppressed source=%s parent=%s reason=parent_unavailable", sid, parent)
                else:
                    recipients.append(("operator", "operator"))
                deliveries = basis["deliveries"]
                for target, generation in recipients:
                    key = json.dumps([target, generation], separators=(",", ":"))
                    delivery = deliveries.get(key)
                    if delivery is not None:
                        if target != "operator":
                            prior = await self.store.get_tell_delivery(delivery["tell_id"])
                            if prior is not None:
                                # A committed receipt owns recovery. Never re-paste
                                # pending/unsubmitted messages to manufacture green.
                                delivery["outcome"] = self._context_delivery_outcome(prior["reply"])
                                await self.store.record_nudge(sid, kind, now, json.dumps(basis, sort_keys=True))
                                continue
                        if delivery["outcome"] == "attempting":
                            delivery["outcome"] = "indeterminate"
                            await self.store.record_nudge(sid, kind, now, json.dumps(basis, sort_keys=True))
                            log.warning("subsystem=context_nudge bug_ref=context_notifications_handoff_proof "
                                        "receipt_missing source=%s target=%s tell_id=%s action=owner_reconcile",
                                        sid, target, delivery["tell_id"])
                        if delivery["outcome"] not in {"retryable", "notify_retryable"}:
                            continue
                        if now - delivery["last_attempt"] < self.config.cooldown_s:
                            continue
                    result.candidates += 1
                    if result.attempted >= self.config.max_per_pass:
                        result._capped = True
                        continue
                    if target == "operator" and self.notify is None:
                        result.errors += 1
                        log.warning("subsystem=context_nudge bug_ref=context_notifications_handoff_proof "
                                    "operator_route_unavailable source=%s", sid)
                        continue
                    if delivery is None:
                        identity = [sid, row["created_at"], kind, basis["epoch"], target, generation]
                        tell_id = "context-nudge:" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
                        crossing = basis["crossing"]
                        action = ("Prepare a cold-resumable checkpoint and arrange succession with your parent/Nexus."
                                  if kind == NUDGE_KIND_CONTEXT_HANDOFF else
                                  "Review context quality and prepare your next checkpoint.")
                        if target != sid:
                            action = (f"Coordinate a checkpoint and succession for {sid}."
                                      if kind == NUDGE_KIND_CONTEXT_HANDOFF else
                                      f"Review context quality and checkpoint readiness for {sid}.")
                        text = (f"{kind}: {sid} crossed {crossing['tokens']:,} context tokens "
                                f"of {crossing['window']:,} at {crossing['observed_at']}. {action}")
                        delivery = {"tell_id": tell_id, "message": text}
                        deliveries[key] = delivery
                    delivery.update(outcome="attempting", last_attempt=now)
                    # Crash without a receipt is ambiguous; the durable attempt
                    # fences automatic resubmission after restart.
                    await self.store.record_nudge(sid, kind, now, json.dumps(basis, sort_keys=True))
                    result.attempted += 1
                    try:
                        if target == "operator":
                            receipt = await self.notify.create_internal_notification(
                                producer="context_nudge", title=kind.replace("_", " ").capitalize(),
                                body=delivery["message"], dedup_key=delivery["tell_id"], severity="warning")
                            delivery.update(outcome="delivered", notification_id=receipt["notification_id"])
                            result.sent += 1
                        else:
                            reply = await self.comms.tell({"tell_id": delivery["tell_id"],
                                                         "stream_id": target, "message": delivery["message"]})
                            delivery["outcome"] = self._context_delivery_outcome(reply)
                            if delivery["outcome"] == "delivered":
                                result.sent += 1
                            else:
                                result.pending += 1
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # a failure must never mint a new tell ID
                        result.errors += 1
                        # Only route refusals prove there was no pane input.
                        # Generic delivery/transport errors may follow a paste.
                        precommit = isinstance(exc, VerbError) and exc.code in {
                            "unknown_session", "host_offline", "routing_integrity_mismatch",
                            "codex_reset_blocked", "codex_initial_prompt_pending",
                        }
                        delivery["outcome"] = ("notify_retryable" if target == "operator" else
                                               "retryable" if precommit else "indeterminate")
                        log.warning("subsystem=context_nudge bug_ref=context_notifications_handoff_proof "
                                    "delivery_failed source=%s target=%s outcome=%s error=%s",
                                    sid, target, delivery["outcome"], exc)
                    await self.store.record_nudge(sid, kind, now, json.dumps(basis, sort_keys=True))
                    log.info("subsystem=context_nudge bug_ref=context_notifications_handoff_proof "
                             "source=%s kind=%s epoch=%s target=%s outcome=%s tell_id=%s",
                             sid, kind, basis["epoch"], target, delivery["outcome"], delivery["tell_id"])
        return result

    # -- one pass ----------------------------------------------------------

    async def run_pass(self) -> NudgePassResult:
        """One reminder sweep. The forced-trigger seam tests call directly, so no
        test waits out a cadence."""
        now = time.time()
        rows = self.sessions.list_open()
        open_ids = {str(r.get("stream_id") or "") for r in rows if r.get("stream_id")}
        states = await self.store.nudge_states(keep_stream_ids=open_ids)

        # Deterministic order (stream_id, then title before card) so the per-pass
        # cap is stable across passes and reproducible in tests.  Group only
        # after each kind has passed its own need + cooldown gates: one stream
        # with two due kinds is one tell candidate, while a partial cooldown
        # leaves the other kind as a single-kind candidate.
        candidates: list[tuple[str, tuple[str, ...], dict[str, Any]]] = []
        for row in sorted(rows, key=lambda r: str(r.get("stream_id") or "")):
            sid = str(row.get("stream_id") or "")
            if not sid or not self._eligible(row) or not self._known_capture(row):
                continue
            if row.get("working") is not False:
                continue
            if row.get("operator_activity_generation") != row.get("session_generation"):
                restored = self.sessions.restore_genuine_activity(
                    sid, await self.store.fetch_session_event_tail(sid, limit=500),
                )
                if restored is not None:
                    row = restored
            if not self._has_activity_floor(row, now=now):
                continue
            if not await self._passes_user_turn_grace(row):
                continue
            due: list[str] = []
            if self._needs_title(row) and row.get("working") is False:
                if not self._within_cooldown(row, NUDGE_KIND_TITLE, states.get((sid, NUDGE_KIND_TITLE)), now=now):
                    due.append(NUDGE_KIND_TITLE)
            card_state = states.get((sid, NUDGE_KIND_CARD))
            if self._needs_card(row, now=now) and self._status_engaged(row, card_state, now=now):
                if not self._within_cooldown(row, NUDGE_KIND_CARD, card_state, now=now):
                    due.append(NUDGE_KIND_CARD)
            if due:
                candidates.append((sid, tuple(due), row))

        result = await self._context_pass(rows, now)
        remaining = max(0, self.config.max_per_pass - result.attempted)
        result.candidates += len(candidates)
        result._capped = result._capped or len(candidates) > remaining
        for sid, kinds, row in candidates[:remaining]:
            result.attempted += 1
            combined = len(kinds) == 2
            text = (
                "\n".join((NUDGE_TITLE_TEXT, NUDGE_CARD_TEXT))
                if combined
                else (NUDGE_TITLE_TEXT if kinds[0] == NUDGE_KIND_TITLE else NUDGE_CARD_TEXT)
            )
            tell_id = (
                f"nudge:combined:{sid}:{self._tell_epoch(sid, now)}"
                if combined
                else f"nudge:{kinds[0]}:{sid}:{self._tell_epoch(sid, now)}"
            )
            try:
                await self.comms.tell({
                    "tell_id": tell_id,
                    "stream_id": sid,
                    "message": text,
                })
                result.sent += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a dead pane is not a pass failure
                result.errors += 1
                log.warning("nudge %s tell failed stream=%s: %s",
                            "combined" if combined else kinds[0], sid, exc)
            if combined:
                await self.store.record_nudges([
                    (sid, NUDGE_KIND_TITLE, now, ""),
                    (sid, NUDGE_KIND_CARD, now, self._card_basis(row)),
                ])
            else:
                kind = kinds[0]
                basis = "" if kind == NUDGE_KIND_TITLE else self._card_basis(row)
                # Stamp the cooldown on success OR failure: a pane that fails the
                # echo receipt must still be paced, never re-nudged every pass.
                await self.store.record_nudge(sid, kind, now, basis)
        return result

    async def run_forever(self) -> None:
        """Cadence + backoff. Cancelled at shutdown; never swallows Cancelled."""
        cfg = self.config
        delay = cfg.interval_s if cfg.first_delay_s is None else cfg.first_delay_s
        failures = 0
        while True:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            try:
                result = await self.run_pass()
                failures = 0
                delay = cfg.interval_s
                if result.sent or result.errors:
                    log.info("nudges: %d sent, %d error(s), %d candidate(s)%s",
                             result.sent, result.errors, result.candidates,
                             " (capped)" if result.capped else "")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                failures += 1
                delay = min(cfg.backoff_max_s, cfg.backoff_base_s * (2 ** (failures - 1)))
                log.warning("nudge pass failed (%d): %s; retrying in %.0fs", failures, exc, delay)
