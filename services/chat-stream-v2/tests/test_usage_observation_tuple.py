"""Per-provider observation tuple (observed_at, account_id, collection) in usage_state.json.

Spec: spec_pentacle__satellite_usage_freshness_collector_2026_10 (AC2, AC6, AC7).
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import usage_history
from usage_collector import UsageStateCollector, bounded_run
from usage_history import HistoryLog, claude_cache_lines, claude_probe_lines, codex_probe_lines
from usage_state import UsageStateError, UsageStateStore, canonical_state, validate_state


T1 = "2026-10-07T15:00:00Z"
T2 = "2026-10-07T15:10:00Z"
ORG, ORG_OLD = "org-current", "org-previous"
ACCT, ACCT_OLD = "acct-current", "acct-previous"
REPO_ROOT = Path(__file__).resolve().parents[3]


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _claude_payload() -> dict:
    return {
        "week_all_pct": 80, "week_all_resets": "Oct 11 at 2am (America/Chicago)",
        "week_fable_pct": 12, "week_fable_resets": "Oct 11 at 2am (America/Chicago)",
    }


def _codex_payload(at: str) -> dict:
    return {"pct": 10, "resets_text": "Oct 13 at 10:28pm (CDT)",
            "resets_at_iso": "2026-10-14T03:28:51Z", "upstream_reported_at": at}


def _config(tmp_path: Path, *, cache_account=ACCT, oauth_account=ACCT, org=ORG, fetched=T1,
            fable=True) -> Path:
    utilization = {"seven_day": {"utilization": 80, "resets_at": "2026-10-11T07:00:00Z"}}
    if fable:
        utilization["seven_day_fable"] = {"utilization": 12, "resets_at": "2026-10-11T07:00:00Z"}
    path = tmp_path / "claude.json"
    path.write_text(json.dumps({
        "oauthAccount": {"accountUuid": oauth_account, "organizationUuid": org},
        "cachedUsageUtilization": {
            "accountUuid": cache_account, "fetchedAtMs": _ms(fetched), "utilization": utilization,
        },
    }))
    return path


class Clock:
    """Constant clock, or (step > 0) one that advances ``step`` seconds per call."""

    def __init__(self, value: str, step: int = 0) -> None:
        self.moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        self.step = step

    def __call__(self) -> str:
        value = self.moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.moment = datetime.fromtimestamp(self.moment.timestamp() + self.step, tz=timezone.utc)
        return value


def _collector(tmp_path: Path, clock: Clock, *, claude=None, codex=None, config: Path | None = None,
               seen_env: list | None = None) -> UsageStateCollector:
    def run(command, **kwargs):
        if seen_env is not None:
            seen_env.append((command[0], kwargs.get("env")))
        outcome = claude if command[0] == "claude" else codex
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, subprocess.CompletedProcess):
            return outcome
        return subprocess.CompletedProcess(command, 0, json.dumps(outcome), "")

    return UsageStateCollector(
        state_path=tmp_path / "usage_state.json", claude_command=("claude",), codex_command=("codex",),
        run=run, now_fn=clock, claude_config_path=config, host="amaterasu",
    )


def _obs(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "usage_state.json").read_text())["observations"]


# --- AC2: the tuple ---------------------------------------------------------------------------

def test_c1_match_stamps_org_account_and_provider_observation_time(tmp_path):
    clock = Clock(T1)
    _collector(tmp_path, clock, claude=_claude_payload(), codex=_codex_payload(T1),
               config=_config(tmp_path)).run_once()
    obs = _obs(tmp_path)
    assert obs["claude"] == {
        "observed_at": T1, "account_id": ORG,
        "collection": {"status": "ok", "attempted_at": T1, "error": None},
    }
    assert obs["codex"] == {
        "observed_at": T1, "account_id": None,
        "collection": {"status": "ok", "attempted_at": T1, "error": None},
    }
    # The file stays valid under the canonical validators (additive field).
    state = json.loads((tmp_path / "usage_state.json").read_text())
    assert validate_state(state).observations == state["observations"]


def test_claude_observed_at_prefers_the_cache_stamp_refreshed_by_this_run(tmp_path):
    # started 15:00:00, scrape completes 15:00:10, cache read at 15:00:20; the CLI refreshed at :05.
    _collector(tmp_path, Clock("2026-10-07T15:00:00Z", step=10), claude=_claude_payload(),
               codex=_codex_payload("2026-10-07T15:00:00Z"),
               config=_config(tmp_path, fetched="2026-10-07T15:00:05Z")).run_once()
    claude = _obs(tmp_path)["claude"]
    assert claude["observed_at"] == "2026-10-07T15:00:05Z"
    assert claude["collection"]["attempted_at"] == "2026-10-07T15:00:10Z"


def test_old_cache_stamp_is_never_adopted_as_the_observation_time(tmp_path):
    # The scrape just succeeded; a cache last refreshed hours ago must not back-date it.
    clock = Clock(T2)
    _collector(tmp_path, clock, claude=_claude_payload(), codex=_codex_payload(T2),
               config=_config(tmp_path, fetched="2026-10-07T07:35:22Z")).run_once()
    assert _obs(tmp_path)["claude"]["observed_at"] == T2


def test_c1_mismatch_leaves_account_null(tmp_path):
    _collector(tmp_path, Clock(T1), claude=_claude_payload(), codex=_codex_payload(T1),
               config=_config(tmp_path, cache_account=ACCT_OLD)).run_once()
    claude = _obs(tmp_path)["claude"]
    assert claude["account_id"] is None
    assert claude["collection"]["status"] == "ok"
    assert claude["observed_at"] == T1


def test_unreadable_config_leaves_account_null(tmp_path):
    _collector(tmp_path, Clock(T1), claude=_claude_payload(), codex=_codex_payload(T1),
               config=tmp_path / "missing.json").run_once()
    assert _obs(tmp_path)["claude"]["account_id"] is None


def test_failed_collection_keeps_old_value_stamp_and_account(tmp_path):
    config = _config(tmp_path)
    _collector(tmp_path, Clock(T1), claude=_claude_payload(), codex=_codex_payload(T1), config=config).run_once()
    before = UsageStateStore(tmp_path / "usage_state.json").load()
    # Account switched on the host; the next probe fails. Nothing is re-stamped or re-attributed.
    config = _config(tmp_path, cache_account=ACCT_OLD + "-2", oauth_account=ACCT_OLD + "-2", org=ORG_OLD)
    _collector(tmp_path, Clock(T2), claude=RuntimeError("boom"), codex=RuntimeError("boom"),
               config=config).run_once()
    obs = _obs(tmp_path)
    for provider in ("claude", "codex"):
        assert obs[provider]["observed_at"] == T1
        assert obs[provider]["collection"]["status"] == "failed"
        assert obs[provider]["collection"]["attempted_at"] == T2
        assert obs[provider]["collection"]["error"]
    assert obs["claude"]["account_id"] == ORG  # never the new org, never freshened
    after = UsageStateStore(tmp_path / "usage_state.json").load()
    assert after.lkg == before.lkg and after.codex_lkg == before.codex_lkg


def test_no_update_keeps_old_value_and_ages(tmp_path):
    _collector(tmp_path, Clock(T1), claude=_claude_payload(), codex=_codex_payload(T1),
               config=_config(tmp_path)).run_once()
    _collector(tmp_path, Clock(T2), claude={"status": "no_update"}, codex={"status": "fallback_required"},
               config=_config(tmp_path, fetched=T2)).run_once()
    obs = _obs(tmp_path)
    assert obs["claude"]["observed_at"] == T1 and obs["claude"]["account_id"] == ORG
    assert obs["claude"]["collection"] == {"status": "no_update", "attempted_at": T2, "error": None}
    assert obs["codex"]["observed_at"] == T1
    assert obs["codex"]["collection"]["status"] == "no_update"


def test_first_ever_failure_has_no_observation(tmp_path):
    _collector(tmp_path, Clock(T1), claude=RuntimeError("x"), codex=_codex_payload(T1)).run_once()
    claude = _obs(tmp_path)["claude"]
    assert claude["observed_at"] is None and claude["account_id"] is None
    assert claude["collection"]["status"] == "failed"


def test_pinned_executable_missing_is_a_distinct_failure_code(tmp_path):
    missing = subprocess.CompletedProcess(("claude",), 1, "", "pinned_executable_missing: PENTACLE_CLAUDE_BIN")
    _collector(tmp_path, Clock(T1), claude=missing, codex=_codex_payload(T1)).run_once()
    assert _obs(tmp_path)["claude"]["collection"]["error"] == "pinned_executable_missing"


def test_state_without_observations_still_loads_and_is_preserved_shape(tmp_path):
    store = UsageStateStore(tmp_path / "usage_state.json")
    _collector(tmp_path, Clock(T1), claude=_claude_payload(), codex=_codex_payload(T1)).run_once()
    legacy = json.loads((tmp_path / "usage_state.json").read_text())
    legacy.pop("observations")
    assert validate_state(legacy).observations is None
    # The v1 canonical validator is untouched.
    assert set(canonical_state(store.load().lkg, store.load().health)) == {
        "schema_version", "claude_fable_lkg", "claude_health"}


@pytest.mark.parametrize("mutate", [
    lambda o: o["claude"]["collection"].update(status="fresh"),
    lambda o: o["claude"].update(observed_at="yesterday"),
    lambda o: o["claude"].update(account_id=7),
    lambda o: o["claude"]["collection"].update(error={"token": "x"}),
    lambda o: o.update(gemini=o["claude"]),
    lambda o: o["claude"].update(extra=1),
])
def test_malformed_observation_is_rejected(tmp_path, mutate):
    _collector(tmp_path, Clock(T1), claude=_claude_payload(), codex=_codex_payload(T1),
               config=_config(tmp_path)).run_once()
    state = json.loads((tmp_path / "usage_state.json").read_text())
    mutate(state["observations"])
    with pytest.raises((UsageStateError, ValueError)):
        validate_state(state)


def test_other_unexpected_v2_keys_are_still_rejected(tmp_path):
    _collector(tmp_path, Clock(T1), claude=_claude_payload(), codex=_codex_payload(T1)).run_once()
    state = json.loads((tmp_path / "usage_state.json").read_text())
    state["unexpected"] = True
    with pytest.raises(UsageStateError, match="unexpected v2 keys"):
        validate_state(state)


def test_limits_frame_rows_do_not_carry_the_tuple(tmp_path):
    # The desktop limits frame is the validated LKG rows byte-for-byte; the tuple must not leak into it.
    _collector(tmp_path, Clock(T1), claude=_claude_payload(), codex=_codex_payload(T1),
               config=_config(tmp_path)).run_once()
    loaded = UsageStateStore(tmp_path / "usage_state.json").load()
    for row in [*loaded.lkg, loaded.codex_lkg]:
        assert set(row) == {"id", "label", "pct", "resets_at_iso", "resets_text",
                            "upstream_reported_at", "probed_at"}


# --- AC6: no token read, OAuth disabled, bounded runs -----------------------------------------

def test_probe_environment_disables_oauth_and_strips_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-must-not-leak")
    monkeypatch.setenv("PENTACLE_USAGE_CLAUDE_OAUTH", "1")
    seen: list = []
    _collector(tmp_path, Clock(T1), claude=_claude_payload(), codex=_codex_payload(T1), seen_env=seen).run_once()
    assert seen
    for _name, env in seen:
        assert env["PENTACLE_USAGE_CLAUDE_OAUTH"] == "0"
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert not [key for key in env if "TOKEN" in key.upper() or "SECRET" in key.upper()]


def test_collector_output_never_contains_token_fields(tmp_path):
    config = _config(tmp_path)
    data = json.loads(config.read_text())
    data["oauthAccount"]["accessToken"] = "sk-secret-token"
    data["claudeAiOauth"] = {"accessToken": "sk-secret-token"}
    config.write_text(json.dumps(data))
    _collector(tmp_path, Clock(T1), claude=_claude_payload(), codex=_codex_payload(T1), config=config).run_once()
    for name in ("usage_state.json", "usage_history.jsonl"):
        assert "sk-secret-token" not in (tmp_path / name).read_text()


def test_bounded_run_kills_the_whole_process_group_on_timeout(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    script = (
        "import subprocess,sys,time\n"
        f"p=subprocess.Popen(['sleep','60']); open({str(pidfile)!r},'w').write(str(p.pid)); time.sleep(60)\n"
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        bounded_run((sys.executable, "-c", script), capture_output=True, text=True, timeout=2)
    assert time.monotonic() - started < 15
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail("grandchild survived the timeout cleanup")


def test_bounded_run_returns_completed_process(tmp_path):
    result = bounded_run((sys.executable, "-c", "print('ok')"), capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 and result.stdout.strip() == "ok"


def test_hung_probe_is_recorded_failed_and_next_tick_proceeds(tmp_path):
    _collector(tmp_path, Clock(T1), claude=subprocess.TimeoutExpired("claude", 75),
               codex=_codex_payload(T1)).run_once()
    assert _obs(tmp_path)["claude"]["collection"]["status"] == "failed"
    _collector(tmp_path, Clock(T2), claude=_claude_payload(), codex=_codex_payload(T2)).run_once()
    assert _obs(tmp_path)["claude"]["collection"]["status"] == "ok"
    assert _obs(tmp_path)["claude"]["observed_at"] == T2


def _collect_tool():
    sys.path.insert(0, str(REPO_ROOT / "services/chat-stream-v2/tools"))
    try:
        import collect_usage_state as tool
    finally:
        sys.path.pop(0)
    return tool


def test_one_invocation_at_a_time_lock_skips_a_second_tick(tmp_path):
    tool = _collect_tool()
    state = tmp_path / "usage_state.json"
    sentinel = tmp_path / "probe-ran"
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("check_claude_usage.py", "check_codex_usage.py"):
        (scripts / name).write_text(f"open({str(sentinel)!r}, 'a').write('x')\nprint('{{\"status\":\"no_update\"}}')\n")
    lock = state.with_name(state.name + ".lock")
    with open(lock, "w") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert tool.main(["--state", str(state), "--shared-scripts", str(scripts)]) == 0
    assert not sentinel.exists()
    assert not state.exists()
    # Once released the next tick runs.
    assert tool.main(["--state", str(state), "--shared-scripts", str(scripts),
                      "--claude-config", str(tmp_path / "none.json")]) == 0
    assert sentinel.exists()


# --- AC6: pinned executables, no PATH fallback -----------------------------------------------

def _decoy(tmp_path: Path, name: str) -> tuple[Path, Path]:
    bindir = tmp_path / "decoy-bin"
    bindir.mkdir(exist_ok=True)
    sentinel = tmp_path / f"{name}-decoy-ran"
    script = bindir / name
    script.write_text(f"#!/bin/sh\necho ran >> {sentinel}\nexit 0\n")
    script.chmod(0o755)
    return bindir, sentinel


@pytest.mark.parametrize("probe, binary, variable, extra", [
    ("check_claude_usage.py", "claude", "PENTACLE_CLAUDE_BIN", []),
    ("check_claude_usage.py", "claude", "PENTACLE_CLAUDE_BIN", ["--claude", "claude"]),
    ("check_codex_usage.py", "codex", "PENTACLE_CODEX_BIN", []),
])
def test_probe_refuses_a_missing_pin_and_never_consults_path(tmp_path, probe, binary, variable, extra):
    bindir, sentinel = _decoy(tmp_path, binary)
    env = {key: value for key, value in os.environ.items() if not key.startswith("PENTACLE_")}
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    result = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / probe), "--json", *extra],
                            capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 1
    assert "pinned_executable_missing" in result.stderr
    assert not sentinel.exists(), "a PATH decoy executable ran"


@pytest.mark.parametrize("value", ["relative/claude", "claude", "/nonexistent/claude", ""])
def test_pin_must_be_an_absolute_executable_file(tmp_path, value):
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import check_claude_usage as probe
    finally:
        sys.path.pop(0)
    assert probe.pinned_executable({"PENTACLE_CLAUDE_BIN": value}, ("PENTACLE_CLAUDE_BIN",)) is None


def test_probes_use_exactly_the_pinned_paths(tmp_path, monkeypatch):
    fake_claude = tmp_path / "bin" / "claude"
    fake_codex = tmp_path / "bin" / "codex"
    fake_claude.parent.mkdir()
    for fake in (fake_claude, fake_codex):
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
    monkeypatch.setenv("PENTACLE_CLAUDE_BIN", str(fake_claude))
    monkeypatch.setenv("PENTACLE_CODEX_BIN", str(fake_codex))
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import check_claude_usage as claude_probe
        import check_codex_usage as codex_probe
    finally:
        sys.path.pop(0)
    seen = {}
    monkeypatch.setattr(claude_probe, "collect", lambda **kw: seen.update(claude=kw["claude"]) or _claude_payload())
    monkeypatch.setattr(claude_probe.shutil, "which", lambda name, *a, **k: "/usr/bin/tmux" if name == "tmux" else pytest.fail("PATH lookup"))
    monkeypatch.setattr(sys, "argv", ["check_claude_usage.py", "--json"])
    assert claude_probe.main() == 0
    assert seen["claude"] == str(fake_claude)

    monkeypatch.setattr(codex_probe, "collect_usage", lambda **kw: seen.update(codex=kw["codex_bin"]) or _codex_payload(T1))
    assert not hasattr(codex_probe, "shutil"), "the Codex probe must not import PATH discovery"
    assert codex_probe.main(["--json"]) == 0
    assert seen["codex"] == str(fake_codex)


def test_codex_probe_is_quota_only():
    # The only JSON-RPC methods the Codex probe sends are the handshake and the rate-limit read.
    text = (REPO_ROOT / "scripts/check_codex_usage.py").read_text()
    methods = sorted(set(__import__("re").findall(r'"method":\s*"([^"]+)"', text)))
    assert methods == ["account/rateLimits/read", "initialize", "notifications/initialized"]


def test_oauth_probe_path_is_disabled_by_default(monkeypatch):
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import check_claude_usage as probe
    finally:
        sys.path.pop(0)
    monkeypatch.delenv("PENTACLE_USAGE_CLAUDE_OAUTH", raising=False)
    assert probe.oauth_enabled() is False
    monkeypatch.setenv("PENTACLE_USAGE_CLAUDE_OAUTH", "0")
    assert probe.oauth_enabled() is False


# --- AC7: history contract, through the shipped writers --------------------------------------

def _history(tmp_path: Path) -> list[dict]:
    path = tmp_path / "usage_history.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _two_ticks(tmp_path, *, claude, config, codex=None):
    codex = codex if codex is not None else _codex_payload(T1)
    first = _collector(tmp_path, Clock(T1), claude=claude, codex=codex, config=config)
    first.run_once()
    second = _collector(tmp_path, Clock("2026-10-07T15:10:00Z"), claude=claude, codex=codex, config=config)
    second.run_once()
    return _history(tmp_path)


def test_two_tick_history_rows_follow_the_shipped_writers(tmp_path):
    config = _config(tmp_path)  # cache: seven_day + seven_day_fable populated -> C = 2
    claude = _claude_payload()  # probe: week_all + week_fable populated -> P_claude = 2
    rows = _two_ticks(tmp_path, claude=claude, config=config)
    # Expectations are derived from the shipped writers' own emitted rows.
    probe_per_tick = (
        claude_probe_lines(claude, host="amaterasu", probed_at=T1)
        + codex_probe_lines(_codex_payload(T1), host="amaterasu", probed_at=T1)
    )
    cache_distinct = claude_cache_lines(json.loads(config.read_text()), host="amaterasu", probed_at=T1)
    P, C = len(probe_per_tick), len(cache_distinct)
    assert (P, C) == (3, 2)
    probes = [row for row in rows if row["source"] == "probe"]
    caches = [row for row in rows if row["source"] == "cache"]
    assert len(probes) == 2 * P
    assert len(caches) == C
    assert len(rows) == 2 * P + C
    # Window identities (one row per populated window per tick) and probe-per-tick identity.
    assert sorted((r["provider"], r["window_kind"]) for r in probes) == sorted(
        [("claude", "seven_day"), ("claude", "seven_day_fable"), ("codex", "codex")] * 2)
    assert sorted(r["window_kind"] for r in caches) == ["seven_day", "seven_day_fable"]
    # Cache rows all come from tick 1; tick 2 appended none (same observation).
    assert {r["probed_at"] for r in caches} == {T1}
    assert {r["observed_at"] for r in caches} == {T1}
    assert sorted({r["probed_at"] for r in probes}) == [T1, "2026-10-07T15:10:00Z"]


def test_absent_windows_emit_nothing_of_either_source(tmp_path):
    config = _config(tmp_path, fable=False)  # cache: only seven_day (C = 1)
    claude = {"week_all_pct": 80, "week_all_resets": "Oct 11", "week_fable_pct": None, "week_fable_resets": ""}
    rows = _two_ticks(tmp_path, claude=claude, config=config, codex={"status": "no_update"})
    assert not [r for r in rows if r["window_kind"] == "seven_day_fable"]
    assert not [r for r in rows if r["provider"] == "codex"]
    assert len([r for r in rows if r["source"] == "probe"]) == 2     # 1 claude window x 2 ticks
    assert len([r for r in rows if r["source"] == "cache"]) == 1


def test_dedupe_key_is_the_shipped_contract():
    base = dict(host="amaterasu", provider="claude", account_id=ORG, window_kind="seven_day",
                window_minutes=10080, pct=80, resets_at="2026-10-11T07:00:00Z")
    cache = {**base, "source": "cache", "observed_at": T1, "probed_at": T1}
    probe = {**base, "source": "probe", "observed_at": T1, "probed_at": T1}
    rollout = {**base, "source": "rollout", "observed_at": T1, "probed_at": T1}
    assert usage_history.dedupe_key(probe) is None
    assert usage_history.dedupe_key(cache) == (
        "cache", "amaterasu", T1, "claude", ORG, "seven_day", 10080, "2026-10-11T07:00:00Z", 80)
    assert usage_history.dedupe_key(rollout) == (
        "rollout", "claude", ORG, "seven_day", 10080, "2026-10-11T07:00:00Z", 80)


def test_probe_rows_are_excluded_from_calibration_fitting(tmp_path):
    sys.path.insert(0, str(REPO_ROOT / "services/chat-stream-v2/tools"))
    try:
        import usage_rollup
    finally:
        sys.path.pop(0)
    rows = _two_ticks(tmp_path, claude=_claude_payload(), config=_config(tmp_path))
    probes = [row for row in rows if row["source"] == "probe"]
    assert probes
    for window in ("seven_day", "seven_day_fable"):
        # Probe rows carry no account, so no account's fitting input ever includes them ...
        lines, excluded = usage_rollup.history_lines(rows, None, window)
        assert lines == []
        assert all(item["reason"] == "probe_source" for item in excluded) and excluded
        # ... and for the cache account the fitting input is the cache rows only.
        lines, _ = usage_rollup.history_lines(rows, ORG, window)
        assert [line["source"] for line in lines] == ["cache"]
