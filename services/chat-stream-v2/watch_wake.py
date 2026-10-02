"""Agent-only wake/watch RPCs and reconciler callback; delivery stays in outbox."""
from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
import hashlib
import json
import os
import re
import time

from outbound_notices import NoticeDecision


def _fleet_observation_known(row):
    """Distinguish a captured idle state from inventory's boot-time false default."""
    generation = str(row.get("session_generation") or "")
    if not generation or not isinstance(row.get("working"), bool):
        return False
    return row.get("capture_generation") == generation and row.get("capture_liveness") == "idle"


def registration_payload(kind, message, now):
    payload = {"request_id": message.get("request_id")}
    if kind == "wake":
        relative, absolute = message.get("in"), message.get("at")
        if (relative is None) == (absolute is None):
            raise ValueError("exactly_one_time_required")
        if relative is not None:
            match = re.fullmatch(r"([1-9][0-9]*)([smhd])", str(relative))
            if not match:
                raise ValueError("invalid_duration")
            delay = int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
            # datetime's range is the durable wire timestamp range.
            try:
                datetime.fromtimestamp(now + delay)
            except (ValueError, OverflowError, OSError):
                raise ValueError("invalid_duration") from None
            payload["delay_seconds"] = delay
        else:
            if not isinstance(absolute, str) or not re.fullmatch(
                r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", absolute
            ):
                raise ValueError("invalid_timestamp")
            try:
                payload["due_at"] = datetime.fromisoformat(absolute.replace("Z", "+00:00")).timestamp()
            except ValueError:
                raise ValueError("invalid_timestamp") from None
        if not isinstance(message.get("urgent", False), bool) or not isinstance(message.get("note", ""), str):
            raise ValueError("invalid_request")
        payload.update(note=message.get("note", ""), urgent=message.get("urgent", False))
    else:
        raw = message.get("on")
        triggers = raw.split(",") if isinstance(raw, str) else raw
        if not isinstance(triggers, list) or not triggers or any(
            not isinstance(t, str) or not (t in {"idle", "end", "blocker"} or re.fullmatch(r"quiet=[1-9][0-9]*", t))
            for t in triggers
        ):
            raise ValueError("invalid_triggers")
        try:
            for trigger in triggers:
                if trigger.startswith("quiet="):
                    datetime.fromtimestamp(now + int(trigger.split("=")[1]) * 60)
        except (ValueError, OverflowError, OSError):
            raise ValueError("invalid_triggers") from None
        if not isinstance(message.get("repeat", False), bool):
            raise ValueError("invalid_request")
        payload.update(child_stream_id=message.get("child_stream_id"),
                       triggers=list(dict.fromkeys(triggers)), repeat=message.get("repeat", False))
    return payload


