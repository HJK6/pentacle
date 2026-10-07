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
    ssh/machine-profile route (spec_pentacle__satellite_usage_freshness_collector_2026_10):
      - the satellite's own cadence file (``usage_state.json`` written by its
        collector) and the on-host cache ``~/.claude.json`` →
        ``cachedUsageUtilization`` are plucked together in one ssh call, on-host and
        allowlisted, so no token, env or capture data leaves the machine. The
        cache is anchored to the currently logged-in account via
        ``oauthAccount.accountUuid`` and aged via ``fetchedAtMs``;
      - per provider the freshest account-matched observation is selected
        (``select_claude_row``): a fresh, ok, account-matched cadence row first, a
        newer cache observation next, else the newest matched value as ``stale``;
        every row states ``observed_at``, ``age_seconds``, ``account_id`` and the
        collector's ``collection`` status, so a stale or failed reading is never
        printed as fresh;
      - a TUI/OAuth probe fallback (deployed ``check_claude_usage.py``) only when
        no account-matched observation exists;
      - Codex from the cadence row, else the deployed ``check_codex_usage.py``.

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

#: Two 600 s collector ticks fit with one missed tick of slack.
DEFAULT_MAX_AGE_SECONDS = 900
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
    "cadence": None,
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
try:
    path = os.environ.get("PENTACLE_USAGE_STATE_PATH") or os.path.expanduser(
        "~/.local/share/pentacle-stream/usage_state.json")
    with open(path) as handle:
        state = json.load(handle)
    def keep(source, keys):
        return {key: source.get(key) for key in keys} if isinstance(source, dict) else None
    observations = state.get("observations")
    out["cadence"] = {
        "claude_fable_lkg": [keep(row, ("id", "pct", "resets_at_iso", "resets_text"))
                             for row in state.get("claude_fable_lkg") or () if isinstance(row, dict)],
        "claude_health": keep(state.get("claude_health"), ("outcome", "attempted_at", "upstream_reported_at")),
        "codex_lkg": keep(state.get("codex_lkg"), ("pct", "resets_at_iso", "resets_text", "upstream_reported_at")),
        "codex_health": keep(state.get("codex_health"), ("outcome", "attempted_at", "upstream_reported_at")),
        "observations": None if not isinstance(observations, dict) else {
            name: {"observed_at": entry.get("observed_at"), "account_id": entry.get("account_id"),
                   "collection": keep(entry.get("collection"), ("status", "attempted_at", "error"))}
            for name, entry in observations.items() if isinstance(entry, dict)},
    }
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
         five_hour_pct=None, note=None, account_id=None, observed_at=None, collector=None) -> dict:
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
        # The full tuple: when the value was observed, for which account, and what the
        # host's collector last did (None when the host has no cadence file).
        "status": outcome,
        "observed_at": observed_at if observed_at is not None else fetched_at,
        "collector": collector,
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


# --- cadence observation + per-provider selection (pure) --------------------
COLLECTOR_STATUSES = frozenset({"ok", "no_update", "failed"})


def _parse_iso_ms(value) -> int | None:
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
    return int(moment.timestamp() * 1000)


def _cadence_observation(cadence, provider: str) -> dict | None:
    """The cadence file's tuple for ``provider`` (claude|codex), or None without a usable stamp.

    A file written before the observation tuple existed has none; its provider health stamps
    stand in (``upstream_reported_at``), with no account and a status mapped from its outcome.
    """
    if not isinstance(cadence, dict):
        return None
    entry = (cadence.get("observations") or {}).get(provider) if isinstance(cadence.get("observations"), dict) else None
    if isinstance(entry, dict):
        collection = entry.get("collection") if isinstance(entry.get("collection"), dict) else {}
        status = collection.get("status") if collection.get("status") in COLLECTOR_STATUSES else "failed"
        account = entry.get("account_id")
        return {
            "observed_ms": _parse_iso_ms(entry.get("observed_at")),
            "account_id": account if isinstance(account, str) and account else None,
            "collector": {"status": status, "attempted_at": collection.get("attempted_at"),
                          "error": collection.get("error") if isinstance(collection.get("error"), str) else None},
            "legacy": False,
        }
    health = cadence.get(f"{provider}_health")
    if not isinstance(health, dict):
        return None
    return {
        "observed_ms": _parse_iso_ms(health.get("upstream_reported_at")),
        "account_id": None,
        "collector": {"status": "ok" if health.get("outcome") == "ok" else "failed",
                      "attempted_at": health.get("attempted_at"), "error": None},
        "legacy": True,
    }


