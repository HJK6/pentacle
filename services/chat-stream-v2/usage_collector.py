"""External collector that persists shared Claude and Codex CLI observations."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from usage_history import (
    HISTORY_FILENAME, HistoryLog, claude_cache_lines, claude_probe_lines,
    codex_probe_lines,
)
from usage_accounting import iso_from_ms
from usage_state import (
    UsageStateStore,
    _validate_codex_lkg,
    canonical_state,
    utc_rfc3339,
)


_CLAUDE_USAGE_FIELDS = frozenset({
    "week_all_pct", "week_all_resets", "week_fable_pct", "week_fable_resets",
})
_CODEX_USAGE_FIELDS = frozenset({
    "pct", "resets_text", "resets_at_iso", "upstream_reported_at",
})
_BENIGN_STATUSES = frozenset({"no_update", "fallback_required"})
PROBE_TIMEOUT_SECONDS = 75
PINNED_EXECUTABLE_MISSING = "pinned_executable_missing"
log = logging.getLogger("chat_streamd_v2.usage_collector")


def probe_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for the probe children: OAuth path explicitly disabled, no token variables.

    The CLI ``/usage`` scrape is the observation; the optional OAuth-token probe stays off
    whatever the caller's environment says.
    """
    env = {
        key: value for key, value in (os.environ if base is None else base).items()
        if "TOKEN" not in key.upper() and "SECRET" not in key.upper()
    }
    env["PENTACLE_USAGE_CLAUDE_OAUTH"] = "0"
    return env


def bounded_run(command, *, capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, env=None):
    """``subprocess.run`` that owns the child's whole process group.

    A timeout kills the group (the probe's tmux server / ``codex app-server`` children
    included), reaps it, and raises ``TimeoutExpired`` so the next tick starts clean.
    """
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None, text=text, env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.communicate(timeout=5)
        except Exception:  # noqa: BLE001 - the group is already dead; never hang the next tick
            pass
        raise
    # Sweep stragglers the probe left in its group after a normal exit.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


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


def _claude_identity(data: object, *, started: str, ended: str) -> tuple[str | None, str | None]:
    """(account_id, cache_observed_at) from ONE read of ``~/.claude.json``.

    The account is the OAuth organization only when the cache and the OAuth login name the same
    ``accountUuid`` in this read (C1); otherwise null. The cache stamp is returned only when this
    run refreshed it (``started <= fetchedAtMs <= ended``); an older stamp is never adopted.
    Nothing else in the file is read, so no token field can reach the state.
    """
    if not isinstance(data, dict):
        return None, None
    cache = data.get("cachedUsageUtilization")
    oauth = data.get("oauthAccount")
    cache = cache if isinstance(cache, dict) else {}
    oauth = oauth if isinstance(oauth, dict) else {}
    cache_account = cache.get("accountUuid")
    org = oauth.get("organizationUuid")
    account_id = org if (
        isinstance(cache_account, str) and cache_account
        and cache_account == oauth.get("accountUuid")
        and isinstance(org, str) and org
    ) else None
    fetched = iso_from_ms(cache.get("fetchedAtMs"))
    refreshed = None
    if fetched is not None and account_id is not None:
        moment = utc_rfc3339(fetched)
        if utc_rfc3339(started) <= moment <= utc_rfc3339(ended):
            refreshed = fetched
    return account_id, refreshed


def _collection(status: str, attempted_at: str, error: str | None = None) -> dict:
    return {"status": status, "attempted_at": attempted_at, "error": error}


