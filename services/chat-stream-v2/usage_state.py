"""Crash-safe, versioned persistence for Claude usage freshness state.

The state file deliberately contains only the joint Claude/Fable last-known-good
rows and the Claude probe-health record.  It is not a provider cache and never
stores pane output, credentials, commands, or exception text.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


log = logging.getLogger("public_chat_stream.usage_state")

STATE_SCHEMA_VERSION = 1
STATE_KEYS = {"schema_version", "claude_fable_lkg", "claude_health"}
STATE_SCHEMA_VERSION_V2 = 2
STATE_KEYS_V2 = STATE_KEYS | {"codex_lkg", "codex_health"}
LKG_ROW_KEYS = {
    "id", "label", "pct", "resets_at_iso", "resets_text",
    "upstream_reported_at", "probed_at",
}
HEALTH_KEYS = {
    "attempted_at", "outcome", "error", "upstream_reported_at",
    "probed_at", "stale_after_seconds",
}
CLAUDE_FAILURES = {
    "auth_error",
    "parser_error",
    "provider_error",
    "timeout",
    "transport_error",
    "internal_error",
}
PERSISTED_OUTCOMES = {"never", "ok", *CLAUDE_FAILURES}
ERRORS = {
    "auth_error": {
        "code": "claude_not_authenticated",
        "message": "Claude is not authenticated",
    },
    "provider_error": {
        "code": "usage_provider_error",
        "message": "Provider usage is unavailable",
    },
    "parser_error": {
        "code": "claude_usage_parse_failed",
        "message": "Claude usage could not be parsed",
    },
    "timeout": {
        "code": "claude_usage_timeout",
        "message": "Claude usage probe timed out",
    },
    "transport_error": {
        "code": "claude_usage_transport_failed",
        "message": "Claude usage transport failed",
    },
    "internal_error": {
        "code": "claude_usage_internal_error",
        "message": "Claude usage probe failed internally",
    },
    "store_error": {
        "code": "usage_state_write_failed",
        "message": "Claude usage state could not be saved",
    },
}

PROVIDER_ERROR_CODES = {
    "claude": "claude_usage_provider_error",
    "codex": "codex_usage_provider_error",
}
LEGACY_PROVIDER_ERROR_CODES = {"claude_subscription_unavailable"}

_UTC_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)


class UsageStateError(Exception):
    """Base class for state validation/write failures."""


class UsageStatePreReplaceError(UsageStateError):
    """The target file was not replaced."""


class UsageStatePostReplaceError(UsageStateError):
    """The target was replaced, but the directory fsync failed."""


@dataclass(frozen=True)
class LoadedUsageState:
    lkg: list[dict] | None
    health: dict | None
    codex_lkg: dict | None = None
    codex_health: dict | None = None


def utc_rfc3339(value: object, *, nullable: bool = True) -> datetime | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not _UTC_RFC3339.fullmatch(value):
        raise ValueError("timestamp is not UTC RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp is not UTC RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamp is not UTC RFC3339")
    return parsed


def _validate_lkg_row(row: object, expected_id: str, expected_label: str) -> dict:
    if not isinstance(row, dict) or set(row) != LKG_ROW_KEYS:
        raise ValueError("invalid LKG row shape")
    if row["id"] != expected_id or row["label"] != expected_label:
        raise ValueError("invalid LKG row identity")
    pct = row["pct"]
    if pct is not None and (
        isinstance(pct, bool) or not isinstance(pct, int) or not 0 <= pct <= 100
    ):
        raise ValueError("invalid LKG percentage")
    for key in ("resets_at_iso", "resets_text", "upstream_reported_at", "probed_at"):
        if row[key] is not None and not isinstance(row[key], str):
            raise ValueError("invalid LKG field")
    if pct is None and (row["resets_at_iso"] is not None or row["resets_text"] is not None):
        raise ValueError("null LKG percentage has reset text")
    if row["upstream_reported_at"] is not None or row["probed_at"] is not None:
        raise ValueError("Claude/Fable LKG stamps must be null")
    return {key: row[key] for key in (
        "id", "label", "pct", "resets_at_iso", "resets_text",
        "upstream_reported_at", "probed_at",
    )}


def validate_health(
    health: object,
    *,
    allow_store_error: bool = False,
    provider: str = "claude",
) -> dict:
    if not isinstance(health, dict) or set(health) != HEALTH_KEYS:
        raise ValueError("invalid provider health shape")
    outcome = health["outcome"]
    allowed = PERSISTED_OUTCOMES | ({"store_error"} if allow_store_error else set())
    if outcome not in allowed:
        raise ValueError("invalid provider health outcome")
    attempted_at = utc_rfc3339(health["attempted_at"])
    upstream = utc_rfc3339(health["upstream_reported_at"])
    probed = utc_rfc3339(health["probed_at"])
    threshold = health["stale_after_seconds"]
    if isinstance(threshold, bool) or not isinstance(threshold, int) or not 60 <= threshold <= 86400:
        raise ValueError("invalid provider health threshold")
    error = health["error"]
    if outcome in {"never", "ok"}:
        if error is not None:
            raise ValueError("successful/never health cannot carry an error")
    else:
        if not isinstance(error, dict) or set(error) != {"code", "message"}:
            raise ValueError("invalid provider health error")
        if outcome == "provider_error":
            accepted_codes = {ERRORS[outcome]["code"]}
            codexode = PROVIDER_ERROR_CODES.get(provider)
            if codexode is not None:
                accepted_codes.add(codexode)
            if provider == "claude":
                accepted_codes.update(LEGACY_PROVIDER_ERROR_CODES)
            if (
                error["code"] not in accepted_codes
                or not isinstance(error["message"], str)
                or not error["message"].strip()
            ):
                raise ValueError("invalid provider health error")
        elif error != ERRORS[outcome]:
            raise ValueError("invalid provider health error")
    if outcome == "never":
        if attempted_at is not None or upstream is not None or probed is not None:
            raise ValueError("never health must have null timestamps")
    elif attempted_at is None:
        raise ValueError("attempted health timestamp is required")
    if (upstream is None) != (probed is None):
        raise ValueError("receipt/completion timestamps must be paired")
    if upstream is not None and probed is not None:
        # A failed attempt deliberately keeps the prior successful receipt /
        # completion pair while advancing attempted_at, so attempted_at may be
        # newer than that retained pair. Only a current successful observation
        # has the T0 <= T1 <= T2 ordering requirement.
        if outcome == "ok" and attempted_at is not None and attempted_at > upstream:
            raise ValueError("receipt precedes attempt")
        if upstream > probed:
            raise ValueError("completion precedes receipt")
    return {
        "attempted_at": health["attempted_at"],
        "outcome": outcome,
        "error": None if error is None else {"code": error["code"], "message": error["message"]},
        "upstream_reported_at": health["upstream_reported_at"],
        "probed_at": health["probed_at"],
        "stale_after_seconds": threshold,
    }


def validate_state(payload: object) -> LoadedUsageState:
    if isinstance(payload, dict) and payload.get("schema_version") == STATE_SCHEMA_VERSION_V2:
        if set(payload) != STATE_KEYS_V2:
            raise UsageStateError("usage state has unexpected v2 keys")
        base_payload = {key: payload[key] for key in STATE_KEYS}
        base_payload["schema_version"] = STATE_SCHEMA_VERSION
        base = validate_state(base_payload)
        codex_lkg = _validate_codex_lkg(payload["codex_lkg"])
        codex_health = validate_health(payload["codex_health"], provider="codex")
        return LoadedUsageState(
            lkg=base.lkg,
            health=base.health,
            codex_lkg=codex_lkg,
            codex_health=codex_health,
        )
    if not isinstance(payload, dict) or set(payload) != STATE_KEYS:
        raise ValueError("invalid usage state shape")
    if payload["schema_version"] != STATE_SCHEMA_VERSION:
        raise ValueError("unknown usage state schema")
    raw_lkg = payload["claude_fable_lkg"]
    if raw_lkg is None:
        lkg = None
    else:
        if not isinstance(raw_lkg, list) or len(raw_lkg) != 2:
            raise ValueError("invalid usage LKG")
        lkg = [
            _validate_lkg_row(raw_lkg[0], "claude", "Claude"),
            _validate_lkg_row(raw_lkg[1], "fable", "Fable"),
        ]
    health = validate_health(payload["claude_health"])
    if health["outcome"] == "never" and lkg is not None:
        raise ValueError("never health cannot have LKG")
    if health["outcome"] == "ok" and lkg is None:
        raise ValueError("ok health requires LKG")
    if health["outcome"] not in {"never", "ok"}:
        has_pair = health["upstream_reported_at"] is not None
        if (lkg is None) != (not has_pair):
            raise ValueError("failure health/LKG truth table mismatch")
    return LoadedUsageState(lkg=lkg, health=health)


def canonical_state(lkg: list[dict] | None, health: dict) -> dict:
    validated = validate_state({
        "schema_version": STATE_SCHEMA_VERSION,
        "claude_fable_lkg": lkg,
        "claude_health": health,
    })
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "claude_fable_lkg": validated.lkg,
        "claude_health": validated.health,
    }


def canonical_state_v2(
    lkg: list[dict] | None,
    health: dict,
    *,
    codex_lkg: dict,
    codex_health: dict,
) -> dict:
    payload = canonical_state(lkg, health)
    payload["schema_version"] = STATE_SCHEMA_VERSION_V2
    payload["codex_lkg"] = _validate_codex_lkg(codex_lkg)
    payload["codex_health"] = validate_health(codex_health, provider="codex")
    return payload


def _validate_codex_lkg(row: object) -> dict:
    if not isinstance(row, dict) or set(row) != LKG_ROW_KEYS:
        raise ValueError("invalid Codex LKG row")
    # Codex carries an upstream receipt stamp and the collector's completion
    # stamp; both must be UTC RFC3339 (or null). Null them only for the shared
    # row-shape check, which forbids stamps on the Claude/Fable rows.
    upstream_reported_at = utc_rfc3339(row["upstream_reported_at"])
    probed_at = utc_rfc3339(row["probed_at"])
    if (upstream_reported_at is None) != (probed_at is None):
        raise ValueError("Codex LKG timestamps must be paired")
    if (
        upstream_reported_at is not None
        and upstream_reported_at.replace(microsecond=0) > probed_at.replace(microsecond=0)
    ):
        raise ValueError("Codex upstream timestamp cannot follow collection")
    normalized = dict(row)
    normalized["upstream_reported_at"] = None
    normalized["probed_at"] = None
    _validate_lkg_row(normalized, "codex", "Codex")
    # Re-emit in the canonical LKG key order (matching _validate_lkg_row and the
    # old probe's wire order) so the three-row limits frame is byte-consistent.
    return {key: row[key] for key in (
        "id", "label", "pct", "resets_at_iso", "resets_text",
        "upstream_reported_at", "probed_at",
    )}


class UsageStateStore:
    """Load and atomically replace the usage state object."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        fsync_fn: Callable[[int], None] = os.fsync,
        replace_fn: Callable[[str, str], None] = os.replace,
    ) -> None:
        self.path = Path(path)
        self._fsync = fsync_fn
        self._replace = replace_fn
        self._warned_invalid = False

    def load(self) -> LoadedUsageState:
        if not self.path.exists():
            return LoadedUsageState(lkg=None, health=None)
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            return validate_state(payload)
        except Exception:  # noqa: BLE001 - invalid/missing state has one safe sentinel
            if not self._warned_invalid:
                log.warning("usage state ignored: invalid or unreadable persisted object")
                self._warned_invalid = True
            return LoadedUsageState(lkg=None, health=None)

    def save(
        self,
        lkg: list[dict] | None,
        health: dict,
        *,
        codex_lkg: dict | None = None,
        codex_health: dict | None = None,
    ) -> None:
        try:
            payload = (
                canonical_state(lkg, health)
                if codex_lkg is None and codex_health is None
                else canonical_state_v2(
                    lkg,
                    health,
                    codex_lkg=codex_lkg or {},
                    codex_health=codex_health or {},
                )
            )
        except Exception as exc:  # noqa: BLE001 - classify as pre-replace write failure
            raise UsageStatePreReplaceError("usage state validation failed") from exc

        parent = self.path.parent
        temp_path: str | None = None
        fd: int | None = None
        replaced = False
        try:
            parent.mkdir(parents=True, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=str(parent)
            )
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = None
                json.dump(payload, handle, ensure_ascii=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                self._fsync(handle.fileno())
            self._replace(temp_path, str(self.path))
            replaced = True
            directory_fd = os.open(str(parent), os.O_RDONLY)
            try:
                self._fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except UsageStateError:
            raise
        except Exception as exc:  # noqa: BLE001 - preserve exact pre/post truth
            if replaced:
                raise UsageStatePostReplaceError("usage state directory durability uncertain") from exc
            raise UsageStatePreReplaceError("usage state replace did not commit") from exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass
