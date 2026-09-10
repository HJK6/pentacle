from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
import random
import threading
import time
from typing import Any, Callable
import uuid
from pathlib import Path

import websockets

from . import agent_orch_version
from .config import Config
from .schema import validate_inbox


logger = logging.getLogger(__name__)

AGENT_ORCH_CAPABILITY_FLAGS = ["spec_id", "stream_token", "status"]


def _agent_orch_capabilities_payload() -> dict[str, Any]:
    return {"version": agent_orch_version(), "flags": list(AGENT_ORCH_CAPABILITY_FLAGS)}


def stream_token_from_env() -> str | None:
    """Read the seat token from its private file, falling back to legacy env.

    Spawned v2 seats carry only ``AGENT_ORCH_STREAM_TOKEN_FILE`` in their
    environment. Refuse a file that is not a user-owned, mode-0600 regular
    file so the replacement cannot silently become another readable surface.
    """
    token_file = os.environ.get("AGENT_ORCH_STREAM_TOKEN_FILE")
    if token_file:
        try:
            path = Path(token_file).expanduser()
            stat_result = path.stat()
            if (
                not path.is_file()
                or stat_result.st_uid != os.getuid()
                or stat_result.st_mode & 0o077
            ):
                return None
            value = path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            return None
        return value or None
    token = os.environ.get("AGENT_ORCH_STREAM_TOKEN")
    return token if token else None


_stream_token_from_env = stream_token_from_env


WEBSOCKET_MAX_SIZE = 128 * 1024 * 1024
WEBSOCKET_KEEPALIVE_MAX_S = 600.0
WEBSOCKET_CLOSE_TIMEOUT_MAX_S = 60.0
WEBSOCKET_SNAPSHOT_TIMEOUT_DEFAULT_S = 10.0
WEBSOCKET_SNAPSHOT_TIMEOUT_MIN_S = 0.25
WEBSOCKET_SNAPSHOT_TIMEOUT_MAX_S = 300.0
RPC_RETRY_BACKOFF_MAX_S = 60.0
RPC_RETRY_DEADLINE_MAX_S = 3600.0
# Covers the daemon's 180s Codex boot/proof absolute deadline plus connection
# and durable-readback overhead. Explicit caller timeouts remain authoritative.
SPAWN_RPC_TIMEOUT_DEFAULT_S = 185.0


# Stage B retry-safe verbs. Keep this list narrow and explicit: every entry is
# either read-only, final-state idempotent, or daemon-deduped by a stable key.
RPC_RETRY_ELIGIBLE_TYPES = frozenset(
    {
        "wake.register", "wake.list", "wake.cancel",
        "watch.register", "watch.list", "watch.cancel",
        "await_report",
        "await_spawn",
        "fetch_blob",
        "inspect_stream",
        "thread.read",
        "ledger_get",
        "inbound_audit",
        "prompt.ask",
        "prompt.answer",
        "prompt.cancel",
        "prompt.list",
        "prompt.status",
        "park",
        "reconcile.status",
        "report",
        "schedule.get",
        "schedule.list",
        "send.cancel",
        "set_visibility",
        "tell",
    }
)


@dataclass(frozen=True)
class WebsocketKeepalive:
    ping_interval: float
    ping_timeout: float
    close_timeout: float


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    backoff_base_s: float
    backoff_cap_s: float
    jitter_fraction: float
    deadline_s: float


