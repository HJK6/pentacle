"""Bounded, token-free telemetry for per-seat stream-token lifecycle events."""

from __future__ import annotations

from collections import Counter, deque
from datetime import datetime, timezone
import logging
import re
from typing import Any


log = logging.getLogger("chat_streamd_v2.seat_token")

TOKEN_REASON_ABSENT = "absent"
TOKEN_REASON_MALFORMED = "malformed"
TOKEN_REASON_EXPIRED = "expired"
TOKEN_REASON_WRONG_SEAT = "wrong-seat"
TOKEN_REASON_INTERNAL_ERROR = "internal-error"
TOKEN_REASON_VERIFIED = "verified"
TOKEN_REASON_ISSUED = "issued"

TOKEN_FAILURE_REASONS = frozenset(
    {
        TOKEN_REASON_ABSENT,
        TOKEN_REASON_MALFORMED,
        TOKEN_REASON_EXPIRED,
        TOKEN_REASON_WRONG_SEAT,
        TOKEN_REASON_INTERNAL_ERROR,
    }
)
TOKEN_REASON_CODES = TOKEN_FAILURE_REASONS | {TOKEN_REASON_VERIFIED}

_SAFE_LABEL = re.compile(r"[^A-Za-z0-9._:/-]")
_MAX_LABEL = 128


def safe_label(value: Any) -> str:
    """Return a bounded label that cannot carry arbitrary log payloads."""
    text = str(value or "")
    text = _SAFE_LABEL.sub("_", text)
    return text[:_MAX_LABEL]


class SeatTokenTelemetry:
    """In-memory lifecycle counters and recent records.

    The recorder deliberately has no token-valued argument. The only fields it
    stores or logs are event names, reason codes, bounded operation labels, and
    stream identities. This makes the no-secret invariant structural as well as
    testable.
    """

    def __init__(self, *, max_recent: int = 256) -> None:
        self._counts: Counter[str] = Counter()
        self._failure_reasons: Counter[str] = Counter()
        self._recent: deque[dict[str, str]] = deque(maxlen=max_recent)

    def record_issuance(self, *, stream_id: str, operation: str = "spawn") -> None:
        self._record(
            event="issuance",
            outcome="success",
            reason_code=TOKEN_REASON_ISSUED,
            stream_id=stream_id,
            operation=operation,
        )

    def record_verification(
        self,
        *,
        reason_code: str,
        stream_id: str = "",
        operation: str = "",
    ) -> None:
        if reason_code not in TOKEN_REASON_CODES:
            reason_code = TOKEN_REASON_INTERNAL_ERROR
        if reason_code == TOKEN_REASON_VERIFIED:
            outcome = "success"
            event = "verification_success"
        else:
            outcome = "failure"
            event = "verification_failure"
            self._failure_reasons[reason_code] += 1
        self._record(
            event=event,
            outcome=outcome,
            reason_code=reason_code,
            stream_id=stream_id,
            operation=operation,
        )

    def _record(
        self,
        *,
        event: str,
        outcome: str,
        reason_code: str,
        stream_id: str,
        operation: str,
    ) -> None:
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        record = {
            "event": event,
            "outcome": outcome,
            "reason_code": reason_code,
            "stream_id": safe_label(stream_id),
            "operation": safe_label(operation),
            "timestamp": timestamp,
        }
        self._counts[event] += 1
        self._recent.append(record)
        log.debug(
            "seat_token event=%s outcome=%s reason_code=%s stream_id=%s operation=%s",
            record["event"],
            record["outcome"],
            record["reason_code"],
            record["stream_id"],
            record["operation"],
        )

    def snapshot(self) -> dict[str, Any]:
        """Return client-readable telemetry without any credential material."""
        return {
            "counts": {
                "issuance": self._counts["issuance"],
                "verification_success": self._counts["verification_success"],
                "verification_failure": self._counts["verification_failure"],
            },
            "failure_reasons": dict(sorted(self._failure_reasons.items())),
            "recent": list(self._recent),
        }
