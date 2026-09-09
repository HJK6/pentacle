"""Durable daemon-originated outbound notices.

The v2 daemon has one outbound-notice substrate for messages it creates on its
own behalf. Producers persist a notice before attempting delivery; this module
owns the claim/lease, retry, terminal-state, and recipient-pane recovery loop.
Interactive ``tell`` calls still use :class:`comms.Comms` directly, but daemon
notices enter here so a restart cannot lose a report or reconciler tell.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
import time
from typing import Any, Awaitable, Callable, Iterable
import uuid

from submission_events import COMMITTED_PENDING_PROOF_STATUSES, PROOF_TERMINAL_BOUND_S
from v2_runtime import env_number

log = logging.getLogger("chat_streamd_v2.outbound_notices")

NOTICE_KIND_REPORT = "report"
NOTICE_KIND_RECONCILER = "reconciler"
NOTICE_KIND_SPAWN_FAILURE = "spawn_failure"

_NON_URGENT_KINDS = frozenset({
    NOTICE_KIND_REPORT,
    NOTICE_KIND_RECONCILER,
    NOTICE_KIND_SPAWN_FAILURE,
    "watch", "wake",
})

_TERMINAL_CODES = frozenset({
    "bad_request",
    "from_stream_id_forbidden",
    "tell_id_conflict",
    "unsupported_host",
    "unknown_session",
})

# Delivery policy is deliberately fixed in the module. Only the lease and
# retry budget are fleet-level controls: those values depend on recipient
# latency and the acceptable terminal-failure boundary, respectively.
NOTICE_INTERVAL_S = 5.0
NOTICE_MAX_PER_PASS = 32
NOTICE_LEASE_S = 30.0
NOTICE_BACKOFF_BASE_S = 2.0
NOTICE_BACKOFF_MAX_S = 60.0
NOTICE_MAX_ATTEMPTS = 10
NOTICE_FIRST_DELAY_S = 0.0


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch_from_iso(value: object) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def notice_needle(tell_id: str) -> str:
    """Return the stable pane marker used to prove a prior paste landed."""
    return f"[pentacle-notice:{tell_id}]"


def ensure_notice_marker(tell_id: str, body: str) -> str:
    """Make the tell id observable in the recipient capture exactly once."""
    marker = notice_needle(tell_id)
    text = str(body or "")
    return text if marker in text else f"{marker}\n{text}"


@dataclass(frozen=True)
class OutboundNoticeConfig:
    lease_s: float = NOTICE_LEASE_S
    max_attempts: int = NOTICE_MAX_ATTEMPTS

    @classmethod
    def from_env(cls) -> "OutboundNoticeConfig":
        return cls(
            lease_s=max(
                0.1,
                env_number(os.environ, "PENTACLE_OUTBOUND_NOTICE_LEASE_S", cls.lease_s, float),
            ),
            max_attempts=max(
                1,
                env_number(
                    os.environ, "PENTACLE_OUTBOUND_NOTICE_MAX_ATTEMPTS", cls.max_attempts, int,
                ),
            ),
        )


@dataclass(frozen=True)
class NoticeDecision:
    """A producer-specific pre-delivery decision."""

    action: str = "deliver"
    reason: str = ""
    next_action: str = ""

    @classmethod
    def retry(cls, reason: str, next_action: str = "") -> "NoticeDecision":
        return cls("retry", reason, next_action)

    @classmethod
    def terminal(cls, reason: str, next_action: str = "") -> "NoticeDecision":
        return cls("terminal", reason, next_action)


class OutboundNoticeConflict(ValueError):
    """A dedupe key was reused for a different recipient or payload."""


Guard = Callable[[dict[str, Any]], Awaitable[NoticeDecision | None]]
TerminalCallback = Callable[[dict[str, Any], str, str], Awaitable[None]]
LockFactory = Callable[[dict[str, Any]], Any]


class OutboundNoticeQueue:
    """Single durable producer/consumer queue for daemon-originated tells."""

    def __init__(
        self,
        store: Any,
        comms: Any = None,
        *,
        config: OutboundNoticeConfig | None = None,
        owner: str | None = None,
    ) -> None:
        self.store = store
        self.comms = comms
        self.config = config or OutboundNoticeConfig.from_env()
        self.owner = owner or f"outbound:{os.getpid()}:{uuid.uuid4().hex}"
        self._guards: dict[str, Guard] = {}
        self._terminal_callbacks: dict[str, TerminalCallback] = {}
        self._lock_factories: dict[str, LockFactory] = {}

    def register_kind(
        self,
        kind: str,
        *,
        guard: Guard | None = None,
        on_terminal: TerminalCallback | None = None,
        lock_factory: LockFactory | None = None,
    ) -> None:
        if guard is not None:
            self._guards[kind] = guard
        if on_terminal is not None:
            self._terminal_callbacks[kind] = on_terminal
        if lock_factory is not None:
            self._lock_factories[kind] = lock_factory

    async def enqueue(
        self,
        *,
        kind: str,
        dedupe_key: str,
        recipient_stream_id: str,
        body: str,
        tell_id: str,
        source_stream_id: str | None = None,
        episode_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: str | None = None,
        watch_fact: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a notice before any delivery attempt is scheduled."""
        marked_body = ensure_notice_marker(tell_id, body)
        try:
            return await self.store.enqueue_outbound_notice(
                notice_id=tell_id,
                kind=kind,
                dedupe_key=dedupe_key,
                recipient_stream_id=recipient_stream_id,
                tell_id=tell_id,
                body=marked_body,
                source_stream_id=source_stream_id,
                episode_id=episode_id,
                metadata=metadata,
                created_at=created_at,
                watch_fact=watch_fact,
            )
        except ValueError as exc:
            if "outbound_notice_conflict" in str(exc):
                raise OutboundNoticeConflict(str(exc)) from exc
            raise

    async def deliver_now(self, notice_id: str, *, lock_held: bool = False) -> bool:
        """Claim and attempt one notice immediately, ignoring its backoff timer."""
        row = await self.store.claim_outbound_notice(
            notice_id,
            owner=self.owner,
            lease_s=self.config.lease_s,
            force=True,
        )
        if row is None:
            return False
        return (await self._deliver_claimed(row, lock_held=lock_held)) == "delivered"

    async def drain_once(
        self,
        *,
        limit: int | None = None,
        kinds: Iterable[str] | None = None,
        force: bool = False,
    ) -> int:
        """Claim due notices atomically and process one bounded batch."""
        cap = max(1, int(limit or NOTICE_MAX_PER_PASS))
        kind_set = set(kinds) if kinds is not None else None
        notice_ids = await self.store.list_outbound_notice_ids(
            limit=cap,
            now=time.time(),
            kinds=kind_set,
            force=force,
        )
        completed = 0
        for notice_id in notice_ids:
            row = await self.store.claim_outbound_notice(
                notice_id,
                owner=self.owner,
                lease_s=self.config.lease_s,
                force=force,
            )
            if row is None:
                continue
            if (await self._deliver_claimed(row)) == "delivered":
                completed += 1
        return completed

    async def _deliver_claimed(self, row: dict[str, Any], *, lock_held: bool = False) -> str:
        kind = str(row.get("kind") or "")
        if not lock_held:
            lock_factory = self._lock_factories.get(kind)
            if lock_factory is not None:
                async with lock_factory(row):
                    return await self._deliver_claimed(row, lock_held=True)
        guard = self._guards.get(kind)
        if guard is not None:
            try:
                decision = await guard(row)
            except Exception as exc:  # noqa: BLE001 - guard failure is retryable
                return await self._retry(
                    row,
                    f"notice guard failed: {exc}",
                    "retry the guarded notice",
                )
            if decision is not None and decision.action != "deliver":
                if decision.action == "terminal":
                    await self._terminal(row, decision.reason, decision.next_action)
                    return "terminal"
                return await self._retry(row, decision.reason, decision.next_action)

        try:
            if self.comms is None:
                raise RuntimeError("outbound transport unavailable")
            message = {
                "tell_id": str(row["tell_id"]),
                "stream_id": str(row["recipient_stream_id"]),
                "to_stream_id": str(row["recipient_stream_id"]),
                "message": str(row["body"]),
                # Report and reconciler notices are informational queue
                # deliveries; only an explicitly urgent notice may interrupt
                # an in-flight provider turn.
                "urgent": kind not in _NON_URGENT_KINDS,
            }
            deliver_notice = getattr(self.comms, "deliver_outbound_notice", None)
            if callable(deliver_notice):
                reply = await deliver_notice(
                    message,
                    check_existing=int(row.get("attempts") or 0) > 1,
                )
            else:
                # Small in-process test transports and older adapters expose
                # only the established tell seam. Production Comms always has
                # deliver_outbound_notice, which is where marker scans live.
                reply = await self.comms.tell(message)
        except Exception as exc:  # noqa: BLE001 - durable failure state below
            code = str(getattr(exc, "code", "") or "")
            reason = str(exc)[:400]
            if code in _TERMINAL_CODES:
                await self._terminal(row, reason, "inspect the terminal notice reason and recipient lifecycle")
                return "terminal"
            return await self._retry(row, reason, "retry after the recorded backoff")

        if not (isinstance(reply, dict) and reply.get("delivery_status") == "delivered"):
            proof_pending = (
                isinstance(reply, dict)
                and reply.get("delivery_status") in COMMITTED_PENDING_PROOF_STATUSES
            )
            authoritative_negative = proof_pending and reply.get("proof_state") == "pending"
            return await self._retry(
                row,
                "submission_proof_pending" if proof_pending else "submission_unconfirmed",
                "retry evidence-only recipient readback",
                proof_pending=proof_pending,
                authoritative_negative=authoritative_negative,
            )

        await self.store.complete_outbound_notice(str(row["notice_id"]), owner=self.owner)
        return "delivered"

    async def _retry(
        self, row: dict[str, Any], error: str, next_action: str, *, proof_pending: bool = False,
        authoritative_negative: bool = False,
    ) -> str:
        attempts = max(1, int(row.get("attempts") or 1))
        if attempts >= self.config.max_attempts:
            if proof_pending and not (
                authoritative_negative and await self._terminal_proof_negative(row)
            ):
                attempts = self.config.max_attempts - 1
            else:
                await self._terminal(
                    row,
                    "retry_budget_exhausted: " + error,
                    "inspect transport and replay deliberately",
                )
                return "terminal"
        delay = min(
            NOTICE_BACKOFF_MAX_S,
            NOTICE_BACKOFF_BASE_S * (2 ** max(0, attempts - 1)),
        )
        await self.store.fail_outbound_notice(
            str(row["notice_id"]),
            owner=self.owner,
            error=error,
            next_attempt_at=time.time() + delay,
            next_action=next_action,
        )
        return "retry"

    async def _terminal_proof_negative(self, row: dict[str, Any]) -> bool:
        """A missing event is terminal only after time and lifecycle evidence."""
        created = _epoch_from_iso(row.get("created_at"))
        if created is None or time.time() - created < PROOF_TERMINAL_BOUND_S:
            return False
        stream_id = str(row.get("recipient_stream_id") or "")
        host, sep, name = stream_id.partition(":")
        if not sep or not host or not name:
            return False
        recipient = await self.store.fetch_session(host, name)
        return isinstance(recipient, dict) and str(recipient.get("status") or "") == "closed"

    async def _terminal(self, row: dict[str, Any], reason: str, next_action: str) -> None:
        await self.store.terminal_outbound_notice(
            str(row["notice_id"]),
            owner=self.owner,
            reason=reason or "terminal outbound notice failure",
            next_action=next_action or "inspect the outbound notice row",
        )
        callback = self._terminal_callbacks.get(str(row.get("kind") or ""))
        if callback is not None:
            try:
                await callback(row, reason, next_action)
            except Exception:  # noqa: BLE001 - terminal state is already durable
                log.exception("outbound notice terminal callback failed notice=%s", row.get("notice_id"))

    async def run_forever(self) -> None:
        """Restart sweep plus bounded periodic retry loop."""
        failures = 0
        delay = NOTICE_FIRST_DELAY_S
        while True:
            try:
                if delay:
                    await asyncio.sleep(delay)
                await self.drain_once()
                failures = 0
                delay = NOTICE_INTERVAL_S
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad pass must not kill retries
                failures += 1
                delay = min(
                    NOTICE_BACKOFF_MAX_S,
                    NOTICE_BACKOFF_BASE_S * (2 ** max(0, failures - 1)),
                )
                log.exception("outbound notice sweep failed; retrying in %.1fs", delay)
