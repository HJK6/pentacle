"""On-demand per-host Claude/Codex usage readback.

Reads one named host's *current* account-period usage once and returns sanitized,
per-provider rows with provenance. This is the account-period ``/usage`` LIMITS
view (weekly %, resets, health) — NOT the per-stream token-accounting usage that
``agent-orch inspect`` surfaces.

Design (spec: spec_pentacle__per_host_usage_readback_on_demand_2026_10):
  * ``thoth`` / the local host: read the live cadence file written by the
    ``com.pentacle.usage-state-collector`` launchd job (no re-probe). The local
    host is agent-orch's own host id (``~/.agent-orch/config.json``
    ``local_host_id``, then the hostname). A satellite reading itself has no
    cadence file, so it runs the satellite probes below locally, without ssh.
  * a satellite (merlin, amaterasu): read once over the existing authenticated
    ssh/machine-profile route:
      - Claude weekly % from the on-host cache ``~/.claude.json`` →
        ``cachedUsageUtilization`` (plucked on-host so the OAuth token never
        leaves the machine), anchored to the currently logged-in account via
        ``oauthAccount.accountUuid`` and aged via ``fetchedAtMs``;
      - a TUI/OAuth probe fallback (deployed ``check_claude_usage.py``) only when
        the cache has no usable, account-matched weekly number;
      - Codex from the deployed ``check_codex_usage.py``.

Only sanitized numeric fields ever cross the wire; no credentials, account email,
or raw provider UI. ``pct`` is ``None`` for every unavailable outcome — never a
fabricated ``0``.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path


# --- outcome taxonomy -------------------------------------------------------
# Exactly one outcome per provider row. ``pct`` is present only for ok/stale.
OUTCOME_OK = "ok"
OUTCOME_STALE = "stale"
OUTCOME_ACCOUNT_MISMATCH = "account_mismatch"
OUTCOME_NO_WEEKLY_LIMITS = "no_weekly_limits"
OUTCOME_AUTH_ERROR = "auth_error"
OUTCOME_PARSER_ERROR = "parser_error"
OUTCOME_PROVIDER_ERROR = "provider_error"
OUTCOME_TRANSPORT_ERROR = "transport_error"
OUTCOME_TIMEOUT = "timeout"

PCT_OUTCOMES = frozenset({OUTCOME_OK, OUTCOME_STALE})

DEFAULT_MAX_AGE_SECONDS = 600
DEFAULT_RUNTIME_DIR = "~/repos/pentacle-public-runtime"
DEFAULT_CONNECT_TIMEOUT = 8.0
DEFAULT_READ_TIMEOUT = 75.0

#: Transport sentinels used by run_remote -> classifiers (not real provider exits).
EXIT_SSH_TIMEOUT = 124
EXIT_SSH_TRANSPORT = 127

#: Runs on the remote host via ``python3 -``. Reads ``~/.claude.json`` and prints
#: ONLY the allowlisted usage/anchor fields as JSON. The OAuth token and every
#: other field are never read into anything that is printed — this is the
#: token-safety boundary (C3): nothing but these keys leaves the host.
CLAUDE_CACHE_PLUCK = r"""
import json, os
out = {
    "oauth_account_uuid": None,
    "oauth_organization_uuid": None,
    "cache_account_uuid": None,
    "fetched_at_ms": None,
    "seven_day_pct": None,
    "seven_day_resets": None,
    "five_hour_pct": None,
    "five_hour_resets": None,
    "seven_day_fable_pct": None,
    "seven_day_fable_resets": None,
}
try:
    with open(os.path.expanduser("~/.claude.json")) as handle:
        data = json.load(handle)
    oauth = data.get("oauthAccount") or {}
    out["oauth_account_uuid"] = oauth.get("accountUuid")
    out["oauth_organization_uuid"] = oauth.get("organizationUuid")
    cache = data.get("cachedUsageUtilization") or {}
    out["cache_account_uuid"] = cache.get("accountUuid")
    out["fetched_at_ms"] = cache.get("fetchedAtMs")
    util = cache.get("utilization") or {}
    seven = util.get("seven_day") or {}
    five = util.get("five_hour") or {}
    out["seven_day_pct"] = seven.get("utilization")
    out["seven_day_resets"] = seven.get("resets_at")
    out["five_hour_pct"] = five.get("utilization")
    out["five_hour_resets"] = five.get("resets_at")
    fable = util.get("seven_day_fable")
    if isinstance(fable, dict):
        out["seven_day_fable_pct"] = fable.get("utilization")
        out["seven_day_fable_resets"] = fable.get("resets_at")
    else:
        for entry in util.get("limits") or ():
            scope = (entry.get("scope") or {}) if isinstance(entry, dict) else {}
            model = (scope.get("model") or {}) if isinstance(scope, dict) else {}
            if (isinstance(entry, dict) and entry.get("kind") == "weekly_scoped"
                    and isinstance(model, dict)
                    and str(model.get("display_name") or "").strip().casefold() == "fable"):
                out["seven_day_fable_pct"] = entry.get("percent")
                out["seven_day_fable_resets"] = entry.get("resets_at")
                break
