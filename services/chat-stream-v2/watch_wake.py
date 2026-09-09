"""Agent-only wake/watch RPCs and reconciler callback; delivery stays in outbox."""
from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
import hashlib
import re
import time

from outbound_notices import NoticeDecision


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
    def __init__(self, store, sessions, outbound=None, *, clock=time.time):
        self.store, self.sessions, self.clock = store, sessions, clock
        if outbound is not None:
            for kind in ("watch", "wake", "wake_urgent", "report", "reconciler"):
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
        observations = {f"{r['host']}:{r['session_name']}": r for r in self.sessions.list_open()}
        await self.store.evaluate_watch_wake(observations, now=self.clock())

    @asynccontextmanager
    async def delivery_locks(self, row):
        # Same locks used by session close/replacement/reparent. Sorted lock
        # order also handles a seat being both parent and child concurrently.
        async with AsyncExitStack() as stack:
            for sid in sorted({row.get("recipient_stream_id"), row.get("source_stream_id")} - {None, ""}):
                await stack.enter_async_context(self.store.routing_integrity_lifecycle_lock(sid))
            yield

    async def delivery_guard(self, row):
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