def _failure_code(provider: str, error: Exception | None) -> str:
    text = str(error) if error is not None else ""
    if PINNED_EXECUTABLE_MISSING in text:
        return PINNED_EXECUTABLE_MISSING
    if isinstance(error, subprocess.TimeoutExpired):
        return f"{provider}_usage_timeout"
    return f"{provider}_usage_provider_error"


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
        run=bounded_run,
        now_fn=_now,
        claude_config_path: str | Path | None = None,
        history_path: str | Path | None = None,
        host: str | None = None,
    ) -> None:
        self._store = UsageStateStore(state_path)
        self._claude_command = claude_command
        self._codex_command = codex_command
        self._run = run
        self._now = now_fn
        #: ``~/.claude.json`` for same-observation ``cache`` history lines;
        #: None disables the cache reader (the display probe is unchanged).
        self._claude_config_path = Path(claude_config_path).expanduser() if claude_config_path else None
        self._history = HistoryLog(
            Path(history_path) if history_path else Path(state_path).with_name(HISTORY_FILENAME)
        )
        self._host = host or os.environ.get("PENTACLE_HOST_ID") or os.environ.get("AGENT_ORCH_HOST_ID") or "thoth"
        self._history_lines: list[dict] = []

    def _read_claude_config(self) -> object:
        if self._claude_config_path is None:
            return None
        try:
            with self._claude_config_path.open(encoding="utf-8") as handle:
                return json.load(handle)
        except Exception as exc:  # noqa: BLE001 - identity is best effort; null account is the safe value
            log.warning("claude cache unreadable for usage identity: %s", type(exc).__name__)
            return None

    def _record_history(self) -> None:
        """Append this run's percent observations; never fails the state write."""
        lines = self._history_lines
        self._history_lines = []
        if self._claude_config_path is not None:
            try:
                with self._claude_config_path.open(encoding="utf-8") as handle:
                    data = json.load(handle)
                lines = claude_cache_lines(data, host=self._host, probed_at=self._now()) + lines
            except Exception as exc:  # noqa: BLE001 - history never fails the collector run
                log.warning("claude cache unreadable for usage history: %s", type(exc).__name__)
        try:
            self._history.append(lines)
        except Exception as exc:  # noqa: BLE001 - history never fails the collector run
            log.warning("usage history append failed: %s", exc)

    def _json(self, command: tuple[str, ...]) -> dict | None:
        """Return the CLI's observation dict, or ``None`` for a benign no-update.

        The shared CLIs emit ``{"status": "no_update"|"fallback_required", ...}``
        when they have no fresh pcts; that means "keep the prior good value", not
        a provider failure, so it maps to ``None`` (no observation this cycle). A
        nonzero exit or non-object stdout is a real failure and raises.
        """
        result = self._run(
            command, capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, env=probe_environment(),
        )
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
        # The observation tuple keeps its old observed_at/account_id on a failed or no-update
        # collection: nothing is re-stamped from poll time and no earlier account is freshened.
        observations = {
            provider: dict(entry)
            for provider, entry in (state.observations or {}).items()
        }
        for provider in ("claude", "codex"):
            observations.setdefault(provider, {"observed_at": None, "account_id": None, "collection": None})
        started = self._now()
        try:
            payload = self._json(self._claude_command)
            if payload is not None:  # None == benign no-update: keep prior
                rows = _claude_rows(payload)
                done = self._now()
                health = _ok(done)
                canonical_state(rows, health)  # reject malformed rows -> _failed
                lkg, claude_health = rows, health
                self._history_lines += claude_probe_lines(payload, host=self._host, probed_at=done)
                account_id, refreshed = _claude_identity(self._read_claude_config(), started=started, ended=self._now())
                observations["claude"] = {
                    "observed_at": refreshed or done, "account_id": account_id,
                    "collection": _collection("ok", done),
                }
            else:
                observations["claude"]["collection"] = _collection("no_update", self._now())
        except Exception as exc:
            claude_health = _failed(claude_health, self._now(), provider="claude", error=exc)
            observations["claude"]["collection"] = _collection(
                "failed", claude_health["attempted_at"], _failure_code("claude", exc))
        try:
            payload = self._json(self._codex_command)
            if payload is not None:
                done = self._now()
                row = _codex_row(payload, done)
                _validate_codex_lkg(row)  # reject malformed row -> _failed
                codex_lkg, codex_health = row, _ok(done)
                self._history_lines += codex_probe_lines(payload, host=self._host, probed_at=done)
                observations["codex"] = {
                    "observed_at": payload.get("upstream_reported_at"), "account_id": None,
                    "collection": _collection("ok", done),
                }
            else:
                observations["codex"]["collection"] = _collection("no_update", self._now())
        except Exception as exc:
            codex_health = _failed(codex_health, self._now(), provider="codex", error=exc)
            observations["codex"]["collection"] = _collection(
                "failed", codex_health["attempted_at"], _failure_code("codex", exc))
        self._store.save(
            lkg, claude_health, codex_lkg=codex_lkg, codex_health=codex_health, observations=observations,
        )
        self._record_history()
