"""Durable, request-scoped final rulings for assistant-owned lanes.

The host admission freeze is deliberately independent of this state machine.
An approved intent still passes through the ordinary spawn/close path.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from typing import Any

from store_assistant_binding import _seat_conn
from store_lifecycle_authority import MAX_REASON, scrub
from _shared.spawn_objective import resolve_objective


RULING_REQUESTS_DDL = """
CREATE TABLE IF NOT EXISTS v2_assistant_lane_rulings (
    ruling_request_id TEXT PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL,
    intent_digest TEXT NOT NULL,
    intent_json TEXT NOT NULL,
    requester_stream_id TEXT NOT NULL,
    requester_generation TEXT NOT NULL,
    authority_stream_id TEXT NOT NULL,
    authority_generation TEXT NOT NULL,
    target_stream_id TEXT,
    target_generation TEXT,
    linked_from TEXT,
    created_at REAL NOT NULL,
    deadline REAL NOT NULL,
    state TEXT NOT NULL,
    ruling TEXT,
    reason TEXT,
    conditions TEXT,
    ruling_key TEXT,
    ruling_digest TEXT,
    outcome_json TEXT,
    mirror_sent INTEGER NOT NULL DEFAULT 0
)
"""
BART_LANE_OWNERSHIP_DDL = """
CREATE TABLE IF NOT EXISTS v2_assistant_bart_lanes (
    target_stream_id TEXT NOT NULL,
    target_generation TEXT NOT NULL,
    admitted_at REAL NOT NULL,
    primary_stream_id TEXT NOT NULL,
    primary_generation TEXT NOT NULL,
    admission_request_id TEXT NOT NULL,
    PRIMARY KEY(target_stream_id,target_generation)
)
"""
RULING_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS v2_assistant_lane_ruling_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ruling_request_id TEXT NOT NULL,
    event TEXT NOT NULL,
    actor_stream_id TEXT NOT NULL,
    actor_generation TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at REAL NOT NULL
)
"""

_STREAM_RE = re.compile(r"^[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+$")
_ACTION_KINDS = {"spawn", "session_close", "composite_admit", "composite_close"}
_TERMINAL = {"done", "denied", "revised", "approved_but_not_closed", "release_blocked"}
log = logging.getLogger("chat_streamd_v2.assistant_lane_rulings")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _acceptance_brief(intent: dict[str, Any]) -> str:
    explicit = str(intent.get("acceptance") or "").strip()
    if explicit:
        return explicit[:2000]
    prompt = str(intent.get("initial_prompt") or "")
    match = re.search(r"(?im)^\s*(?:#{1,4}\s*)?acceptance(?:\s+criteria)?\s*:?\s*(.*)$", prompt)
    if match:
        rest = prompt[match.start(1):]
        return re.split(r"(?m)^\s*#{1,4}\s+", rest, maxsplit=1)[0].strip()[:2000]
    return "not specified in spawn intent"


