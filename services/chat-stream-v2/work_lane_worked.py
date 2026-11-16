"""Display-only worked time: one background ledger pass per ten-minute TTL."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable

from _shared.usage_time import iso

log = logging.getLogger(__name__)


def worked_from_time(metrics: dict | None, as_of: str) -> dict | None:
    if metrics is None:
        return None
    elapsed = metrics["elapsed_delivery_h"]
    active = [metrics.get(key, {}).get("value") for key in ("activity_proxy_h", "codex_activity_proxy_h")]
    values = [value for value in active if value is not None]
    return {"elapsed_h": elapsed.get("elapsed_so_far_h") if elapsed["censored"] else elapsed.get("value_h"),
            "censored": elapsed["censored"], "active_h": sum(values) if values else None,
            "seats": metrics["seats"], "qa_rounds": metrics["qa_rounds"], "as_of": as_of}


def read_worked(spec_ids: set[str], data_dir: Path, work_root: Path, now: float) -> dict:
    # Lazy import stays in the worker, including the CLI rollup's module setup.
    from tools.usage_rollup import batched_time_metrics
    return {spec: worked_from_time(metrics, iso(now)) for spec, metrics in
            batched_time_metrics(spec_ids, data_dir=data_dir, work_root=work_root, now=now).items()}


class WorkedCache:
    def __init__(self, data_dir: Path | None, work_root: Path | None, *,
                 changed: Callable[[], None], reader=read_worked, clock=time.monotonic,
                 wall_clock=time.time, ttl_s=600.0, timeout_s=60.0):
        self.data_dir, self.work_root = data_dir, work_root
        self.changed, self.reader, self.clock, self.wall_clock = changed, reader, clock, wall_clock
        self.ttl_s, self.timeout_s = ttl_s, timeout_s
        self.values: dict[str, dict | None] = {}
        self._due = float("-inf")
        self._task: asyncio.Task | None = None
        self._worker: asyncio.Task | None = None

    def needs_refresh(self) -> bool:
        """Keep the complete lane census on the same TTL as the ledger read."""
        return (self.data_dir is not None and self.work_root is not None and self.clock() >= self._due
                and (self._task is None or self._task.done())
                and (self._worker is None or self._worker.done()))

    def refresh(self, spec_ids: set[str]) -> None:
        """Schedule from the inventory drain; reads only ever see the cached pass."""
        if not self.needs_refresh():
            return
        self._due = self.clock() + self.ttl_s
        if not spec_ids:
            self.values = {}
            return
        self._task = asyncio.create_task(self._refresh(spec_ids))

    async def _refresh(self, spec_ids: set[str]) -> None:
        self._worker = asyncio.create_task(asyncio.to_thread(
            self.reader, spec_ids, self.data_dir, self.work_root, self.wall_clock()))
        self._worker.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        try:
            values = await asyncio.wait_for(asyncio.shield(self._worker), self.timeout_s)
        except Exception:
            # A timed-out thread cannot be killed. Keep its handle so it never
            # overlaps a later pass and never publishes a late result.
            log.warning("work lane worked-time pass unavailable", exc_info=True)
            values = {spec: None for spec in spec_ids}
        self.values = values
        self.changed()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
