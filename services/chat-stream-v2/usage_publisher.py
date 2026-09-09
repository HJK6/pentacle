"""Validated mtime publisher for externally collected usage state."""

from __future__ import annotations

import inspect
import logging
import asyncio
from pathlib import Path

from usage_state import UsageStateStore


log = logging.getLogger("public_chat_stream.usage_publisher")


def _empty_row(identifier: str, label: str) -> dict:
    # Canonical LKG key order so a v1/empty fallback row is byte-consistent with
    # the loaded/validated Provider A and Provider B rows in the same frame.
    return {
        "id": identifier,
        "label": label,
        "pct": None,
        "resets_at_iso": None,
        "resets_text": None,
        "upstream_reported_at": None,
        "probed_at": None,
    }


class UsageStatePublisher:
    def __init__(self, broadcast, *, state_path: str | Path | None) -> None:
        self._broadcast = broadcast
        self._store = UsageStateStore(state_path) if state_path else None
        self._mtime_ns: int | None = None
        self._limits = [_empty_row("provider_a", "Provider A"), _empty_row("provider_b", "Provider B"), _empty_row("provider_c", "Provider C")]
        self._provider_a_health: dict | None = None

    def snapshot(self) -> list[dict]:
        # Defensive copy: the same rows feed the broadcast frame and every hello
        # snapshot; callers must not mutate the publisher's held state.
        return [dict(row) for row in self._limits]

    def health_snapshot(self) -> dict | None:
        if not self._provider_a_health:
            return None
        return {"schema_version": 1, "provider_a": dict(self._provider_a_health)}

    def _changed_mtime(self) -> bool:
        if self._store is None:
            return False
        try:
            mtime_ns = self._store.path.stat().st_mtime_ns
        except FileNotFoundError:
            return False
        if mtime_ns == self._mtime_ns:
            return False
        self._mtime_ns = mtime_ns
        return True

    async def publish_if_changed(self) -> bool:
        if not self._changed_mtime() or self._store is None:
            return False
        state = self._store.load()
        # load() returns the empty sentinel (health is None) for a missing or
        # unreadable/invalid state file. Hold the prior published frame and health
        # rather than broadcast a degraded frame (provider_c blanked, limits_health
        # dropped from hello) built from a half-empty sentinel.
        if state.health is None:
            log.debug("usage_state unusable; holding prior limits frame")
            return False
        limits = [*(state.lkg or self._limits[:2]), state.provider_c_lkg or _empty_row("provider_c", "Provider C")]
        self._provider_a_health = state.health
        if limits == self._limits:
            return False
        self._limits = limits
        result = self._broadcast({"type": "limits.update", "limits": limits})
        if inspect.isawaitable(result):
            await result
        return True

    async def run_forever(self, interval_s: float = 1.0) -> None:
        while True:
            await self.publish_if_changed()
            await asyncio.sleep(interval_s)