class AssistantLaneRulings:
    def __init__(self, server: Any) -> None:
        self.server = server
        self.store = server.store
        self._worker: asyncio.Task[None] | None = None
        self._release_locks: dict[str, asyncio.Lock] = {}
        self._authority_lock = asyncio.Lock()
        try:
            configured = float(os.environ.get("PENTACLE_RULING_SLA_S", "600"))
        except ValueError:
            configured = 600.0
        self.sla_s = min(3600.0, max(1.0, configured))

    def _composite(self) -> Any:
        return self.server.assistant_composite

    @staticmethod
    def _audit_conn(conn: sqlite3.Connection, request_id: str, event: str,
                    stream_id: str, generation: str, detail: str = "") -> None:
        conn.execute("INSERT INTO v2_assistant_lane_ruling_audit "
                     "(ruling_request_id,event,actor_stream_id,actor_generation,detail,created_at) "
                     "VALUES (?,?,?,?,?,?)",
                     (request_id, event, stream_id, generation, detail[:500], time.time()))

    async def _audit(self, request_id: str, event: str, stream_id: str,
                     generation: str, detail: str = "") -> None:
        def op(conn: sqlite3.Connection) -> None:
            with conn:
                self._audit_conn(conn, request_id, event, stream_id, generation, detail)
        await self.store.submit(op)

    async def binding(self) -> dict[str, Any]:
        composite = self._composite()
        if composite is None or not composite.enabled or not composite.config.direct_primary:
            return {"state": "unconfigured", "source": "none", "stream_id": "", "generation": ""}
        value = await self.store.get("assistant.authority.stream_id")
        source = "kv" if value is not None else "env"
        selected = str(value if value is not None else composite.env_config.authority_stream_id).strip()
        if not selected:
            return {"state": "unconfigured", "source": source, "stream_id": "", "generation": ""}
        if selected == "disabled":
            return {"state": "disabled", "source": "kv", "stream_id": "", "generation": ""}
        if not _STREAM_RE.fullmatch(selected):
            raise ValueError("assistant_authority_stream_id_invalid")
        if selected == composite.config.direct_primary_stream_id:
            return {"state": "same_primary", "source": source, "stream_id": selected,
                    "generation": composite.config.direct_primary_generation}
        host, _, name = selected.partition(":")
        seat = await self.store.fetch_session(host, name)
        generation = str((seat or {}).get("session_generation") or "")
        available = bool(seat and seat.get("status") == "open"
                         and seat.get("pane_status") == "pane_alive"
                         and generation)
        return {"state": "ready" if available else "unavailable", "source": source,
                "stream_id": selected, "generation": generation}

    async def configure(self, value: str, *, actor_stream_id: str, actor_generation: str) -> dict[str, Any]:
        composite = self._composite()
        if composite is None or (actor_stream_id, actor_generation) != (
            composite.config.direct_primary_stream_id, composite.config.direct_primary_generation,
        ):
            raise ValueError("assistant_authority_config_unauthorized")
        if value != "disabled" and value and not _STREAM_RE.fullmatch(value):
            raise ValueError("assistant_authority_stream_id_invalid")
        if value and value != "disabled":
            host, _, name = value.partition(":")
            seat = await self.store.fetch_session(host, name)
            if (seat is None or seat.get("status") != "open"
                    or seat.get("pane_status") != "pane_alive"
                    or not seat.get("session_generation")):
                raise ValueError("assistant_authority_target_unavailable")
        async with self._authority_lock:
            await self.store.put("assistant.authority.stream_id", value)
            result = await self.binding()
            if result["state"] in {"disabled", "unconfigured", "same_primary"}:
                await self._disable_pending()
        self._ensure_worker()
        return {"type": "assistant.authority.ok", **result}

    @staticmethod
    def _safe_intent(msg: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in msg.items()
                if key not in {"stream_token", "_auth_context", "_ruling_release",
                               "_ruling_report", "_ruling_report_waiver"}}

    async def request_spawn(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        composite = self._composite()
        if composite is None or not composite.enabled or not composite.config.direct_primary:
            return None
        auth = msg.get("_auth_context") or {}
        if (not auth.get("token_verified") or msg.get("handoff")
                or str(msg.get("role") or "").lower() not in {"lead", "nexus"}
                or (auth.get("stream_id"), auth.get("session_generation")) != (
                    composite.config.direct_primary_stream_id,
                    composite.config.direct_primary_generation,
                )):
            return None
        binding = await self.binding()
        if binding["state"] in {"disabled", "unconfigured", "same_primary"}:
            return None
        message = self._safe_intent(msg)
        if not str(message.get("objective") or "").strip():
            brief_reader = getattr(self.server.spawnctl, "_brief_from_message", None)
            brief = await brief_reader(message) if callable(brief_reader) else str(message.get("initial_prompt") or "")
            objective, source, error = resolve_objective(
                message.get("objective"), objective_supported=message.get("objective_supported"),
                parent_stream_id=message.get("parent_stream_id"), brief=brief,
                title=message.get("title"), objective_source=message.get("objective_source"),
            )
            if error:
                raise ValueError(error)
            message["objective"] = objective
            message["objective_source"] = source
        key = str(message.get("idempotency_key") or message.get("request_id") or "")
        if not key:
            raise ValueError("assistant_ruling_spawn_key_required")
        # A CLI retry carries a fresh RPC request ID. Keep the daemon intent
        # stable under its idempotency key, including an implicit seat name.
        message["request_id"] = key
        if not message.get("session_name"):
            message["session_name"] = "v2-" + hashlib.sha256(key.encode()).hexdigest()[:12]
        target = f"{message.get('host') or self.server.local_host}:{message['session_name']}"
        request = await self._create(
            action="spawn", key="spawn:" + key, intent=message,
            requester=str(auth["stream_id"]), requester_generation=str(auth["session_generation"]),
            binding=binding, target=target, target_generation="",
        )
        if request["state"] == "bypassed":
            return None
        if request["state"] == "done" and request.get("outcome_json"):
            return {**json.loads(request["outcome_json"]), "ruling_request_id": request["ruling_request_id"],
                    "unruled": request["ruling"] is None}
        if request["state"] in {"denied", "revised", "release_blocked"}:
            return {"type": "spawn.ruling_refused", "ruling_request_id": request["ruling_request_id"],
                    "state": request["state"], "reason": request["reason"]}
        if request["_binding_state"] == "unavailable":
            await self._expire(request["ruling_request_id"], "authority_unavailable")
            completed = await self._fetch(request["ruling_request_id"])
            if completed and completed["state"] == "done" and completed["outcome_json"]:
                return {**json.loads(completed["outcome_json"]), "ruling_request_id": request["ruling_request_id"],
                        "unruled": True}
            return {"type": "spawn.ruling_release_blocked", "ruling_request_id": request["ruling_request_id"],
                    "state": completed["state"] if completed else "unknown"}
        await self._enqueue_notice(request)
        self._ensure_worker()
        return {"type": "spawn.pending_ruling", "ruling_request_id": request["ruling_request_id"],
                "state": request["state"], "stream_id": target,
                "spawn_request_id": message.get("request_id")}

    async def request_close(self, msg: dict[str, Any], *, target_stream_id: str,
                            target_generation: str, auth: dict[str, Any]) -> dict[str, Any] | None:
        if msg.get("_ruling_release") is self:
            return None
        owned = await self.store.submit(lambda conn: _row(conn.execute(
            "SELECT * FROM v2_assistant_bart_lanes WHERE target_stream_id=? AND target_generation=?",
            (target_stream_id, target_generation),
        ).fetchone()))
        if owned is None:
            return None
        if auth.get("operator_authenticated"):
            await self._audit("operator-override:" + str(msg.get("request_id") or uuid.uuid4().hex),
                              "operator_override", str(auth.get("operator_principal") or "operator"),
                              "", f"target={target_stream_id};generation={target_generation}")
            return None
        binding = await self.binding()
        if binding["state"] in {"disabled", "unconfigured", "same_primary"}:
            return None
        requester = str(auth.get("stream_id") or "")
        generation = str(auth.get("session_generation") or "")
        if not requester or not generation:
            raise ValueError("assistant_lane_close_requester_unverified")
        message = self._safe_intent(msg)
        expected = message.get("expected_generation")
        if expected is not None and (not isinstance(expected, str) or expected.strip() != target_generation):
            raise ValueError("lifecycle_generation_mismatch")
        message["expected_generation"] = target_generation
        key = str(message.get("request_id") or "")
        if not key:
            raise ValueError("assistant_ruling_close_key_required")
        policy = self.server.sessions.assistant
        async with policy.authority_lock:
            prior = await self.store.submit(lambda conn: _row(conn.execute(
                "SELECT * FROM v2_assistant_lane_rulings WHERE request_key=?",
                ("session_close:" + key,),
            ).fetchone()))
            prior_intent = json.loads(prior["intent_json"]) if prior else {}
            prior_safe = self._safe_intent(prior_intent)
            if prior:
                # Legacy intents omitted the wire fence, but their reservation
                # already bound a generation. Normalize only for comparison;
                # never rewrite the durable intent or its digest on replay.
                prior_safe.setdefault("expected_generation", prior["target_generation"])
            report = await self.store.find_report(target_stream_id, statuses=("done", "error", "aborted"),
                                                  session_generation=target_generation)
            replay_waiver = isinstance(prior_intent.get("_ruling_report_waiver"), dict)
            if replay_waiver:
                if (prior["requester_stream_id"] != requester or prior["requester_generation"] != generation
                        or prior["target_stream_id"] != target_stream_id or prior["target_generation"] != target_generation
                        or prior_safe != message):
                    raise ValueError("assistant_ruling_request_key_conflict")
                message = prior_intent
            if report is None or replay_waiver:
                if not await policy.manager_holds(auth):
                    raise ValueError("assistant_lane_close_report_required")
                if code := policy.manager_request_code(msg):
                    raise ValueError(code)
                message["_ruling_report_waiver"] = {
                    "manager_stream_id": requester, "manager_generation": generation,
                    "target_generation": target_generation, "reason": scrub(msg["reason"], MAX_REASON),
                }
            else:
                message["_ruling_report"] = {
                    "report_id": report.get("report_id"), "summary": str(report.get("summary") or "")[:1200],
                    "qa_verdict": report.get("qa_verdict"), "residuals": report.get("next_action"),
                    "digest": _digest(report),
                }
                if prior and "expected_generation" not in prior_intent and expected is None:
                    message.pop("expected_generation")
            request = await self._create(
                action="session_close", key="session_close:" + key, intent=message,
                requester=requester, requester_generation=generation, binding=binding,
                target=target_stream_id, target_generation=target_generation,
            )
        if request["state"] == "bypassed":
            return None
        if request["state"] == "done" and request.get("outcome_json"):
            return {**json.loads(request["outcome_json"]), "ruling_request_id": request["ruling_request_id"]}
        if request["state"] in {"denied", "revised", "approved_but_not_closed", "release_blocked"}:
            return {"type": "close.ruling_refused", "ruling_request_id": request["ruling_request_id"],
                    "state": request["state"], "reason": request["reason"]}
        if request["_binding_state"] == "unavailable":
            await self._expire(request["ruling_request_id"], "authority_unavailable")
            completed = await self._fetch(request["ruling_request_id"])
            if completed and completed["state"] == "done" and completed["outcome_json"]:
                return {**json.loads(completed["outcome_json"]), "ruling_request_id": request["ruling_request_id"],
                        "unruled": True}
            return {"type": "close.ruling_release_blocked", "ruling_request_id": request["ruling_request_id"],
                    "state": completed["state"] if completed else "unknown"}
        await self._enqueue_notice(request)
        self._ensure_worker()
        return {"type": "close.pending_ruling", "ruling_request_id": request["ruling_request_id"],
                "state": request["state"], "stream_id": target_stream_id}

    async def request_composite(self, msg: dict[str, Any], *, actor_stream_id: str,
                                actor_generation: str, operation: str, lane_id: str,
                                expected_lane_version: int | None) -> dict[str, Any] | None:
        if msg.get("_ruling_release") is self:
            return None
        composite = self._composite()
        if composite is None or (actor_stream_id, actor_generation) != (
            composite.config.direct_primary_stream_id, composite.config.direct_primary_generation,
        ):
            return None
        binding = await self.binding()
        if binding["state"] in {"disabled", "unconfigured", "same_primary"}:
            return None
        action = "composite_admit" if operation == "lane.admit" else "composite_close"
        if action == "composite_close":
            previous = await self.store.submit(lambda conn: conn.execute(
                "SELECT 1 FROM v2_assistant_lane_rulings WHERE action='composite_admit' "
                "AND target_stream_id=? AND state='done' LIMIT 1", (lane_id,),
            ).fetchone() is not None)
            if not previous:  # pre-activation lane
                return None
        message = self._safe_intent(msg)
        message["_ruling_composite"] = {"dispatch_id": msg.get("dispatch_id"),
                                         "lane_id": lane_id, "expected_lane_version": expected_lane_version}
        key = str(message.get("request_id") or "")
        if not key:
            raise ValueError("assistant_ruling_operation_key_required")
        request = await self._create(
            action=action, key=action + ":" + key, intent=message,
            requester=actor_stream_id, requester_generation=actor_generation,
            binding=binding, target=lane_id, target_generation="",
        )
        if request["state"] == "bypassed":
            return None
        if request["state"] == "done" and request.get("outcome_json"):
            return {**json.loads(request["outcome_json"]), "ruling_request_id": request["ruling_request_id"]}
        if request["state"] in {"denied", "revised", "release_blocked"}:
            return {"type": "assistant.operation.ruling_refused",
                    "ruling_request_id": request["ruling_request_id"],
                    "state": request["state"], "reason": request["reason"]}
        if request["_binding_state"] == "unavailable":
            await self._expire(request["ruling_request_id"], "authority_unavailable")
            completed = await self._fetch(request["ruling_request_id"])
            if completed and completed["state"] == "done" and completed["outcome_json"]:
                return {**json.loads(completed["outcome_json"]), "ruling_request_id": request["ruling_request_id"],
                        "unruled": True}
            return {"type": "assistant.operation.ruling_release_blocked",
                    "ruling_request_id": request["ruling_request_id"],
                    "state": completed["state"] if completed else "unknown"}
        await self._enqueue_notice(request)
        self._ensure_worker()
        return {"type": "assistant.operation.pending_ruling",
                "ruling_request_id": request["ruling_request_id"], "state": request["state"],
                "lane_id": lane_id}

    async def _create(
        self, *, action: str, key: str, intent: dict[str, Any], requester: str,
        requester_generation: str, binding: dict[str, Any], target: str,
        target_generation: str, linked_from: str = "",
    ) -> dict[str, Any]:
        if action not in _ACTION_KINDS:
            raise ValueError("assistant_ruling_action_invalid")
        request_id = "ruling-" + uuid.uuid4().hex
        intent_digest = _digest(intent)
        now = time.time()
        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            with conn:
                existing = _row(conn.execute(
                    "SELECT * FROM v2_assistant_lane_rulings WHERE request_key=?", (key,)
                ).fetchone())
                if existing:
                    if (existing["intent_digest"] != intent_digest
                            or existing["requester_stream_id"] != requester
                            or existing["requester_generation"] != requester_generation):
                        raise ValueError("assistant_ruling_request_key_conflict")
                    return existing
                if not linked_from:
                    prior = conn.execute(
                        "SELECT ruling_request_id FROM v2_assistant_lane_rulings WHERE action=? "
                        "AND target_stream_id=? AND requester_stream_id=? AND state='revised' "
                        "ORDER BY created_at DESC LIMIT 1", (action, target, requester),
                    ).fetchone()
                    prior_link = str(prior[0]) if prior else ""
                else:
                    prior_link = linked_from
                conn.execute(
                    "INSERT INTO v2_assistant_lane_rulings (ruling_request_id,request_key,action,intent_digest,intent_json,"
                    "requester_stream_id,requester_generation,authority_stream_id,authority_generation,target_stream_id,"
                    "target_generation,linked_from,created_at,deadline,state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (request_id, key, action, intent_digest, _canonical(intent), requester,
                     requester_generation, binding["stream_id"], binding["generation"], target,
                     target_generation, prior_link or None, now, now + self.sla_s, "pending"),
                )
                self._audit_conn(conn, request_id, "request", requester, requester_generation, action)
                if isinstance(intent.get("_ruling_report_waiver"), dict):
                    self._audit_conn(conn, request_id, "report_prerequisite_waived", requester,
                                     requester_generation, scrub(intent.get("reason"), 500) or "")
                return dict(conn.execute(
                    "SELECT * FROM v2_assistant_lane_rulings WHERE ruling_request_id=?", (request_id,)
                ).fetchone())
        async with self._authority_lock:
            binding = await self.binding()
            if binding["state"] in {"disabled", "unconfigured", "same_primary"}:
                return {"state": "bypassed"}
            request = await self.store.submit(op)
            request["_binding_state"] = binding["state"]
            return request

    async def _fetch(self, request_id: str) -> dict[str, Any] | None:
        return await self.store.submit(lambda conn: _row(conn.execute(
            "SELECT * FROM v2_assistant_lane_rulings WHERE ruling_request_id=?", (request_id,)
        ).fetchone()))

    async def latest_for_target(self, target_stream_id: str) -> dict[str, Any] | None:
        row = await self.store.submit(lambda conn: _row(conn.execute(
            "SELECT * FROM v2_assistant_lane_rulings WHERE target_stream_id=? "
            "ORDER BY created_at DESC LIMIT 1", (target_stream_id,),
        ).fetchone()))
        if row is None:
            return None
        return {"ruling_request_id": row["ruling_request_id"], "action": row["action"],
                "state": row["state"], "ruling": row["ruling"], "reason": row["reason"],
                "authority_stream_id": row["authority_stream_id"],
                "authority_generation": row["authority_generation"],
                "target_generation": row["target_generation"],
                "created_at": row["created_at"], "deadline": row["deadline"],
                "outcome": json.loads(row["outcome_json"]) if row["outcome_json"] else None}

    async def await_spawn(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        request_id = str(msg.get("spawn_request_id") or "")
        stream_id = str(msg.get("stream_id") or msg.get("to_stream_id") or "")
        def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            if request_id:
                found = conn.execute(
                    "SELECT * FROM v2_assistant_lane_rulings WHERE action='spawn' "
                    "AND json_extract(intent_json,'$.request_id')=? ORDER BY created_at DESC LIMIT 1",
                    (request_id,),
                ).fetchone()
            elif stream_id:
                found = conn.execute(
                    "SELECT * FROM v2_assistant_lane_rulings WHERE action='spawn' "
                    "AND target_stream_id=? ORDER BY created_at DESC LIMIT 1",
                    (stream_id,),
                ).fetchone()
            else:
                return None
            return _row(found)
        row = await self.store.submit(op)
        if row is None or row["state"] == "done":
            return None
        return {"type": "await_spawn.ok", "ok": row["state"] not in {"denied", "revised", "release_blocked"},
                "state": "pending_ruling" if row["state"] == "pending" else row["state"],
                "ruling_request_id": row["ruling_request_id"], "stream_id": row["target_stream_id"],
                "spawn_request_id": json.loads(row["intent_json"]).get("request_id"),
                "reason": row["reason"]}

    async def _enqueue_notice(self, request: dict[str, Any]) -> None:
        rid = request["ruling_request_id"]
        intent = json.loads(request["intent_json"])
        fields = {key: intent.get(key) for key in ("objective", "provider", "model", "effort", "spec_id", "role", "budget", "eta", "_ruling_report", "_ruling_report_waiver")
                  if intent.get(key) is not None}
        if request["action"] == "spawn":
            fields["acceptance"] = _acceptance_brief(intent)
        if intent.get("initial_prompt"):
            fields["brief_excerpt"] = str(intent["initial_prompt"])[:1600]
        body = "[Assistant lane ruling request]\n" + _canonical({
            "ruling_request_id": rid, "action": request["action"],
            "target": request["target_stream_id"], "brief": fields,
            "deadline": request["deadline"], "intent_digest": request["intent_digest"],
        })
        await self.store.enqueue_outbound_notice(
            notice_id="assistant-lane-ruling:" + rid, kind="assistant_lane_ruling_request",
            dedupe_key="assistant-lane-ruling:" + rid, recipient_stream_id=request["authority_stream_id"],
            tell_id="assistant-lane-ruling:" + rid, source_stream_id=request["requester_stream_id"],
            body=body, metadata={"authority_generation": request["authority_generation"],
                                 "ruling_request_id": rid},
        )

    async def ruling(self, msg: dict[str, Any], *, actor_stream_id: str, actor_generation: str) -> dict[str, Any]:
        rid = str(msg.get("ruling_request_id") or "")
        ruling = str(msg.get("ruling") or "")
        reason = str(msg.get("reason") or "").strip()
        conditions = str(msg.get("conditions") or "").strip()
        key = str(msg.get("request_id") or "")
        if not rid or not key or ruling not in {"approve", "revise", "deny"}:
            raise ValueError("assistant_ruling_payload_invalid")
        if ruling in {"revise", "deny"} and not reason:
            raise ValueError("assistant_ruling_reason_required")
        if len(reason) > 1000 or len(conditions) > 2000:
            raise ValueError("assistant_ruling_text_too_long")
        digest = _digest({"ruling": ruling, "reason": reason, "conditions": conditions})
        now = time.time()
        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            with conn:
                request = _row(conn.execute(
                    "SELECT * FROM v2_assistant_lane_rulings WHERE ruling_request_id=?", (rid,)
                ).fetchone())
                if request is None:
                    raise ValueError("assistant_ruling_unknown_request")
                if (actor_stream_id, actor_generation) != (
                    request["authority_stream_id"], request["authority_generation"],
                ):
                    raise ValueError("assistant_ruling_authority_unverified")
                advisor = _seat_conn(conn, actor_stream_id)
                if (advisor is None or advisor.get("status") != "open"
                        or advisor.get("session_generation") != actor_generation):
                    raise ValueError("assistant_ruling_authority_unavailable")
                if _digest(json.loads(request["intent_json"])) != request["intent_digest"]:
                    raise ValueError("assistant_ruling_intent_corrupt")
                if request["ruling_key"]:
                    if request["ruling_key"] == key and request["ruling_digest"] == digest:
                        return {**request, "duplicate": True}
                    raise ValueError("assistant_ruling_conflict")
                if request["state"] != "pending" or now >= request["deadline"]:
                    raise ValueError("assistant_ruling_stale")
                if request["target_generation"]:
                    target = _seat_conn(conn, request["target_stream_id"])
                    if target is None or target.get("session_generation") != request["target_generation"]:
                        raise ValueError("assistant_ruling_target_generation_changed")
                new_state = {"approve": "approved", "revise": "revised", "deny": "denied"}[ruling]
                conn.execute(
                    "UPDATE v2_assistant_lane_rulings SET state=?,ruling=?,reason=?,conditions=?,ruling_key=?,ruling_digest=? "
                    "WHERE ruling_request_id=? AND state='pending'",
                    (new_state, ruling, reason, conditions, key, digest, rid),
                )
                self._audit_conn(conn, rid, "ruling", actor_stream_id, actor_generation, ruling)
                return dict(conn.execute(
                    "SELECT * FROM v2_assistant_lane_rulings WHERE ruling_request_id=?", (rid,)
                ).fetchone())
        try:
            request = await self.store.submit(op)
        except ValueError as exc:
            await self._audit(rid, "refused", actor_stream_id, actor_generation, str(exc))
            raise
        if request["state"] == "approved":
            await self._release(request)
        elif request["state"] in {"revised", "denied"} and not request.get("duplicate"):
            await self._result_notice(request)
        latest = await self._fetch(rid)
        return {"type": "assistant.ruling.ok", "ruling_request_id": rid,
                "state": latest["state"], "duplicate": bool(request.get("duplicate")),
                "outcome": json.loads(latest["outcome_json"]) if latest.get("outcome_json") else None}

    async def _result_notice(self, request: dict[str, Any]) -> None:
        rid = request["ruling_request_id"]
        await self.store.enqueue_outbound_notice(
            notice_id="assistant-lane-ruling-result:" + rid,
            kind="assistant_lane_ruling_result", dedupe_key="assistant-lane-ruling-result:" + rid,
            recipient_stream_id=request["requester_stream_id"],
            tell_id="assistant-lane-ruling-result:" + rid,
            source_stream_id=request["authority_stream_id"],
            body="[Assistant lane ruling] " + _canonical({
                "ruling_request_id": rid, "action": request["action"],
                "target": request["target_stream_id"], "ruling": request["ruling"],
                "state": request["state"], "reason": request["reason"],
                "conditions": request["conditions"],
                "outcome": json.loads(request["outcome_json"]) if request.get("outcome_json") else None,
            }),
            metadata={"authority_generation": request["requester_generation"]},
        )

    async def _expire(self, rid: str, reason: str) -> None:
        now = time.time()
        def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            with conn:
                conn.execute(
                    "UPDATE v2_assistant_lane_rulings SET state='unruled',reason=? "
                    "WHERE ruling_request_id=? AND state='pending'", (reason, rid),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    actor = conn.execute("SELECT authority_stream_id,authority_generation FROM "
                                         "v2_assistant_lane_rulings WHERE ruling_request_id=?", (rid,)).fetchone()
                    self._audit_conn(conn, rid, "timeout", actor[0], actor[1], reason)
                return _row(conn.execute(
                    "SELECT * FROM v2_assistant_lane_rulings WHERE ruling_request_id=?", (rid,)
                ).fetchone())
        request = await self.store.submit(op)
        if request and request["state"] == "unruled":
            await self._mirror_unruled(request)
            await self._release(request)

    async def _mirror_unruled(self, request: dict[str, Any]) -> None:
        if request.get("mirror_sent"):
            return
        composite = self._composite()
        if composite is None or not composite.enabled:
            return
        rid = request["ruling_request_id"]
        line = (f"Astra unavailable; proceeded unruled: {request['action']} "
                f"{request['target_stream_id']} ({rid}; {request.get('reason') or 'deadline'}).")
        event = {"stream_id": composite.config.stream_id, "provider": "composite",
                 "kind": "ASSIST_TEXT", "text": line, "publish_kind": "status",
                 "timestamp": time.time(), "attachments": [],
                 "raw": {"assistant_composite": True, "ruling_request_id": rid}}
        event_id = await self.store.append_session_event(
            composite.config.stream_id, event, identity="ruling-unruled:" + rid, limit=100,
        )
        if event_id is None:
            previous = await self.store.submit(lambda conn: conn.execute(
                "SELECT event_id,event_json FROM session_event_tail WHERE identity=?",
                ("ruling-unruled:" + rid,),
            ).fetchone())
            if previous is None:
                raise ValueError("assistant_ruling_mirror_receipt_missing")
            event_id = int(previous["event_id"])
            event = json.loads(previous["event_json"])
        if self.server.broadcast is not None:
            await self.server.broadcast({"type": "chat.event", "event": {**event, "daemon_seq": event_id}})
        def mark_mirrored(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute("UPDATE v2_assistant_lane_rulings SET mirror_sent=1 WHERE ruling_request_id=?", (rid,))
        await self.store.submit(mark_mirrored)

    async def _release(self, request: dict[str, Any]) -> None:
        rid = request["ruling_request_id"]
        lock = self._release_locks.setdefault(rid, asyncio.Lock())
        async with lock:
            current = await self._fetch(rid)
            if current is None or current["state"] not in {"approved", "unruled", "unruled_disabled"}:
                return
            intent = json.loads(current["intent_json"])
            intent["_auth_context"] = {
                "token_verified": True, "stream_id": current["requester_stream_id"],
                "session_generation": current["requester_generation"],
            }
            requester_host, _, requester_name = current["requester_stream_id"].partition(":")
            requester = await self.store.fetch_session(requester_host, requester_name)
            if requester is None or requester.get("status") != "open" or requester.get("session_generation") != current["requester_generation"]:
                await self._mark(rid, "release_blocked", {"error": "requester_generation_changed"})
                return
            try:
                if current["action"] == "spawn":
                    result = await self.server.spawnctl.spawn(intent, self.server.local_host)
                    if result.get("type") != "spawn.ok":
                        await self._mark(rid, "release_blocked", result)
                        return
                    stream_id = str(result.get("stream_id") or current["target_stream_id"])
                    host, _, name = stream_id.partition(":")
                    seat = await self.store.fetch_session(host, name)
                    if seat and seat.get("session_generation"):
                        await self._record_ownership(current, stream_id, seat["session_generation"])
                elif current["action"] == "session_close":
                    target_host, _, target_name = current["target_stream_id"].partition(":")
                    target = await self.store.fetch_session(target_host, target_name)
                    expected = intent.get("expected_generation", current["target_generation"])
                    if (expected != current["target_generation"] or target is None
                            or target.get("session_generation") != expected):
                        await self._mark(rid, "approved_but_not_closed", {"error": "target_generation_changed"})
                        return
                    if isinstance(intent.get("_ruling_report_waiver"), dict):
                        policy = self.server.sessions.assistant
                        if (not await policy.manager_holds(intent["_auth_context"])
                                or policy.manager_request_code(intent)):
                            await self._mark(rid, "approved_but_not_closed", {"error": "manager_waiver_authority_lost"})
                            return
                    else:
                        report = await self.store.find_report(
                            current["target_stream_id"], statuses=("done", "error", "aborted"),
                            session_generation=current["target_generation"],
                        )
                        if report is None or report.get("report_id") != intent.get("_ruling_report", {}).get("report_id"):
                            await self._mark(rid, "approved_but_not_closed", {"error": "report_fence_moved"})
                            return
                    intent.setdefault("expected_generation", expected)
                    intent["_ruling_release"] = self
                    result = await self.server._on_close(intent)
                    if result.get("type") == "close.deferred":
                        # Safe deferral is still actionable. The existing tick
                        # retries this state through all ordinary close fences.
                        if current.get("outcome_json") != _canonical(result):
                            await self._mark(rid, current["state"], result)
                        return
                    if result.get("type") not in {"close.ok", "close.already_closed"}:
                        await self._mark(rid, "approved_but_not_closed" if current["state"] == "approved" else "release_blocked", result)
                        return
                elif current["action"] in {"composite_admit", "composite_close"}:
                    intent["_ruling_release"] = self
                    result = await self._composite().operation(
                        intent, actor_stream_id=current["requester_stream_id"])
                else:  # pragma: no cover
                    raise ValueError("assistant_ruling_action_invalid")
            except Exception as exc:
                if getattr(exc, "code", "") == "spawn_frozen":
                    return
                failed_state = ("approved_but_not_closed" if current["action"] in {"session_close", "composite_close"}
                                and current["state"] == "approved" else "release_blocked")
                await self._mark(rid, failed_state, {"error": str(exc)[:300],
                    "error_code": getattr(exc, "code", "release_failed"),
                    **getattr(exc, "extra", {})})
                return
            await self._mark(rid, "done", result)

    async def _record_ownership(self, request: dict[str, Any], stream_id: str, generation: str) -> None:
        def op(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO v2_assistant_bart_lanes VALUES (?,?,?,?,?,?)",
                    (stream_id, generation, time.time(), request["requester_stream_id"],
                     request["requester_generation"], request["ruling_request_id"]),
                )
        await self.store.submit(op)

    async def _mark(self, rid: str, state: str, outcome: dict[str, Any]) -> None:
        def op(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute(
                    "UPDATE v2_assistant_lane_rulings SET state=?,outcome_json=? WHERE ruling_request_id=?",
                    (state, _canonical(outcome), rid),
                )
                actor = conn.execute("SELECT requester_stream_id,requester_generation FROM "
                                     "v2_assistant_lane_rulings WHERE ruling_request_id=?", (rid,)).fetchone()
                self._audit_conn(conn, rid, "release", actor[0], actor[1], state)
        await self.store.submit(op)
        if state in {"done", "release_blocked", "approved_but_not_closed"}:
            request = await self._fetch(rid)
            if request is not None:
                await self._result_notice(request)

    async def _disable_pending(self) -> None:
        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            with conn:
                pending = list(conn.execute("SELECT ruling_request_id,authority_stream_id,authority_generation "
                                            "FROM v2_assistant_lane_rulings WHERE state='pending'"))
                conn.execute("UPDATE v2_assistant_lane_rulings SET state='unruled_disabled',reason='authority_disabled' "
                             "WHERE state='pending'")
                for rid, stream_id, generation in pending:
                    self._audit_conn(conn, rid, "disabled", stream_id, generation)
                return [dict(row) for row in conn.execute(
                    "SELECT * FROM v2_assistant_lane_rulings WHERE state='unruled_disabled'")]
        for request in await self.store.submit(op):
            await self._mirror_unruled(request)
            await self._release(request)

    async def tick(self) -> None:
        now = time.time()
        rows = await self.store.submit(lambda conn: [dict(row) for row in conn.execute(
            "SELECT * FROM v2_assistant_lane_rulings WHERE state='pending' "
            "OR state IN ('approved','unruled','unruled_disabled')",
        )])
        for row in rows:
            if row["state"] == "pending":
                if row["deadline"] <= now:
                    await self._expire(row["ruling_request_id"], "deadline")
                else:
                    await self._enqueue_notice(row)
            else:
                if row["state"] in {"unruled", "unruled_disabled"}:
                    await self._mirror_unruled(row)
                await self._release(row)

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="assistant-lane-rulings")

    async def start(self) -> None:
        self._ensure_worker()
        await self.tick()

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                await self.tick()
            except Exception:
                log.exception("assistant lane ruling reconciliation failed; will retry")