def _cadence_candidate(cadence, provider: str, lkg_id: str | None = None) -> dict | None:
    """A cadence value with its tuple, or None when the file holds no usable percentage/stamp."""
    obs = _cadence_observation(cadence, provider)
    if obs is None:
        return None
    if provider == "codex":
        lkg = cadence.get("codex_lkg")
    else:
        rows = {r.get("id"): r for r in cadence.get("claude_fable_lkg") or [] if isinstance(r, dict)}
        lkg = rows.get(lkg_id or "claude")
    lkg = lkg if isinstance(lkg, dict) else {}
    pct = _coerce_pct(lkg.get("pct"))
    observed_ms = obs["observed_ms"]
    if observed_ms is None and provider == "codex":
        observed_ms = _parse_iso_ms(lkg.get("upstream_reported_at"))
    if pct is None or observed_ms is None:
        return {"collector": obs["collector"], "usable": False}
    return {**obs, "observed_ms": observed_ms, "pct": pct, "usable": True,
            "resets_at_iso": lkg.get("resets_at_iso"), "resets_text": lkg.get("resets_text")}


def _age_s(observed_ms: int, now_ms: int) -> int:
    return max(0, int((now_ms - observed_ms) / 1000))


def _cadence_row(host, provider, cand, *, now_ms, max_age_s, account_id=None) -> dict:
    """``ok`` only for a collector-ok observation within ``max_age_s``; anything else is ``stale``."""
    age_ms = now_ms - cand["observed_ms"]
    fresh = cand["collector"]["status"] == "ok" and age_ms <= max_age_s * 1000
    return _row(
        host, provider, OUTCOME_OK if fresh else OUTCOME_STALE,
        pct=cand["pct"], resets_at_iso=cand["resets_at_iso"], resets_text=cand["resets_text"],
        source="cadence-file", probed_at=_iso_from_ms(now_ms), fetched_at=_iso_from_ms(cand["observed_ms"]),
        age_seconds=_age_s(cand["observed_ms"], now_ms), account_id=account_id,
        observed_at=_iso_from_ms(cand["observed_ms"]), collector=cand["collector"],
    )


def select_claude_row(host: str, plucked: dict, *, now_ms: int, max_age_s: int = DEFAULT_MAX_AGE_SECONDS,
                      provider: str = "claude", use_cache: bool = True,
                      account_matching: bool = True) -> dict:
    """Pick the freshest account-matched Claude observation (spec Target State 3).

    (a) a cadence observation that is collector-``ok``, account-matched to the host's current
        login (C1) and within ``max_age_s`` -> ``ok``, ``source=cadence-file``;
    (b) else a cache observation that is newer than the cadence one -> ``source=claude-cache``
        with its honest age, still reporting the collector's own status;
    (c) else the newest account-matched value from either source as ``stale``.
    Returns a finished row, or ``{"fallback": True, "reason": ...}`` when nothing matched.
    ``account_matching=False`` (a host reading its own cadence file, with no cache pluck)
    trusts the file's account and reports it; ``use_cache=False`` skips the cache source.
    """
    plucked = plucked if isinstance(plucked, dict) else {}
    cadence = plucked.get("cadence")
    cand = _cadence_candidate(cadence, "claude", lkg_id=provider)
    collector = cand["collector"] if cand else None
    current_org = plucked.get("oauth_organization_uuid")
    matched = bool(cand and cand.get("usable") and (
        not account_matching or (cand["account_id"] and cand["account_id"] == current_org)))
    cache = classify_claude_cache(host, plucked, now_ms=now_ms, max_age_s=max_age_s) if use_cache else {
        "fallback": True, "reason": "cache_miss"}
    cache_row = None if cache.get("fallback") else cache
    if matched and cand["collector"]["status"] == "ok" and now_ms - cand["observed_ms"] <= max_age_s * 1000:
        return _cadence_row(host, provider, cand, now_ms=now_ms, max_age_s=max_age_s, account_id=cand["account_id"])
    cache_ms = _parse_iso_ms(cache_row["fetched_at"]) if cache_row else None
    if cache_row and (not matched or (cache_ms or 0) > cand["observed_ms"]):
        return {**cache_row, "collector": collector}
    if matched:
        return _cadence_row(host, provider, cand, now_ms=now_ms, max_age_s=max_age_s, account_id=cand["account_id"])
    mismatch = (cache.get("reason") == "account_mismatch"
                or bool(cand and cand.get("usable") and cand["account_id"] and cand["account_id"] != current_org))
    return {"fallback": True, "reason": "account_mismatch" if mismatch else "cache_miss", "collector": collector}


