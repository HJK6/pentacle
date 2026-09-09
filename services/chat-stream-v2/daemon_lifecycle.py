"""Process-local daemon identity for chat-stream v2."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import socket
import uuid
from typing import Any


class DaemonLifecycle:
    """Keep reservation identity and the daemon.stats lifecycle shape in memory."""

    def __init__(
        self,
        _store: Any,
        *,
        host: str,
        pid: int | None = None,
        instance_id: str | None = None,
    ) -> None:
        self.host = str(host or socket.gethostname())
        self.pid = int(os.getpid() if pid is None else pid)
        self.instance_id = instance_id or f"{self.host}:{self.pid}:{uuid.uuid4().hex}"
        self._events: list[dict[str, Any]] = []

    def _record(self, event_type: str, *, reason: str) -> dict[str, Any]:
        event = {
            "event_id": uuid.uuid4().hex,
            "instance_id": self.instance_id,
            "event_type": event_type,
            "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "host": self.host,
            "pid": self.pid,
            "previous_instance_id": None,
            "related_event_id": self._events[-1]["event_id"] if self._events else None,
            "reason": reason,
            "metadata": {},
        }
        self._events.append(event)
        return event

    async def start(self) -> dict[str, Any]:
        """Record this process's start without creating durable forensics."""
        if self._events:
            return self._events[0]
        return self._record("daemon_start", reason="startup")

    async def stop(self, *, reason: str = "shutdown") -> dict[str, Any] | None:
        """Record a local stop once when startup completed."""
        if not self._events or self._events[-1]["event_type"] == "daemon_stop":
            return None
        return self._record("daemon_stop", reason=str(reason or "shutdown"))

    async def snapshot(self, *, limit: int = 20) -> dict[str, Any]:
        """Return the historical daemon.stats shape for this process only."""
        effective_limit = max(1, min(int(limit), 500))
        return {
            "instance_id": self.instance_id,
            "events": list(reversed(self._events[-effective_limit:])),
        }
