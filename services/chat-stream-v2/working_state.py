from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


DEFAULT_WATCHDOG_SILENCE_S = 600.0
_WORKING_LABEL_DURATION_RE = re.compile(
    r"(?:(\d+)\s*h(?![a-z]))?\s*(?:(\d+)\s*m(?![a-z]))?\s*(\d+)\s*s(?![a-z])",
    re.I,
)


def parse_working_label_seconds(label: str | None) -> int | None:
    if not label:
        return None
    match = _WORKING_LABEL_DURATION_RE.search(label)
    if not match:
        return None
    return (
        int(match.group(1) or 0) * 3600
        + int(match.group(2) or 0) * 60
        + int(match.group(3))
    )


def _iso_from_ms(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class StreamWorkingState:
    stream_id: str
    host: str = "unknown"
    provider: str = "claude"
    session_id: str = ""
    session_name: str = ""
    tokens_phase: str = "idle"
    turn_started_ms: int | None = None
    last_observed_ms: int = 0
    last_emit_ms: int = 0

    def payload(self, now_ms: int) -> dict[str, Any]:
        return {
            "stream_id": self.stream_id,
            "timestamp": _iso_from_ms(now_ms),
            "tokens_input": 0,
            "tokens_output": 0,
            "tokens_cache_read": 0,
            "tokens_cache_creation": 0,
            "tokens_phase": self.tokens_phase,
            "shell_count_started": 0,
            "tasks": [],
            "task_summary": {"total": 0, "done": 0, "in_progress": 0, "open": 0},
            "elapsed_ms": (
                max(0, now_ms - self.turn_started_ms)
                if self.turn_started_ms is not None
                else 0
            ),
        }


class WorkingStateTracker:
    HEARTBEAT_MS = 5000
    ACTIVE_WINDOW_MS = 30000

    def __init__(self, *, watchdog_silence_s: float = DEFAULT_WATCHDOG_SILENCE_S) -> None:
        if watchdog_silence_s <= 0:
            raise ValueError("watchdog_silence_s must be > 0")
        self._streams: dict[str, StreamWorkingState] = {}
        self.watchdog_silence_ms = int(watchdog_silence_s * 1000)

    def get(self, stream_id: str, now_ms: int | None = None) -> dict[str, Any] | None:
        state = self._streams.get(stream_id)
        return None if state is None else state.payload(self._resolve_now(now_ms))

    def snapshot(self, now_ms: int | None = None) -> dict[str, dict[str, Any]]:
        current = self._resolve_now(now_ms)
        return {stream_id: state.payload(current) for stream_id, state in self._streams.items()}

    @staticmethod
    def _resolve_now(now_ms: int | None) -> int:
        return int(time.time() * 1000) if now_ms is None else int(now_ms)

    def drop_stream(self, stream_id: str) -> bool:
        return self._streams.pop(stream_id, None) is not None

    def observe(self, event: dict[str, Any], now_ms: int) -> dict[str, Any] | None:
        stream_id = str(event.get("stream_id") or "")
        if not stream_id:
            return None
        state = self._streams.setdefault(stream_id, StreamWorkingState(stream_id=stream_id))
        state.host = str(event.get("host") or state.host)
        state.provider = str(event.get("provider") or state.provider)
        state.session_id = str(event.get("session_id") or state.session_id)
        state.session_name = str(event.get("session_name") or state.session_name)
        before = state.payload(now_ms)
        raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
        working = bool(raw.get("working"))
        state.tokens_phase = "down" if working else "idle"
        state.last_observed_ms = now_ms
        if working:
            elapsed_s = parse_working_label_seconds(
                raw.get("working_label") if isinstance(raw.get("working_label"), str) else None
            )
            state.turn_started_ms = now_ms - elapsed_s * 1000 if elapsed_s is not None else (
                state.turn_started_ms if state.turn_started_ms is not None else now_ms
            )
        else:
            state.turn_started_ms = None
        after = state.payload(now_ms)
        if self._same_state(before, after):
            return None
        state.last_emit_ms = now_ms
        return after

    def heartbeat(self, now_ms: int) -> list[dict[str, Any]]:
        emitted: list[dict[str, Any]] = []
        for state in self._streams.values():
            if (
                state.tokens_phase != "idle"
                and state.last_observed_ms
                and now_ms - state.last_observed_ms >= self.watchdog_silence_ms
            ):
                state.tokens_phase = "idle"
                state.turn_started_ms = None
                payload = state.payload(now_ms)
                payload["reason"] = "watchdog"
                state.last_emit_ms = now_ms
                emitted.append(payload)
                continue
            if (
                not state.last_observed_ms
                or now_ms - state.last_observed_ms > self.ACTIVE_WINDOW_MS
                or state.last_emit_ms
                and now_ms - state.last_emit_ms < self.HEARTBEAT_MS
            ):
                continue
            payload = state.payload(now_ms)
            state.last_emit_ms = now_ms
            emitted.append(payload)
        return emitted

    @staticmethod
    def _same_state(before: dict[str, Any], after: dict[str, Any]) -> bool:
        return {
            key: value for key, value in before.items() if key not in {"timestamp", "elapsed_ms"}
        } == {
            key: value for key, value in after.items() if key not in {"timestamp", "elapsed_ms"}
        }