def _float_from_env(name: str, default: float, *, max_value: float, min_value: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value <= 0:
        return default
    if min_value is not None:
        value = max(value, min_value)
    return min(value, max_value)


def _snapshot_timeout_from_env(default: float = WEBSOCKET_SNAPSHOT_TIMEOUT_DEFAULT_S) -> float:
    return _float_from_env(
        "AGENT_ORCH_WS_SNAPSHOT_TIMEOUT_S",
        default,
        min_value=WEBSOCKET_SNAPSHOT_TIMEOUT_MIN_S,
        max_value=WEBSOCKET_SNAPSHOT_TIMEOUT_MAX_S,
    )


def _clamp_snapshot_timeout(value: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return _snapshot_timeout_from_env()
    if timeout <= 0:
        return _snapshot_timeout_from_env()
    return min(max(timeout, WEBSOCKET_SNAPSHOT_TIMEOUT_MIN_S), WEBSOCKET_SNAPSHOT_TIMEOUT_MAX_S)


def _nonnegative_float_from_env(name: str, default: float, *, max_value: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value < 0:
        return default
    return min(value, max_value)


def _optional_positive_float_from_env(name: str, *, max_value: float) -> float | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if value <= 0:
        return None
    return min(value, max_value)


def _int_from_env(name: str, default: int, *, max_value: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value <= 0:
        return default
    return min(value, max_value)


def _websocket_keepalive_from_env() -> WebsocketKeepalive:
    ping_interval = _float_from_env(
        "AGENT_ORCH_WS_PING_INTERVAL_S",
        30.0,
        max_value=WEBSOCKET_KEEPALIVE_MAX_S,
    )
    ping_timeout = _float_from_env(
        "AGENT_ORCH_WS_PING_TIMEOUT_S",
        60.0,
        max_value=WEBSOCKET_KEEPALIVE_MAX_S,
    )
    # Invariant: the pong-wait window must be at least the ping cadence. A
    # pathological env (timeout < interval) would otherwise make keepalive
    # *less* tolerant than intended and could trip 1011 on a healthy-but-slow
    # link — the opposite of this hardening's goal. Defaults (30/60) make this
    # a no-op.
    ping_timeout = max(ping_timeout, ping_interval)
    return WebsocketKeepalive(
        ping_interval=ping_interval,
        ping_timeout=ping_timeout,
        close_timeout=_float_from_env(
            "AGENT_ORCH_WS_CLOSE_TIMEOUT_S",
            5.0,
            max_value=WEBSOCKET_CLOSE_TIMEOUT_MAX_S,
        ),
    )


def _rpc_retry_policy_from_env(call_timeout: float) -> RetryPolicy:
    configured_deadline = _optional_positive_float_from_env(
        "AGENT_ORCH_RPC_RETRY_DEADLINE_S",
        max_value=RPC_RETRY_DEADLINE_MAX_S,
    )
    max_attempts = _int_from_env("AGENT_ORCH_RPC_RETRY_MAX_ATTEMPTS", 3, max_value=100)
    call_deadline = call_timeout if call_timeout > 0 else 30.0
    # Decouple the TOTAL retry deadline from a single call timeout. The retry
    # loop splits the remaining deadline across the attempts still left
    # (`_retry_wait_timeout` = remaining / attempts_left), so a total deadline
    # equal to ONE call_timeout starves each of `max_attempts` attempts to
    # ~call_timeout/attempts (≈10s for the 30s/3-attempt default) — a
    # healthy-but-slow daemon whose reply lands just under call_timeout then
    # trips rpc_timeout and a reconnect storm. Default the total to
    # call_deadline * max_attempts so every attempt gets its full per-attempt
    # budget without operators having to set AGENT_ORCH_RPC_RETRY_DEADLINE_S.
    # When the env IS set it is an explicit TOTAL budget: honor it as given
    # (previously min()'d against one call_timeout, which capped it below the
    # single-call value and made the knob nearly useless).
    if configured_deadline is None:
        deadline_s = min(RPC_RETRY_DEADLINE_MAX_S, call_deadline * max_attempts)
    else:
        deadline_s = configured_deadline
    return RetryPolicy(
        max_attempts=max_attempts,
        backoff_base_s=_float_from_env(
            "AGENT_ORCH_RPC_RETRY_BACKOFF_BASE_S",
            0.25,
            max_value=RPC_RETRY_BACKOFF_MAX_S,
        ),
        backoff_cap_s=_float_from_env(
            "AGENT_ORCH_RPC_RETRY_BACKOFF_CAP_S",
            2.0,
            max_value=RPC_RETRY_BACKOFF_MAX_S,
        ),
        jitter_fraction=_nonnegative_float_from_env(
            "AGENT_ORCH_RPC_RETRY_JITTER_FRACTION",
            0.30,
            max_value=10.0,
        ),
        deadline_s=deadline_s,
    )


def _rpc_verb(payload: dict[str, Any]) -> str:
    return str(payload.get("type") or "")


def _is_rpc_retry_eligible(payload: dict[str, Any]) -> bool:
    verb = _rpc_verb(payload)
    if verb == "send":
        return payload.get("msg_id") is not None
    if verb == "spawn":
        idempotency_key = payload.get("idempotency_key")
        return isinstance(idempotency_key, str) and idempotency_key != ""
    return verb in RPC_RETRY_ELIGIBLE_TYPES


def _retry_backoff_s(policy: RetryPolicy, attempt: int) -> float:
    base = min(policy.backoff_cap_s, policy.backoff_base_s * (2 ** max(0, attempt - 1)))
    if policy.jitter_fraction <= 0:
        return base
    return base * (1.0 + random.random() * policy.jitter_fraction)


def _retry_deadline(now: float, policy: RetryPolicy) -> float:
    return now + max(0.0, policy.deadline_s)


def _retry_wait_timeout(deadline: float, attempt: int, max_attempts: int) -> float:
    remaining = max(0.0, deadline - time.monotonic())
    attempts_left = max(1, max_attempts - attempt + 1)
    return max(0.01, remaining / attempts_left)


def _retry_next_delay(policy: RetryPolicy, deadline: float, attempt: int) -> tuple[float | None, str]:
    if attempt >= policy.max_attempts:
        return None, "retry_exhausted"
    delay = _retry_backoff_s(policy, attempt)
    remaining = deadline - time.monotonic()
    if remaining <= 0 or delay >= remaining:
        return None, "retry_deadline_exceeded"
    return delay, ""


def _log_rpc_retry(verb: str, attempt: int, max_attempts: int, reason: str, backoff_s: float) -> None:
    logger.warning(
        "agent-orch rpc retry verb=%s attempt=%s/%s reason=%s backoff=%.3fs",
        verb,
        attempt,
        max_attempts,
        reason,
        backoff_s,
    )


def _log_rpc_giveup(verb: str, attempts: int, reason: str) -> None:
    logger.warning("agent-orch rpc retry give-up verb=%s attempts=%s reason=%s", verb, attempts, reason)


def _rpc_failure_type(prefix: str) -> str:
    return f"{prefix}.error" if prefix == "spawn" else f"{prefix}.indeterminate"


def _retry_failure_response(
    prefix: str,
    request_id: str,
    *,
    reason: str,
    attempts: int,
    sent: bool = False,
    progress: list[dict[str, Any]] | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    if prefix == "send":
        response: dict[str, Any] = {
            "type": "send.transport",
            "request_id": request_id,
            "outcome": "transmit_delivered_awaiting_result" if sent else "transmit_failed",
            "reason": reason,
            "attempts": attempts,
        }
        if sent:
            response["progress"] = list(progress or [])
        if message:
            response["message"] = message
        return response
    response = {
        "type": _rpc_failure_type(prefix),
        "request_id": request_id,
        "reason": reason,
        "attempts": attempts,
    }
    if message:
        response["message"] = message
    return response


class SnapshotTimeout(TimeoutError):
    pass


@dataclass
class PendingRpc:
    prefix: str
    event: threading.Event
    payload: dict[str, Any]
    retry_eligible: bool = False
    retry_policy: RetryPolicy | None = None
    retry_deadline: float = 0.0
    verb: str = ""
    response: dict[str, Any] | None = None
    progress: list[dict[str, Any]] | None = None
    sent: bool = False
    attempts: int = 0
    in_flight: bool = False
    reconnect_waiting: bool = False
    resume_in_flight: bool = False
    resume_generation: int = 0
    send_owner: str | None = None


class WebsocketClient:
    def __init__(self, config: Config, snapshot_timeout: float | None = None, rpc_timeout: float = 30.0):
        self.config = config
        self.snapshot_timeout = (
            _snapshot_timeout_from_env() if snapshot_timeout is None else _clamp_snapshot_timeout(snapshot_timeout)
        )
        self.rpc_timeout = rpc_timeout
        self.snapshot: dict[str, Any] | None = None
        self.sessions: dict[str, dict[str, Any]] = {}
        self.schedules: dict[str, dict[str, Any]] = {}
        self.connection_state = "pending"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ws: Any = None
        self._stop = threading.Event()
        self._snapshot_ready = threading.Event()
        self._startup_error: Exception | None = None
        self._pending: dict[str, PendingRpc] = {}
        self._pending_lock = threading.Lock()
        self._callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._snapshot_callbacks: list[Callable[[], None]] = []

    def on_event(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._callbacks.append(callback)

    def on_snapshot(self, callback: Callable[[], None]) -> None:
        self._snapshot_callbacks.append(callback)

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run_thread, name="agent-orch-ws", daemon=True)
        self._thread.start()
        if not self._snapshot_ready.wait(self.snapshot_timeout):
            raise SnapshotTimeout("snapshot_timeout")
        if self._startup_error:
            raise self._startup_error

    def stop(self) -> None:
        self._stop.set()
        if self._loop and self._ws:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
        if self._thread:
            self._thread.join(timeout=5)

    def _run_thread(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_forever())
        finally:
            self._loop.close()

    async def _run_forever(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._connect_and_read()
                backoff = 1.0
                if not self._stop.is_set():
                    self.connection_state = "reconnecting"
                    self._mark_pending_indeterminate()
                    continue
            except Exception as exc:
                if not self._snapshot_ready.is_set():
                    self._startup_error = exc
                    self._snapshot_ready.set()
                    return
                self.connection_state = "reconnecting"
                self._mark_pending_indeterminate()
                delay = min(30.0, backoff) * (1 + random.random() * 0.3)
                backoff = min(30.0, backoff * 2)
                await asyncio.sleep(delay)
        self.connection_state = "failed"

    async def _connect_and_read(self) -> None:
        self.connection_state = "pending"
        keepalive = _websocket_keepalive_from_env()
        async with websockets.connect(
            self.config.ws_url,
            max_size=WEBSOCKET_MAX_SIZE,
            ping_interval=keepalive.ping_interval,
            ping_timeout=keepalive.ping_timeout,
            close_timeout=keepalive.close_timeout,
        ) as ws:
            self._ws = ws
            await ws.send(json.dumps(self._hello(), separators=(",", ":")))
            async for raw in ws:
                message = json.loads(raw)
                await self._handle_message(message)
                if message.get("type") == "snapshot":
                    await self._resume_pending_after_reconnect()
                if self._stop.is_set():
                    break

    def _hello(self) -> dict[str, Any]:
        hello: dict[str, Any] = {
            "type": "hello",
            "client": "agent-orch",
            "subscribe": {"all": True, "include_subagents": True},
            "host": self.config.host_id,
            "agent_orch_capabilities": _agent_orch_capabilities_payload(),
        }
        leader_stream_id = _resolved_rpc_from_stream_id()
        stream_token = _stream_token_from_env()
        if leader_stream_id and stream_token:
            hello["from_stream_id"] = leader_stream_id
            hello["stream_token"] = stream_token
        if self.config.token:
            hello["token"] = self.config.token
        return hello

    async def _handle_message(self, message: dict[str, Any]) -> None:
        message_type = message.get("type")
        if message_type in {"auth.error", "hello.error"}:
            raise PermissionError(str(message.get("error_code") or message.get("error") or message_type))
        if message_type == "ping":
            if self._ws:
                await self._ws.send(json.dumps({"type": "pong"}, separators=(",", ":")))
            return
        if message_type == "snapshot":
            hosts = message.get("hosts", [])
            if hosts and self.config.host_id not in _host_ids(hosts):
                raise RuntimeError("unknown_local_host")
            self.snapshot = message
            self._replace_sessions(message.get("sessions", []))
            self._replace_schedules(message.get("schedules", []))
            self.connection_state = "connected"
            self._snapshot_ready.set()
            for callback in self._snapshot_callbacks:
                threading.Thread(target=callback, name="agent-orch-reconcile", daemon=True).start()
            return
        if message_type == "session.inventory":
            self._replace_sessions(message.get("sessions", []))
            return
        if message_type == "schedule.inventory":
            self._replace_schedules(message.get("schedules", []))
            return
        if isinstance(message_type, str) and message_type.startswith("schedule_"):
            self._apply_schedule_event(message)
            for callback in self._callbacks:
                callback(message)
            return
        if message_type in {
            "chat.event",
            "session.died",
            "completion.report",
            "recovery.resolution",
            "peer.message.delivered",
            "peer.message.undeliverable",
        }:
            for callback in self._callbacks:
                callback(message)
            return
        request_id = message.get("request_id")
        if isinstance(request_id, str):
            self._resolve_pending(request_id, message)

    def _replace_sessions(self, sessions: Any) -> None:
        if not isinstance(sessions, list):
            return
        next_sessions: dict[str, dict[str, Any]] = {}
        for session in sessions:
            if isinstance(session, dict) and isinstance(session.get("stream_id"), str):
                next_sessions[session["stream_id"]] = session
        self.sessions = next_sessions

    def _replace_schedules(self, schedules: Any) -> None:
        if not isinstance(schedules, list):
            return
        next_schedules: dict[str, dict[str, Any]] = {}
        for schedule in schedules:
            if isinstance(schedule, dict) and isinstance(schedule.get("schedule_id"), str):
                next_schedules[schedule["schedule_id"]] = schedule
        self.schedules = next_schedules

    def _apply_schedule_event(self, message: dict[str, Any]) -> None:
        schedule_id = message.get("schedule_id")
        if not isinstance(schedule_id, str):
            return
        state = str(message.get("state") or "")
        if state in {"cancelled", "fired", "expired", "failed"}:
            self.schedules.pop(schedule_id, None)
            return
        current = dict(self.schedules.get(schedule_id) or {})
        current.update({k: v for k, v in message.items() if k != "type"})
        self.schedules[schedule_id] = current

    def _resolve_pending(self, request_id: str, message: dict[str, Any]) -> None:
        with self._pending_lock:
            pending = self._pending.get(request_id)
            if not pending:
                return
            message_type = str(message.get("type", ""))
            if not message_type.startswith(f"{pending.prefix}."):
                return
            if pending.prefix == "send" and message_type == "send.progress":
                if pending.progress is None:
                    pending.progress = []
                pending.progress.append(dict(message))
                return
            if pending.prefix == "send" and message_type != "send.result" and not message_type.endswith(".error"):
                return
            if pending.response is not None:
                return
            pending.response = message
            pending.in_flight = False
            pending.reconnect_waiting = False
            pending.resume_in_flight = False
            pending.send_owner = None
            pending.event.set()

    def _mark_pending_indeterminate(self) -> None:
        events_to_signal: list[threading.Event] = []
        now = time.monotonic()
        with self._pending_lock:
            for request_id, pending in list(self._pending.items()):
                if pending.retry_eligible and pending.response is None:
                    reason = self._pending_retry_giveup_reason(pending, now=now)
                    if reason is None:
                        pending.in_flight = False
                        pending.reconnect_waiting = True
                        pending.resume_in_flight = False
                        if pending.send_owner is None:
                            pending.resume_generation += 1
                        continue
                    pending.response = _retry_failure_response(
                        pending.prefix,
                        request_id,
                        reason=reason,
                        attempts=max(1, pending.attempts),
                        sent=pending.sent,
                        progress=pending.progress,
                    )
                    pending.in_flight = False
                    pending.reconnect_waiting = False
                    pending.resume_in_flight = False
                    pending.send_owner = None
                    events_to_signal.append(pending.event)
                    continue
                if pending.response is not None:
                    continue
                if pending.prefix == "send" and pending.sent:
                    pending.response = {
                        "type": "send.transport",
                        "request_id": request_id,
                        "outcome": "transmit_delivered_awaiting_result",
                        "reason": "stream_disconnect_after_send",
                        "progress": list(pending.progress or []),
                    }
                else:
                    pending.response = {
                        "type": _rpc_failure_type(pending.prefix),
                        "request_id": request_id,
                        "reason": f"stream_disconnect_after_{pending.prefix}",
                    }
                pending.in_flight = False
                pending.reconnect_waiting = False
                pending.resume_in_flight = False
                pending.send_owner = None
                events_to_signal.append(pending.event)
        for event in events_to_signal:
            event.set()

    def _pending_retry_giveup_reason(self, pending: PendingRpc, *, now: float | None = None) -> str | None:
        policy = pending.retry_policy
        if not policy:
            return "retry_exhausted"
        current = time.monotonic() if now is None else now
        if current >= pending.retry_deadline:
            return "retry_deadline_exceeded"
        if pending.attempts >= policy.max_attempts:
            return "retry_exhausted"
        return None

    def _pending_retry_deadline_expired(self, pending: PendingRpc, *, now: float | None = None) -> bool:
        if not pending.retry_policy:
            return False
        current = time.monotonic() if now is None else now
        return current >= pending.retry_deadline

    def _rpc_call_timeout(self, payload: dict[str, Any]) -> float:
        timeout = self.rpc_timeout
        if payload.get("type") in {"await_report", "await_spawn"}:
            try:
                semantic_timeout = float(payload.get("timeout", timeout))
            except (TypeError, ValueError):
                semantic_timeout = timeout
            timeout = min(timeout, max(0.01, semantic_timeout + 1.0))
        return timeout

    def _pending_wait_timeout(self, pending: PendingRpc) -> float:
        if pending.reconnect_waiting or pending.resume_in_flight or pending.send_owner is not None:
            return max(0.01, pending.retry_deadline - time.monotonic())
        if pending.retry_eligible and pending.retry_policy:
            return min(
                self._rpc_call_timeout(pending.payload),
                _retry_wait_timeout(pending.retry_deadline, pending.attempts, pending.retry_policy.max_attempts),
            )
        return self._rpc_call_timeout(pending.payload)

    def _claim_pending_send(
        self,
        request_id: str,
        pending: PendingRpc,
        *,
        owner: str,
        attempt: int,
        resume_generation: int | None = None,
    ) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._pending_lock:
            current = self._pending.get(request_id)
            if current is not pending or pending.response is not None:
                return None
            if pending.retry_eligible and self._pending_retry_deadline_expired(pending, now=now):
                return None
            if pending.send_owner is not None or pending.attempts != attempt:
                return None
            if resume_generation is None:
                if pending.in_flight or pending.reconnect_waiting or pending.resume_in_flight:
                    return None
            else:
                if (
                    pending.resume_generation != resume_generation
                    or not pending.resume_in_flight
                    or pending.reconnect_waiting
                    or pending.in_flight
                ):
                    return None
            pending.send_owner = owner
            return dict(pending.payload)

    def _clear_pending_send_claim(
        self,
        request_id: str,
        pending: PendingRpc,
        *,
        owner: str,
        resume_failed: bool = False,
    ) -> None:
        with self._pending_lock:
            current = self._pending.get(request_id)
            if current is not pending or pending.send_owner != owner:
                return
            pending.send_owner = None
            pending.in_flight = False
            if owner.startswith("resume:"):
                pending.resume_in_flight = False
                pending.reconnect_waiting = resume_failed and pending.response is None

    async def _resume_pending_after_reconnect(self) -> None:
        if not self._ws:
            return
        resumable: list[tuple[str, PendingRpc, int, int]] = []
        events_to_signal: list[threading.Event] = []
        now = time.monotonic()
        with self._pending_lock:
            for request_id, pending in list(self._pending.items()):
                if not pending.reconnect_waiting or pending.response is not None or pending.send_owner is not None:
                    continue
                reason = self._pending_retry_giveup_reason(pending, now=now)
                if reason is not None:
                    pending.response = _retry_failure_response(
                        pending.prefix,
                        request_id,
                        reason=reason,
                        attempts=max(1, pending.attempts),
                        sent=pending.sent,
                        progress=pending.progress,
                    )
                    pending.reconnect_waiting = False
                    pending.resume_in_flight = False
                    pending.in_flight = False
                    pending.send_owner = None
                    events_to_signal.append(pending.event)
                    continue
                pending.attempts += 1
                if pending.verb == "send":
                    pending.payload["retry"] = True
                pending.reconnect_waiting = False
                pending.resume_in_flight = True
                pending.in_flight = False
                pending.resume_generation += 1
                resumable.append((request_id, pending, pending.attempts, pending.resume_generation))
        for event in events_to_signal:
            event.set()
        for request_id, pending, attempt, generation in resumable:
            owner = f"resume:{generation}"
            payload = self._claim_pending_send(
                request_id,
                pending,
                owner=owner,
                attempt=attempt,
                resume_generation=generation,
            )
            if payload is None:
                with self._pending_lock:
                    current = self._pending.get(request_id)
                    if (
                        current is pending
                        and pending.response is None
                        and pending.resume_generation == generation
                        and pending.send_owner is None
                    ):
                        pending.resume_in_flight = False
                continue
            try:
                sent = await self._send_pending_payload(request_id, pending, payload, owner=owner)
            except Exception:
                self._clear_pending_send_claim(request_id, pending, owner=owner, resume_failed=True)
                raise
            if not sent:
                continue

    async def _send_pending_payload(
        self,
        request_id: str,
        pending: PendingRpc,
        payload: dict[str, Any],
        *,
        owner: str,
    ) -> bool:
        with self._pending_lock:
            current = self._pending.get(request_id)
            if current is not pending or pending.response is not None or pending.send_owner != owner:
                return False
            if pending.retry_eligible and self._pending_retry_deadline_expired(pending):
                pending.send_owner = None
                pending.resume_in_flight = False
                return False
        try:
            await self._ws.send(json.dumps(payload, separators=(",", ":")))
        except Exception:
            self._clear_pending_send_claim(request_id, pending, owner=owner, resume_failed=owner.startswith("resume:"))
            raise
        with self._pending_lock:
            current = self._pending.get(request_id)
            if current is pending and pending.send_owner == owner:
                pending.send_owner = None
                pending.sent = True
                pending.resume_in_flight = False
                pending.reconnect_waiting = False
                if pending.response is None:
                    pending.in_flight = True
                return True
        return False

    def _rpc(self, prefix: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload.get("request_id") or f"{prefix}-{uuid.uuid4()}"
        payload["request_id"] = request_id
        call_timeout = self._rpc_call_timeout(payload)
        retry_policy = _rpc_retry_policy_from_env(call_timeout)
        retry_eligible = _is_rpc_retry_eligible(payload)
        retry_deadline = _retry_deadline(time.monotonic(), retry_policy)
        verb = _rpc_verb(payload)
        pending = PendingRpc(
            prefix=prefix,
            event=threading.Event(),
            payload=payload,
            retry_eligible=retry_eligible,
            retry_policy=retry_policy,
            retry_deadline=retry_deadline,
            verb=verb,
            progress=[] if prefix == "send" else None,
        )
        with self._pending_lock:
            self._pending[request_id] = pending
        try:
            while True:
                with self._pending_lock:
                    if pending.response is not None or pending.event.is_set():
                        if pending.response is not None and prefix == "send":
                            pending.response.setdefault("progress", list(pending.progress or []))
                        return pending.response or {"type": _rpc_failure_type(prefix), "request_id": request_id}
                    if pending.in_flight or pending.reconnect_waiting or pending.resume_in_flight or pending.send_owner is not None:
                        wait_for_existing_attempt = True
                        wait_timeout = self._pending_wait_timeout(pending)
                        attempts = max(1, pending.attempts)
                        wait_attempts = attempts
                    else:
                        wait_for_existing_attempt = False
                        pending.attempts += 1
                        attempts = pending.attempts
                        wait_attempts = attempts
                        if attempts > 1 and verb == "send":
                            payload["retry"] = True
                        pending.response = None
                    pending.event.clear()
                if wait_for_existing_attempt:
                    if pending.event.wait(wait_timeout):
                        continue
                    with self._pending_lock:
                        if pending.response is not None or pending.event.is_set():
                            continue
                        if pending.reconnect_waiting or pending.resume_in_flight or pending.send_owner is not None:
                            giveup_reason = self._pending_retry_giveup_reason(pending)
                            if giveup_reason is None:
                                continue
                            attempts = max(1, pending.attempts)
                            _log_rpc_giveup(verb, attempts, giveup_reason)
                            return _retry_failure_response(
                                prefix,
                                request_id,
                                reason=giveup_reason,
                                attempts=attempts,
                                sent=pending.sent,
                                progress=pending.progress,
                            )
                        if pending.in_flight and pending.attempts > wait_attempts:
                            continue
                        pending.in_flight = False
                        attempts = max(1, pending.attempts)
                    if not retry_eligible:
                        if prefix == "send":
                            return {
                                "type": "send.transport",
                                "request_id": request_id,
                                "outcome": "transmit_delivered_awaiting_result",
                                "reason": "rpc_timeout",
                                "progress": list(pending.progress or []),
                            }
                        return {"type": _rpc_failure_type(prefix), "request_id": request_id, "reason": "rpc_timeout"}
                    delay, giveup_reason = _retry_next_delay(retry_policy, retry_deadline, attempts)
                    if delay is None:
                        _log_rpc_giveup(verb, attempts, giveup_reason)
                        return _retry_failure_response(
                            prefix,
                            request_id,
                            reason=giveup_reason,
                            attempts=attempts,
                            sent=pending.sent,
                            progress=pending.progress,
                        )
                    _log_rpc_retry(verb, attempts + 1, retry_policy.max_attempts, "rpc_timeout", delay)
                    time.sleep(delay)
                    continue
                if not self._loop or not self._ws:
                    if not retry_eligible:
                        if prefix == "send":
                            return {
                                "type": "send.transport",
                                "request_id": request_id,
                                "outcome": "transmit_failed",
                                "reason": "websocket_not_connected",
                            }
                        return {"type": _rpc_failure_type(prefix), "request_id": request_id, "reason": "websocket_not_connected"}
                    delay, giveup_reason = _retry_next_delay(retry_policy, retry_deadline, attempts)
                    if delay is None:
                        _log_rpc_giveup(verb, attempts, giveup_reason)
                        return _retry_failure_response(
                            prefix,
                            request_id,
                            reason=giveup_reason,
                            attempts=attempts,
                            sent=pending.sent,
                            progress=pending.progress,
                        )
                    _log_rpc_retry(verb, attempts + 1, retry_policy.max_attempts, "websocket_not_connected", delay)
                    time.sleep(delay)
                    continue
                try:
                    owner = f"rpc:{attempts}"
                    claimed_payload = self._claim_pending_send(request_id, pending, owner=owner, attempt=attempts)
                    if claimed_payload is None:
                        with self._pending_lock:
                            if pending.response is not None or pending.event.is_set():
                                continue
                            giveup_reason = (
                                self._pending_retry_giveup_reason(pending) if pending.retry_eligible else None
                            )
                            if giveup_reason is not None:
                                _log_rpc_giveup(verb, max(1, pending.attempts), giveup_reason)
                                return _retry_failure_response(
                                    prefix,
                                    request_id,
                                    reason=giveup_reason,
                                    attempts=max(1, pending.attempts),
                                    sent=pending.sent,
                                    progress=pending.progress,
                                )
                        continue
                    send_coro = self._send_pending_payload(request_id, pending, claimed_payload, owner=owner)
                    try:
                        future = asyncio.run_coroutine_threadsafe(send_coro, self._loop)
                    except Exception:
                        send_coro.close()
                        self._clear_pending_send_claim(request_id, pending, owner=owner)
                        raise
                    future.result(timeout=5)
                except Exception as exc:
                    if not retry_eligible:
                        if prefix == "send":
                            return {
                                "type": "send.transport",
                                "request_id": request_id,
                                "outcome": "transmit_failed",
                                "reason": "websocket_send_failed",
                                "message": str(exc),
                            }
                        return {
                            "type": _rpc_failure_type(prefix),
                            "request_id": request_id,
                            "reason": "websocket_send_failed",
                            "message": str(exc),
                        }
                    delay, giveup_reason = _retry_next_delay(retry_policy, retry_deadline, attempts)
                    if delay is None:
                        _log_rpc_giveup(verb, attempts, giveup_reason)
                        return _retry_failure_response(
                            prefix,
                            request_id,
                            reason=giveup_reason,
                            attempts=attempts,
                            sent=pending.sent,
                            progress=pending.progress,
                            message=str(exc),
                        )
                    _log_rpc_retry(verb, attempts + 1, retry_policy.max_attempts, "websocket_send_failed", delay)
                    time.sleep(delay)
                    continue
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def spawn(
        self,
        host: str,
        provider: str,
        parent_stream_id: str | None,
        role: str | None,
        phase: str | None,
        visibility: str | None,
        request_id: str | None = None,
        handoff: bool = False,
        handoff_from_stream_id: str | None = None,
        initial_prompt: str | None = None,
        initial_prompt_blob_sha: str | None = None,
        spec_id: str | None = None,
        resume_session_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "type": "spawn",
            "host": host,
            "provider": provider,
            "parent_stream_id": parent_stream_id,
            "role": role,
            "phase": phase,
        }
        if model is not None:
            payload["model"] = model
        if effort is not None:
            payload["effort"] = effort
        if request_id is not None:
            payload["request_id"] = request_id
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        if visibility is not None:
            payload["visibility"] = visibility
        if handoff:
            payload["handoff"] = True
        if handoff_from_stream_id is not None:
            payload["handoff_from_stream_id"] = handoff_from_stream_id
        if initial_prompt is not None:
            payload["initial_prompt"] = initial_prompt
        if initial_prompt_blob_sha is not None:
            payload["initial_prompt_blob_sha"] = initial_prompt_blob_sha
        if spec_id is not None:
            payload["spec_id"] = spec_id
        if resume_session_id is not None:
            # Daemon resume hook: reuse this claude session_id so the original
            # stream_id/dashboard row reopens via `claude --resume`.
            payload["resume_session_id"] = resume_session_id
        if (parent_stream_id or handoff_from_stream_id) and _stream_token_from_env():
            payload["stream_token"] = _stream_token_from_env()
        spawn_request_id = payload.setdefault("request_id", request_id or f"spawn-{uuid.uuid4()}")
        payload.setdefault("idempotency_key", spawn_request_id)
        payload["client_rpc_timeout_s"] = self.rpc_timeout
        return self._rpc("spawn", payload)

    def schedule_insert(
        self,
        *,
        fires_at_utc: str,
        provider: str,
        target_host: str | None,
        visibility: str,
        created_by_stream_id: str | None,
        initial_prompt: str | None = None,
        initial_prompt_blob_sha: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "schedule.insert",
            "fires_at_utc": fires_at_utc,
            "provider": provider,
            "target_host": target_host,
            "visibility": visibility,
            "created_by_stream_id": created_by_stream_id,
        }
        if initial_prompt is not None:
            payload["initial_prompt"] = initial_prompt
        if initial_prompt_blob_sha is not None:
            payload["initial_prompt_blob_sha"] = initial_prompt_blob_sha
        if created_by_stream_id and _stream_token_from_env():
            payload["stream_token"] = _stream_token_from_env()
        return self._rpc("schedule", payload)

    def schedule_list(self, state: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": "schedule.list"}
        if state is not None:
            payload["state"] = state
        return self._rpc("schedule", payload)

    def schedule_get(self, schedule_id: str) -> dict[str, Any]:
        return self._rpc("schedule", {"type": "schedule.get", "schedule_id": schedule_id})

    def schedule_cancel(self, schedule_id: str) -> dict[str, Any]:
        return self._rpc("schedule", {"type": "schedule.cancel", "schedule_id": schedule_id})

    def schedule_reschedule(self, schedule_id: str, fires_at_utc: str) -> dict[str, Any]:
        return self._rpc(
            "schedule",
            {"type": "schedule.reschedule", "schedule_id": schedule_id, "fires_at_utc": fires_at_utc},
        )

    def schedule_run(self, schedule_id: str) -> dict[str, Any]:
        return self._rpc("schedule", {"type": "schedule.run", "schedule_id": schedule_id})

    def send_rpc(
        self,
        host: str,
        session_name: str,
        text: str,
        from_stream_id: str | None = None,
        msg_id: int | None = None,
        retry: bool = False,
        inbox: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": "send", "host": host, "session_name": session_name, "text": text}
        if from_stream_id is not None:
            payload["from_stream_id"] = from_stream_id
            if _stream_token_from_env():
                payload["stream_token"] = _stream_token_from_env()
        if msg_id is not None:
            payload["msg_id"] = msg_id
        if retry:
            payload["retry"] = True
        if inbox is not None:
            validate_inbox(inbox)
            payload["inbox"] = inbox
        return self._rpc("send", payload)

    def send_cancel(self, msg_id: int, request_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": "send.cancel", "msg_id": int(msg_id)}
        if request_id is not None:
            payload["request_id"] = request_id
        return self._rpc("send.cancel", payload)

    def tell(
        self,
        tell_id: str,
        from_stream_id: str,
        to_stream_id: str,
        text: str,
        ttl_seconds: int = 300,
        request_id: str | None = None,
        urgent: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "type": "tell",
            "tell_id": tell_id,
            "from_stream_id": from_stream_id,
            "to_stream_id": to_stream_id,
            "text": text,
            "ttl_seconds": ttl_seconds,
        }
        if request_id is not None:
            payload["request_id"] = request_id
        if urgent:
            payload["urgent"] = True
        if _stream_token_from_env():
            payload["stream_token"] = _stream_token_from_env()
        return self._rpc("tell", payload)

    def close_rpc(
        self,
        host: str,
        session_name: str,
        reason: str,
        *,
        operator_confirm: bool = False,
        force: bool = False,
        defer_if_working: bool = False,
        from_stream_id: str | None = None,
        caller_stream_id: str | None = None,
        progeny_stream_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": "close", "host": host, "session_name": session_name, "reason": reason}
        if operator_confirm:
            payload["operator_confirm"] = True
        if force:
            payload["force"] = True
        if defer_if_working:
            payload["defer_if_working"] = True
        if from_stream_id:
            payload["from_stream_id"] = from_stream_id
        if caller_stream_id is not None:
            payload["caller_stream_id"] = caller_stream_id
        if progeny_stream_id is not None:
            payload["progeny_stream_id"] = progeny_stream_id
        if (from_stream_id or caller_stream_id) and _stream_token_from_env():
            payload["stream_token"] = _stream_token_from_env()
        return self._rpc("close", payload)

    def set_visibility(self, host: str, session_name: str, visibility: str) -> dict[str, Any]:
        return self._rpc(
            "set_visibility",
            {
                "type": "set_visibility",
                "host": host,
                "session_name": session_name,
                "visibility": visibility,
            },
        )

    def rename(self, host: str, session_name: str, display_name: str, source: str = "agent") -> dict[str, Any]:
        return self._rpc(
            "rename",
            {
                "type": "rename",
                "host": host,
                "session_name": session_name,
                "display_name": display_name,
                "source": source,
            },
        )

    def inspect_stream(self, stream_id: str, msg_id: int | None = None, event_tail: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": "inspect_stream", "stream_id": stream_id}
        if msg_id is not None:
            payload["msg_id"] = msg_id
        if event_tail is not None:
            payload["event_tail"] = event_tail
        return self._rpc("inspect_stream", payload)

    def spawn_catalog_get(self) -> dict[str, Any]:
        return self._rpc("spawn_catalog_get", {"type": "spawn_catalog_get"})

    def await_report(self, stream_id: str, msg_id: int | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "await_report",
            "stream_id": stream_id,
            "timeout": timeout,
        }
        # Omit msg_id entirely in stream mode so the daemon resolves on this
        # stream's terminal report (any msg_id) or on close.
        if msg_id is not None:
            payload["msg_id"] = msg_id
        return self._rpc("await_report", payload)

    def report(
        self,
        report_id: str,
        from_stream_id: str,
        msg_id: int,
        status: str,
        payload: dict[str, Any] | None = None,
        *,
        result_blob_sha: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": "report",
            "report_id": report_id,
            "from_stream_id": from_stream_id,
            "msg_id": msg_id,
            "status": status,
        }
        if payload:
            body.update(payload)
        if reason is not None:
            body["reason"] = reason
        if result_blob_sha is not None:
            body["result_blob_sha"] = result_blob_sha
        if _stream_token_from_env():
            body["stream_token"] = _stream_token_from_env()
        return self._rpc("report", body)

    def upload_blob(self, data: bytes, *, request_id: str | None = None) -> dict[str, Any]:
        return asyncio.run(upload_blob_once(self.config, data, timeout=self.rpc_timeout, request_id=request_id))

    def upload_prompt_blob(self, text: str, *, request_id: str | None = None) -> str:
        response = asyncio.run(
            upload_prompt_blob_once(
                self.config,
                text.encode("utf-8"),
                timeout=self.rpc_timeout,
                request_id=request_id,
            )
        )
        if response.get("type") != "upload_prompt_blob.ok":
            raise RuntimeError(str(response.get("error_code") or response.get("error") or "upload_prompt_blob_failed"))
        return str(response["prompt_blob_sha"])

    def fetch_blob(self, blob_sha: str) -> bytes:
        return asyncio.run(fetch_blob_once(self.config, blob_sha, timeout=self.rpc_timeout))


def _host_ids(hosts: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(hosts, dict):
        # chat_streamd ships snapshot.hosts as a dict keyed by host name,
        # with the value being a status object ({host, online, checked_at, ...}).
        for key, value in hosts.items():
            if isinstance(key, str):
                ids.add(key)
            if isinstance(value, dict):
                inner = value.get("host") or value.get("id") or value.get("name")
                if isinstance(inner, str):
                    ids.add(inner)
    elif isinstance(hosts, list):
        for host in hosts:
            if isinstance(host, str):
                ids.add(host)
            elif isinstance(host, dict):
                value = host.get("id") or host.get("host") or host.get("name")
                if isinstance(value, str):
                    ids.add(value)
    return ids


async def _fetch_snapshot_async(config: Config, timeout: float, *, events_mode: str | None = None) -> dict[str, Any]:
    keepalive = _websocket_keepalive_from_env()
    async with websockets.connect(
        config.ws_url,
        max_size=WEBSOCKET_MAX_SIZE,
        ping_interval=keepalive.ping_interval,
        ping_timeout=keepalive.ping_timeout,
        close_timeout=keepalive.close_timeout,
    ) as ws:
        hello: dict[str, Any] = {
            "type": "hello",
            "client": "agent-orch",
            "subscribe": {"all": True, "include_subagents": True},
            "host": config.host_id,
            "agent_orch_capabilities": _agent_orch_capabilities_payload(),
        }
        if events_mode:
            hello["subscribe"]["events_mode"] = events_mode
        if config.token:
            hello["token"] = config.token
        stream_id = _resolved_rpc_from_stream_id()
        stream_token = _stream_token_from_env()
        if stream_id and stream_token:
            hello.update(from_stream_id=stream_id, stream_token=stream_token)
        await ws.send(json.dumps(hello, separators=(",", ":")))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.01, deadline - time.monotonic()))
            message = json.loads(raw)
            if message.get("type") in {"auth.error", "hello.error"}:
                raise PermissionError(str(message.get("error_code") or message.get("error") or message["type"]))
            if message.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong"}, separators=(",", ":")))
                continue
            if message.get("type") == "snapshot":
                hosts = message.get("hosts", [])
                if hosts and config.host_id not in _host_ids(hosts):
                    raise RuntimeError("unknown_local_host")
                return message
    raise SnapshotTimeout("snapshot_timeout")


def fetch_snapshot(config: Config, timeout: float | None = None, *, events_mode: str | None = None) -> dict[str, Any]:
    snapshot_timeout = _snapshot_timeout_from_env() if timeout is None else _clamp_snapshot_timeout(timeout)
    policy = _rpc_retry_policy_from_env(snapshot_timeout)
    deadline = _retry_deadline(time.monotonic(), policy)
    attempts = 0
    last_exc: Exception | None = None
    while True:
        attempts += 1
        try:
            return asyncio.run(
                _fetch_snapshot_async(
                    config,
                    min(snapshot_timeout, _retry_wait_timeout(deadline, attempts, policy.max_attempts)),
                    events_mode=events_mode,
                )
            )
        except PermissionError:
            raise
        except (asyncio.TimeoutError, SnapshotTimeout, OSError) as exc:
            last_exc = exc
            delay, giveup_reason = _retry_next_delay(policy, deadline, attempts)
            if delay is None:
                _log_rpc_giveup("snapshot", attempts, giveup_reason)
                raise SnapshotTimeout(f"snapshot_timeout:{giveup_reason}:attempts={attempts}") from last_exc
            _log_rpc_retry("snapshot", attempts + 1, policy.max_attempts, "rpc_timeout", delay)
            time.sleep(delay)


def _hello(config: Config, from_stream_id: str | None = None, *, infer_internal_leader: bool = True) -> dict[str, Any]:
    hello: dict[str, Any] = {
        "type": "hello",
        "client": "agent-orch",
        "subscribe": {"all": True, "include_subagents": True},
        "host": config.host_id,
        "agent_orch_capabilities": _agent_orch_capabilities_payload(),
    }
    resolved_stream_id = _resolved_rpc_from_stream_id(from_stream_id) if infer_internal_leader else from_stream_id
    if resolved_stream_id:
        hello["from_stream_id"] = resolved_stream_id
        stream_token = _stream_token_from_env()
        if stream_token:
            hello["stream_token"] = stream_token
    if config.token:
        hello["token"] = config.token
    return hello


def _resolved_rpc_from_stream_id(from_stream_id: str | None = None) -> str | None:
    if from_stream_id:
        return from_stream_id
    for name in (
        "AGENT_ORCH_INTERNAL_LEADER_STREAM_ID",
        "AGENT_ORCH_STREAM_ID",
        "PENTACLE_STREAM_ID",
    ):
        value = os.environ.get(name)
        if value:
            return value
    return None


def _attach_agent_identity(payload: dict[str, Any]) -> str | None:
    """Carry the calling seat's identity on one-shot authenticated RPCs."""
    stream_id = (
        payload.get("from_stream_id")
        if isinstance(payload.get("from_stream_id"), str) and payload.get("from_stream_id")
        else _resolved_rpc_from_stream_id()
    )
    if not stream_id:
        return None
    payload["from_stream_id"] = stream_id
    if "stream_token" not in payload:
        stream_token = _stream_token_from_env()
        if stream_token:
            payload["stream_token"] = stream_token
    return stream_id


def _rpc_hello(config: Config, from_stream_id: str | None = None, *, infer_from_env: bool = True) -> dict[str, Any]:
    resolved_from_stream_id = _resolved_rpc_from_stream_id(from_stream_id) if infer_from_env else from_stream_id
    stream_token = _stream_token_from_env()
    hello = _hello(
        config,
        infer_internal_leader=False,
    )
    if resolved_from_stream_id and stream_token:
        hello["from_stream_id"] = resolved_from_stream_id
        hello["stream_token"] = stream_token
    hello["subscribe"] = {
        "snapshot": False,
        "mode": "rpc",
        "events_mode": "summary",
        "exclude_event_types": [
            "codex.usage",
            "claude.usage",
            "machine.stats",
            "machine.stats.inventory",
        ],
        "opened_by_host_ids": ["__agent_orch_rpc__"],
        "include_subagents": False,
    }
    return hello


async def _connect_ready(config: Config, timeout: float, from_stream_id: str | None = None):
    snapshot_timeout = _clamp_snapshot_timeout(timeout)
    keepalive = _websocket_keepalive_from_env()
    ws = await websockets.connect(
        config.ws_url,
        max_size=WEBSOCKET_MAX_SIZE,
        ping_interval=keepalive.ping_interval,
        ping_timeout=keepalive.ping_timeout,
        close_timeout=keepalive.close_timeout,
    )
    try:
        await ws.send(json.dumps(_hello(config, from_stream_id=from_stream_id), separators=(",", ":")))
        deadline = time.monotonic() + snapshot_timeout
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.01, deadline - time.monotonic()))
            message = json.loads(raw)
            if message.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong"}, separators=(",", ":")))
                continue
            if message.get("type") == "snapshot":
                return ws, message
            if message.get("type") in {"auth.error", "hello.error"}:
                raise PermissionError(str(message.get("error_code") or message.get("error") or message["type"]))
        raise SnapshotTimeout("snapshot_timeout")
    except BaseException:
        await ws.close()
        raise


async def _connect_rpc_ready(config: Config, from_stream_id: str | None = None, *, infer_from_env: bool = True):
    keepalive = _websocket_keepalive_from_env()
    ws = await websockets.connect(
        config.ws_url,
        max_size=WEBSOCKET_MAX_SIZE,
        ping_interval=keepalive.ping_interval,
        ping_timeout=keepalive.ping_timeout,
        close_timeout=keepalive.close_timeout,
    )
    await ws.send(json.dumps(_rpc_hello(config, from_stream_id=from_stream_id, infer_from_env=infer_from_env), separators=(",", ":")))
    return ws


async def _read_rpc_frame(
    ws: Any,
    request_id: str | None,
    *,
    deadline: float,
    matches: Callable[[dict[str, Any]], bool],
    timeout_message: str,
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.01, deadline - time.monotonic()))
        except asyncio.TimeoutError as exc:
            raise TimeoutError(timeout_message) from exc
        message = json.loads(raw)
        message_type = str(message.get("type", ""))
        if message_type == "ping":
            await ws.send(json.dumps({"type": "pong"}, separators=(",", ":")))
            continue
        if message_type in {"auth.error", "hello.error"}:
            raise PermissionError(str(message.get("error_code") or message.get("error") or message_type))
        if message_type in {"welcome", "ready", "snapshot"}:
            continue
        if request_id is not None and message.get("request_id") != request_id:
            continue
        if matches(message):
            return message
    raise TimeoutError(timeout_message)


async def _read_rpc_response(
    ws: Any,
    request_id: str,
    *,
    prefix: str,
    deadline: float,
    timeout_message: str | None = None,
) -> dict[str, Any]:
    return await _read_rpc_frame(
        ws,
        request_id,
        deadline=deadline,
        matches=lambda message: (
            str(message.get("type", "")).startswith(f"{prefix}.")
            or (prefix.startswith("asset.") and message.get("type") == "asset.error")
        ),
        timeout_message=timeout_message or f"{prefix.replace('.', '_')}_timeout",
    )


async def _one_shot_rpc(
    config: Config,
    payload: dict[str, Any],
    *,
    prefix: str,
    timeout: float = 30.0,
    from_stream_id: str | None = None,
    infer_from_env: bool = True,
    response_timeout_extra: float = 0.0,
) -> dict[str, Any]:
    request_id = payload["request_id"]
    retry_policy = _rpc_retry_policy_from_env(timeout)
    retry_eligible = _is_rpc_retry_eligible(payload)
    retry_deadline = _retry_deadline(time.monotonic(), retry_policy)
    verb = _rpc_verb(payload)
    attempts = 0
    sent_any = False
    last_message: str | None = None
    while True:
        attempts += 1
        if attempts > 1 and verb == "send":
            payload["retry"] = True
        ws = None
        try:
            wait_timeout = timeout
            if retry_eligible:
                wait_timeout = min(timeout, _retry_wait_timeout(retry_deadline, attempts, retry_policy.max_attempts))
            if infer_from_env:
                ws = await _connect_rpc_ready(config, from_stream_id=from_stream_id)
            else:
                ws = await _connect_rpc_ready(config, from_stream_id=from_stream_id, infer_from_env=False)
            await ws.send(json.dumps(payload, separators=(",", ":")))
            sent_any = True
            response_deadline = time.monotonic() + wait_timeout + response_timeout_extra
            return await _read_rpc_response(ws, request_id, prefix=prefix, deadline=response_deadline)
        except PermissionError:
            raise
        except TimeoutError as exc:
            retry_reason = "rpc_timeout"
            last_message = str(exc)
            if not retry_eligible:
                raise
        except Exception as exc:
            retry_reason = "websocket_send_failed"
            last_message = str(exc)
            if not retry_eligible:
                raise
        finally:
            if ws is not None:
                await ws.close()
        delay, giveup_reason = _retry_next_delay(retry_policy, retry_deadline, attempts)
        if delay is None:
            _log_rpc_giveup(verb, attempts, giveup_reason)
            return _retry_failure_response(
                prefix,
                request_id,
                reason=giveup_reason,
                attempts=attempts,
                sent=sent_any,
                message=last_message,
            )
        _log_rpc_retry(verb, attempts + 1, retry_policy.max_attempts, retry_reason, delay)
        await asyncio.sleep(delay)


async def report_once(config: Config, payload: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    from_stream_id = payload.get("from_stream_id") if isinstance(payload.get("from_stream_id"), str) else None
    if from_stream_id and "stream_token" not in payload and _stream_token_from_env():
        payload["stream_token"] = _stream_token_from_env()
    payload.setdefault("request_id", f"report-{uuid.uuid4()}")
    return await _one_shot_rpc(config, payload, prefix="report", timeout=timeout, from_stream_id=from_stream_id)


async def tell_once(config: Config, payload: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    from_stream_id = payload.get("from_stream_id") if isinstance(payload.get("from_stream_id"), str) else None
    if from_stream_id and "stream_token" not in payload and _stream_token_from_env():
        payload["stream_token"] = _stream_token_from_env()
    payload.setdefault("request_id", f"tell-{uuid.uuid4()}")
    return await _one_shot_rpc(config, payload, prefix="tell", timeout=timeout, from_stream_id=from_stream_id)


async def ledger_get_once(config: Config, tell_id: str, *, timeout: float = 30.0) -> dict[str, Any]:
    payload = {
        "type": "ledger_get",
        "request_id": f"ledger-get-{uuid.uuid4()}",
        "tell_id": tell_id,
    }
    return await _one_shot_rpc(config, payload, prefix="ledger_get", timeout=timeout)


async def inbound_audit_once(
    config: Config, stream_id: str, *, limit: int = 50, timeout: float = 30.0,
) -> dict[str, Any]:
    payload = {
        "type": "inbound_audit",
        "request_id": f"inbound-audit-{uuid.uuid4()}",
        "stream_id": stream_id,
        "limit": int(limit),
    }
    return await _one_shot_rpc(config, payload, prefix="inbound_audit", timeout=timeout)


async def park_once(config: Config, payload: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    from_stream_id = payload.get("from_stream_id") if isinstance(payload.get("from_stream_id"), str) else None
    if from_stream_id and "stream_token" not in payload and _stream_token_from_env():
        payload["stream_token"] = _stream_token_from_env()
    payload.setdefault("request_id", f"{payload.get('type', 'park')}-{uuid.uuid4()}")
    return await _one_shot_rpc(config, payload, prefix=str(payload.get("type") or "park"), timeout=timeout, from_stream_id=from_stream_id)


async def coordination_once(config: Config, payload: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    from_stream_id = payload.get("from_stream_id") if isinstance(payload.get("from_stream_id"), str) else None
    if from_stream_id and "stream_token" not in payload and _stream_token_from_env():
        payload["stream_token"] = _stream_token_from_env()
    payload.setdefault("request_id", f"{payload.get('type', 'coordination')}-{uuid.uuid4()}")
    family = str(payload.get("type", "")).split(".", 1)[0]
    prefix = family if family in {"watch", "wake"} else "coordination"
    return await _one_shot_rpc(config, payload, prefix=prefix, timeout=timeout, from_stream_id=from_stream_id)


async def spawn_cancel_once(config: Config, payload: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    from_stream_id = payload.get("from_stream_id") if isinstance(payload.get("from_stream_id"), str) else None
    if from_stream_id and "stream_token" not in payload and _stream_token_from_env():
        payload["stream_token"] = _stream_token_from_env()
    payload.setdefault("request_id", f"spawn_cancel-{uuid.uuid4()}")
    return await _one_shot_rpc(config, payload, prefix="spawn_cancel", timeout=timeout, from_stream_id=from_stream_id)


async def spawn_status_once(config: Config, payload: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    from_stream_id = payload.get("from_stream_id") if isinstance(payload.get("from_stream_id"), str) else None
    if from_stream_id and "stream_token" not in payload and _stream_token_from_env():
        payload["stream_token"] = _stream_token_from_env()
    payload.setdefault("request_id", f"spawn_status-{uuid.uuid4()}")
    return await _one_shot_rpc(config, payload, prefix="spawn_status", timeout=timeout, from_stream_id=from_stream_id)


async def spawn_freeze_once(config: Config, payload: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    from_stream_id = payload.get("from_stream_id") if isinstance(payload.get("from_stream_id"), str) else None
    if from_stream_id and "stream_token" not in payload and _stream_token_from_env():
        payload["stream_token"] = _stream_token_from_env()
    payload.setdefault("request_id", f"{payload.get('type', 'spawn_freeze')}-{uuid.uuid4()}")
    return await _one_shot_rpc(config, payload, prefix=str(payload.get("type") or "spawn_freeze"), timeout=timeout, from_stream_id=from_stream_id)


async def notification_create_once(
    config: Config, payload: dict[str, Any], *, timeout: float = 30.0
) -> dict[str, Any]:
    from_stream_id = (
        payload.get("answer_to_stream_id")
        if isinstance(payload.get("answer_to_stream_id"), str)
        else None
    )
    ws = await _connect_rpc_ready(config, from_stream_id=from_stream_id)
    try:
        request_id = payload.setdefault("request_id", f"notification-create-{uuid.uuid4()}")
        await ws.send(json.dumps(payload, separators=(",", ":")))
        deadline = time.monotonic() + timeout
        return await _read_rpc_response(
            ws,
            request_id,
            prefix="notification.create",
            deadline=deadline,
            timeout_message="notification_create_timeout",
        )
    finally:
        await ws.close()
    raise TimeoutError("notification_create_timeout")


async def notification_resolve_by_dedup_once(
    config: Config, payload: dict[str, Any], *, timeout: float = 30.0
) -> dict[str, Any]:
    from_stream_id = _attach_agent_identity(payload)
    payload.setdefault("request_id", f"notification-resolve-dedup-{uuid.uuid4()}")
    return await _one_shot_rpc(
        config,
        payload,
        prefix="notification.resolve_by_dedup",
        timeout=timeout,
        from_stream_id=from_stream_id,
    )


async def notification_resolve_once(
    config: Config, payload: dict[str, Any], *, timeout: float = 30.0
) -> dict[str, Any]:
    from_stream_id = _attach_agent_identity(payload)
    payload.setdefault("request_id", f"notification-resolve-{uuid.uuid4()}")
    return await _one_shot_rpc(
        config,
        payload,
        prefix="notification.resolve",
        timeout=timeout,
        from_stream_id=from_stream_id,
    )


async def investigation_once(
    config: Config, payload: dict[str, Any], *, timeout: float = 30.0
) -> dict[str, Any]:
    payload.setdefault("request_id", f"{payload.get('type', 'investigation')}-{uuid.uuid4()}")
    return await _one_shot_rpc(config, payload, prefix="investigation", timeout=timeout)


async def nexus_once(
    config: Config, payload: dict[str, Any], *, timeout: float = 30.0
) -> dict[str, Any]:
    if "from_stream_id" not in payload:
        stream_id = _resolved_rpc_from_stream_id()
        if stream_id:
            payload["from_stream_id"] = stream_id
    if "stream_token" not in payload and _stream_token_from_env():
        payload["stream_token"] = _stream_token_from_env()
    payload.setdefault("request_id", f"{payload.get('type', 'nexus')}-{uuid.uuid4()}")
    return await _one_shot_rpc(config, payload, prefix="nexus", timeout=timeout)


async def repo_once(
    config: Config, payload: dict[str, Any], *, timeout: float = 30.0
) -> dict[str, Any]:
    if "from_stream_id" not in payload:
        stream_id = _resolved_rpc_from_stream_id()
        if stream_id:
            payload["from_stream_id"] = stream_id
    if "stream_token" not in payload and _stream_token_from_env():
        payload["stream_token"] = _stream_token_from_env()
    payload.setdefault("request_id", f"{payload.get('type', 'repo')}-{uuid.uuid4()}")
    return await _one_shot_rpc(config, payload, prefix="repo", timeout=timeout)


async def asset_publish_once(
    config: Config, payload: dict[str, Any], *, timeout: float = 30.0
) -> dict[str, Any]:
    from_stream_id = (
        payload.get("from_stream_id")
        if isinstance(payload.get("from_stream_id"), str)
        else payload.get("stream_id")
        if isinstance(payload.get("stream_id"), str)
        else None
    )
    if from_stream_id and "stream_token" not in payload and _stream_token_from_env():
        payload["stream_token"] = _stream_token_from_env()
    payload.setdefault("request_id", f"asset-publish-{uuid.uuid4()}")
    return await _one_shot_rpc(
        config,
        payload,
        prefix="asset.publish",
        timeout=timeout,
        from_stream_id=from_stream_id,
    )


def _asset_caller_identity(payload: dict[str, Any]) -> str | None:
    caller_stream_id = (
        payload.get("from_stream_id")
        if isinstance(payload.get("from_stream_id"), str)
        else payload.get("caller_stream_id")
        if isinstance(payload.get("caller_stream_id"), str)
        else payload.get("stream_id")
        if isinstance(payload.get("stream_id"), str)
        else _resolved_rpc_from_stream_id()
    )
    if caller_stream_id:
        payload.setdefault("from_stream_id", caller_stream_id)
        if "stream_token" not in payload and _stream_token_from_env():
            payload["stream_token"] = _stream_token_from_env()
    return caller_stream_id


async def asset_list_once(
    config: Config, payload: dict[str, Any], *, timeout: float = 30.0
) -> dict[str, Any]:
    caller_stream_id = _asset_caller_identity(payload)
    payload.setdefault("request_id", f"asset-list-{uuid.uuid4()}")
    return await _one_shot_rpc(
        config, payload, prefix="asset.list", timeout=timeout, from_stream_id=caller_stream_id
    )


async def asset_get_once(
    config: Config, payload: dict[str, Any], *, timeout: float = 30.0
) -> dict[str, Any]:
    caller_stream_id = _asset_caller_identity(payload)
    payload.setdefault("request_id", f"asset-get-{uuid.uuid4()}")
    return await _one_shot_rpc(
        config, payload, prefix="asset.get", timeout=timeout, from_stream_id=caller_stream_id
    )


async def asset_health_once(
    config: Config, payload: dict[str, Any], *, timeout: float = 30.0
) -> dict[str, Any]:
    payload.setdefault("request_id", f"asset-health-{uuid.uuid4()}")
    return await _one_shot_rpc(config, payload, prefix="asset.health", timeout=timeout)


async def asset_comments_list_once(
    config: Config, payload: dict[str, Any], *, timeout: float = 30.0
) -> dict[str, Any]:
    caller_stream_id = _asset_caller_identity(payload)
    payload.setdefault("request_id", f"asset-comments-list-{uuid.uuid4()}")
    return await _one_shot_rpc(
        config, payload, prefix="asset.comments.list", timeout=timeout, from_stream_id=caller_stream_id
    )


async def asset_comment_resolve_once(
    config: Config, payload: dict[str, Any], *, timeout: float = 30.0
) -> dict[str, Any]:
    caller_stream_id = _asset_caller_identity(payload)
    payload.setdefault("request_id", f"asset-comment-resolve-{uuid.uuid4()}")
    return await _one_shot_rpc(
        config, payload, prefix="asset.comment.resolve", timeout=timeout, from_stream_id=caller_stream_id
    )


async def notification_await_once(
    config: Config,
    notification_id: str,
    *,
    timeout: float = 30.0,
    request_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(notification_id, str) or not notification_id:
        raise ValueError("invalid_notification_id")
    ws = await _connect_rpc_ready(config)
    request_id = request_id or f"notification-await-{uuid.uuid4()}"
    try:
        payload: dict[str, Any] = {
            "type": "notification.await",
            "request_id": request_id,
            "notification_id": notification_id,
            "timeout": timeout,
        }
        await ws.send(json.dumps(payload, separators=(",", ":")))
        deadline = time.monotonic() + timeout + 1.0
        return await _read_rpc_response(
            ws,
            request_id,
            prefix="notification.await",
            deadline=deadline,
            timeout_message="notification_await_timeout",
        )
    finally:
        await ws.close()
    raise TimeoutError("notification_await_timeout")


async def prompt_ask_once(
    config: Config, payload: dict[str, Any], *, timeout: float = 30.0
) -> dict[str, Any]:
    envelope = payload.get("envelope") if isinstance(payload.get("envelope"), dict) else {}
    from_stream_id = (
        envelope.get("producer_stream_id")
        if isinstance(envelope.get("producer_stream_id"), str)
        else None
    )
    payload.setdefault("request_id", f"prompt-ask-{uuid.uuid4()}")
    if from_stream_id is not None:
        payload["from_stream_id"] = from_stream_id
        stream_token = _stream_token_from_env()
        if stream_token:
            payload["stream_token"] = stream_token
    return await _one_shot_rpc(
        config,
        payload,
        prefix="prompt",
        timeout=timeout,
        from_stream_id=from_stream_id,
    )


async def prompt_status_once(
    config: Config, question_id: str, *, timeout: float = 30.0
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "prompt.status",
        "request_id": f"prompt-status-{uuid.uuid4()}",
        "question_id": question_id,
    }
    return await _one_shot_rpc(config, payload, prefix="prompt", timeout=timeout)


async def prompt_answer_once(
    config: Config, payload: dict[str, Any], *, timeout: float = 30.0
) -> dict[str, Any]:
    from_stream_id = _attach_agent_identity(payload)
    payload.setdefault("request_id", f"prompt-answer-{uuid.uuid4()}")
    return await _one_shot_rpc(
        config, payload, prefix="prompt", timeout=timeout, from_stream_id=from_stream_id
    )


async def prompt_cancel_once(
    config: Config, payload: dict[str, Any], *, timeout: float = 30.0
) -> dict[str, Any]:
    from_stream_id = _attach_agent_identity(payload)
    payload.setdefault("request_id", f"prompt-cancel-{uuid.uuid4()}")
    return await _one_shot_rpc(
        config, payload, prefix="prompt", timeout=timeout, from_stream_id=from_stream_id
    )


async def prompt_list_once(
    config: Config, payload: dict[str, Any], *, timeout: float = 30.0
) -> dict[str, Any]:
    payload.setdefault("request_id", f"prompt-list-{uuid.uuid4()}")
    return await _one_shot_rpc(config, payload, prefix="prompt", timeout=timeout)


async def _inbox_once_attempt(config: Config, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    stream_id = payload.get("stream_id") if isinstance(payload.get("stream_id"), str) else None
    if stream_id and "stream_token" not in payload and _stream_token_from_env():
        payload["stream_token"] = _stream_token_from_env()
    ws = await _connect_rpc_ready(config, from_stream_id=stream_id)
    try:
        request_id = payload.setdefault("request_id", f"inbox-{uuid.uuid4()}")
        await ws.send(json.dumps(payload, separators=(",", ":")))
        deadline = time.monotonic() + timeout
        return await _read_rpc_response(ws, request_id, prefix="inbox", deadline=deadline)
    finally:
        await ws.close()
    raise TimeoutError("inbox_timeout")


async def inbox_once(config: Config, payload: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    if payload.get("drain"):
        return await _inbox_once_attempt(config, payload, timeout=timeout)
    policy = _rpc_retry_policy_from_env(timeout)
    deadline = _retry_deadline(time.monotonic(), policy)
    attempts = 0
    last_exc: Exception | None = None
    while True:
        attempts += 1
        try:
            return await _inbox_once_attempt(
                config,
                payload,
                timeout=min(timeout, _retry_wait_timeout(deadline, attempts, policy.max_attempts)),
            )
        except (TimeoutError, OSError) as exc:
            last_exc = exc
            delay, giveup_reason = _retry_next_delay(policy, deadline, attempts)
            if delay is None:
                _log_rpc_giveup("inbox", attempts, giveup_reason)
                raise TimeoutError(f"inbox_timeout:{giveup_reason}:attempts={attempts}") from last_exc
            _log_rpc_retry("inbox", attempts + 1, policy.max_attempts, "rpc_timeout", delay)
            await asyncio.sleep(delay)


async def send_once(config: Config, payload: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    from_stream_id = payload.get("from_stream_id") if isinstance(payload.get("from_stream_id"), str) else None
    if from_stream_id and "stream_token" not in payload and _stream_token_from_env():
        payload["stream_token"] = _stream_token_from_env()
    if isinstance(payload.get("inbox"), dict):
        validate_inbox(payload["inbox"])
    payload["type"] = "send"
    payload.setdefault("request_id", f"send-{uuid.uuid4()}")
    return await _one_shot_rpc(config, payload, prefix="send", timeout=timeout, from_stream_id=from_stream_id)


async def send_receipt_once(
    config: Config,
    stream_id: str,
    request_id: str,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Read the daemon's zero-or-one latest receipt projection for a send."""
    target = str(stream_id or "").strip()
    receipt_request = str(request_id or "").strip()
    if ":" not in target or not receipt_request:
        raise ValueError("invalid_send_receipt_query")
    # The generic one-shot RPC hello intentionally scopes visibility to its
    # internal sentinel. Receipt lookup must use the same normal client
    # visibility projection as chat events, or every actual target is hidden.
    ws, _snapshot = await _connect_ready(config, timeout)
    try:
        await ws.send(json.dumps({
            "type": "send.receipt.get",
            "to_stream_id": target,
            "request_id": receipt_request,
        }, separators=(",", ":")))
        return await _read_rpc_response(
            ws, receipt_request, prefix="send.receipt.get",
            deadline=time.monotonic() + timeout,
        )
    finally:
        await ws.close()


async def spawn_once(
    config: Config, payload: dict[str, Any], *, timeout: float = SPAWN_RPC_TIMEOUT_DEFAULT_S,
) -> dict[str, Any]:
    parent_stream_id = payload.get("parent_stream_id") if isinstance(payload.get("parent_stream_id"), str) else None
    handoff_from_stream_id = (
        payload.get("handoff_from_stream_id") if isinstance(payload.get("handoff_from_stream_id"), str) else None
    )
    asserted_stream_id = handoff_from_stream_id or parent_stream_id
    if asserted_stream_id and "stream_token" not in payload and _stream_token_from_env():
        payload["stream_token"] = _stream_token_from_env()
    payload["type"] = "spawn"
    request_id = payload.setdefault("request_id", f"spawn-{uuid.uuid4()}")
    payload.setdefault("idempotency_key", request_id)
    response = await _one_shot_rpc(
        config,
        payload,
        prefix="spawn",
        timeout=timeout,
        from_stream_id=asserted_stream_id,
        infer_from_env=asserted_stream_id is not None,
    )
    if isinstance(response, dict) and response.get("type") == "spawn.ok" and response.get("state") == "starting":
        return await _await_starting_spawn(config, response, deadline=time.monotonic() + max(0.1, timeout))
    return response


async def _await_starting_spawn(
    config: Config, accepted: dict[str, Any], *, deadline: float,
) -> dict[str, Any]:
    """Wait once on the subscribed inventory stream after V2 admission."""
    stream_id = str(accepted.get("stream_id") or (accepted.get("session") or {}).get("stream_id") or "")
    if not stream_id:
        return accepted

    def indeterminate(session: dict[str, Any] | None, receipt: dict[str, Any]) -> dict[str, Any]:
        raw_receipt = receipt if isinstance(receipt, dict) else {}
        proof_watermark = raw_receipt.get("proof_watermark")
        proof_watermark_state = raw_receipt.get("proof_watermark_state")
        proof_watermark_reason = raw_receipt.get("proof_watermark_reason")
        delivery_failed_at = raw_receipt.get("delivery_failed_at")
        try:
            if not isinstance(delivery_failed_at, str) or not delivery_failed_at:
                raise ValueError
            datetime.fromisoformat(delivery_failed_at.replace("Z", "+00:00"))
        except ValueError:
            delivery_failed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        normalized_receipt = {
            **raw_receipt,
            "state": "indeterminate",
            "delivery_status": "indeterminate",
            "bootstrap_state": "starting",
            "proof_state": (
                raw_receipt.get("proof_state")
                if raw_receipt.get("proof_state") in {"pending", "unreachable"}
                else "pending"
            ),
            "proof_watermark": proof_watermark if type(proof_watermark) is int else None,
            "proof_watermark_state": (
                proof_watermark_state
                if proof_watermark_state in {"reachable", "unreachable"}
                else None
            ),
            "proof_watermark_reason": (
                proof_watermark_reason if isinstance(proof_watermark_reason, str) else None
            ),
            "failure_code": (
                raw_receipt.get("failure_code")
                if isinstance(raw_receipt.get("failure_code"), str) and raw_receipt["failure_code"]
                else "initial_prompt_delivery_unproven"
            ),
            "failure_reason": (
                raw_receipt.get("failure_reason")
                if isinstance(raw_receipt.get("failure_reason"), str) and raw_receipt["failure_reason"]
                else "initial prompt delivery remains unproven"
            ),
            "delivery_failed_at": delivery_failed_at,
        }
        return {
            "type": "spawn.indeterminate",
            "ok": True,
            "request_id": accepted.get("request_id"),
            "stream_id": stream_id,
            "state": "starting",
            "session": {
                **(session if isinstance(session, dict) else {}),
                "stream_id": stream_id,
                "state": "starting",
                "bootstrap_state": "starting",
            },
            "initial_prompt_delivery": normalized_receipt,
        }

    def open_row(session: dict[str, Any] | None) -> bool:
        return bool(
            isinstance(session, dict)
            and session.get("status") == "open"
            and session.get("closed_at") is None
        )

    receipt = accepted.get("initial_prompt_delivery")
    if isinstance(receipt, dict) and receipt.get("state") == "indeterminate":
        return indeterminate(
            accepted.get("session") if isinstance(accepted.get("session"), dict) else None,
            receipt,
        )

    async def durable_indeterminate(fallback_session: dict[str, Any] | None) -> dict[str, Any] | None:
        """Read the outcome store when inventory remains merely `starting`.

        Admission intentionally precedes prompt proof.  The terminal proof can
        therefore be durable without changing the open row's `starting`
        projection; consult the existing read-only await endpoint before
        returning an admitted success.
        """
        request_id = str(accepted.get("request_id") or "").strip()
        payload = {"spawn_request_id": request_id} if request_id else {"stream_id": stream_id}
        try:
            outcome = await await_spawn_once(
                config,
                payload,
                timeout=min(1.0, max(0.1, deadline - time.monotonic())),
            )
        except Exception:  # noqa: BLE001 - an unavailable readback is not proof
            return None
        session = outcome.get("session") if isinstance(outcome.get("session"), dict) else fallback_session
        outcome_receipt = outcome.get("initial_prompt_delivery")
        if not isinstance(outcome_receipt, dict):
            outcome_receipt = receipt if isinstance(receipt, dict) else {}
        return (
            indeterminate(session, outcome_receipt)
            if outcome_receipt.get("state") == "indeterminate" or outcome.get("pending_reconcile")
            else None
        )

    def inventory_session(message: dict[str, Any]) -> dict[str, Any] | None:
        sessions = message.get("sessions") if isinstance(message.get("sessions"), list) else []
        return next(
            (
                session for session in sessions
                if isinstance(session, dict) and session.get("stream_id") == stream_id
            ),
            None,
        )

    def terminal_inventory(message: dict[str, Any]) -> bool:
        session = inventory_session(message)
        return bool(session and session.get("state") in {"ready", "failed"})

    ws, snapshot = await _connect_ready(config, max(0.1, deadline - time.monotonic()))
    try:
        if terminal_inventory(snapshot):
            inventory = snapshot
        else:
            settled = await durable_indeterminate(inventory_session(snapshot))
            if settled is not None:
                return settled
            inventory = await _read_rpc_frame(
                ws, None, deadline=deadline, matches=terminal_inventory,
                timeout_message="spawn_state_timeout",
            )
    except TimeoutError:
        return await durable_indeterminate(inventory_session(snapshot)) or accepted
    finally:
        await ws.close()
    session = inventory_session(inventory)
    if not isinstance(session, dict):
        return accepted
    state = str(session.get("state") or "")
    if state == "ready":
        return {**accepted, "stream_id": stream_id, "state": state, "session": session}
    if open_row(session):
        return indeterminate(session, receipt if isinstance(receipt, dict) else {})
    return {
        "type": "spawn.error", "ok": False, "request_id": accepted.get("request_id"),
        "stream_id": stream_id, "error_code": session.get("error_code") or "spawn_failed",
        "error": session.get("error") or "spawn failed after admission", "session": session,
    }


async def schedule_once(config: Config, payload: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    from_stream_id = (
        payload.get("from_stream_id") if isinstance(payload.get("from_stream_id"), str)
        else payload.get("created_by_stream_id") if isinstance(payload.get("created_by_stream_id"), str)
        else None
    )
    if from_stream_id:
        payload.setdefault("from_stream_id", from_stream_id)
    if from_stream_id and "stream_token" not in payload and _stream_token_from_env():
        payload["stream_token"] = _stream_token_from_env()
    payload.setdefault("request_id", str(uuid.uuid4()))
    return await _one_shot_rpc(config, payload, prefix="schedule", timeout=timeout, from_stream_id=from_stream_id)


async def await_spawn_once(config: Config, payload: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    payload["type"] = "await_spawn"
    payload.setdefault("request_id", f"await-spawn-{uuid.uuid4()}")
    return await _one_shot_rpc(config, payload, prefix="await_spawn", timeout=timeout, response_timeout_extra=1.0)


async def send_cancel_once(config: Config, msg_id: int, *, timeout: float = 30.0) -> dict[str, Any]:
    request_id = f"send-cancel-{uuid.uuid4()}"
    return await _one_shot_rpc(
        config,
        {"type": "send.cancel", "request_id": request_id, "msg_id": int(msg_id)},
        prefix="send.cancel",
        timeout=timeout,
    )


async def set_visibility_once(
    config: Config,
    stream_id: str,
    visibility: str,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    if ":" not in stream_id:
        raise ValueError("invalid_stream_id")
    host, session_name = stream_id.split(":", 1)
    if not host or not session_name:
        raise ValueError("invalid_stream_id")
    request_id = f"visibility-{uuid.uuid4()}"
    return await _one_shot_rpc(
        config,
        {
            "type": "set_visibility",
            "request_id": request_id,
            "host": host,
            "session_name": session_name,
            "visibility": visibility,
        },
        prefix="set_visibility",
        timeout=timeout,
    )


async def spec_update_once(
    config: Config,
    stream_id: str,
    action: str,
    spec_id: str,
    *,
    timeout: float = 30.0,
    from_stream_id: str | None = None,
) -> dict[str, Any]:
    if ":" not in stream_id:
        raise ValueError("invalid_stream_id")
    host, session_name = stream_id.split(":", 1)
    if not host or not session_name:
        raise ValueError("invalid_stream_id")
    request_id = f"spec-{action}-{uuid.uuid4()}"
    payload: dict[str, Any] = {
        "type": "session.spec_update",
        "request_id": request_id,
        "host": host,
        "session_name": session_name,
        "action": action,
        "spec_id": spec_id,
    }
    if from_stream_id:
        payload["from_stream_id"] = from_stream_id
    if from_stream_id and _stream_token_from_env():
        payload["stream_token"] = _stream_token_from_env()
    return await _one_shot_rpc(config, payload, prefix="session.spec_update", timeout=timeout, from_stream_id=from_stream_id)


async def status_card_once(
    config: Config,
    stream_id: str,
    fields: dict[str, Any],
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Set/update the caller's own status card (self-write only)."""
    if ":" not in stream_id:
        raise ValueError("invalid_stream_id")
    host, session_name = stream_id.split(":", 1)
    if not host or not session_name:
        raise ValueError("invalid_stream_id")
    request_id = f"status-card-{uuid.uuid4()}"
    payload: dict[str, Any] = {
        "type": "status_card",
        "request_id": request_id,
        "host": host,
        "session_name": session_name,
        "from_stream_id": stream_id,
        **fields,
    }
    if _stream_token_from_env():
        payload["stream_token"] = _stream_token_from_env()
    return await _one_shot_rpc(config, payload, prefix="status_card", timeout=timeout, from_stream_id=stream_id)


async def drain_sessions_once(
    config: Config,
    stream_ids: list[str],
    *,
    timeout: float = 30.0,
    operator_confirm: bool = False,
) -> dict[str, Any]:
    if not stream_ids:
        raise ValueError("stream_ids_required")
    keys: list[dict[str, str]] = []
    seen: set[str] = set()
    for stream_id in stream_ids:
        if ":" not in stream_id:
            raise ValueError("invalid_stream_id")
        host, session_name = stream_id.split(":", 1)
        if not host or not session_name:
            raise ValueError("invalid_stream_id")
        if host == "*" or session_name == "*" or "*" in host or "*" in session_name:
            raise ValueError("wildcards_not_allowed")
        if stream_id in seen:
            continue
        seen.add(stream_id)
        keys.append({"host": host, "session_name": session_name})
    request_id = f"drain-sessions-{uuid.uuid4()}"
    payload: dict[str, Any] = {
        "type": "drain_sessions",
        "request_id": request_id,
        "keys": keys,
    }
    if operator_confirm:
        payload["operator_confirm"] = True
    return await _one_shot_rpc(
        config,
        payload,
        prefix="drain_sessions",
        timeout=timeout,
    )


async def reconcile_status_once(
    config: Config,
    *,
    host: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    request_id = f"reconcile-status-{uuid.uuid4()}"
    payload: dict[str, Any] = {
        "type": "reconcile.status",
        "request_id": request_id,
    }
    if host:
        payload["host"] = host
    return await _one_shot_rpc(
        config,
        payload,
        prefix="reconcile.status",
        timeout=timeout,
    )


async def rename_once(
    config: Config,
    host: str,
    session_name: str,
    display_name: str,
    *,
    source: str = "agent",
    timeout: float = 30.0,
) -> dict[str, Any]:
    if not host or not session_name:
        raise ValueError("invalid_stream_id")
    ws = await _connect_rpc_ready(config)
    request_id = f"rename-{uuid.uuid4()}"
    payload = {
        "type": "rename",
        "request_id": request_id,
        "host": host,
        "session_name": session_name,
        "display_name": display_name,
        "source": source,
    }
    try:
        await ws.send(json.dumps(payload, separators=(",", ":")))
        deadline = time.monotonic() + timeout
        return await _read_rpc_response(ws, request_id, prefix="rename", deadline=deadline)
    finally:
        await ws.close()
    raise TimeoutError("rename_timeout")


async def inspect_stream_once(
    config: Config,
    stream_id: str,
    *,
    msg_id: int | None = None,
    report_id: str | None = None,
    event_tail: int | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    request_id = f"inspect-{uuid.uuid4()}"
    payload: dict[str, Any] = {"type": "inspect_stream", "request_id": request_id, "stream_id": stream_id}
    if msg_id is not None:
        payload["msg_id"] = msg_id
    if report_id is not None:
        payload["report_id"] = report_id
    if event_tail is not None:
        payload["event_tail"] = event_tail
    return await _one_shot_rpc(config, payload, prefix="inspect_stream", timeout=timeout)


async def role_set_once(
    config: Config,
    stream_id: str,
    role: str,
    *,
    baseline_content: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Set a role on a live seat. Authenticated as the calling seat (env token),
    so the daemon can enforce the nexus authority gate. `baseline_content` is the
    frontmatter-stripped baseline the CLI loaded; None (missing file) delivers
    nothing and the daemon reports `role_baseline: null`."""
    if not stream_id or ":" not in stream_id:
        raise ValueError("invalid_stream_id")
    payload: dict[str, Any] = {
        "type": "role.set",
        "request_id": f"role-set-{uuid.uuid4()}",
        "stream_id": stream_id,
        "role": role,
    }
    if baseline_content is not None:
        payload["baseline_content"] = baseline_content
    from_stream_id = _attach_agent_identity(payload)
    return await _one_shot_rpc(
        config, payload, prefix="role.set", from_stream_id=from_stream_id, timeout=timeout,
    )


async def role_get_once(
    config: Config,
    stream_id: str,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    if not stream_id or ":" not in stream_id:
        raise ValueError("invalid_stream_id")
    payload: dict[str, Any] = {
        "type": "role.get",
        "request_id": f"role-get-{uuid.uuid4()}",
        "stream_id": stream_id,
    }
    return await _one_shot_rpc(config, payload, prefix="role.get", timeout=timeout)


async def spawn_catalog_get_once(
    config: Config,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    payload = {
        "type": "spawn_catalog_get",
        "request_id": f"spawn-catalog-{uuid.uuid4()}",
    }
    return await _one_shot_rpc(config, payload, prefix="spawn_catalog_get", timeout=timeout)


async def close_once(
    config: Config,
    stream_id: str,
    *,
    reason: str = "report_terminate",
    timeout: float = 30.0,
    operator_confirm: bool = False,
    force: bool = False,
    defer_if_working: bool = False,
    from_stream_id: str | None = None,
    caller_stream_id: str | None = None,
    progeny_stream_id: str | None = None,
    disposition_waived_reason: str | None = None,
) -> dict[str, Any]:
    if ":" not in stream_id:
        raise ValueError("invalid_stream_id")
    host, session_name = stream_id.split(":", 1)
    if not host or not session_name:
        raise ValueError("invalid_stream_id")
    ws = await _connect_rpc_ready(config, from_stream_id=from_stream_id or caller_stream_id)
    request_id = f"close-{uuid.uuid4()}"
    try:
        payload: dict[str, Any] = {
            "type": "close",
            "request_id": request_id,
            "host": host,
            "session_name": session_name,
            "reason": reason,
        }
        if operator_confirm:
            payload["operator_confirm"] = True
        if force:
            payload["force"] = True
        if defer_if_working:
            payload["defer_if_working"] = True
        if from_stream_id:
            payload["from_stream_id"] = from_stream_id
        if caller_stream_id is not None:
            payload["caller_stream_id"] = caller_stream_id
        if progeny_stream_id is not None:
            payload["progeny_stream_id"] = progeny_stream_id
        if disposition_waived_reason is not None:
            payload["disposition_waived_reason"] = disposition_waived_reason
        if (from_stream_id or caller_stream_id) and _stream_token_from_env():
            payload["stream_token"] = _stream_token_from_env()
        await ws.send(
            json.dumps(payload, separators=(",", ":"))
        )
        deadline = time.monotonic() + timeout
        return await _read_rpc_response(ws, request_id, prefix="close", deadline=deadline)
    finally:
        await ws.close()
    raise TimeoutError("close_timeout")


async def reparent_once(
    config: Config,
    worker_stream_id: str,
    new_parent_stream_id: str,
    *,
    reason: str = "reparent",
    timeout: float = 30.0,
    from_stream_id: str | None = None,
    caller_stream_id: str | None = None,
) -> dict[str, Any]:
    """Drive the daemon ``reparent`` RPC (P8). The worker is identified by
    ``host``/``session_name`` (split from its stream id, mirroring ``close``);
    ``new_parent_stream_id`` is the ``--to`` target. The caller's asserted
    stream id rides on ``from_stream_id`` (token-verified by the daemon) and is
    echoed in ``caller_stream_id`` for the audit actor.
    """
    if ":" not in worker_stream_id:
        raise ValueError("invalid_stream_id")
    host, session_name = worker_stream_id.split(":", 1)
    if not host or not session_name:
        raise ValueError("invalid_stream_id")
    if ":" not in new_parent_stream_id or not all(new_parent_stream_id.split(":", 1)):
        raise ValueError("invalid_new_parent_stream_id")
    ws = await _connect_rpc_ready(config, from_stream_id=from_stream_id or caller_stream_id)
    request_id = f"reparent-{uuid.uuid4()}"
    try:
        payload: dict[str, Any] = {
            "type": "reparent",
            "request_id": request_id,
            "host": host,
            "session_name": session_name,
            "new_parent_stream_id": new_parent_stream_id,
            "reason": reason,
        }
        if from_stream_id:
            payload["from_stream_id"] = from_stream_id
        if caller_stream_id is not None:
            payload["caller_stream_id"] = caller_stream_id
        if (from_stream_id or caller_stream_id) and _stream_token_from_env():
            payload["stream_token"] = _stream_token_from_env()
        await ws.send(json.dumps(payload, separators=(",", ":")))
        deadline = time.monotonic() + timeout
        return await _read_rpc_response(ws, request_id, prefix="reparent", deadline=deadline)
    finally:
        await ws.close()
    raise TimeoutError("reparent_timeout")


async def grant_token_once(
    config: Config,
    stream_id: str,
    *,
    timeout: float = 30.0,
    from_stream_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Bootstrap-once token mint for a pre-token session row.

    Sends `grant_token` RPC and returns the full daemon response (either
    `grant_token.ok` with the plaintext `stream_token`, or `grant_token.error`
    with `token_already_set`/`stream_unknown`/`invalid_request`). The caller is
    responsible for exporting the plaintext into the agent's pane env.
    """
    if ":" not in stream_id:
        raise ValueError("invalid_stream_id")
    host, session_name = stream_id.split(":", 1)
    if not host or not session_name:
        raise ValueError("invalid_stream_id")
    ws = await _connect_rpc_ready(config, from_stream_id=from_stream_id)
    request_id = request_id or f"grant-token-{uuid.uuid4()}"
    try:
        payload = {
            "type": "grant_token",
            "request_id": request_id,
            "stream_id": stream_id,
        }
        await ws.send(json.dumps(payload, separators=(",", ":")))
        deadline = time.monotonic() + timeout + 1.0
        return await _read_rpc_response(
            ws,
            request_id,
            prefix="grant_token",
            deadline=deadline,
            timeout_message="grant_token_timeout",
        )
    finally:
        await ws.close()
    raise TimeoutError("grant_token_timeout")


async def await_report_once(
    config: Config,
    stream_id: str,
    msg_id: int | None = None,
    *,
    timeout: float = 30.0,
    from_stream_id: str | None = None,
    ownership_token: str | None = None,
    include_details: bool = False,
    include_extras: bool = False,
    request_id: str | None = None,
) -> dict[str, Any]:
    if ":" not in stream_id:
        raise ValueError("invalid_stream_id")
    host, session_name = stream_id.split(":", 1)
    if not host or not session_name:
        raise ValueError("invalid_stream_id")
    # msg_id optional: None → stream mode (omit from the request). When given it
    # must be a valid non-negative int (msg_id mode, unchanged).
    if msg_id is not None and (not isinstance(msg_id, int) or isinstance(msg_id, bool) or msg_id < 0):
        raise ValueError("invalid_msg_id")
    request_id = request_id or f"await-report-{uuid.uuid4()}"
    payload: dict[str, Any] = {
        "type": "await_report",
        "request_id": request_id,
        "stream_id": stream_id,
        "timeout": timeout,
    }
    if msg_id is not None:
        payload["msg_id"] = msg_id
    if ownership_token is not None:
        payload["ownership_token"] = ownership_token
    if include_details:
        payload["include_details"] = True
    if include_extras:
        payload["include_extras"] = True
    return await _one_shot_rpc(
        config,
        payload,
        prefix="await_report",
        timeout=timeout,
        from_stream_id=from_stream_id,
        response_timeout_extra=1.0,
    )


async def upload_blob_once(
    config: Config,
    data: bytes,
    *,
    timeout: float = 30.0,
    request_id: str | None = None,
    chunk_size: int = 1024 * 1024,
) -> dict[str, Any]:
    ws = await _connect_rpc_ready(config)
    request_id = request_id or f"upload-{uuid.uuid4()}"
    try:
        await ws.send(json.dumps({"type": "upload_blob_init", "request_id": request_id, "size_hint_bytes": len(data)}, separators=(",", ":")))
        deadline = time.monotonic() + timeout
        init = await _read_rpc_frame(
            ws,
            request_id,
            deadline=deadline,
            matches=lambda message: message.get("type") == "upload_blob.init.ok"
            or str(message.get("type", "")).startswith("upload_blob.error"),
            timeout_message="upload_blob_timeout",
        )
        if str(init.get("type", "")).startswith("upload_blob.error"):
            return init
        offset = 0
        if not data:
            chunks = [b""]
        else:
            chunks = []
            while offset < len(data):
                chunks.append(data[offset : offset + chunk_size])
                offset += chunk_size
        for index, chunk in enumerate(chunks):
            await ws.send(
                json.dumps(
                    {
                        "type": "upload_blob_chunk",
                        "request_id": request_id,
                        "data_b64": base64.b64encode(chunk).decode("ascii"),
                        "final": index == len(chunks) - 1,
                    },
                    separators=(",", ":"),
                )
            )
            if index == len(chunks) - 1:
                return await _read_rpc_response(
                    ws,
                    request_id,
                    prefix="upload_blob",
                    deadline=deadline,
                    timeout_message="upload_blob_timeout",
                )
    finally:
        await ws.close()
    raise TimeoutError("upload_blob_timeout")


async def upload_prompt_blob_once(
    config: Config,
    data: bytes,
    *,
    timeout: float = 30.0,
    request_id: str | None = None,
    chunk_size: int = 1024 * 1024,
) -> dict[str, Any]:
    ws = await _connect_rpc_ready(config)
    request_id = request_id or f"upload-prompt-{uuid.uuid4()}"
    try:
        await ws.send(json.dumps({"type": "upload_prompt_blob_init", "request_id": request_id, "size_hint_bytes": len(data)}, separators=(",", ":")))
        deadline = time.monotonic() + timeout
        init = await _read_rpc_frame(
            ws,
            request_id,
            deadline=deadline,
            matches=lambda message: message.get("type") == "upload_prompt_blob.init.ok"
            or str(message.get("type", "")).startswith("upload_prompt_blob.error"),
            timeout_message="upload_prompt_blob_timeout",
        )
        if str(init.get("type", "")).startswith("upload_prompt_blob.error"):
            return init
        offset = 0
        chunks = [b""] if not data else []
        while offset < len(data):
            chunks.append(data[offset : offset + chunk_size])
            offset += chunk_size
        for index, chunk in enumerate(chunks):
            await ws.send(
                json.dumps(
                    {
                        "type": "upload_prompt_blob_chunk",
                        "request_id": request_id,
                        "data_b64": base64.b64encode(chunk).decode("ascii"),
                        "final": index == len(chunks) - 1,
                    },
                    separators=(",", ":"),
                )
            )
            if index == len(chunks) - 1:
                return await _read_rpc_response(
                    ws,
                    request_id,
                    prefix="upload_prompt_blob",
                    deadline=deadline,
                    timeout_message="upload_prompt_blob_timeout",
                )
    finally:
        await ws.close()
    raise TimeoutError("upload_prompt_blob_timeout")


async def _fetch_blob_once_attempt(config: Config, blob_sha: str, *, request_id: str, timeout: float) -> bytes:
    ws = await _connect_rpc_ready(config)
    chunks: list[bytes] = []
    try:
        await ws.send(json.dumps({"type": "fetch_blob", "request_id": request_id, "blob_sha": blob_sha}, separators=(",", ":")))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = await _read_rpc_frame(
                ws,
                request_id,
                deadline=deadline,
                matches=lambda candidate: str(candidate.get("type", "")).startswith("fetch_blob."),
                timeout_message="fetch_blob_timeout",
            )
            message_type = message.get("type")
            if message_type == "fetch_blob.error":
                raise FileNotFoundError(str(message.get("error_code") or "blob_unknown"))
            if message_type in {"fetch_blob.chunk", "fetch_blob.ok"} and message.get("content_b64"):
                chunks.append(base64.b64decode(str(message.get("content_b64"))))
            if message_type == "fetch_blob.ok" and message.get("final"):
                return b"".join(chunks)
    finally:
        await ws.close()
    raise TimeoutError("fetch_blob_timeout")


async def fetch_blob_once(config: Config, blob_sha: str, *, timeout: float = 30.0) -> bytes:
    request_id = f"fetch_blob-{uuid.uuid4()}"
    policy = _rpc_retry_policy_from_env(timeout)
    deadline = _retry_deadline(time.monotonic(), policy)
    attempts = 0
    last_exc: Exception | None = None
    while True:
        attempts += 1
        try:
            return await _fetch_blob_once_attempt(
                config,
                blob_sha,
                request_id=request_id,
                timeout=min(timeout, _retry_wait_timeout(deadline, attempts, policy.max_attempts)),
            )
        except FileNotFoundError:
            raise
        except (TimeoutError, OSError) as exc:
            last_exc = exc
            delay, giveup_reason = _retry_next_delay(policy, deadline, attempts)
            if delay is None:
                _log_rpc_giveup("fetch_blob", attempts, giveup_reason)
                raise TimeoutError(f"fetch_blob_timeout:{giveup_reason}:attempts={attempts}") from last_exc
            _log_rpc_retry("fetch_blob", attempts + 1, policy.max_attempts, "rpc_timeout", delay)
            await asyncio.sleep(delay)