except Exception:
    pass
print(json.dumps(out))
"""


class UsageReadbackError(Exception):
    """Sanitized, user-facing error (never carries secrets/raw provider data)."""


# --- host registry (minimal, mirrors chat-stream-v2/machines.py resolution) --
def load_machines(env: dict | None = None) -> list[dict]:
    """Load the host registry from machines.json (inline env, file env, default).

    Returns a list of machine dicts. Mirrors the daemon's resolution order so the
    verb and the daemon agree on host -> ssh_target, without importing the
    (daemon-side, not production-importable) ``machines`` module.
    """
    env = os.environ if env is None else env
    inline = env.get("PENTACLE_MACHINES_JSON")
    if inline:
        payload = json.loads(inline)
    else:
        path = env.get("PENTACLE_MACHINES_FILE") or "~/.config/pentacle-stream/machines.json"
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            # A lone local host is the safe default when no registry is deployed.
            return [{"name": configured_local_host(env), "ssh_target": None}]
        with open(path) as handle:
            payload = json.load(handle)
    machines = payload.get("machines", payload) if isinstance(payload, dict) else payload
    if isinstance(machines, dict):
        machines = list(machines.values())
    return [m for m in machines if isinstance(m, dict)]


def configured_local_host(env: dict | None = None) -> str:
    """PENTACLE_HOST_ID / AGENT_ORCH_HOST_ID, else agent-orch's own host id (config.json, then hostname)."""
    env = os.environ if env is None else env
    return env.get("PENTACLE_HOST_ID") or env.get("AGENT_ORCH_HOST_ID") or _agent_local_host_id()


def _agent_local_host_id() -> str:
    # Imported here: this module is also loaded standalone by the daemon's parity tests.
    from agent_orch.config import local_host_id
    try:
        return local_host_id()
    except (OSError, ValueError, RuntimeError) as exc:
        raise UsageReadbackError(f"cannot determine the local host id ({exc}); set AGENT_ORCH_HOST_ID") from exc


def resolve_host(name: str, env: dict | None = None) -> dict:
    env = os.environ if env is None else env
    machines = load_machines(env)
    for machine in machines:
        if machine.get("name") == name:
            return machine
    known = ", ".join(sorted(m.get("name", "?") for m in machines))
    raise UsageReadbackError(f"unknown host {name!r}; known hosts: {known or '(none)'}")


def is_local(machine: dict, env: dict | None = None) -> bool:
    if machine.get("ssh_target") in (None, ""):
        return True
    return machine.get("name") == configured_local_host(env)


