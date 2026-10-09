"""alerts.py — structured alert emission.

This module owns the single `emit` seam used by callers that have already
detected a condition. It logs a structured alert; it neither detects state nor
spawns, kills, parks, gates, recovers, or otherwise remediates a session.
"""

from __future__ import annotations

import logging

from error_adapters import ErrorFact, adapt

log = logging.getLogger("chat_streamd_v2.alerts")


class Alerts:
    """Structured log emitter for an already-detected alert condition."""

    def __init__(self, store: object = None) -> None:
        self.store = store
        #: ErrorAlerts, attached by Server.configure_error_alerts.
        self.sink: object = None

    def emit(self, kind: str, **fields: object) -> None:
        """Log only; never a durable fact or delivery claim."""
        log.warning("ALERT %s %s", kind, fields)

    async def record(self, kind: str, **fields: object) -> str | None:
        """Durably record the fact a kind's fixed adapter maps; one ALERT log.

        Async producers replace `emit` with `await record` to make a typed
        fact. The log carries only the kind and the typed fact, never the raw
        fields. A mapping error is a caller bug and raises.
        """
        fact = adapt(kind, fields)
        log.warning("ALERT %s %s", kind, fact)
        return None if fact is None else await self.error(fact)

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
