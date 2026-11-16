"""One absolute deadline for graceful daemon shutdown (restart continuity).

launchd SIGKILLs the daemon 20 s after SIGTERM. Every shutdown await shares one
deadline, so stalled steps cannot add up past it: each step gets
min(its own cap, what remains), and a step that overruns is cancelled and
abandoned, never awaited again. `reserve_s` is held back from the ordinary
steps so `store.stop()` always gets a moment to flush.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable
from typing import Any

log = logging.getLogger("chat_streamd_v2.shutdown")

#: Upper bound for the whole shutdown; the env knob may only lower it.
SHUTDOWN_BUDGET_MAX_S = 15.0
SHUTDOWN_BUDGET_ENV = "PENTACLE_V2_SHUTDOWN_BUDGET_S"
#: Seconds kept back from ordinary steps for store.stop().
STORE_STOP_RESERVE_S = 1.0


def configured_budget_s(environ: dict[str, str] | None = None) -> float:
    raw = (os.environ if environ is None else environ).get(SHUTDOWN_BUDGET_ENV, "")
    try:
        value = float(raw) if raw.strip() else SHUTDOWN_BUDGET_MAX_S
    except ValueError:
        log.warning("ignoring invalid %s=%r", SHUTDOWN_BUDGET_ENV, raw)
        value = SHUTDOWN_BUDGET_MAX_S
    if not value > 0:  # also rejects NaN
        return SHUTDOWN_BUDGET_MAX_S
    return min(value, SHUTDOWN_BUDGET_MAX_S)


class ShutdownBudget:
    def __init__(self, total_s: float, *, reserve_s: float = STORE_STOP_RESERVE_S) -> None:
        self.total_s = total_s
        self.reserve_s = min(reserve_s, total_s / 2)
        self.deadline = time.monotonic() + total_s
        self.abandoned: list[str] = []

    def remaining(self, cap: float | None = None, *, reserved: bool = True) -> float:
        """Seconds a step may use: what is left of the deadline (less the
        store reserve unless `reserved=False`), at most `cap`."""
        left = self.deadline - time.monotonic() - (self.reserve_s if reserved else 0.0)
        left = max(0.0, left)
        return left if cap is None else min(cap, left)

    async def step(self, name: str, awaitable: Awaitable[Any], *, cap: float) -> Any:
        """Await one shutdown step within min(cap, remaining). On overrun the
        step is cancelled and abandoned: its cancellation is not waited for,
        because a step that swallows cancellation would otherwise hold the
        shutdown open. A failing step is logged; shutdown continues."""
        task = asyncio.ensure_future(awaitable)
        timeout = self.remaining(cap)
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            task.cancel()
            task.add_done_callback(_consume)
            self.abandoned.append(name)
            log.warning("shutdown step %s exceeded %.2fs; abandoned", name, timeout)
            return None
        if task.cancelled():
            return None
        if task.exception() is not None:
            log.error("shutdown step %s failed", name, exc_info=task.exception())
            return None
        return task.result()


def _consume(task: asyncio.Future[Any]) -> None:
    if not task.cancelled():
        task.exception()