# --- IO seam (monkeypatched in tests) ---------------------------------------
def run_remote(
    ssh_target: str,
    command: str,
    *,
    input_text: str | None = None,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    run=subprocess.run,
) -> tuple[int, str, str]:
    """Run ``command`` on ``ssh_target`` over the authenticated ssh route.

    Returns ``(exit_code, stdout, stderr)``. Never raises; transport/timeout
    failures are mapped to sentinel exit codes with a sanitized stderr.
    """
    argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={int(connect_timeout)}",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        ssh_target,
        command,
    ]
    try:
        proc = run(argv, input=input_text, capture_output=True, text=True, timeout=read_timeout)
    except subprocess.TimeoutExpired:
        return EXIT_SSH_TIMEOUT, "", "ssh read timed out"
    except (OSError, subprocess.SubprocessError) as exc:
        return EXIT_SSH_TRANSPORT, "", f"ssh transport failure ({type(exc).__name__})"
    # OpenSSH itself exits 255 on connect/auth/transport failure (distinct from a
    # remote probe's own non-zero exit). Normalize it to the transport sentinel so
    # classifiers never mistake an unreachable host for a provider/no-weekly result.
    if proc.returncode == 255:
        return EXIT_SSH_TRANSPORT, proc.stdout or "", (proc.stderr or "").strip() or "ssh connect failed (255)"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def run_local(
    ssh_target: str | None,
    command: str,
    *,
    input_text: str | None = None,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    run=subprocess.run,
) -> tuple[int, str, str]:
    """``run_remote``'s contract for the local host: the same probe command, run by ``sh -c`` without ssh."""
    try:
        proc = run(["sh", "-c", command], input=input_text, capture_output=True, text=True, timeout=read_timeout)
    except subprocess.TimeoutExpired:
        return EXIT_SSH_TIMEOUT, "", "local probe timed out"
    except (OSError, subprocess.SubprocessError) as exc:
        return EXIT_SSH_TRANSPORT, "", f"local probe failure ({type(exc).__name__})"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


