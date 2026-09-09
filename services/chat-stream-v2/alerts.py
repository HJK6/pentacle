"""alerts.py — structured alert emission.

This module owns the single `emit` seam used by callers that have already
detected a condition. It logs a structured alert; it neither detects state nor
spawns, kills, parks, gates, recovers, or otherwise remediates a session.
"""

from __future__ import annotations

import logging

log = logging.getLogger("chat_streamd_v2.alerts")


class Alerts:
    """Structured log emitter for an already-detected alert condition."""

    def __init__(self, store: object = None) -> None:
        self.store = store

    def emit(self, kind: str, **fields: object) -> None:
        log.warning("ALERT %s %s", kind, fields)
