"""External collector that persists shared Claude and Codex CLI observations."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from usage_state import (
    UsageStateStore,
    _validate_codex_lkg,
    canonical_state,
)


_CLAUDE_USAGE_FIELDS = frozenset({
    "week_all_pct", "week_all_resets", "week_fable_pct", "week_fable_resets",
})
_CODEX_USAGE_FIELDS = frozenset({
    "pct", "resets_text", "resets_at_iso", "upstream_reported_at",
})
_BENIGN_STATUSES = frozenset({"no_update", "fallback_required"})


def _now() -> str:
    # Keep sub-second precision: the Codex probe stamps ``upstream_reported_at``
    # with microseconds, and the desktop validator requires the collector's
    # completion stamp to not precede it. Truncating here made a same-second
    # completion look earlier than the upstream receipt.
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _row(
    identifier: str,
    label: str,
    *,
    pct: int | None = None,
    resets_text: str | None = None,
    resets_at_iso: str | None = None,
    probed_at: str | None = None,
    upstream_reported_at: str | None = None,
) -> dict:
    if pct is None:
        resets_text = None
        resets_at_iso = None
    return {
        "id": identifier,
        "label": label,
        "pct": pct,
        "resets_text": resets_text,
        "resets_at_iso": resets_at_iso,
        "probed_at": probed_at,
        "upstream_reported_at": upstream_reported_at,
    }


def _require_fields(payload: dict, fields: frozenset[str], provider: str) -> None:
    """Reject a partial CLI object before nullable row mapping can mask it."""
    missing = sorted(fields - set(payload))
    if missing:
        raise ValueError(f"{provider} usage payload missing: {', '.join(missing)}")


def _claude_rows(payload: dict) -> list[dict]:
    """Map the shared ``check_claude_usage.py --json`` canonical payload onto the
    Claude (weekly all-models) and Fable last-known-good rows."""
    _require_fields(payload, _CLAUDE_USAGE_FIELDS, "Claude")
    return [
        _row(
            "claude",
            "Claude",
            pct=payload.get("week_all_pct"),
            resets_text=payload.get("week_all_resets") or None,
        ),
        _row(
            "fable",
            "Fable",
            pct=payload.get("week_fable_pct"),
            resets_text=payload.get("week_fable_resets") or None,
        ),
    ]


def _codex_row(payload: dict, now: str) -> dict:
    """Map the shared ``check_codex_usage.py --json`` weekly payload onto the
    Codex row, stamping ``probed_at`` with this collector's completion time."""
    _require_fields(payload, _CODEX_USAGE_FIELDS, "Codex")
    return _row(
        "codex",
        "Codex",
        pct=payload.get("pct"),
        resets_text=payload.get("resets_text"),
        resets_at_iso=payload.get("resets_at_iso"),
        probed_at=now,
        upstream_reported_at=payload.get("upstream_reported_at"),
    )


def _never() -> dict:
    return {
        "outcome": "never",
        "attempted_at": None,
        "probed_at": None,
        "upstream_reported_at": None,
        "stale_after_seconds": 600,
        "error": None,
    }


def _ok(now: str) -> dict:
    return {
        "outcome": "ok",
        "attempted_at": now,
        "probed_at": now,
        "upstream_reported_at": now,
        "stale_after_seconds": 600,
        "error": None,
    }


def _failed(
    previous: dict | None,
    now: str,
    provider: str = "claude",
    error: Exception | None = None,
) -> dict:
    previous = previous or _never()
    message = str(error).strip() if error is not None else ""
    if not message:
        message = f"{provider.capitalize()} usage provider error"
    return {
        "outcome": "provider_error",
        "attempted_at": now,
        "probed_at": previous["probed_at"],
        "upstream_reported_at": previous["upstream_reported_at"],
        "stale_after_seconds": previous["stale_after_seconds"],
        "error": {
            "code": f"{provider}_usage_provider_error",
            "message": message,
        },
    }


class UsageStateCollector:
    def __init__(
        self,
        *,
        state_path: str | Path,
        claude_command: tuple[str, ...],
        codex_command: tuple[str, ...],
        run=subprocess.run,
        now_fn=_now,
    ) -> None:
        self._store = UsageStateStore(state_path)
        self._claude_command = claude_command
        self._codex_command = codex_command
        self._run = run
        self._now = now_fn

    def _json(self, command: tuple[str, ...]) -> dict | None:
        """Return the CLI's observation dict, or ``None`` for a benign no-update.

        The shared CLIs emit ``{"status": "no_update"|"fallback_required", ...}``
        when they have no fresh pcts; that means "keep the prior good value", not
        a provider failure, so it maps to ``None`` (no observation this cycle). A
        nonzero exit or non-object stdout is a real failure and raises.
        """
        result = self._run(command, capture_output=True, text=True, timeout=75)
        if result.returncode:
            raise RuntimeError(result.stderr or "usage CLI failed")
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError("usage CLI returned a non-object")
        if "status" in value:
            if value["status"] in _BENIGN_STATUSES:
                return None
            raise ValueError("usage CLI returned an unrecognized status")
        return value

    def run_once(self) -> None:
        # Prior state is the retention floor: a failed or no-update provider
        # keeps its prior LKG (None when never observed) so the OTHER provider's
        # fresh observation still persists. Each provider is validated inside its
        # own try, so malformed CLI output routes to that provider's failure path
        # (prior LKG retained, health recorded) instead of aborting the write.
        state = self._store.load()
        lkg = state.lkg
        codex_lkg = state.codex_lkg or _row("codex", "Codex")
        claude_health = state.health or _never()
        codex_health = state.codex_health or _never()
        try:
            payload = self._json(self._claude_command)
            if payload is not None:  # None == benign no-update: keep prior
                rows = _claude_rows(payload)
                done = self._now()
                health = _ok(done)
                canonical_state(rows, health)  # reject malformed rows -> _failed
                lkg, claude_health = rows, health
        except Exception as exc:
            claude_health = _failed(claude_health, self._now(), provider="claude", error=exc)
        try:
            payload = self._json(self._codex_command)
            if payload is not None:
                done = self._now()
                row = _codex_row(payload, done)
                _validate_codex_lkg(row)  # reject malformed row -> _failed
                codex_lkg, codex_health = row, _ok(done)
        except Exception as exc:
            codex_health = _failed(codex_health, self._now(), provider="codex", error=exc)
        self._store.save(lkg, claude_health, codex_lkg=codex_lkg, codex_health=codex_health)