# --- helpers ----------------------------------------------------------------
def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _iso_from_ms(ms: int | None) -> str | None:
    if not isinstance(ms, (int, float)):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _coerce_pct(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = int(value)
    return value if 0 <= value <= 100 else None


def _parse_json_obj(text: str) -> dict | None:
    """Parse the last non-empty line of ``text`` as a JSON object.

    Returns the dict, or ``None`` when the text is not valid JSON OR parses to a
    non-object (``[]``, ``null``, ``"s"``, ``1``). Guards against ``.get`` on a
    non-dict, which would otherwise raise ``AttributeError`` and crash the read.
    """
    try:
        data = json.loads((text or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return data if isinstance(data, dict) else None


def _row(host, provider, outcome, *, pct=None, resets_at_iso=None, resets_text=None,
         source=None, probed_at=None, fetched_at=None, age_seconds=None,
         five_hour_pct=None, note=None, account_id=None) -> dict:
    if outcome not in PCT_OUTCOMES:
        pct = None  # never a fabricated value for an unavailable outcome
    return {
        "host": host,
        "provider": provider,
        "outcome": outcome,
        "pct": pct,
        "resets_at_iso": resets_at_iso,
        "resets_text": resets_text,
        "source": source,
        "probed_at": probed_at,
        "fetched_at": fetched_at,
        "age_seconds": age_seconds,
        "five_hour_pct": five_hour_pct,
        "note": note,
        "account_id": account_id,
    }


# --- Claude cache classification (pure; C1 anchor + C2 staleness) -----------
def classify_claude_cache(host: str, plucked: dict, *, now_ms: int,
                          max_age_s: int = DEFAULT_MAX_AGE_SECONDS) -> dict:
    """Map the sanitized cache pluck to a row, or signal that fallback is needed.

    Returns either a finished row (outcome ok/stale) or a sentinel
    ``{"fallback": True, "reason": "cache_miss" | "account_mismatch"}``.
    """
    if not isinstance(plucked, dict):  # defensive: non-object pluck -> fall back
        return {"fallback": True, "reason": "cache_miss"}
    seven_pct = _coerce_pct(plucked.get("seven_day_pct"))
    cache_uuid = plucked.get("cache_account_uuid")
    oauth_uuid = plucked.get("oauth_account_uuid")
    if seven_pct is None or not cache_uuid:
        return {"fallback": True, "reason": "cache_miss"}
    # C1 (fail closed): only use the cache when BOTH account ids are present and
    # exactly equal. A missing oauth anchor means we cannot prove the cache
    # belongs to the currently logged-in account, so we must fall back rather than
    # risk presenting a prior account's value as current.
    if not oauth_uuid or cache_uuid != oauth_uuid:
        return {"fallback": True, "reason": "account_mismatch"}
    # C2 (fail closed): require a valid millisecond stamp and compare in ms, so a
    # missing/invalid stamp is never "ok", and 600.x s at a 600 s cutoff is stale.
    fetched_ms = plucked.get("fetched_at_ms")
    if not isinstance(fetched_ms, (int, float)) or isinstance(fetched_ms, bool) or fetched_ms <= 0:
        outcome = OUTCOME_STALE  # value shown, flagged; freshness unverifiable
        age_seconds = None
    else:
        age_ms = now_ms - fetched_ms
        age_seconds = max(0, int(age_ms / 1000))
        outcome = OUTCOME_OK if age_ms <= max_age_s * 1000 else OUTCOME_STALE
    five_pct = _coerce_pct(plucked.get("five_hour_pct"))
    org = plucked.get("oauth_organization_uuid")
    return _row(
        host, "claude", outcome,
        account_id=org if isinstance(org, str) and org else None,
        pct=seven_pct,
        resets_at_iso=plucked.get("seven_day_resets"),
        source="claude-cache",
        fetched_at=_iso_from_ms(fetched_ms),
        probed_at=_iso_from_ms(now_ms),
        age_seconds=age_seconds,
        five_hour_pct=five_pct,
    )


# --- Claude TUI/OAuth fallback classification (pure) ------------------------
def classify_claude_fallback(host: str, exit_code: int, stdout: str, stderr: str,
                             *, mismatch: bool, now_ms: int) -> dict:
    """Classify the deployed check_claude_usage.py result for the fallback path.

    Transport/timeout are distinguished from provider failures; a successful parse
    with a weekly % is ``ok``. When the cache was for a different account and the
    live probe yields no current-account number, the row is ``account_mismatch``
    (never the stale account's value), with the specific sub-reason in ``note``.
    """
    # Compute a single base outcome (and a successful pct row), then apply the
    # account-mismatch wrap uniformly so no branch escapes it (F3).
    stdout = (stdout or "").strip()
    low = (stderr or "").casefold()
    pct = None
    resets_text = None
    if exit_code == EXIT_SSH_TIMEOUT:
        base = OUTCOME_TIMEOUT
    elif exit_code in (EXIT_SSH_TRANSPORT, 255):
        base = OUTCOME_TRANSPORT_ERROR
    elif exit_code == 0 and stdout:
        data = _parse_json_obj(stdout)
        if data is None:  # not valid JSON, or valid but not an object ([], null, "s", 1)
            base = OUTCOME_PARSER_ERROR
        else:
            pct = _coerce_pct(data.get("week_all_pct"))
            resets_text = data.get("week_all_resets")
            base = OUTCOME_OK if pct is not None else OUTCOME_PARSER_ERROR
    elif "sign in" in low or "log in" in low or "logged in" in low:
        base = OUTCOME_AUTH_ERROR
    elif "did not provide labeled weekly" in low:
        base = OUTCOME_TIMEOUT  # the probe's own 60s deadline expired
    elif "must be installed" in low or "not found" in low or "not a trusted" in low:
        base = OUTCOME_PROVIDER_ERROR
    elif exit_code == 0:
        base = OUTCOME_PARSER_ERROR  # exit 0 but nothing to parse
    else:
        base = OUTCOME_PROVIDER_ERROR
    if base == OUTCOME_OK:
        return _row(host, "claude", OUTCOME_OK, pct=pct, resets_text=resets_text,
                    source="claude-probe", probed_at=_iso_from_ms(now_ms))
    if mismatch:
        # Cache was for another account and we could not read a current-account
        # number (whatever the reason): report account_mismatch (pct null, never
        # the stale account's value), keeping the specific cause in `note`.
        return _row(host, "claude", OUTCOME_ACCOUNT_MISMATCH, source="claude-probe", note=base)
    return _row(host, "claude", base, source="claude-probe")


# --- Codex probe classification (pure) --------------------------------------
def classify_codex_probe(host: str, exit_code: int, stdout: str, stderr: str,
                         *, now_ms: int) -> dict:
    if exit_code == EXIT_SSH_TIMEOUT:
        return _row(host, "codex", OUTCOME_TIMEOUT, source="codex-app-server")
    if exit_code in (EXIT_SSH_TRANSPORT, 255):
        return _row(host, "codex", OUTCOME_TRANSPORT_ERROR, source="codex-app-server")
    stdout = (stdout or "").strip()
    if exit_code == 0 and stdout:
        data = _parse_json_obj(stdout)
        if data is None:
            return _row(host, "codex", OUTCOME_PARSER_ERROR, source="codex-app-server")
        pct = _coerce_pct(data.get("pct"))
        if pct is not None:
            return _row(host, "codex", OUTCOME_OK, pct=pct,
                        resets_at_iso=data.get("resets_at_iso"),
                        resets_text=data.get("resets_text"),
                        source="codex-app-server",
                        probed_at=data.get("upstream_reported_at") or _iso_from_ms(now_ms))
        return _row(host, "codex", OUTCOME_NO_WEEKLY_LIMITS, source="codex-app-server")
    low = (stderr or "").casefold()
    if "login" in low or "sign in" in low:
        return _row(host, "codex", OUTCOME_AUTH_ERROR, source="codex-app-server")
    return _row(host, "codex", OUTCOME_PROVIDER_ERROR, source="codex-app-server")


# --- local cadence file -----------------------------------------------------
def local_usage_state_path(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    override = env.get("PENTACLE_USAGE_STATE_PATH")
    if override:
        return Path(os.path.expanduser(override))
    return Path.home() / ".local/share/pentacle-stream/usage_state.json"


def parse_cadence_state(host: str, state: dict) -> list[dict]:
    """Map a schema-v2 usage_state.json into per-provider rows (source=cadence-file)."""
    rows: list[dict] = []
    health = {
        "claude": state.get("claude_health") or {},
        "codex": state.get("codex_health") or {},
    }

    def health_outcome(provider: str) -> str:
        out = (health[provider].get("outcome") or "").strip()
        return out if out else OUTCOME_PROVIDER_ERROR

    lkg_pairs = state.get("claude_fable_lkg") or []
    by_id = {r.get("id"): r for r in lkg_pairs if isinstance(r, dict)}
    for provider in ("claude", "fable"):
        row = by_id.get(provider) or {}
        h = "claude"  # both claude & fable share the claude probe's health
        outcome = health_outcome(h)
        pct = _coerce_pct(row.get("pct"))
        if pct is None and outcome in PCT_OUTCOMES:
            outcome = OUTCOME_NO_WEEKLY_LIMITS
        rows.append(_row(
            host, provider, outcome if pct is not None or outcome not in PCT_OUTCOMES else OUTCOME_NO_WEEKLY_LIMITS,
            pct=pct,
            resets_at_iso=row.get("resets_at_iso"),
            resets_text=row.get("resets_text"),
            source="cadence-file",
            probed_at=row.get("probed_at") or (health[h].get("probed_at")),
        ))

    codex_row = state.get("codex_lkg") or {}
    outcome = health_outcome("codex")
    pct = _coerce_pct(codex_row.get("pct"))
    if pct is None and outcome in PCT_OUTCOMES:
        outcome = OUTCOME_NO_WEEKLY_LIMITS
    rows.append(_row(
        host, "codex", outcome,
        pct=pct,
        resets_at_iso=codex_row.get("resets_at_iso"),
        resets_text=codex_row.get("resets_text"),
        source="cadence-file",
        probed_at=codex_row.get("probed_at") or health["codex"].get("probed_at"),
    ))
    return rows


def read_local(host: str, env: dict | None = None) -> list[dict]:
    path = local_usage_state_path(env)
    if not path.exists():
        return [
            _row(host, "claude", OUTCOME_TRANSPORT_ERROR, source="cadence-file",
                 note="no usage_state.json; is the collector running?"),
            _row(host, "codex", OUTCOME_TRANSPORT_ERROR, source="cadence-file",
                 note="no usage_state.json; is the collector running?"),
        ]
    try:
        with path.open(encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        state = None
    if not isinstance(state, dict):  # unreadable or non-object JSON
        return [
            _row(host, "claude", OUTCOME_PARSER_ERROR, source="cadence-file"),
            _row(host, "codex", OUTCOME_PARSER_ERROR, source="cadence-file"),
        ]
    return parse_cadence_state(host, state)


# --- percent history (usage_history.jsonl) -----------------------------------
# Same line format as chat-stream-v2/usage_history.py (pinned by a parity test;
# agent-orch cannot import daemon modules). Lines are written only on the host
# whose ledger lives beside the file (sessions.db present), i.e. Thoth.
HISTORY_FIELDS = (
    "observed_at", "probed_at", "host", "provider", "account_id",
    "window_kind", "window_minutes", "pct", "resets_at", "source",
)


def _history_iso_from_ms(value) -> str | None:
    """Exact UTC ISO-8601 ``Z`` for positive integer epoch ms; else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    seconds, millis = divmod(value, 1000)
    try:
        stamp = datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return None
    return f"{stamp}.{millis:03d}Z" if millis else f"{stamp}Z"


def _history_iso(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    moment = moment.astimezone(timezone.utc)
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%S")
    millis = moment.microsecond // 1000
    return f"{stamp}.{millis:03d}Z" if millis else f"{stamp}Z"


def claude_cache_history_lines(host: str, plucked: dict, *, probed_at: str | None) -> list[dict]:
    """``cache`` lines from ONE pluck: account only on the same-read C1 match."""
    if not isinstance(plucked, dict) or not probed_at:
        return []
    observed_at = _history_iso_from_ms(plucked.get("fetched_at_ms"))
    if observed_at is None:
        return []
    cache_uuid = plucked.get("cache_account_uuid")
    org = plucked.get("oauth_organization_uuid")
    account_id = org if (
        isinstance(cache_uuid, str) and cache_uuid
        and cache_uuid == plucked.get("oauth_account_uuid")
        and isinstance(org, str) and org
    ) else None
    lines = []
    for window_kind, pct_key, resets_key in (
        ("seven_day", "seven_day_pct", "seven_day_resets"),
        ("seven_day_fable", "seven_day_fable_pct", "seven_day_fable_resets"),
    ):
        raw = plucked.get(pct_key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        pct = int(round(raw))
        if not 0 <= pct <= 100:
            continue
        lines.append({
            "observed_at": observed_at, "probed_at": probed_at, "host": host,
            "provider": "claude", "account_id": account_id, "window_kind": window_kind,
            "window_minutes": 10080, "pct": pct,
            "resets_at": _history_iso(plucked.get(resets_key)), "source": "cache",
        })
    return lines


def history_path(env: dict | None = None) -> Path | None:
    env = os.environ if env is None else env
    # The process environment also counts so a caller-supplied env dict (tests,
    # probes) can never redirect lines into the real ledger host's file.
    override = env.get("PENTACLE_USAGE_HISTORY_PATH") or os.environ.get("PENTACLE_USAGE_HISTORY_PATH")
    if override:
        return Path(os.path.expanduser(override))
    base = Path.home() / ".local/share/pentacle-stream"
    return base / "usage_history.jsonl" if (base / "sessions.db").exists() else None


def _cache_key(line: dict) -> tuple:
    return ("cache", line.get("host"), line.get("observed_at"), line.get("provider"),
            line.get("account_id"), line.get("window_kind"), line.get("window_minutes"),
            line.get("resets_at"), line.get("pct"))


def append_history(lines: list[dict], env: dict | None = None) -> int:
    """Locked append of cache lines not already recorded; returns lines written."""
    path = history_path(env)
    if path is None or not lines:
        return 0
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            seen = set()
            tail = b""
            for raw in handle:
                tail = raw
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(line, dict) and line.get("source") == "cache":
                    seen.add(_cache_key(line))
            out = []
            for line in lines:
                key = _cache_key(line)
                if key in seen:
                    continue
                seen.add(key)
                out.append(json.dumps({field: line[field] for field in HISTORY_FIELDS},
                                      separators=(",", ":")) + "\n")
            if out:
                prefix = "\n" if tail and not tail.endswith(b"\n") else ""
                handle.seek(0, os.SEEK_END)
                handle.write((prefix + "".join(out)).encode("utf-8"))
                handle.flush()
            return len(out)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# --- remote (satellite) read ------------------------------------------------
def _remote_python_bin(machine: dict) -> str:
    # System python3 suffices: both probes and the pluck are stdlib-only.
    return machine.get("usage_python_bin") or "python3"


def _runtime_dir(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    return env.get("PENTACLE_USAGE_RUNTIME_DIR") or DEFAULT_RUNTIME_DIR


def _remote_script(runtime: str, name: str) -> str:
    """A safe shell token for a deployed probe path, allowing only ``~``/``$HOME``.

    ``shlex.quote`` on a leading ``~`` would defeat tilde expansion, but leaving the
    whole path unquoted (or merely double-quoted) would let a crafted
    ``PENTACLE_USAGE_RUNTIME_DIR`` inject ``$(...)``/backticks. So emit a quoted
    ``"$HOME"`` prefix for a home-relative path and ``shlex.quote`` the remainder;
    an absolute path is fully ``shlex.quote``d. Nothing else expands.
    """
    full = f"{runtime}/scripts/{name}"
    for prefix in ("~/", "$HOME/"):
        if full.startswith(prefix):
            return '"$HOME"/' + shlex.quote(full[len(prefix):])
    if full in ("~", "$HOME"):
        return '"$HOME"'
    return shlex.quote(full)


def read_remote_claude(machine: dict, *, now_ms: int, max_age_s: int,
                       env: dict | None = None, runner=None) -> dict:
    env = os.environ if env is None else env
    runner = runner or run_remote
    host = machine.get("name")
    target = machine.get("ssh_target")
    py = _remote_python_bin(machine)
    # Primary: sanitized cache pluck piped over stdin (token never leaves host).
    code, out, err = runner(target, f"{shlex.quote(py)} -", input_text=CLAUDE_CACHE_PLUCK)
    if code == 124:
        return _row(host, "claude", OUTCOME_TIMEOUT, source="claude-cache")
    if code == 127:
        return _row(host, "claude", OUTCOME_TRANSPORT_ERROR, source="claude-cache")
    plucked = {}
    if code == 0 and out.strip():
        try:
            plucked = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            plucked = {}
    try:
        append_history(claude_cache_history_lines(host, plucked, probed_at=_history_iso_from_ms(now_ms)), env)
    except OSError:
        pass  # history is best effort; the readback row is the command's result
    result = classify_claude_cache(host, plucked, now_ms=now_ms, max_age_s=max_age_s)
    if not result.get("fallback"):
        return result
    mismatch = result.get("reason") == "account_mismatch"
    # Fallback: the deployed TUI/OAuth probe, launched in the remote $HOME (trusted).
    runtime = _runtime_dir(env)
    claude_bin = machine.get("claude_bin") or "claude"
    tmux_bin = machine.get("tmux_bin") or "tmux"
    script = _remote_script(runtime, "check_claude_usage.py")
    bin_dir = os.path.dirname(claude_bin) if "/" in claude_bin else ""
    path_prefix = f"PATH={shlex.quote(bin_dir)}:$PATH " if bin_dir else ""
    cmd = (
        f"{path_prefix}{shlex.quote(py)} {script} --json "
        f"--claude {shlex.quote(claude_bin)} --tmux {shlex.quote(tmux_bin)}"
    )
    code, out, err = runner(target, cmd)
    return classify_claude_fallback(host, code, out, err, mismatch=mismatch, now_ms=now_ms)


def read_remote_codex(machine: dict, *, now_ms: int, env: dict | None = None,
                      runner=None) -> dict:
    env = os.environ if env is None else env
    runner = runner or run_remote
    host = machine.get("name")
    target = machine.get("ssh_target")
    py = _remote_python_bin(machine)
    runtime = _runtime_dir(env)
    codex_bin = machine.get("codex_bin") or "codex"
    bin_dir = os.path.dirname(codex_bin) if "/" in codex_bin else ""
    path_prefix = f"PATH={shlex.quote(bin_dir)}:$PATH " if bin_dir else ""
    script = _remote_script(runtime, "check_codex_usage.py")
    cmd = f"{path_prefix}{shlex.quote(py)} {script} --json"
    code, out, err = runner(target, cmd)
    return classify_codex_probe(host, code, out, err, now_ms=now_ms)


def read_remote(machine: dict, *, max_age_s: int = DEFAULT_MAX_AGE_SECONDS,
                env: dict | None = None, runner=None,
                now_ms: int | None = None) -> list[dict]:
    now = now_ms if now_ms is not None else _now_ms()
    runner = runner or run_remote
    return [
        read_remote_claude(machine, now_ms=now, max_age_s=max_age_s, env=env, runner=runner),
        read_remote_codex(machine, now_ms=now, env=env, runner=runner),
    ]


# --- top-level orchestration ------------------------------------------------
def read_host(name: str, *, max_age_s: int = DEFAULT_MAX_AGE_SECONDS,
              env: dict | None = None, runner=None, local_runner=None,
              now_ms: int | None = None) -> list[dict]:
    env = os.environ if env is None else env
    machine = resolve_host(name, env)
    if not is_local(machine, env):
        return read_remote(machine, max_age_s=max_age_s, env=env, runner=runner, now_ms=now_ms)
    if local_usage_state_path(env).exists():  # the collector host (Thoth): live cadence file, no re-probe
        return read_local(machine.get("name", name), env)
    # A satellite runs no collector: run the satellite probes here, without ssh.
    return read_remote(machine, max_age_s=max_age_s, env=env, runner=local_runner or run_local, now_ms=now_ms)


# --- rendering --------------------------------------------------------------
def render_table(rows: list[dict]) -> str:
    lines = [f"{'HOST':<10} {'PROVIDER':<8} {'USAGE':>6} {'OUTCOME':<17} {'SOURCE':<14} RESETS"]
    for r in rows:
        pct = f"{r['pct']}%" if r["pct"] is not None else "—"
        resets = r.get("resets_text") or r.get("resets_at_iso") or ""
        age = r.get("age_seconds")
        if r["outcome"] == OUTCOME_STALE and age is not None:
            resets = (resets + f"  (cache age {age}s)").strip()
        lines.append(
            f"{r['host']:<10} {r['provider']:<8} {pct:>6} "
            f"{r['outcome']:<17} {(r.get('source') or ''):<14} {resets}".rstrip()
        )
    return "\n".join(lines)