class WatchWake:
    def __init__(self, store, sessions, outbound=None, *, clock=time.time, root_binding=None):
        self.store, self.sessions, self.clock = store, sessions, clock
        self.root_binding = root_binding
        if outbound is not None:
            for kind in ("watch", "wake", "wake_urgent", "wake_missed", "report", "reconciler", "tree_idle", "lane_digest"):
                outbound.register_kind(kind, guard=self.delivery_guard, lock_factory=self.delivery_locks)

    def wire_handlers(self):
        return {f"{kind}.{verb}": self.handle for kind in ("wake", "watch")
                for verb in ("register", "list", "cancel")}

    async def handle(self, message):
        kind, verb = message["type"].split(".")
        try:
            auth = message.get("_auth_context") or {}
            owner = auth.get("stream_id")
            if not auth.get("token_verified") or not owner:
                raise ValueError("not_authenticated")
            for field in ("from_stream_id", "watcher_stream_id", "owner_stream_id", "actor_stream_id"):
                if message.get(field) and message[field] != owner:
                    raise ValueError("caller_mismatch")
            if not self.store:
                raise ValueError("unavailable")
            host, _, name = owner.partition(":")
            session = await self.store.fetch_session(host, name)
            if not session or session["status"] != "open":
                raise ValueError("stale_generation")
            token = message.get("stream_token")
            if not isinstance(token, str) or hashlib.sha256(token.encode()).hexdigest() != session.get("token_hash"):
                raise ValueError("stale_generation")
            generation = session["session_generation"]
            if verb == "register":
                payload = registration_payload(kind, message, self.clock())
                result = await self.store.register_watch_wake(kind, owner, generation, payload, now=self.clock())
                response = {kind: result}
            elif verb == "list":
                response = {("watches" if kind == "watch" else "wakes"): await self.store.list_watch_wake(kind, owner, generation)}
            else:
                if not message.get("request_id"):
                    raise ValueError("request_id_required")
                response = await self.store.cancel_watch_wake(kind, owner, generation, message.get("id"),
                                                             request_id=message["request_id"])
            return {"type": f"{kind}.{verb}.ok", "ok": True, "request_id": message.get("request_id"), **response}
        except ValueError as exc:
            return {"type": f"{kind}.error", "ok": False, "error_code": str(exc), "request_id": message.get("request_id")}

    async def tick(self):
        observations = {}
        for row in self.sessions.list_open():
            if str(row.get("provider") or "") == "composite":
                continue
            observation = {**row, "_fleet_observed": _fleet_observation_known(row)}
            observations[f"{row['host']}:{row['session_name']}"] = observation
        binding = self.root_binding() if callable(self.root_binding) else None
        await self.store.evaluate_watch_wake(observations, now=self.clock(), root_binding=binding)

    async def missed_wake_alarm(self):
        """Separate callback, run before `tick`, so it still runs when `tick` raises."""
        grace = os.environ.get("PENTACLE_MISSED_WAKE_S", "")
        await self.store.missed_wake_alarm(now=self.clock(), grace_s=int(grace) if grace.isdigit() else 300)

    @asynccontextmanager
    async def delivery_locks(self, row):
        # Same locks used by session close/replacement/reparent. Sorted lock
        # order also handles a seat being both parent and child concurrently.
        async with AsyncExitStack() as stack:
            for sid in sorted({row.get("recipient_stream_id"), row.get("source_stream_id")} - {None, ""}):
                await stack.enter_async_context(self.store.routing_integrity_lifecycle_lock(sid))
            yield

    async def delivery_guard(self, row):
        if row.get("kind") in ("tree_idle", "lane_digest"):
            binding = self.root_binding() if callable(self.root_binding) else None
            metadata = row.get("metadata")
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            env = "PENTACLE_TREE_IDLE_S" if row["kind"] == "tree_idle" else "PENTACLE_LANE_DIGEST_S"
            if (not binding or tuple(binding) != (row.get("recipient_stream_id"), (metadata or {}).get("root_generation"))
                    or os.environ.get(env) == "0"):
                return NoticeDecision("terminal", "fleet_root_rebound_or_disabled", "use current root generation")
            host, _, name = row["recipient_stream_id"].partition(":")
            root = await self.store.fetch_session(host, name)
            if (not root or root.get("status") != "open"
                    or root.get("session_generation") != binding[1]
                    or root.get("offline_since_ts") or root.get("presumed_dead_at")
                    or root.get("pane_status") == "pane_dead"):
                return NoticeDecision("terminal", "fleet_root_unavailable", "wait for current live root")
            if root.get("pane_status") != "pane_alive":
                return NoticeDecision.retry("fleet_root_unverified", "wait for a live pane observation")
            live_root = self.sessions.get(row["recipient_stream_id"]) or {}
            if (live_root.get("session_generation") != binding[1]
                    or live_root.get("online") is not True
                    or live_root.get("pane_status") != "pane_alive"):
                return NoticeDecision.retry("fleet_root_unverified", "wait for a live pane observation")
        if not await self.store.watch_notice_valid(row["notice_id"]):
            return NoticeDecision("terminal", "watch_lifecycle_retired", "register work in the current generation")
        return None


async def run_reconcile_callbacks(*callbacks):
    """One failed callback never skips pin drift, question expiry or watches."""
    import logging
    for callback in callbacks:
        try:
            await callback()
        except Exception:
            logging.getLogger(__name__).exception("reconciler callback failed: %s", callback.__qualname__)