def select_codex_row(host: str, plucked: dict, *, now_ms: int, max_age_s: int = DEFAULT_MAX_AGE_SECONDS) -> dict | None:
    """The cadence Codex row when it is fresh and collector-ok; otherwise None (caller probes)."""
    cand = _cadence_candidate((plucked or {}).get("cadence"), "codex")
    if cand and cand.get("usable") and cand["collector"]["status"] == "ok" \
            and now_ms - cand["observed_ms"] <= max_age_s * 1000:
        return _cadence_row(host, "codex", cand, now_ms=now_ms, max_age_s=max_age_s)
    return None


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


def _legacy_cadence_rows(host: str, state: dict) -> list[dict]:
    """Rows from the file's own health/LKG only (the pre-tuple mapping)."""
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


def parse_cadence_state(host: str, state: dict, *, now_ms: int | None = None,
                        max_age_s: int = DEFAULT_MAX_AGE_SECONDS) -> list[dict]:
    """Map a schema-v2 usage_state.json into per-provider rows (source=cadence-file).

    With the observation tuple, each row states its observed time, age, account and the
    collector's status, and is ``ok`` only while fresh and collector-ok. A file written
    before the tuple existed keeps its health outcome, gaining only a stamp-derived age.
    """
    now = now_ms if now_ms is not None else _now_ms()
    tupled = isinstance(state.get("observations"), dict)
    rows = _legacy_cadence_rows(host, state)
    for index, (provider, key) in enumerate((("claude", "claude"), ("fable", "claude"), ("codex", "codex"))):
        row = rows[index]
        cand = _cadence_candidate(state, key, lkg_id=provider)
        if cand is None:
            continue
        row["collector"] = cand["collector"]
        if not cand.get("usable"):
            continue
        if tupled:
            rows[index] = _cadence_row(
                host, provider, cand, now_ms=now, max_age_s=max_age_s, account_id=cand["account_id"])
        elif row["pct"] is not None:  # legacy file: stamp-derived age only
            row["observed_at"] = row["fetched_at"] = _iso_from_ms(cand["observed_ms"])
            row["age_seconds"] = _age_s(cand["observed_ms"], now)
            if row["outcome"] == OUTCOME_OK and now - cand["observed_ms"] > max_age_s * 1000:
                row["outcome"] = row["status"] = OUTCOME_STALE
    return rows


def read_local(host: str, env: dict | None = None, *, now_ms: int | None = None,
               max_age_s: int = DEFAULT_MAX_AGE_SECONDS) -> list[dict]:
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
    return parse_cadence_state(host, state, now_ms=now_ms, max_age_s=max_age_s)


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


def _pluck(machine: dict, runner) -> tuple[int, dict]:
    """One ssh call: the sanitized cache + cadence pluck piped over stdin (token never leaves host)."""
    code, out, _err = runner(machine.get("ssh_target"), f"{shlex.quote(_remote_python_bin(machine))} -",
                             input_text=CLAUDE_CACHE_PLUCK)
    plucked = {}
    if code == 0 and out.strip():
        try:
            plucked = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            plucked = {}
    return code, plucked if isinstance(plucked, dict) else {}


def read_remote_claude(machine: dict, *, now_ms: int, max_age_s: int,
                       env: dict | None = None, runner=None, pluck: tuple[int, dict] | None = None) -> dict:
    env = os.environ if env is None else env
    runner = runner or run_remote
    host = machine.get("name")
    target = machine.get("ssh_target")
    py = _remote_python_bin(machine)
    code, plucked = pluck if pluck is not None else _pluck(machine, runner)
    if code == 124:
        return _row(host, "claude", OUTCOME_TIMEOUT, source="claude-cache")
    if code == 127:
        return _row(host, "claude", OUTCOME_TRANSPORT_ERROR, source="claude-cache")
    try:
        append_history(claude_cache_history_lines(host, plucked, probed_at=_history_iso_from_ms(now_ms)), env)
    except OSError:
        pass  # history is best effort; the readback row is the command's result
    result = select_claude_row(host, plucked, now_ms=now_ms, max_age_s=max_age_s)
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
        f"{path_prefix}PENTACLE_CLAUDE_BIN={shlex.quote(claude_bin)} {shlex.quote(py)} {script} --json "
        f"--claude {shlex.quote(claude_bin)} --tmux {shlex.quote(tmux_bin)}"
    )
    code, out, err = runner(target, cmd)
    row = classify_claude_fallback(host, code, out, err, mismatch=mismatch, now_ms=now_ms)
    row["collector"] = result.get("collector")
    return row


