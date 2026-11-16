"""Named Store call timing: slow-call lines, an hourly top list and a daily alert.

`Store.submit` names each callable at admission (`admit`); the worker hands the
name and its existing timestamps to `StoreCallDiag.record` after the call's
outcome is captured. Lines and alerts carry operation names and numbers only:
never a repr, argument, payload, id or credential. Nothing here can change a
call's result, exception or future: every step is guarded, and a failure is
counted and dropped.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import re

from error_adapters import ErrorFact

log = logging.getLogger("chat_streamd_v2.store")

#: Execution above this writes one `store_call_slow` line.
SLOW_LINE_S = 0.25
#: Execution above this raises `store_call_slow`, once per operation per UTC day.
ALERT_S = 2.0
SUMMARY_INTERVAL_S = 3600.0
TOP_N = 5
NAME_MAX = 96
UNNAMED = "unnamed"
_UNSAFE = re.compile(r"[^A-Za-z0-9._:-]")

#: True inside the alert task: Store calls admitted under it never alert.
alert_path: ContextVar[bool] = ContextVar("store_call_alert_path", default=False)


def call_name(fn) -> str:
    """The callable's static qualname, reduced to safe characters and bounded."""
    qualname = getattr(fn, "__qualname__", None)
    if not isinstance(qualname, str):
        return UNNAMED
    return _UNSAFE.sub("", qualname)[:NAME_MAX] or UNNAMED


def admit(fn) -> tuple[str, bool]:
    """Name and alert-path flag for a call at admission; naming never fails it."""
    try:
        name = call_name(fn)
    except Exception:
        name = UNNAMED
    return name, alert_path.get()


def _utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class StoreCallDiag:
    """Totals and alert state touched only by the Store worker thread."""

    def __init__(self, *, today=_utc_day) -> None:
        #: async callable(ErrorFact); the daemon binds `Alerts.error`.
        self.alert = None
        self.failures = 0
        self._today = today
        self._totals: dict[str, list] = {}
        self._window_start: float | None = None
        self._alert_day: str | None = None
        self._alerted: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    def record(self, name: str, quiet: bool, loop, queued: float, started: float, finished: float) -> None:
        try:
            execution = max(0.0, finished - started)
            totals = self._totals.setdefault(name, [0, 0.0])
            totals[0] += 1
            totals[1] += execution
            if execution > SLOW_LINE_S:
                log.info("store_call_slow %s", json.dumps(
                    {"name": name, "queue_ms": round(max(0.0, started - queued) * 1000, 3),
                     "execution_ms": round(execution * 1000, 3)}, separators=(",", ":")))
                if execution > ALERT_S and not quiet:
                    self._hand_off(name, loop)
            if self._window_start is None:
                self._window_start = finished
            elif finished - self._window_start >= SUMMARY_INTERVAL_S:
                self._summarize(finished)
        except Exception:
            self.failures += 1

    def _summarize(self, now: float) -> None:
        top = sorted(self._totals.items(), key=lambda item: item[1][1], reverse=True)[:TOP_N]
        log.info("store_call_top %s", json.dumps(
            {"window_s": round(now - self._window_start, 3),
             "top": [{"name": name, "calls": calls, "execution_ms": round(total * 1000, 3)}
                     for name, (calls, total) in top],
             "instrumentation_failures": self.failures}, separators=(",", ":")))
        self._totals, self._window_start = {}, now

    def _hand_off(self, name: str, loop) -> None:
        # Schedule only; the worker never waits on, re-enters or networks for the alert.
        if self.alert is None:
            return
        day = self._today()
        if day != self._alert_day:
            self._alert_day, self._alerted = day, set()
        if name in self._alerted:
            return
        self._alerted.add(name)
        loop.call_soon_threadsafe(self._start_alert, name, day)

    def _start_alert(self, name: str, day: str) -> None:
        try:
            task = asyncio.get_running_loop().create_task(self._alert(name, day))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception:
            self.failures += 1

    async def _alert(self, name: str, day: str) -> None:
        alert_path.set(True)
        try:
            await self.alert(ErrorFact("daemon_runtime.v1", "store_call_slow", f"{name}:{day}"))
        except Exception:
            self.failures += 1
            log.warning("store_call_alert_failed name=%s", name)
