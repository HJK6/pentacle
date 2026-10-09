"""alerts.py — structured alert emission.

This module owns the single `emit` seam used by callers that have already
detected a condition. It logs a structured alert; it neither detects state nor
spawns, kills, parks, gates, recovers, or otherwise remediates a session.
"""

from __future__ import annotations

import asyncio
import logging

from error_adapters import ErrorFact, adapt

log = logging.getLogger("chat_streamd_v2.alerts")


class Alerts:
    """Structured log emitter for an already-detected alert condition."""

    def __init__(self, store: object = None) -> None:
        self.store = store
        #: ErrorAlerts, attached by Server.configure_error_alerts.
        self.sink: object = None
        self._pending: set[asyncio.Task] = set()

    def emit(self, kind: str, **fields: object) -> None:
        log.warning("ALERT %s %s", kind, fields)
        try:
            fact = adapt(kind, fields)
        except Exception:
            # A detector must never fail because its alert mapping is wrong.
            log.warning("subsystem=error_alerts kind=%s action=adapter_rejected", kind)
            return
        if fact is None:
            return
        try:
            task = asyncio.get_running_loop().create_task(self.error(fact))
        except RuntimeError:
            log.warning("subsystem=error_alerts kind=%s action=sink_unavailable", kind)
            return
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def error(self, fact: ErrorFact, *, principal: str | None = None) -> str | None:
        """Durably record one typed fact; delivery follows the outbox pass.

        Returns the notification_id after commit, or None when the alert core
        is not configured. `principal` is only an already-authenticated
        existing identity; the default is this daemon.
        """
        if not isinstance(fact, ErrorFact):
            raise ValueError("invalid_error_fact")
        if self.sink is None:
            log.warning("subsystem=error_alerts family=%s action=sink_unavailable", fact.family)
            return None
        return await self.sink.emit(fact, principal=principal)