def read_remote_codex(machine: dict, *, now_ms: int, env: dict | None = None,
                      runner=None, plucked: dict | None = None,
                      max_age_s: int = DEFAULT_MAX_AGE_SECONDS) -> dict:
    env = os.environ if env is None else env
    runner = runner or run_remote
    host = machine.get("name")
    target = machine.get("ssh_target")
    cadence_row = select_codex_row(host, plucked or {}, now_ms=now_ms, max_age_s=max_age_s)
    if cadence_row is not None:
        return cadence_row
    py = _remote_python_bin(machine)
    runtime = _runtime_dir(env)
    codex_bin = machine.get("codex_bin") or "codex"
    bin_dir = os.path.dirname(codex_bin) if "/" in codex_bin else ""
    path_prefix = f"PATH={shlex.quote(bin_dir)}:$PATH " if bin_dir else ""
    script = _remote_script(runtime, "check_codex_usage.py")
    cmd = f"{path_prefix}PENTACLE_CODEX_BIN={shlex.quote(codex_bin)} {shlex.quote(py)} {script} --json"
    code, out, err = runner(target, cmd)
    row = classify_codex_probe(host, code, out, err, now_ms=now_ms)
    cand = _cadence_candidate((plucked or {}).get("cadence"), "codex")
    if cand is not None:
        row["collector"] = cand["collector"]
        if row["outcome"] != OUTCOME_OK and cand.get("usable"):
            # The live read failed; the cadence value is older, so it is shown aged, never fresh.
            return _cadence_row(host, "codex", cand, now_ms=now_ms, max_age_s=max_age_s)
    if row["outcome"] == OUTCOME_OK and row["observed_at"] is None:
        row["observed_at"] = row["probed_at"]
    return row


def read_remote(machine: dict, *, max_age_s: int = DEFAULT_MAX_AGE_SECONDS,
                env: dict | None = None, runner=None,
                now_ms: int | None = None) -> list[dict]:
    now = now_ms if now_ms is not None else _now_ms()
    runner = runner or run_remote
    pluck = _pluck(machine, runner)
    return [
        read_remote_claude(machine, now_ms=now, max_age_s=max_age_s, env=env, runner=runner, pluck=pluck),
        read_remote_codex(machine, now_ms=now, env=env, runner=runner, plucked=pluck[1], max_age_s=max_age_s),
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
        return read_local(machine.get("name", name), env, now_ms=now_ms, max_age_s=max_age_s)
    # A satellite runs no collector: run the satellite probes here, without ssh.
    return read_remote(machine, max_age_s=max_age_s, env=env, runner=local_runner or run_local, now_ms=now_ms)


# --- rendering --------------------------------------------------------------
def _human_age(seconds) -> str:
    if not isinstance(seconds, (int, float)):
        return "—"
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def render_table(rows: list[dict]) -> str:
    lines = [f"{'HOST':<10} {'PROVIDER':<8} {'USAGE':>6} {'STATUS':<17} {'SOURCE':<14} "
             f"{'OBSERVED (UTC)':<20} {'AGE':>5} {'ACCOUNT':<8} {'COLLECTOR':<9} RESETS"]
    for r in rows:
        pct = f"{r['pct']}%" if r["pct"] is not None else "—"
        resets = r.get("resets_text") or r.get("resets_at_iso") or ""
        observed = (r.get("observed_at") or "—")[:20]
        account = (r.get("account_id") or "—")[:8]
        collector = ((r.get("collector") or {}).get("status")) or "—"
        lines.append(
            f"{r['host']:<10} {r['provider']:<8} {pct:>6} "
            f"{r['outcome']:<17} {(r.get('source') or ''):<14} "
            f"{observed:<20} {_human_age(r.get('age_seconds')):>5} {account:<8} {collector:<9} {resets}".rstrip()
        )
    return "\n".join(lines)
