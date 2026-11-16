"""Shared, bounded ``session.inventory`` emission."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable


DEFAULT_INVENTORY_MIN_INTERVAL_S = 2.0
# Observation timestamps are useful on the wire, but they are not changes to
# the inventory.  In particular, context_updated_at advances on every sampled
# footer even when its token/window/level tuple is unchanged.
_VOLATILE_SIGNATURE_FIELDS = frozenset({
    "last_activity", "genuine_activity_at", "context_updated_at",
})


class InventoryEmitter:
    """Emit changed inventory snapshots at most once per field-churn window.

    A membership shrink bypasses the interval. Changes suppressed by the
    interval schedule their own delayed flush, so delivery never depends on an
    unrelated later observation pass.
    """

    def __init__(
        self,
        sessions: Any,
        broadcast: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        min_interval_s: float = DEFAULT_INVENTORY_MIN_INTERVAL_S,
    ) -> None:
        self.sessions = sessions
        self.broadcast = broadcast
        self.min_interval_s = max(0.0, float(min_interval_s))
        self._last_signature: list[dict[str, Any]] | None = None
        self._last_emit_monotonic = float("-inf")
        self._flush_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def last_signature(self) -> list[dict[str, Any]] | None:
        return None if self._last_signature is None else [dict(row) for row in self._last_signature]

    def prime(self) -> None:
        """Set the dedup baseline without sending a snapshot.

        Boot adoption happens before the first periodic reconcile. Priming lets
        that first no-op pass stay silent while a later adopted or changed row
        still emits normally.
        """
        if self._last_signature is None:
            self._last_signature = self.signature_for_sessions(self.sessions.list_open())

    @staticmethod
    def signature_for_sessions(snapshot: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return the dedup identity, excluding volatile activity fields.

        The wire payload retains the fields. They simply cannot make a full
        inventory worth rebroadcasting on every observation tick.
        """
        signature = [
            {key: value for key, value in row.items() if key not in _VOLATILE_SIGNATURE_FIELDS}
            for row in snapshot
            if isinstance(row, dict)
        ]
        for row in signature:
            if isinstance(row.get("agents"), list):
                row["agents"] = [{key: value for key, value in agent.items() if key != "since"} for agent in row["agents"]]
        return sorted(
            signature,
            key=lambda row: (
                str(row.get("stream_id") or ""),
                str(row.get("host") or ""),
                str(row.get("session_name") or ""),
            ),
        )

    @staticmethod
    def _ids(signature: list[dict[str, Any]] | None) -> set[str]:
        return {str(row.get("stream_id") or "") for row in (signature or [])}

    @staticmethod
    def _working_edge(
        previous: list[dict[str, Any]] | None,
        current: list[dict[str, Any]],
    ) -> bool:
        """Urgent timer-start or explicit terminal-idle working transition."""
        prior_by_id = {
            str(row.get("stream_id") or ""): row
            for row in (previous or [])
        }
        return any(
            (bool(row.get("working"))
             and not bool(prior_by_id.get(str(row.get("stream_id") or ""), {}).get("working")))
            or (row.get("working") is False
                and prior_by_id.get(str(row.get("stream_id") or ""), {}).get("working") is True)
            for row in current
        )

    async def emit_if_changed(self, *, immediate: bool = False) -> bool:
        """Emit a changed snapshot, optionally bypassing the churn throttle.

        Normal observer traffic remains bounded by ``min_interval_s``. A
        converge-now RPC passes ``immediate=True`` so its synchronous response
        cannot acknowledge a change that is only waiting in a delayed flush.
        Signature deduplication still applies in both modes.
        """
        async with self._lock:
            return await self._emit_current_locked(immediate=immediate)

    async def _emit_current_locked(self, *, immediate: bool = False) -> bool:
        snapshot = self.sessions.list_open()
        signature = self.signature_for_sessions(snapshot)
        previous = self._last_signature
        previous_ids = self._ids(previous)
        current_ids = self._ids(signature)
        shrink = bool(previous_ids) and current_ids < previous_ids
        working_edge = self._working_edge(previous, signature)
        if signature == previous and not shrink:
            return False

        now = time.monotonic()
        # Timer start and explicit terminal idle are latency-critical. Holding
        # idle for the full interval consumes the client's delivery budget
        # before serialization/transport; ordinary field churn stays bounded.
        if (
            not immediate
            and not (shrink or working_edge)
            and now - self._last_emit_monotonic < self.min_interval_s
        ):
            self._schedule_flush_locked(self.min_interval_s - (now - self._last_emit_monotonic))
            return False

        self._last_signature = signature
        self._last_emit_monotonic = now
        await self.broadcast({"type": "session.inventory", "sessions": snapshot})
        return True

    def _schedule_flush_locked(self, delay: float) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return
        self._flush_task = asyncio.create_task(self._flush_after_delay(max(0.0, delay)))

    async def _flush_after_delay(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            async with self._lock:
                self._flush_task = None
                await self._emit_current_locked()
        except asyncio.CancelledError:
            raise
