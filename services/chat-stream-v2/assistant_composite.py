"""Daemon-owned pane-less assistant composite conversation.

This is intentionally small: one persisted synthetic stream, one ordered
routing queue, and typed authority operations.  Backend seats remain ordinary
hidden sessions and can run concurrently; their visible prose is projected
back here only through ``assistant.publish``.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import re
import time
import traceback
import uuid
from typing import Any, Awaitable, Callable, Protocol

from assistant_router import AssistantRouterProcessError


COMPOSITE_CAPABILITY = "assistant_composite_v1"
COMPOSITE_GENERATION = "assistant-composite-v1"
# The fleet endpoint is deployment configuration, not a product identity.
# Keeping this empty also lets a feature-disabled daemon start before a local
# router has been configured.
DEFAULT_ROUTER_ENDPOINT = ""
DEFAULT_ROUTER_TIMEOUT_S = 45.0
_STREAM_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+$")
_ROUTING_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.IGNORECASE)
# These are daemon-generated labels, never sufficient user-facing evidence for
# a local model to select a task lane.  This remains a reject-only guard, not
# general topic/keyword routing.
_NON_DISTINCTIVE_SUMMARY_TOKENS = frozenset({
    "assistant", "composite", "lane", "summary", "bounded", "task", "work",
    "project", "request", "issue", "item", "topic", "active", "current", "new", "plan",
})
# Anchoring is deliberate: a word such as “there” or “continue” inside
# independent prose is not structural continuation.
_STRUCTURAL_CONTINUATION_RE = re.compile(
    r"^\s*(?:(?:please\s+)?continue(?:\s+(?:it|there|this|with\s+that))?"
    r"|(?:(?:what(?:'s|\s+is)\s+)?(?:the\s+)?next\s+step(?:\s+there)?))\s*[?.!]*\s*$",
    re.IGNORECASE,
)


log = logging.getLogger("chat_streamd_v2.assistant_composite")
_ROUTER_FAILURE_MESSAGE_MAX = 512
_ROUTER_FAILURE_TRACEBACK_MAX = 4096


def _env_bool(env: dict[str, str], key: str, default: bool = False) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _bounded_timeout(value: object, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return min(300.0, max(1.0, result))


@dataclass(frozen=True)
class AssistantCompositeConfig:
    """Portable daemon configuration.  Feature remains off unless enabled."""

    enabled: bool = False
    stream_id: str = ""
    router_endpoint: str = DEFAULT_ROUTER_ENDPOINT
    router_timeout_s: float = DEFAULT_ROUTER_TIMEOUT_S
    router_action_path: str = ""
    astra_stream_id: str = ""
    luna_stream_id: str = ""
    title: str = "Assistant"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "AssistantCompositeConfig":
        values = os.environ if env is None else env
        enabled = _env_bool(values, "PENTACLE_ASSISTANT_COMPOSITE_ENABLED")
        stream_id = str(values.get("PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID") or "").strip()
        if enabled and not _STREAM_ID_RE.fullmatch(stream_id):
            # A malformed feature config must fail closed, rather than accept
            # traffic and leave it without an identity.
            raise ValueError("assistant_composite_stream_id_required")
        endpoint = str(values.get("PENTACLE_ASSISTANT_ROUTER_ENDPOINT") or DEFAULT_ROUTER_ENDPOINT).strip()
        if enabled and (not endpoint.startswith("ssh://") or "/" not in endpoint[6:]):
            raise ValueError("assistant_router_endpoint_required")
        action_path = str(values.get("PENTACLE_ASSISTANT_ROUTER_ACTION_PATH") or "").strip()
        if enabled and (not action_path.startswith("/") or "\x00" in action_path):
            # The local adapter is host-private deployment configuration.  A
            # source tree must never hard-code a user's Windows/runtime path.
            raise ValueError("assistant_router_action_path_required")
        backend_ids: dict[str, str] = {}
        for name in ("PENTACLE_ASSISTANT_ASTRA_STREAM_ID", "PENTACLE_ASSISTANT_LUNA_STREAM_ID"):
            value = str(values.get(name) or "").strip()
            if value and not _STREAM_ID_RE.fullmatch(value):
                raise ValueError("assistant_backend_stream_id_invalid")
            backend_ids[name] = value
        if enabled and (not backend_ids["PENTACLE_ASSISTANT_ASTRA_STREAM_ID"]
                        or not backend_ids["PENTACLE_ASSISTANT_LUNA_STREAM_ID"]):
            raise ValueError("assistant_backend_stream_ids_required")
        return cls(
            enabled=enabled,
            stream_id=stream_id,
            router_endpoint=endpoint,
            router_timeout_s=_bounded_timeout(values.get("PENTACLE_ASSISTANT_ROUTER_TIMEOUT_S"), DEFAULT_ROUTER_TIMEOUT_S),
            router_action_path=action_path,
            astra_stream_id=backend_ids["PENTACLE_ASSISTANT_ASTRA_STREAM_ID"],
            luna_stream_id=backend_ids["PENTACLE_ASSISTANT_LUNA_STREAM_ID"],
            title=str(values.get("PENTACLE_ASSISTANT_COMPOSITE_TITLE") or "Assistant").strip()[:120] or "Assistant",
        )


class Router(Protocol):
    async def classify(self, route: dict[str, Any]) -> dict[str, Any]: ...


Dispatch = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
Broadcast = Callable[[dict[str, Any]], Awaitable[None]]
QuestionOperation = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
QuestionAnswer = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


class AssistantComposite:
    """Owns ordered admission/classification, never downstream completion."""

    def __init__(
        self,
        store: Any,
        *,
        config: AssistantCompositeConfig,
        router: Router | None = None,
        dispatch: Dispatch | None = None,
        broadcast: Broadcast | None = None,
        question_operation: QuestionOperation | None = None,
        question_answer: QuestionAnswer | None = None,
        publication_attachments: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.router = router
        self.dispatch = dispatch
        self.broadcast = broadcast
        self.question_operation = question_operation
        self.question_answer = question_answer
        self.publication_attachments = publication_attachments
        self._worker: asyncio.Task[None] | None = None
        self._worker_lock = asyncio.Lock()
        self._dispatch_tasks: set[asyncio.Task[None]] = set()
        self._owner = f"assistant-router-{uuid.uuid4().hex[:12]}"
        self._activity_lock = asyncio.Lock()
        self._activity = {"version": 1, "inputs": {}, "pending_count": 0, "waiting_for_operator_count": 0, "oldest_pending_at": None, "has_more": False}

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def is_stream(self, stream_id: object) -> bool:
        return self.enabled and str(stream_id or "") == self.config.stream_id

    def is_backend_stream(self, stream_id: object) -> bool:
        """Whether a target is one of this composite's hidden backend seats."""
        return self.enabled and str(stream_id or "") in {
            self.config.astra_stream_id, self.config.luna_stream_id,
        }

    async def suppress_routine_backend_ingress(
        self, *, target_stream_id: str, body: str, msg: dict[str, Any], verb: str,
    ) -> dict[str, Any] | None:
        """Persist routine peer ingress without waking hidden assistant backends.

        This is deliberately a target-scoped filter, not a broker: only the two
        configured assistant backend seats participate.  Operator input and the
        daemon's correlated dispatches keep their existing direct transport.
        Explicit blocker/escalation/decision gates and terminal END/report
        notices remain actionable and therefore pass through unchanged.
        """
        from outbound_notices import ASSISTANT_AUTHORITY_REQUEST_TOKEN
        if msg.get("_assistant_authority_request_token") is ASSISTANT_AUTHORITY_REQUEST_TOKEN:
            from sessions import VerbError
            if not self.enabled or target_stream_id != self.config.astra_stream_id:
                raise VerbError("unknown_session", "configured assistant authority changed")
            return None
        if not self.is_backend_stream(target_stream_id):
            return None
        if msg.get("_assistant_composite_backend_dispatch") is True:
            return None
        auth = msg.get("_auth_context") if isinstance(msg.get("_auth_context"), dict) else {}
        if auth.get("operator_authenticated") is True or msg.get("_assistant_operator_authenticated") is True:
            return None
        text = str(body or "").lstrip()
        upper = text.upper()
        forward = (
            upper.startswith("BLOCKER")
            or upper.startswith("ESCALATION")
            or (upper.startswith("GATE") and "DECISION" in upper)
            or upper.startswith("END")
            or upper.startswith("REPORT")
            or "[ASSISTANT COMPOSITE AUTHORITY DECISION]" in upper
            or "[ASSISTANT COMPOSITE TERMINAL REPORT]" in upper
        )
        if forward:
            return None
        identity = str(msg.get("tell_id") or msg.get("request_id") or "").strip()
        if not identity:
            identity = hashlib.sha256(
                (target_stream_id + "\x00" + verb + "\x00" + text).encode("utf-8"),
            ).hexdigest()[:32]
        event = {
            "stream_id": target_stream_id,
            "provider": "composite",
            "kind": "SYSTEM",
            "text": text,
            "timestamp": _now_iso(),
            "raw": {
                "assistant_composite_routine_ingress": True,
                "verb": verb,
                "from_stream_id": str(msg.get("from_stream_id") or ""),
                "identity": identity,
            },
        }
        # Existing event persistence gives auditors a durable record while
        # avoiding a pane paste/agent wake. It is intentionally bounded by the
        # ordinary session retention path, not a new inbox or transcript store.
        await self.store.append_session_event(
            target_stream_id, event, identity=f"assistant-ingress:{verb}:{identity}", limit=2000,
        )
        return {
            "type": f"{verb}.ok",
            "delivery_status": "persisted",
            "submission_confirmed": False,
            "action_committed": True,
            "assistant_backend_ingress": "persisted_suppressed",
        }

    async def ensure_projection(self) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        row = await self.store.ensure_assistant_composite_projection(
            stream_id=self.config.stream_id, title=self.config.title,
        )
        # The shared sessions schema stays untouched; these public fields are
        # projected on the existing inventory/events transport.
        await self.refresh_activity(broadcast=False)
        return self.project_session(row)

    async def recover(self) -> dict[str, int]:
        """Recover only pre-action classification; retain ambiguous delivery.

        A process can die at any point after the durable ``intent`` write.  It
        is consequently not safe to paste that route again merely because a
        new daemon starts: a provider may already have accepted it.  The store
        re-queues the purely local classification phase, but leaves every
        post-intent route honestly ``uncertain`` for receipt/provider evidence
        rather than claiming exactly-once physical delivery.
        """
        if not self.enabled:
            return {"requeued_classifying": 0, "retained_uncertain": 0}
        result = await self.store.recover_assistant_composite_routes(
            stream_id=self.config.stream_id,
        )
        await self.refresh_activity()
        self._wake_worker()
        return result

    def activity_snapshot(self):
        return deepcopy(self._activity)

    def working_payload(self):
        from assistant_activity import _elapsed
        now = _now_iso()
        return {"stream_id": self.config.stream_id, "timestamp": now,
                "tokens_input": 0, "tokens_output": 0, "tokens_cache_read": 0, "tokens_cache_creation": 0,
                "tokens_phase": "down" if self._activity["pending_count"] else "idle",
                "shell_count_started": 0, "tasks": [],
                "task_summary": {"total": 0, "done": 0, "in_progress": 0, "open": 0},
                "elapsed_ms": _elapsed(self._activity["oldest_pending_at"], now) or 0,
                "assistant_activity": self.activity_snapshot()}

    async def refresh_activity(self, *, broadcast=True):
        if not self.enabled:
            return
        from assistant_activity import summarize_activity
        async with self._activity_lock:
            inputs = await self.store.assistant_composite_activity(stream_id=self.config.stream_id)
            next_activity = summarize_activity(inputs)
            changed = next_activity != self._activity
            self._activity = next_activity
            if changed and broadcast and self.broadcast is not None:
                await self.broadcast({"type": "working.state", **self.working_payload()})

    async def enrich_events(self, events):
        ids = [event.get("message_id") for event in events if event.get("kind") == "USER" and event.get("message_id")]
        activity = await self.store.assistant_composite_activity(stream_id=self.config.stream_id, input_ids=ids)
        return [{**event, "raw": {**(event.get("raw") or {}), "assistant_activity": activity[event["message_id"]]}}
                if event.get("message_id") in activity else event for event in events]

    async def _update_route(self, *args, **kwargs):
        result = await self.store.update_assistant_composite_route(*args, **kwargs)
        await self.refresh_activity()
        return result

    def project_session(self, row: dict[str, Any]) -> dict[str, Any]:
        projected = dict(row)
        projected.update({
            "working": bool(self._activity["pending_count"]),
            "working_label": ("Waiting for Bart" if self._activity["pending_count"] else
                              "Waiting for you" if self._activity["waiting_for_operator_count"] else None),
            "assistant_activity": self.activity_snapshot(),
            "session_kind": "assistant_composite",
            "session_generation": COMPOSITE_GENERATION,
            "provider": "composite",
            "role": "assistant_composite",
            "capabilities": {
                "pane": False,
                "terminal": False,
                COMPOSITE_CAPABILITY: True,
                "reply_metadata_v1": True,
            },
        })
        return projected

    async def accept_input(self, msg: dict[str, Any], *, operator_principal: str | None = None) -> dict[str, Any]:
        """Accept literal operator input once and wake the routing consumer."""
        if not self.enabled:
            raise ValueError("assistant_composite_disabled")
        body = msg.get("text") if "text" in msg else msg.get("message")
        body = "" if body is None else str(body)
        attachments = msg.get("attachments") or []
        if not isinstance(attachments, list) or any(not isinstance(item, dict) for item in attachments):
            raise ValueError("assistant_attachments_invalid")
        if not body and not attachments:
            raise ValueError("assistant_input_required")
        optimistic_id = str(msg.get("optimistic_id") or "").strip()
        explicit_msg_id = str(msg.get("msg_id") or "").strip()
        # Desktop retries rotate request_id.  Its optimistic id is immutable;
        # CLI callers can use their stable commission/msg id instead.
        input_identity = optimistic_id or explicit_msg_id
        if not input_identity:
            raise ValueError("assistant_input_identity_required")
        request_id = str(msg.get("request_id") or input_identity).strip() or input_identity
        existing = await self.store.get_assistant_composite_route(
            stream_id=self.config.stream_id, input_identity=input_identity,
        )
        # An explicit reply is a deterministic correlation request, never an
        # invitation to run the classifier.  Validate a new reply before
        # admitting its USER event; a durable retry returns its old receipt
        # even if the downstream binding has since handed off.
        explicit_target = None if existing is not None else await self._explicit_reply_target({
            "reply_to_message_id": _optional_id(msg.get("reply_to_message_id")),
            "reply_to_question_id": _optional_id(msg.get("reply_to_question_id")),
        })
        record = await self.store.admit_assistant_composite_input(
            stream_id=self.config.stream_id,
            input_identity=input_identity,
            input_request_id=request_id,
            body=body,
            attachments=attachments,
            reply_to_message_id=_optional_id(msg.get("reply_to_message_id")),
            reply_to_question_id=_optional_id(msg.get("reply_to_question_id")),
            actor_stream_id=operator_principal,
        )
        await self.refresh_activity()
        if not record.get("duplicate") and self.broadcast is not None:
            await self.broadcast({"type": "chat.event", "event": (await self.enrich_events([record["event"]]))[0]})
        if not record.get("duplicate"):
            if explicit_target is not None:
                question_id = _optional_id(msg.get("reply_to_question_id"))
                if question_id:
                    # The existing durable question row remains the answer
                    # lifecycle.  Its issuer is historical provenance only;
                    # this accepted composite input is delivered to the lane's
                    # CURRENT binding below, never automatically to a stale
                    # producer pane.
                    if self.question_answer is None:
                        raise ValueError("assistant_question_answer_adapter_unavailable")
                    answered = await self.question_answer(question_id, body, msg)
                    if not bool(answered.get("ok")):
                        updated = await self._update_route(
                            str(record["route_id"]), routing_state="routing_failed",
                            error_code="assistant_question_answer_failed",
                        )
                        if updated is not None:
                            record = updated
                        return {
                            "type": "assistant.send.accepted",
                            "stream_id": self.config.stream_id,
                            "route_id": record["route_id"],
                            "message_id": record["input_identity"],
                            "event_id": record.get("event_id"),
                            "routing_state": record["routing_state"],
                            "delivery_state": record.get("delivery_state"),
                            "duplicate": False,
                        }
                    lane = await self.store.get_assistant_composite_lane_for_question(
                        stream_id=self.config.stream_id, question_id=question_id,
                    )
                    if lane is not None:
                        await self.store.set_assistant_composite_lane_question(
                            stream_id=self.config.stream_id,
                            lane_id=str(lane["lane_id"]), question_id=None,
                        )
                target, generation, reply_lane_id = explicit_target
                dispatch_id = "assistant-reply-" + uuid.uuid4().hex
                backend_context = _backend_routing_context(await self._router_input(record))
                updated = await self._update_route(
                    str(record["route_id"]), routing_state="resolved", delivery_state="intent",
                    dispatch_id=dispatch_id, route_target=target, route_target_generation=generation,
                    route_payload={"schema_version": "assistant-router/v1", "disposition": "lane" if reply_lane_id else "conversation", "lane_id": reply_lane_id,
                                   "depends_on_message_id": None, "reason": "explicit_reply",
                                   "backend_context": backend_context},
                )
                if updated is not None:
                    record = updated
                    self._start_dispatch(updated)
            else:
                self._wake_worker()
        return {
            "type": "assistant.send.accepted",
            "stream_id": self.config.stream_id,
            "route_id": record["route_id"],
            "message_id": record["input_identity"],
            "event_id": record.get("event_id"),
            "routing_state": record["routing_state"],
            "delivery_state": record.get("delivery_state"),
            "duplicate": bool(record.get("duplicate")),
        }

    async def _explicit_reply_target(self, route: dict[str, Any]) -> tuple[str, str, str | None] | None:
        """Use an explicit current correlation without spending a router turn."""
        reply_id = _optional_id(route.get("reply_to_message_id"))
        question_id = _optional_id(route.get("reply_to_question_id"))
        if not reply_id and not question_id:
            return None
        if question_id:
            lane = await self.store.get_assistant_composite_lane_for_question(
                stream_id=self.config.stream_id, question_id=question_id,
            )
            target = str((lane or {}).get("bound_stream_id") or "")
            if lane is None or not target:
                raise ValueError("assistant_explicit_question_lane_unresolved")
            if reply_id:
                message_target = await self._explicit_reply_target({"reply_to_message_id": reply_id, "lane_hint": lane["lane_id"]})
                if message_target is None or message_target[2] != str(lane["lane_id"]):
                    raise ValueError("assistant_explicit_question_reply_mismatch")
            return target, await self._target_generation(target), str(lane["lane_id"])
        prior = await self.store.get_assistant_composite_route(
            stream_id=self.config.stream_id, input_identity=str(reply_id),
        )
        if str(reply_id).startswith("publication:"):
            publication = await self.store.get_assistant_composite_publication(
                stream_id=self.config.stream_id, publication_key=str(reply_id)[len("publication:"):],
            )
            if publication is not None:
                if prior is not None:
                    raise ValueError("assistant_explicit_reply_ambiguous")
                prior = await self.store.get_assistant_composite_route(
                    stream_id=self.config.stream_id, input_identity=publication["reply_to_message_id"],
                )
        if prior is None:
            raise ValueError("assistant_explicit_reply_unresolved")
        try:
            prior_result = json.loads(str(prior.get("route_json") or "{}"))
        except (TypeError, ValueError) as exc:
            raise ValueError("assistant_explicit_reply_unresolved") from exc
        if not isinstance(prior_result, dict):
            raise ValueError("assistant_explicit_reply_unresolved")
        admitted_lanes = await self.store.list_assistant_composite_admitted_lanes(
            stream_id=self.config.stream_id, dispatch_id=str(prior.get("dispatch_id") or ""),
        )
        if len(admitted_lanes) > 1:
            if route.get("lane_hint") not in admitted_lanes:
                raise ValueError("assistant_explicit_reply_ambiguous")
            prior_result["lane_id"] = route["lane_hint"]
        if prior_result.get("lane_id"):
            lane = await self.store.get_assistant_composite_lane(
                stream_id=self.config.stream_id, lane_id=str(prior_result["lane_id"]),
            )
            if not lane or str(lane.get("phase") or "") not in {"discussion", "execution", "waiting"}:
                raise ValueError("assistant_explicit_reply_lane_not_current")
            # Admission precedes lead binding. As with an inferred lane route,
            # its current authority owns replies until a lead has been bound.
            target = await self._target_for_decision({"disposition": "lane", "lane_id": lane["lane_id"]})
        else:
            target = str(prior.get("route_target") or "")
        if not target:
            raise ValueError("assistant_explicit_reply_unresolved")
        return target, await self._target_generation(target), prior_result.get("lane_id")

    def _wake_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_worker(), name="assistant-composite-routing")

    async def _run_worker(self) -> None:
        # One claim/classification at a time.  A dispatch is detached before
        # the next route is claimed, so a slow model/lead cannot serialize
        # independent conversations behind its completion.
        try:
            async with self._worker_lock:
                while True:
                    route = await self.store.claim_assistant_composite_route(
                        stream_id=self.config.stream_id, owner=self._owner,
                    )
                    if route is None:
                        return
                    await self.refresh_activity()
                    await self._classify_one(route)
        except RuntimeError as exc:
            # Daemon teardown owns cancellation/store closure.  A detached
            # worker that loses that race has no legal recovery write to make.
            if str(exc) != "store is not running":
                raise

    async def stop(self) -> None:
        """Cancel non-authoritative in-process workers before store shutdown."""
        tasks = [task for task in (self._worker, *self._dispatch_tasks) if task is not None and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _classify_one(self, route: dict[str, Any]) -> None:
        router_input: dict[str, Any] | None = None
        started = time.monotonic()
        try:
            if self.router is None:
                raise RuntimeError("assistant_router_unconfigured")
            router_input = await self._router_input(route)
            decision = await asyncio.wait_for(
                self.router.classify(router_input), timeout=self.config.router_timeout_s,
            )
            normalized = await self._normalize_decision(decision, route, origin="local_router")
            if normalized["disposition"] != "defer":
                target = await self._target_for_decision(normalized)
                generation = await self._target_generation(target)
            router_decision_receipt_id = "assistant-router-decision-" + hashlib.sha256(
                json.dumps({
                    "route_id": str(route.get("route_id") or ""),
                    "router_input": router_input,
                    "decision": normalized,
                }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
            ).hexdigest()[:32]
            if normalized["disposition"] == "defer":
                await self._update_route(
                    str(route["route_id"]), routing_state="deferred",
                    depends_on_message_id=normalized["depends_on_message_id"],
                    route_payload={**normalized, "router_decision_receipt_id": router_decision_receipt_id},
                )
                return
            dispatch_id = "assistant-dispatch-" + uuid.uuid4().hex
            updated = await self._update_route(
                str(route["route_id"]), routing_state="resolved", delivery_state="intent",
                dispatch_id=dispatch_id, route_target=target, route_target_generation=generation,
                route_payload={
                    **normalized,
                    "router_decision_receipt_id": router_decision_receipt_id,
                    "backend_context": _backend_routing_context(router_input),
                },
            )
            if updated is not None:
                self._start_dispatch(updated)
        except Exception as exc:  # classification failure is not input loss
            await self._fallback_or_fail(
                route,
                f"router_failed:{type(exc).__name__}",
                router_context=router_input,
                failure_record=_router_failure_record(
                    exc, _route_elapsed_ms(route, started),
                ),
            )

    async def _router_input(self, route: dict[str, Any]) -> dict[str, Any]:
        body = str(route.get("body") or "")
        excerpt = body[:4000]
        unresolved_rows = await self.store.list_assistant_composite_unresolved(
            stream_id=self.config.stream_id, exclude_route_id=str(route.get("route_id") or ""), limit=8,
        )
        unresolved = [
            {
                "message_id": item.get("input_identity"),
                "sequence": item.get("event_id"),
                "state": item.get("routing_state"),
                "excerpt": str(item.get("body") or "")[:384],
                "truncated": len(str(item.get("body") or "")) > 384,
            }
            for item in unresolved_rows
        ]
        # Keep the public chat tail small for prompt size, but retain enough of
        # the durable tail to find the latest ASSIST_TEXT for every open lane.
        # The event id stamped by ``fetch_session_event_tail`` is the
        # authoritative ordering key; timestamps are display metadata only.
        event_rows = await self.store.fetch_session_event_tail(self.config.stream_id, limit=128)
        recent_messages: list[dict[str, Any]] = []
        for event in event_rows:
            raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
            message_id = str(raw.get("input_identity") or f"event:{event.get('daemon_seq')}")
            if message_id == str(route.get("input_identity") or ""):
                continue
            text = str(event.get("text") or "")
            recent_messages.append({
                "message_id": message_id,
                "kind": str(event.get("kind") or ""),
                "excerpt": text[:384],
                "truncated": len(text) > 384,
            })
        open_lanes = await self.store.list_assistant_composite_open_lanes(
            stream_id=self.config.stream_id, limit=16,
        )

        last_outbound_by_lane: dict[str, dict[str, Any]] = {}
        route_cache: dict[str, dict[str, Any] | None] = {}
        allowed_publish_kinds = {"prose", "question", "result", "status"}
        for event in event_rows:
            if str(event.get("kind") or "") != "ASSIST_TEXT":
                continue
            raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
            publish_kind = str(event.get("publish_kind") or raw.get("publish_kind") or "")
            if publish_kind not in allowed_publish_kinds:
                continue
            dispatch_id = str(raw.get("dispatch_id") or "").strip()
            if not dispatch_id:
                continue
            if dispatch_id not in route_cache:
                route_cache[dispatch_id] = await self.store.find_assistant_composite_route_by_dispatch(dispatch_id)
            route_for_output = route_cache[dispatch_id]
            if route_for_output is None:
                continue
            try:
                decision = json.loads(str(route_for_output.get("route_json") or "{}"))
            except (TypeError, ValueError):
                continue
            if not isinstance(decision, dict):
                continue
            lane_id = _optional_id(decision.get("lane_id"))
            if not lane_id:
                continue
            try:
                durable_id = int(event.get("daemon_seq"))
            except (TypeError, ValueError):
                continue
            prior = last_outbound_by_lane.get(lane_id)
            try:
                prior_id = int(prior["daemon_seq"]) if prior is not None else -1
            except (TypeError, ValueError):
                prior_id = -1
            if durable_id >= prior_id:
                text = str(event.get("text") or "")
                outbound_excerpt = text[:384]
                last_outbound_by_lane[lane_id] = {
                    "daemon_seq": durable_id,
                    "excerpt": outbound_excerpt,
                    "truncated": len(text) > len(outbound_excerpt),
                }

        lane_context: list[dict[str, Any]] = []
        for item in open_lanes:
            lane_id = _optional_id(item.get("lane_id"))
            last_outbound = last_outbound_by_lane.get(lane_id or "")
            lane_context.append({
                "lane_id": item.get("lane_id"),
                "state": item.get("phase"),
                "version": item.get("version"),
                "backend_kind": item.get("bound_backend_kind"),
                "pending_question_id": item.get("pending_question_id"),
                "summary": str(item.get("summary") or "")[:512],
                "last_outbound_excerpt": (
                    last_outbound["excerpt"] if last_outbound is not None else None
                ),
                "last_outbound_truncated": (
                    bool(last_outbound["truncated"]) if last_outbound is not None else False
                ),
            })
        # The durable store remains authoritative; this contextual list is
        # intentionally only a routing hint and never replaces original input.
        return {
            "schema_version": "assistant-router/v1",
            "message_id": route.get("input_identity"),
            "body_excerpt": excerpt,
            "body_truncated": len(body) > len(excerpt),
            "original_length": len(body),
            "recent_messages": recent_messages[-6:],
            "unresolved_inputs": unresolved,
            "open_lanes": lane_context,
        }

    async def _normalize_decision(
        self, raw: object, route: dict[str, Any], *, origin: str,
    ) -> dict[str, Any]:
        # Origin is private daemon control flow, not caller-provided wire data.
        # Luna has already passed route.resolve's separate token/dispatch/
        # generation checks before reaching this normalizer.
        if origin not in {"local_router", "luna_fallback"}:
            raise ValueError("assistant_router_origin_invalid")
        if not isinstance(raw, dict):
            raise ValueError("assistant_router_result_invalid")
        expected = {
            "schema_version", "disposition", "lane_id", "depends_on_message_id", "reason",
        }
        if set(raw) != expected or raw.get("schema_version") != "assistant-router/v1":
            raise ValueError("assistant_router_result_invalid")
        disposition = str(raw.get("disposition") or "")
        lane_id = _optional_id(raw.get("lane_id"))
        dependency = _optional_id(raw.get("depends_on_message_id"))
        reason = raw.get("reason")
        if disposition not in {"lane", "new_topic", "clarify", "conversation", "defer"}:
            raise ValueError("assistant_router_disposition_invalid")
        if not isinstance(reason, str) or len(reason) > 240:
            raise ValueError("assistant_router_reason_invalid")
        if disposition == "defer":
            if not dependency or dependency == str(route.get("input_identity") or ""):
                raise ValueError("assistant_router_defer_invalid")
            if lane_id is not None:
                raise ValueError("assistant_router_defer_invalid")
            unresolved = await self.store.list_assistant_composite_unresolved(
                stream_id=self.config.stream_id, exclude_route_id=str(route.get("route_id") or ""), limit=8,
            )
            if dependency not in {str(item.get("input_identity") or "") for item in unresolved}:
                raise ValueError("assistant_router_defer_invalid")
        elif disposition == "lane":
            if not lane_id or dependency is not None:
                raise ValueError("assistant_router_lane_invalid")
            lane = await self.store.get_assistant_composite_lane(
                stream_id=self.config.stream_id, lane_id=lane_id,
            )
            if lane is None or str(lane.get("phase") or "") not in {"discussion", "execution", "waiting"}:
                raise ValueError("assistant_router_lane_invalid")
            if origin == "local_router" and not await self._has_local_lane_provenance(route, lane_id):
                # A local guess never becomes an implicit task-lane dispatch.
                # The caller takes the existing one-time asynchronous fallback.
                raise ValueError("assistant_router_lane_provenance_unverified")
        elif lane_id is not None or dependency is not None:
            raise ValueError("assistant_router_target_invalid")
        return {
            "schema_version": "assistant-router/v1", "disposition": disposition,
            "lane_id": lane_id, "depends_on_message_id": dependency, "reason": reason,
        }

    async def _has_local_lane_provenance(self, route: dict[str, Any], lane_id: str) -> bool:
        """Accept a local lane only with current user-facing target evidence."""
        open_lanes = await self.store.list_assistant_composite_open_lanes(
            stream_id=self.config.stream_id, limit=16,
        )
        target = next((item for item in open_lanes if str(item.get("lane_id") or "") == lane_id), None)
        if target is None:
            return False
        input_tokens = _distinctive_summary_tokens(str(route.get("body") or ""))
        target_tokens = _distinctive_summary_tokens(str(target.get("summary") or ""))
        other_tokens: set[str] = set()
        for item in open_lanes:
            if item is target:
                continue
            other_tokens.update(_distinctive_summary_tokens(str(item.get("summary") or "")))
        if input_tokens & (target_tokens - other_tokens):
            return True
        # Structural continuation is retained above as provenance vocabulary,
        # but it is never enough to select the sole remaining lane.  A
        # non-explicit short reply must remain a router decision or fall back
        # to Luna for correlation.
        return False

    async def _target_for_decision(self, decision: dict[str, Any]) -> str:
        """Map frozen classifier dispositions to configured backend bindings."""
        disposition = str(decision["disposition"])
        if disposition == "lane":
            lane = await self.store.get_assistant_composite_lane(
                stream_id=self.config.stream_id, lane_id=str(decision["lane_id"]),
            )
            # An admitted discussion has no bound lead yet: authority owns the
            # admission/bind decision, while an active lane stays with its lead.
            target = str((lane or {}).get("bound_stream_id") or self.config.astra_stream_id)
        elif disposition == "new_topic":
            target = self.config.astra_stream_id
        else:  # conversation and clarify are front-door Luna work
            target = self.config.luna_stream_id
        if not target or not _STREAM_ID_RE.fullmatch(target):
            raise ValueError("assistant_router_target_unavailable")
        return target

    async def _target_generation(self, target: str) -> str:
        host, separator, session_name = target.partition(":")
        if not separator or not host or not session_name:
            raise ValueError("assistant_backend_target_invalid")
        row = await self.store.fetch_session(host, session_name)
        if row is None or str(row.get("status") or "") != "open":
            raise ValueError("assistant_backend_target_unavailable")
        generation = str(row.get("session_generation") or "")
        if not generation:
            raise ValueError("assistant_backend_generation_unavailable")
        return generation

    async def _fallback_or_fail(
        self,
        route: dict[str, Any],
        error_code: str,
        *,
        router_context: dict[str, Any] | None = None,
        failure_record: dict[str, Any] | None = None,
    ) -> None:
        fallback_dispatch_id = "assistant-fallback-" + uuid.uuid4().hex
        fallback_payload: dict[str, Any] = {
            "kind": "luna_fallback_classifier",
            "reason": error_code,
            "routing_context": _fallback_routing_context(router_context),
        }
        if failure_record is not None:
            fallback_payload["router_failure"] = dict(failure_record)
            log.warning(
                "assistant router fallback dispatch_id=%s exception_type=%s elapsed_ms=%s",
                fallback_dispatch_id,
                failure_record.get("exception_type", "Unknown"),
                failure_record.get("elapsed_ms", 0),
            )
        if not self.config.luna_stream_id:
            await self._update_route(
                str(route["route_id"]), routing_state="routing_failed", error_code=error_code,
                route_payload=fallback_payload,
            )
            return
        try:
            generation = await self._target_generation(self.config.luna_stream_id)
        except ValueError:
            await self._update_route(
                str(route["route_id"]), routing_state="routing_failed", error_code=error_code,
                route_payload=fallback_payload,
            )
            return
        fallback_payload["fallback_receipt"] = {
            "receipt_id": "assistant-fallback-receipt-" + hashlib.sha256(
                (str(route.get("route_id") or "") + "\x00" + fallback_dispatch_id).encode("utf-8"),
            ).hexdigest()[:32],
            "dispatch_id": fallback_dispatch_id,
            "kind": "luna_fallback_classifier",
        }
        updated = await self._update_route(
            str(route["route_id"]), routing_state="fallback_dispatched", delivery_state="intent",
            dispatch_id=fallback_dispatch_id, route_target=self.config.luna_stream_id,
            route_target_generation=generation, route_payload=fallback_payload,
            error_code=error_code,
        )
        if updated is not None:
            self._start_dispatch(updated)

    def _start_dispatch(self, route: dict[str, Any]) -> None:
        task = asyncio.create_task(self._dispatch_one(route), name=f"assistant-dispatch:{route['route_id']}")
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._dispatch_tasks.discard)

    async def _dispatch_one(self, route: dict[str, Any]) -> None:
        # `intent` was persisted before this task exists.  Any crash after the
        # downstream action but before a receipt stays uncertain, never blindly
        # reinjected or claimed exactly-once.
        if self.dispatch is None:
            await self._update_route(
                str(route["route_id"]), routing_state=str(route["routing_state"]),
                expected_dispatch_id=str(route["dispatch_id"]), expected_routing_state=str(route["routing_state"]),
                delivery_state="uncertain", error_code="assistant_dispatch_unconfigured",
            )
            return
        try:
            result = await self.dispatch(dict(route))
        except Exception as exc:  # physical boundary is ambiguous
            await self._update_route(
                str(route["route_id"]), routing_state=str(route["routing_state"]),
                expected_dispatch_id=str(route["dispatch_id"]), expected_routing_state=str(route["routing_state"]),
                delivery_state="uncertain", error_code=f"dispatch_exception:{type(exc).__name__}",
            )
            return
        delivery = str(
            result.get("delivery")
            or result.get("delivery_state")
            or result.get("delivery_status")
            or ""
        ).lower()
        if delivery in {"landed", "delivered"}:
            state = "landed"
        elif delivery in {"committed_pending", "committed_pending_proof", "accepted"}:
            state = "committed_pending"
        elif delivery in {"not_landed", "failed", "pasted_unsubmitted"}:
            state = "failed"
        else:
            state = "uncertain"
        await self._update_route(
            str(route["route_id"]), routing_state=str(route["routing_state"]),
            expected_dispatch_id=str(route["dispatch_id"]), expected_routing_state=str(route["routing_state"]),
            delivery_state=state, error_code=None if state != "uncertain" else "dispatch_receipt_ambiguous",
        )

    async def _authenticated_generation(self, msg, actor):
        current = await self._target_generation(str(actor or ""))
        auth = msg.get("_auth_context") or {}
        if "session_generation" in auth and auth["session_generation"] != current:
            raise ValueError("assistant_actor_generation_unverified")
        return current

    async def publish(self, msg: dict[str, Any], *, actor_stream_id: str | None) -> dict[str, Any]:
        if not self.enabled:
            raise ValueError("assistant_composite_disabled")
        publish_fields = {
            "type", "request_id", "composite_stream_id", "dispatch_id",
            "reply_to_message_id", "reply_to_question_id", "publish_kind", "message",
            "attachment_ids", "evidence_refs", "response_state",
        }
        if {str(key) for key in msg if not str(key).startswith("_")} - publish_fields:
            raise ValueError("assistant_publish_payload_invalid")
        if _optional_id(msg.get("composite_stream_id")) != self.config.stream_id:
            raise ValueError("assistant_publish_stream_unverified")
        publication_key = _optional_id(msg.get("request_id"))
        dispatch_id = _optional_id(msg.get("dispatch_id"))
        body = msg.get("message")
        body = "" if body is None else str(body)
        if not publication_key or not dispatch_id or not body:
            raise ValueError("assistant_publish_required_fields")
        kind = str(msg.get("publish_kind") or "")
        if kind not in {"prose", "question", "result", "status"}:
            raise ValueError("assistant_publish_kind_invalid")
        response_state = msg.get("response_state")
        if (response_state is not None and response_state not in {"acknowledged", "final"}
                or response_state is not None and kind == "question"
                or response_state == "acknowledged" and kind == "result"):
            raise ValueError("assistant_publish_response_state_invalid")
        route = await self.store.find_assistant_composite_route_by_dispatch(dispatch_id)
        if route is None or not await self._is_current_dispatch_actor(
            route, str(actor_stream_id or ""), allow_authority=kind != "question",
        ):
            raise ValueError("assistant_publish_provenance_unverified")
        if route["routing_state"] != "resolved":
            raise ValueError("assistant_publish_dispatch_state_invalid")
        actor_generation = await self._authenticated_generation(msg, actor_stream_id)
        reply_to_message_id = _optional_id(msg.get("reply_to_message_id"))
        reply_to_question_id = _optional_id(msg.get("reply_to_question_id"))
        if reply_to_message_id != _optional_id(route.get("input_identity")):
            raise ValueError("assistant_publish_reply_unverified")
        if kind != "question" and reply_to_question_id != _optional_id(route.get("reply_to_question_id")):
            raise ValueError("assistant_publish_question_unverified")
        attachment_ids = _id_list(msg.get("attachment_ids"), "assistant_publish_attachment_ids_invalid")
        evidence_refs = _id_list(msg.get("evidence_refs"), "assistant_publish_evidence_refs_invalid")
        attachments = []
        if attachment_ids:
            if self.publication_attachments is None:
                raise ValueError("assistant_publish_attachment_validation_unavailable")
            attachments = await self.publication_attachments(attachment_ids, route)
        canonical_payload = {
            "composite_stream_id": self.config.stream_id,
            "dispatch_id": dispatch_id,
            "reply_to_message_id": reply_to_message_id,
            "reply_to_question_id": reply_to_question_id,
            "publish_kind": kind,
            "message": body,
            "attachment_ids": attachment_ids,
            "evidence_refs": evidence_refs,
        }
        if response_state is not None:
            canonical_payload["response_state"] = response_state
        event = {
            "stream_id": self.config.stream_id,
            "provider": "composite",
            "kind": "ASSIST_TEXT",
            "text": body,
            "message_id": "publication:" + publication_key,
            "reply_to_message_id": reply_to_message_id,
            "reply_to_question_id": reply_to_question_id,
            "publish_kind": kind,
            "attachments": attachments,
            "timestamp": _now_iso(),
            "raw": {
                "assistant_composite": True,
                "publish_kind": kind,
                "dispatch_id": dispatch_id,
                "reply_to_message_id": reply_to_message_id,
                "reply_to_question_id": reply_to_question_id,
                "actor_stream_id": actor_stream_id,
                "attachment_ids": attachment_ids,
                "evidence_refs": evidence_refs,
            },
        }
        if response_state is not None:
            event["raw"]["response_state"] = response_state
        stored = await self.store.record_assistant_composite_publication(
            stream_id=self.config.stream_id, publication_key=publication_key,
            dispatch_id=dispatch_id, reply_to_message_id=reply_to_message_id,
            reply_to_question_id=reply_to_question_id, publish_kind=kind,
            attachment_ids=attachment_ids, evidence_refs=evidence_refs,
            canonical_payload=canonical_payload, event=event,
            actor_stream_id=actor_stream_id, actor_generation=actor_generation,
            authority_stream_id=self.config.astra_stream_id if kind != "question" else None,
        )
        await self.refresh_activity()
        if not stored.get("duplicate") and self.broadcast is not None:
            await self.broadcast({"type": "chat.event", "event": stored["event"]})
        return {
            "type": "assistant.publish.ok", "publication_key": publication_key,
            "event_id": stored["event_id"], "duplicate": bool(stored.get("duplicate")),
        }

    async def operation(self, msg: dict[str, Any], *, actor_stream_id: str | None) -> dict[str, Any]:
        if not self.enabled:
            raise ValueError("assistant_composite_disabled")
        actor_generation = await self._authenticated_generation(msg, actor_stream_id)
        operation_fields = {
            "type", "request_id", "composite_stream_id", "dispatch_id", "operation", "lane_id",
            "expected_lane_version", "payload", "evidence_refs", "reply_to_message_id",
        }
        if {str(key) for key in msg if not str(key).startswith("_")} - operation_fields:
            raise ValueError("assistant_operation_payload_invalid")
        operation = str(msg.get("operation") or "")
        operation_id = _optional_id(msg.get("request_id"))
        lane_id = _optional_id(msg.get("lane_id"))
        payload = msg.get("payload")
        dispatch_id = _optional_id(msg.get("dispatch_id"))
        reply_to_message_id = _optional_id(msg.get("reply_to_message_id"))
        if (
            _optional_id(msg.get("composite_stream_id")) != self.config.stream_id
            or not operation_id or not dispatch_id or not isinstance(payload, dict)
        ):
            raise ValueError("assistant_operation_required_fields")
        evidence_refs = _id_list(msg.get("evidence_refs"), "assistant_operation_evidence_refs_invalid")
        lane_id, expected_version = _validate_operation_payload(
            operation, lane_id, payload, msg.get("expected_lane_version"), operation_id,
        )
        if operation != "route.resolve" and reply_to_message_id is not None:
            raise ValueError("assistant_operation_reply_unexpected")
        # The route's active dispatch is replaced on a successful fallback
        # resolution. A same immutable operation retry must still reach the
        # store's full payload-digest comparison instead of being rejected only
        # because the route is no longer in fallback_dispatched.
        prior_operation = await self.store.get_assistant_composite_operation(operation_id)
        if operation == "route.resolve" and prior_operation is not None:
            if (
                str(actor_stream_id or "") != self.config.luna_stream_id
                or str(prior_operation.get("stream_id") or "") != self.config.stream_id
                or str(prior_operation.get("dispatch_id") or "") != dispatch_id
                or str(prior_operation.get("operation") or "") != operation
            ):
                raise ValueError("assistant_route_resolve_provenance_unverified")
            await self._target_generation(str(actor_stream_id or ""))
            replay = await self.store.apply_assistant_composite_operation(
                actor_generation=actor_generation, authority_stream_id=self.config.astra_stream_id,
                stream_id=self.config.stream_id, operation_id=operation_id, operation=operation,
                lane_id=None, actor_stream_id=actor_stream_id, payload=payload,
                dispatch_id=dispatch_id, reply_to_message_id=reply_to_message_id,
                expected_lane_version=None, evidence_refs=evidence_refs,
            )
            return {"type": "assistant.operation.ok", **replay}
        route: dict[str, Any] | None = None
        router_result: dict[str, Any] | None = None
        resolved_target = resolved_generation = resolved_dispatch_id = None
        if operation == "route.resolve":
            fallback_dispatch_id = dispatch_id
            original_message_id = reply_to_message_id
            if not fallback_dispatch_id or not original_message_id:
                raise ValueError("assistant_route_resolve_required_fields")
            route = await self.store.find_assistant_composite_route_by_dispatch(fallback_dispatch_id)
            if (
                route is None
                or str(route.get("input_identity") or "") != original_message_id
                or str(route.get("routing_state") or "") != "fallback_dispatched"
                or not await self._is_current_dispatch_actor(route, str(actor_stream_id or ""))
                or str(actor_stream_id or "") != self.config.luna_stream_id
            ):
                raise ValueError("assistant_route_resolve_provenance_unverified")
            router_result = await self._normalize_decision(payload, route, origin="luna_fallback")
            if router_result["disposition"] != "defer":
                resolved_target = await self._target_for_decision(router_result)
                resolved_generation = await self._target_generation(resolved_target)
                resolved_dispatch_id = "assistant-resolve-" + hashlib.sha256(
                    (self.config.stream_id + "\x00" + operation_id).encode("utf-8")
                ).hexdigest()[:32]
        else:
            route = await self.store.find_assistant_composite_route_by_dispatch(dispatch_id)
            if route is None or not await self._is_current_dispatch_actor(
                route, str(actor_stream_id or ""),
                allow_authority=operation in {"lane.admit", "lane.bind", "lane.decision", "lane.close"},
            ):
                raise ValueError("assistant_operation_dispatch_unverified")
        if operation == "lane.bind":
            backend_stream_id = str(payload.get("backend_stream_id") or "").strip()
            backend_generation = str(payload.get("backend_generation") or "").strip()
            backend_kind = str(payload.get("backend_kind") or "").strip()
            if backend_kind not in {"lead", "assistant_conversation", "assistant_authority"}:
                raise ValueError("assistant_lane_bind_invalid")
            current_generation = await self._target_generation(backend_stream_id)
            if not backend_generation or current_generation != backend_generation:
                raise ValueError("assistant_lane_bind_target_generation_invalid")
        await self._authorize_operation(operation, lane_id, payload, actor_stream_id)
        if operation in {"question.open", "question.cancel"}:
            if self.question_operation is None:
                raise ValueError("assistant_question_adapter_unavailable")
            if lane_id is None or expected_version is None:
                raise ValueError("assistant_operation_expected_version_required")
            # This is before the external durable-question side effect.  The
            # question adapter receives operation_id as its existing durable
            # request key, so a post-effect retry cannot create a second card.
            preflight = await self.store.preflight_assistant_composite_question_operation(
                actor_generation=actor_generation, authority_stream_id=self.config.astra_stream_id,
                stream_id=self.config.stream_id, lane_id=lane_id,
                expected_lane_version=expected_version, operation_id=operation_id,
                operation=operation, dispatch_id=dispatch_id, payload=payload,
                evidence_refs=evidence_refs, actor_stream_id=actor_stream_id,
            )
            if preflight.get("committed"):
                return {
                    "type": "assistant.operation.ok",
                    "operation_id": operation_id,
                    "operation": operation,
                    "lane_id": lane_id,
                    "duplicate": True,
                    "lane": preflight["lane"],
                }
            # Forward to the existing durable prompt/question store.  The
            # actor's verified auth context is retained by Server; no composite
            # question table or stale auto-consent path exists here.
            question = await self.question_operation(operation, msg)
            if not str(question.get("type") or "").endswith(".ok"):
                await self.store.fail_assistant_composite_question_bridge(
                    stream_id=self.config.stream_id, lane_id=lane_id, operation_id=operation_id,
                )
                raise ValueError("assistant_question_operation_failed")
            question_record = question.get("question") if isinstance(question.get("question"), dict) else {}
            question_id = _optional_id(question_record.get("question_id") or payload.get("question_id"))
            if operation == "question.open" and not question_id:
                # The notifier reported success but omitted its durable ID. Do
                # not guess or clear the prepared bridge: recovery must retain
                # this ambiguous cross-store boundary for inspection/retry.
                raise ValueError("assistant_question_adapter_invalid")
            audit = await self.store.apply_assistant_composite_operation(
                actor_generation=actor_generation, authority_stream_id=self.config.astra_stream_id,
                stream_id=self.config.stream_id, operation_id=operation_id, operation=operation,
                lane_id=lane_id, actor_stream_id=actor_stream_id, payload=payload,
                dispatch_id=dispatch_id, reply_to_message_id=reply_to_message_id,
                expected_lane_version=expected_version, evidence_refs=evidence_refs,
                question_id=question_id,
            )
            await self.refresh_activity()
            return {"type": "assistant.operation.ok", **audit, "question": question.get("question")}
        authority_generation = None
        if operation == "authority.request":
            if evidence_refs:
                raise ValueError("assistant_authority_request_invalid")
            if prior_operation is None:
                authority_generation = await self._target_generation(self.config.astra_stream_id)
        result = await self.store.apply_assistant_composite_operation(
            conversation_stream_id=self.config.luna_stream_id,
            authority_generation=authority_generation,
                actor_generation=actor_generation, authority_stream_id=self.config.astra_stream_id,
            stream_id=self.config.stream_id, operation_id=operation_id, operation=operation,
            lane_id=lane_id, actor_stream_id=actor_stream_id, payload=payload,
            dispatch_id=dispatch_id, reply_to_message_id=reply_to_message_id,
            expected_lane_version=expected_version, evidence_refs=evidence_refs,
            route_id=str((route or {}).get("route_id") or "") or None,
            route_dispatch_id=resolved_dispatch_id,
            route_target=resolved_target,
            route_target_generation=resolved_generation,
            route_defer_dependency=(
                str((router_result or {}).get("depends_on_message_id") or "") or None
            ),
            authority_wake_recipient=(
                self.config.astra_stream_id
                if operation == "lane.decision" and str(actor_stream_id or "") != self.config.astra_stream_id
                else None
            ),
            authority_wake_tell_id=(
                f"assistant-decision:{operation_id}"
                if operation == "lane.decision" and str(actor_stream_id or "") != self.config.astra_stream_id
                else None
            ),
        )
        if operation == "route.resolve":
            # The transaction changed deferred -> resolved/intent before this
            # detached action exists.  If we crash here, startup recovery marks
            # that intent uncertain instead of blind-reinjecting it.
            if not result.get("duplicate") and resolved_dispatch_id:
                route = await self.store.find_assistant_composite_route_by_dispatch(
                    resolved_dispatch_id,
                )
                if route is None:  # pragma: no cover - transaction invariant
                    raise RuntimeError("assistant_route_resolve_receipt_missing")
                self._start_dispatch(route)
            self._wake_worker()
        await self.refresh_activity()
        return {"type": "assistant.operation.ok", **result}

    async def _is_current_dispatch_actor(
        self, route: dict[str, Any], actor: str, *, allow_authority: bool = False,
    ) -> bool:
        """Bind a response to the original dispatch and current seat lineage."""
        target = str(route.get("route_target") or "")
        expected_generation = str(route.get("route_target_generation") or "")
        if not actor or not target or not expected_generation:
            return False
        host, separator, session_name = actor.partition(":")
        if not separator or not host or not session_name:
            return False
        current = await self.store.fetch_session(host, session_name)
        if current is None or str(current.get("status") or "") != "open":
            return False
        try:
            await self.store.authorize_assistant_composite_dispatch(
                stream_id=self.config.stream_id, dispatch_id=str(route.get("dispatch_id") or ""),
                actor=actor, generation=str(current.get("session_generation") or ""),
                authority_stream_id=self.config.astra_stream_id if allow_authority else None)
            return True
        except ValueError:
            return False

    async def _authorize_operation(
        self, operation: str, lane_id: str | None, payload: dict[str, Any], actor: str | None,
    ) -> None:
        actor = str(actor or "")
        if not actor:
            raise ValueError("assistant_operation_actor_unverified")
        await self._target_generation(actor)
        astra = self.config.astra_stream_id
        if operation == "authority.request" and actor != self.config.luna_stream_id:
            raise ValueError("assistant_operation_luna_required")
        if operation == "lane.admit" and actor != astra:
            raise ValueError("assistant_operation_astra_required")
        if operation in {"lane.bind", "lane.close"} and actor != astra:
            raise ValueError("assistant_operation_astra_required")
        if operation == "lane.decision":
            transition = str(payload.get("transition") or "")
            if transition in {"reopen", "cancel"} and actor != astra:
                raise ValueError("assistant_operation_astra_required")
            if transition in {"start", "wait"} and actor != astra:
                lane = await self.store.get_assistant_composite_lane(
                    stream_id=self.config.stream_id, lane_id=str(lane_id or ""),
                )
                if (
                    lane is None
                    or str(lane.get("bound_stream_id") or "") != actor
                    or str(lane.get("bound_generation") or "") != await self._target_generation(actor)
                ):
                    raise ValueError("assistant_operation_bound_lead_required")
        if operation in {"question.open", "question.cancel"}:
            lane = await self.store.get_assistant_composite_lane(
                stream_id=self.config.stream_id, lane_id=str(lane_id or ""),
            )
            if (
                lane is None
                or str(lane.get("bound_stream_id") or "") != actor
                or str(lane.get("bound_generation") or "") != await self._target_generation(actor)
            ):
                raise ValueError("assistant_operation_bound_lead_required")
        if operation not in {
            "lane.admit", "lane.bind", "lane.decision", "lane.close",
            "question.open", "question.cancel", "route.resolve", "authority.request",
        }:
            raise ValueError("assistant_operation_invalid")

    async def terminal_report(self, report: dict[str, Any], *, actor_generation: str) -> dict[str, Any] | None:
        """Connect existing terminal reports to the narrow lane completion rule."""
        if not self.enabled or str(report.get("status") or "") not in {"done", "error", "aborted"}:
            return None
        source = str(report.get("actor_stream_id") or report.get("from_stream_id") or "")
        report_id = str(report.get("report_id") or "")
        lane_id = _optional_id(report.get("lane_id"))
        dispatch_id = _optional_id(report.get("dispatch_id"))
        if source and report_id and lane_id and dispatch_id:
            host, separator, session_name = source.partition(":")
            if not separator:
                return None
            generation = str(actor_generation or "")
            if not generation:
                raise ValueError("assistant_actor_generation_unverified")
            route = await self.store.find_assistant_composite_route_by_dispatch(dispatch_id)
            if route is None or not await self._is_current_dispatch_actor(route, source):
                return None
            completed = await self.store.complete_assistant_composite_lane(
                stream_id=self.config.stream_id, lane_id=lane_id, dispatch_id=dispatch_id,
                actor_stream_id=source, actor_generation=generation, report_id=report_id,
                authority_wake_recipient=self.config.astra_stream_id or None,
            )
            await self.refresh_activity()
            return completed
        return None


def _bounded_text(value: object, limit: int) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _route_elapsed_ms(route: dict[str, Any], started: float) -> int:
    """Measure from durable input admission, with a monotonic fallback."""
    accepted = route.get("created_at")
    if accepted:
        try:
            elapsed = int((datetime.now(timezone.utc) - datetime.fromisoformat(
                str(accepted).replace("Z", "+00:00"),
            )).total_seconds() * 1000)
            if elapsed >= 0:
                return elapsed
        except (TypeError, ValueError):
            pass
    return max(0, int((time.monotonic() - started) * 1000))


def _router_failure_record(exc: Exception, elapsed_ms: int) -> dict[str, Any]:
    record = {
        "exception_type": type(exc).__name__,
        "message": _bounded_text(str(exc), _ROUTER_FAILURE_MESSAGE_MAX),
        "traceback": _bounded_text(traceback.format_exc(), _ROUTER_FAILURE_TRACEBACK_MAX),
        "elapsed_ms": max(0, int(elapsed_ms)),
    }
    if isinstance(exc, AssistantRouterProcessError):
        record.update(returncode=exc.returncode, stdout=exc.stdout, stderr=exc.stderr)
    return record


def _optional_id(value: object) -> str | None:
    text = str(value or "").strip()
    return text if text else None


def _id_list(value: object, error_code: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(error_code)
    values = [item.strip() for item in value]
    if len(values) > 16 or len(set(values)) != len(values):
        raise ValueError(error_code)
    return values


def _validate_operation_payload(
    operation: str, lane_id: str | None, payload: dict[str, Any], expected: object, operation_id: str,
) -> tuple[str | None, int | None]:
    """Frozen operation envelope/payload schemas; no alias-driven workflow."""
    schemas: dict[str, tuple[set[str], set[str]]] = {
        "authority.request": ({"reason"}, set()),
        "lane.admit": ({"mode", "request_message_id"}, {"subject", "target_lane_id", "parent_lane_id", "split_group_id"}),
        "lane.bind": ({"backend_kind", "backend_stream_id", "backend_generation"}, set()),
        "lane.decision": ({"decision_id", "transition", "from_phase", "to_phase", "operator_basis_message_ids"}, {"reason"}),
        "lane.close": ({"completion_message_id", "completion_disposition"}, set()),
        # ``actions`` is the existing durable-question store's fixed companion
        # to its prompt envelope; it is not an alternate question protocol.
        "question.open": ({"envelope"}, {"actions"}),
        "question.cancel": ({"question_id"}, set()),
        "route.resolve": ({"schema_version", "disposition", "lane_id", "depends_on_message_id", "reason"}, set()),
    }
    required, optional = schemas.get(operation, (set(), set()))
    if not required or set(payload) - required - optional or not required <= set(payload):
        raise ValueError("assistant_operation_payload_invalid")
    if operation == "lane.decision" and "reason" in payload:
        reason = payload["reason"]
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1024:
            raise ValueError("assistant_decision_reason_invalid")
    if operation == "authority.request":
        reason = payload.get("reason")
        if lane_id is not None or not isinstance(reason, str) or not reason.strip() or len(reason) > 1024:
            raise ValueError("assistant_authority_request_invalid")
    if operation == "lane.admit":
        mode = str(payload.get("mode") or "")
        if mode == "new":
            if lane_id is not None or not str(payload.get("subject") or "").strip():
                raise ValueError("assistant_lane_admit_invalid")
            lane_id = "assistant-lane-" + hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:24]
        elif mode == "fold":
            lane_id = _optional_id(payload.get("target_lane_id"))
            if not lane_id:
                raise ValueError("assistant_lane_admit_invalid")
        else:
            raise ValueError("assistant_lane_admit_invalid")
    needs_version = operation not in {"lane.admit", "route.resolve", "authority.request"} or str(payload.get("mode") or "") == "fold"
    if needs_version:
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1 or not lane_id:
            raise ValueError("assistant_operation_expected_version_required")
        return lane_id, expected
    if expected is not None:
        raise ValueError("assistant_operation_expected_version_unexpected")
    return lane_id, None


def _distinctive_summary_tokens(text: str) -> set[str]:
    return {
        token.casefold()
        for token in _ROUTING_TOKEN_RE.findall(text)
        if len(token) >= 4 and token.casefold() not in _NON_DISTINCTIVE_SUMMARY_TOKENS
    }


def _backend_routing_context(router_input: dict[str, Any] | None) -> dict[str, Any]:
    """Persist bounded durable state for normal and fallback backend prompts."""
    if not isinstance(router_input, dict):
        return {"open_lanes": [], "unresolved_inputs": [], "recent_messages": []}
    context: dict[str, Any] = {}
    for key in ("open_lanes", "unresolved_inputs", "recent_messages"):
        value = router_input.get(key)
        context[key] = [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    context["message_id"] = _optional_id(router_input.get("message_id"))
    context["original_length"] = int(router_input.get("original_length") or 0)
    canonical = json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    context["context_digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return context


def _fallback_routing_context(router_input: dict[str, Any] | None) -> dict[str, Any]:
    """Fallback reuses the exact persisted bounded context, never re-samples."""
    return _backend_routing_context(router_input)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
