"""Fixed adapters from already-detected conditions to typed error facts.

This module is the one registration home for error-alert producers. Each
family lists its fixed code set; each `Alerts.emit` kind that should alert has
one pure mapper below. Mappers carry no body, path, URL, severity, recipient,
wake flag or secret: delivery, wording and recipients belong to ErrorAlerts.
Adding a producer is a source change here, never runtime registration.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Mapping

#: family -> fixed codes. Add one line per accepted producer family.
FAMILY_CODES: dict[str, frozenset[str]] = {
    "voice_operation.v1": frozenset({
        "upload_read_failed", "upload_transport_failed", "upload_milestone_missing",
        "transcribe_transport_failed", "transcribe_failed", "transcribe_milestone_missing",
        "send_transport_failed", "send_unconfirmed", "operation_cancelled", "recovered",
        "report_gap", "no_speech", "too_long", "unsupported", "permission_declined",
    }),
}
CONDITIONS = frozenset({"active", "recovered", "cancelled", "unknown"})
_EPISODE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_STAGE = re.compile(r"[a-z_]{1,32}")


@dataclass(frozen=True)
class ErrorFact:
    """One episode's current typed state; same episode_id updates one record."""

    family: str
    code: str
    episode_id: str
    condition: str = "active"
    stage: str | None = None

    def __post_init__(self) -> None:
        if (
            not all(isinstance(v, str) for v in (self.family, self.code, self.condition))
            or self.code not in FAMILY_CODES.get(self.family, ())
            or not isinstance(self.episode_id, str)
            or not _EPISODE.fullmatch(self.episode_id)
            or self.condition not in CONDITIONS
            or (self.stage is not None and not (isinstance(self.stage, str) and _STAGE.fullmatch(self.stage)))
        ):
            raise ValueError("invalid_error_fact")


#: Alerts kind -> pure mapper(fields) returning a fact, or None to stay log-only.
#: Producers call `await alerts.record(kind, **fields)`; it commits before return.
ADAPTERS: dict[str, Callable[[Mapping[str, object]], ErrorFact | None]] = {}


def adapt(kind: str, fields: Mapping[str, object]) -> ErrorFact | None:
    mapper = ADAPTERS.get(kind)
    return mapper(fields) if mapper is not None else None
