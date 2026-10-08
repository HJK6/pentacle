"""One configured external-work liveness check on the existing notice outbox.

The front desk attests what it read about work running outside the fleet; the
daemon keeps the deadlines and reminds the currently bound front desk through
the durable outbox. It never opens the queue, fetches mail or dispatches work.
Contract: docs/external-work.md.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
import re
import time

from assistant_composite import AssistantCompositeConfig
from outbound_notices import NOTICE_KIND_EXTERNAL_WORK_DUE, NoticeDecision

log = logging.getLogger("chat_streamd_v2.external_work")

CONFIG_ENV = "PENTACLE_EXTERNAL_WORK_CONFIG"
CONFIG_KEYS = frozenset({"v", "watch_id", "label", "queue_ref", "assistant_name"})
# The primary assistant: the only one whose front desk this check serves.
ASSISTANT_NAME = AssistantCompositeConfig.name
CHECK_INTERVAL_S = 7200
ACTION_INTERVAL_S = 3600
REMINDER_INTERVAL_S = 7200
OBSERVATION_MAX_AGE_S = 900
RECORD_MAX_BYTES = 16 * 1024
KNOWN_STATES = ("working", "idle", "waiting_on_fleet")
BLOCKED_STATES = ("idle", "waiting_on_fleet")
RECORD_KEYS = frozenset({
    "watch_id", "request_id", "expected_version", "observed_at", "state", "current_packet_ref",
    "evidence_refs", "queue_sha256", "next_packet_ref", "supply_gap", "blocker",
})
OBSERVATION_KEYS = tuple(sorted(RECORD_KEYS - {"watch_id", "expected_version"}))
STATE_KEYS = frozenset({
    "v", "created_at", "last_verified_at", "observation", "blocked_since", "episode", "active_reasons",
    "notice_sequence", "last_notice_at", "active_notice_id", "active_recipient", "clock_high_water",
})
ERROR_CODES = frozenset({
    "not_authenticated", "fd_not_current", "unavailable", "disabled", "config_invalid", "invalid_request",
    "observation_stale", "idempotency_conflict", "version_conflict",
})
_WATCH_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ExternalWorkConfig:
    """`status` is `disabled` (unset), `config_invalid`, or `enabled`."""

    status: str = "disabled"
    watch_id: str = ""
    label: str = ""
    queue_ref: str = ""
    assistant_name: str = ASSISTANT_NAME

    @property
    def enabled(self) -> bool:
        return self.status == "enabled"


def _text(value, limit):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= limit
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def parse_config(raw) -> ExternalWorkConfig:
    if (not isinstance(raw, dict) or set(raw) != CONFIG_KEYS or type(raw["v"]) is not int or raw["v"] != 1
            or not isinstance(raw["watch_id"], str) or not _WATCH_ID.fullmatch(raw["watch_id"])
            or not _text(raw["label"], 80) or not _text(raw["queue_ref"], 512)
            or raw["assistant_name"] != ASSISTANT_NAME):
        raise ValueError("config_invalid")
    return ExternalWorkConfig("enabled", raw["watch_id"], raw["label"], raw["queue_ref"], ASSISTANT_NAME)


def load_config(environ=None) -> ExternalWorkConfig:
    """Read the private config once. A bad file disables the check visibly, never the daemon."""
    path = str((os.environ if environ is None else environ).get(CONFIG_ENV) or "").strip()
    if not path:
        return ExternalWorkConfig("disabled")
    try:
        with open(path, encoding="utf-8") as handle:
            return parse_config(json.load(handle))
    except (OSError, ValueError):
        # Fixed text: neither the path nor the file content is logged.
        log.error("subsystem=external_work error=config_invalid action=disabled")
        return ExternalWorkConfig("config_invalid")


def validate_record(record) -> dict:
    """Shape-check one front-desk observation. Any defect is `invalid_request`."""
    try:
        if not isinstance(record, dict) or set(record) != RECORD_KEYS:
            raise ValueError
        if len(canonical(record).encode()) > RECORD_MAX_BYTES:
            raise ValueError
        version, state, refs = record["expected_version"], record["state"], record["evidence_refs"]
        blocker = record["blocker"]
        optional = [record[key] for key in ("current_packet_ref", "next_packet_ref", "supply_gap")]
        if (not isinstance(record["watch_id"], str) or not _WATCH_ID.fullmatch(record["watch_id"])
                or not _text(record["request_id"], 128)
                or not isinstance(version, int) or isinstance(version, bool) or version < 0
                or not _number(record["observed_at"])
                or state not in (*KNOWN_STATES, "unknown")
                or any(value is not None and not _text(value, 512) for value in optional)
                or not isinstance(refs, list) or len(refs) > 10
                or any(not _text(ref, 512) for ref in refs) or len(set(refs)) != len(refs)
                or not isinstance(record["queue_sha256"], str) or not _SHA256.fullmatch(record["queue_sha256"])
                or (record["next_packet_ref"] is None) == (record["supply_gap"] is None)):
            raise ValueError
        if blocker is not None and (not isinstance(blocker, dict) or set(blocker) != {"owner", "reason"}
                                    or not _text(blocker["owner"], 256) or not _text(blocker["reason"], 256)):
            raise ValueError
        if ((state in KNOWN_STATES and not refs) or (state == "working" and record["current_packet_ref"] is None)
                or (state == "waiting_on_fleet" and blocker is None)):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("invalid_request") from None
    return record


def record_digest(record) -> str:
    return hashlib.sha256(canonical(record).encode()).hexdigest()


def new_state(now) -> dict:
    return {"v": 1, "created_at": now, "last_verified_at": None, "observation": None, "blocked_since": None,
            "episode": 0, "active_reasons": [], "notice_sequence": 0, "last_notice_at": None,
            "active_notice_id": None, "active_recipient": None, "clock_high_water": now}


def derive_reasons(state, now) -> list[str]:
    observation, verified, blocked = state["observation"], state["last_verified_at"], state["blocked_since"]
    reasons = []
    if blocked is not None and now >= blocked + ACTION_INTERVAL_S:
        reasons.append("action_due")
    if verified is None or now >= verified + CHECK_INTERVAL_S:
        reasons.append("check_due")
    if observation is None or observation["state"] == "unknown":
        reasons.append("state_unknown")
    if observation is None or observation["next_packet_ref"] is None:
        reasons.append("supply_gap")
    return reasons


def due_at(state, now):
    if state["active_reasons"]:
        return now
    deadlines = [state["last_verified_at"] + CHECK_INTERVAL_S]
    if state["blocked_since"] is not None:
        deadlines.append(state["blocked_since"] + ACTION_INTERVAL_S)
    return min(deadlines)


def apply_observation(state, record) -> None:
    """Only evidence-backed `working` clears the blocked clock; `unknown` verifies nothing."""
    kind, observed_at = record["state"], record["observed_at"]
    if kind != "unknown":
        state["last_verified_at"] = observed_at
    if kind in BLOCKED_STATES and state["blocked_since"] is None:
        state["blocked_since"] = observed_at
    elif kind == "working":
        state["blocked_since"] = None
    state["observation"] = {key: record[key] for key in OBSERVATION_KEYS}


def notice_id(watch_id, episode, sequence, recipient) -> str:
    identity = "\0".join((watch_id, str(episode), str(sequence), recipient["stream_id"], recipient["generation"]))
    return "xw:" + hashlib.sha256(identity.encode()).hexdigest()


def install(server, store, sessions, outbound, composite, environ=None) -> "ExternalWork":
    """Daemon startup wiring: load the private config once and serve the verbs."""
    runtime = ExternalWork(store, sessions, outbound, config=load_config(environ),
                           env_binding=composite._env_binding)
    server.external_work = runtime
    server.handlers.update(runtime.wire_handlers())
    return runtime


class ExternalWork:
    """Wire verbs, the outbox tick and the delivery guard for the one configured watch."""

    def __init__(self, store, sessions, outbound=None, *, config, env_binding=None, clock=time.time):
        self.store, self.sessions, self.config, self.clock = store, sessions, config, clock
        self._env_binding = env_binding
        if outbound is not None:
            # Registered even when disabled, so a queued reminder is refused rather than delivered.
            outbound.register_kind(NOTICE_KIND_EXTERNAL_WORK_DUE, guard=self.delivery_guard,
                                   lock_factory=self.delivery_locks)
            outbound.external_work = self

    def env_binding(self) -> dict:
        return dict(self._env_binding() or {}) if callable(self._env_binding) else {}

    def wire_handlers(self):
        return {"external_work.show": self.handle, "external_work.record": self.handle}

    async def handle(self, message):
        verb = str(message.get("type") or "").rpartition(".")[2]
        request_id, record = message.get("request_id"), message.get("record")
        if request_id is None and isinstance(record, dict) and isinstance(record.get("request_id"), str):
            request_id = record["request_id"]
        try:
            auth = message.get("_auth_context") or {}
            actor = auth.get("stream_id")
            if not auth.get("token_verified") or not actor or message.get("from_stream_id") not in (None, "", actor):
                raise ValueError("not_authenticated")
            if not self.store:
                raise ValueError("unavailable")
            host, _, name = str(actor).partition(":")
            session = await self.store.fetch_session(host, name)
            token = message.get("stream_token")
            if (not session or session["status"] != "open" or not isinstance(token, str)
                    or hashlib.sha256(token.encode()).hexdigest() != session.get("token_hash")):
                raise ValueError("not_authenticated")
            caller = dict(config=self.config, env_binding=self.env_binding(), actor=actor,
                          generation=session["session_generation"], now=self.clock())
            if verb == "show":
                result = await self.store.external_work_show(**caller)
            else:
                if validate_record(record)["request_id"] != request_id:
                    raise ValueError("invalid_request")
                result = await self.store.external_work_record(record=record, **caller)
            reply = {"type": f"external_work.{verb}.ok", "ok": True, **result}
            if request_id is not None:
                reply.setdefault("request_id", request_id)
            return reply
        except ValueError as exc:
            code = str(exc) if str(exc) in ERROR_CODES else "unavailable"
            reply = {"type": "external_work.error", "ok": False, "error_code": code}
            if request_id is not None:
                reply["request_id"] = request_id
            return reply

    async def tick(self):
        """One bounded store transaction on the existing outbox pass; no network or file reads."""
        if not self.config.enabled or not self.store:
            return None
        return await self.store.external_work_tick(
            config=self.config, env_binding=self.env_binding(), now=self.clock())

    @asynccontextmanager
    async def delivery_locks(self, row):
        # The lock session close/replacement/rebind take, as for the other root-bound notices.
        async with self.store.routing_integrity_lifecycle_lock(row["recipient_stream_id"]):
            yield

    async def delivery_guard(self, row):
        verdict = await self.store.external_work_notice_verdict(
            row["notice_id"], config=self.config, env_binding=self.env_binding())
        if verdict["code"] != "ok":
            return NoticeDecision.terminal(verdict["code"], "run agent-orch external-work show")
        stream_id, generation = row["recipient_stream_id"], verdict["generation"]
        host, _, name = stream_id.partition(":")
        desk = await self.store.fetch_session(host, name)
        if (not desk or desk.get("status") != "open" or desk.get("session_generation") != generation
                or desk.get("offline_since_ts") or desk.get("presumed_dead_at")
                or desk.get("pane_status") == "pane_dead"):
            return NoticeDecision.terminal("external_work_recipient_unavailable", "wait for the current front desk")
        live = self.sessions.get(stream_id) or {}
        if (desk.get("pane_status") != "pane_alive" or live.get("session_generation") != generation
                or live.get("online") is not True or live.get("pane_status") != "pane_alive"):
            return NoticeDecision.defer("external_work_recipient_unverified", "wait for a live pane observation")
        return None
