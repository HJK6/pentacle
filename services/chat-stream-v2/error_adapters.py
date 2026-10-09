"""Fixed adapters from already-detected conditions to typed error facts.

This module is the one registration home for error-alert producers. Each
family lists its fixed code set; each `Alerts.emit` kind that should alert has
one pure mapper below. Mappers carry no body, path, URL, severity, recipient,
wake flag or secret: delivery, wording and recipients belong to ErrorAlerts.
Adding a producer is a source change here, never runtime registration.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Callable, Mapping

#: family -> fixed codes. Add one line per accepted producer family.
FAMILY_CODES: dict[str, frozenset[str]] = {
    "work_lane.v1": frozenset({"lane_stale", "lane_completed"}),
    "session_lifecycle": frozenset({"session_dead", "close_failed", "close_carcass", "reap_exhausted", "reap_fenced"}),
    "integrity": frozenset({"pin_drift", "close_claim_mismatch"}),
    "system_deploy": frozenset({"deploy_failed"}),
    "system_backup": frozenset({"backup_failed"}),
    "bot_messaging": frozenset({"registration_failed", "delivery_failed"}),
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


def _episode_id(code: str, fields: Mapping[str, object], keys: tuple[str, ...]) -> str | None:
    """Hash only the row's ordered keys; malformed inputs remain log-only."""
    try:
        values = []
        for key in keys:
            value = fields.get(key)
            if value is None:
                return None
            text = str(value)
            if not text:
                return None
            values.append(text)
        return code + ":" + hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()[:40]
    except Exception:
        return None


def _map_fields(family: str, code: str, fields: Mapping[str, object],
                keys: tuple[str, ...], *, stage: str | None = None) -> ErrorFact | None:
    episode_id = _episode_id(code, fields, keys)
    return None if episode_id is None else ErrorFact(family, code, episode_id, stage=stage)


def _map_system_deploy(fields: Mapping[str, object]) -> ErrorFact | None:
    dedup = fields.get("dedup_key")
    if (fields.get("severity") != "critical" or not isinstance(dedup, str)
            or not re.fullmatch(r"pipeline\|[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\|[0-9]{4}-[0-9]{2}-[0-9]{2}", dedup)):
        return None
    return _map_fields("system_deploy", "deploy_failed", fields, ("dedup_key",))


def _map_system_backup(fields: Mapping[str, object]) -> ErrorFact | None:
    dedup = fields.get("dedup_key")
    if (fields.get("severity") != "critical" or not isinstance(dedup, str)
            or not re.fullmatch(r"wmi-backup\|[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\|[0-9]{4}-[0-9]{2}-[0-9]{2}", dedup)):
        return None
    return _map_fields("system_backup", "backup_failed", fields, ("dedup_key",))


def _map_bot_messaging(fields: Mapping[str, object]) -> ErrorFact | None:
    step, status, operation = (fields.get(key) for key in ("step", "status_class", "operation_id"))
    if (not isinstance(step, str) or step not in {"registration", "delivery"}
            or not isinstance(status, str) or status not in {
                "callback_rejected", "callback_timeout", "callback_unreachable", "challenge_mismatch",
                "aws_error", "handoff_failed", "unknown"}
            or not isinstance(operation, str)
            or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", operation)):
        return None
    return _map_fields("bot_messaging", step + "_failed", fields, ("step", "operation_id"), stage=status)


#: Alerts kind -> pure mapper(fields) returning a fact, or None to stay log-only.
#: Producers call `await alerts.record(kind, **fields)`; it commits before return.
ADAPTERS: dict[str, Callable[[Mapping[str, object]], ErrorFact | None]] = {
    "reconciler_session_dead": lambda fields: _map_fields("session_lifecycle", "session_dead", fields, ("episode_id",)),
    "close_failed": lambda fields: _map_fields("session_lifecycle", "close_failed", fields, ("stream_id", "generation", "reason")),
    "close_carcass": lambda fields: _map_fields("session_lifecycle", "close_carcass", fields, ("stream_id", "generation")),
    "deferred_reap_exhausted": lambda fields: _map_fields("session_lifecycle", "reap_exhausted", fields, ("stream_id", "generation")),
    "reap_fenced": lambda fields: _map_fields("session_lifecycle", "reap_fenced", fields, ("stream_id", "generation")),
    "pin_drift": lambda fields: _map_fields("integrity", "pin_drift", fields, ("host", "pinned_sha", "daemon_sha")),
    "close_claim_mismatch": lambda fields: _map_fields("integrity", "close_claim_mismatch", fields, ("report_id",)),
    "system_deploy_failed": _map_system_deploy,
    "system_backup_failed": _map_system_backup,
    "bot_messaging_failed": _map_bot_messaging,
}


def adapt(kind: str, fields: Mapping[str, object]) -> ErrorFact | None:
    mapper = ADAPTERS.get(kind)
    return mapper(fields) if mapper is not None else None
