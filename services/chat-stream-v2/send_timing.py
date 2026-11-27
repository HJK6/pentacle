"""In-process reply and confirmation totals for completed chat sends."""

import json
import logging
import math
import time

log = logging.getLogger("chat_streamd_v2.store")
SUMMARY_INTERVAL_S = 3600.0


class SendTiming:
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._window_start = clock()
        self._reply_ms: list[float] = []
        self._counts = {
            "reply_under_500ms": 0, "reply_under_1s": 0,
            "reply_under_2s": 0, "reply_2s_or_more": 0,
            "proof_under_1s": 0, "queued": 0,
            "pending_proof": 0, "failed": 0, "coalesced": 0,
            "backend_attempts": 0, "backend_proof_under_1s": 0, "backend_pending_proof": 0, "backend_worst_ms": 0,
        }
        self.failures = 0

    def record(self, started: float, result: dict, *, backend: bool = False) -> None:
        try:
            now = self._clock()
            if result.get("coalesced"):
                self._counts["coalesced"] += not backend
            elif backend:
                elapsed = now - started
                self._counts["backend_attempts"] += 1
                self._counts["backend_proof_under_1s"] += elapsed < 1 and result.get("submission_confirmed") is True
                self._counts["backend_pending_proof"] += result.get("delivery") == "committed_pending_proof"
                self._counts["backend_worst_ms"] = max(self._counts["backend_worst_ms"], round(elapsed * 1000, 3))
            else:
                elapsed = now - started
                confirmed = result.get("submission_confirmed") is True
                self._reply_ms.append(elapsed * 1000)
                self._counts["reply_under_500ms"] += elapsed < 0.5
                self._counts["reply_under_2s"] += elapsed < 2
                self._counts["reply_2s_or_more"] += elapsed >= 2
                if elapsed < 1:
                    self._counts["reply_under_1s"] += 1
                    self._counts["proof_under_1s"] += confirmed
                if confirmed:
                    self._counts["queued"] += result.get("provider_queued") is True
                elif result.get("delivery") == "not_landed":
                    self._counts["failed"] += 1
                else:
                    self._counts["pending_proof"] += 1
            if not backend: self.flush(now)
        except Exception:
            self.failures += 1

    def flush(self, now: float, *, force: bool = False) -> None:
        try:
            if force or now - self._window_start >= SUMMARY_INTERVAL_S:
                self._summarize(now)
        except Exception:
            self.failures += 1

    def _summarize(self, now: float) -> None:
        reply_ms = self._reply_ms
        attempts = len(reply_ms)
        reply_ms.sort()
        window = {
            "window_s": round(now - self._window_start, 3),
            "attempts": attempts,
            **self._counts,
            "backend_worst_ms": self._counts["backend_worst_ms"] if self._counts["backend_attempts"] else None,
            "reply_p95_ms": round(reply_ms[math.ceil(attempts * 0.95) - 1], 3) if attempts else None,
            "reply_worst_ms": round(reply_ms[-1], 3) if attempts else None,
            "instrumentation_failures": self.failures,
        }
        self._window_start = now
        self._reply_ms = []
        self._counts = dict.fromkeys(self._counts, 0)
        log.info("send_latency %s", json.dumps(window, separators=(",", ":")))


send_counter = SendTiming()
