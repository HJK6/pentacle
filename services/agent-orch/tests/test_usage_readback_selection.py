"""Per-provider selection between a satellite's cadence file and its CLI cache (AC3, AC4 shape).

Spec: spec_pentacle__satellite_usage_freshness_collector_2026_10, Target State 3.
"""
from __future__ import annotations

import json
import subprocess as sp
from datetime import datetime, timezone

import pytest

from agent_orch import usage_readback as ur

NOW_MS = 1_791_400_000_000
ORG, ORG_OLD = "org-current", "org-previous"
ACCT, ACCT_OLD = "acct-current", "acct-previous"
MACHINE = {"name": "amaterasu", "ssh_target": "u@amaterasu", "claude_bin": "/opt/tools/bin/claude",
           "codex_bin": "/opt/tools/bin/codex", "tmux_bin": "/usr/bin/tmux"}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PENTACLE_USAGE_HISTORY_PATH", str(tmp_path / "usage_history.jsonl"))
    monkeypatch.setattr(ur, "_agent_local_host_id", lambda: "thoth")


def iso(age_s: float) -> str:
    return datetime.fromtimestamp(NOW_MS / 1000 - age_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cache(age_s=60, *, pct=80, account=ACCT, oauth=ACCT, org=ORG):
    return {
        "oauth_account_uuid": oauth, "oauth_organization_uuid": org, "cache_account_uuid": account,
        "fetched_at_ms": NOW_MS - int(age_s * 1000), "seven_day_pct": pct, "seven_day_resets": "2026-10-11T07:00:00Z",
        "five_hour_pct": 17, "five_hour_resets": None,
    }


def cadence(age_s=120, *, pct=78, account=ORG, status="ok", error=None, codex_age=120, codex_pct=10,
            codex_status="ok", observations=True):
    state = {
        "claude_fable_lkg": [{"id": "claude", "pct": pct, "resets_at_iso": None, "resets_text": "Oct 11"},
                             {"id": "fable", "pct": 9, "resets_at_iso": None, "resets_text": "Oct 11"}],
        "claude_health": {"outcome": "ok" if status == "ok" else "provider_error", "attempted_at": iso(30),
                          "upstream_reported_at": iso(age_s)},
        "codex_lkg": {"pct": codex_pct, "resets_at_iso": "2026-10-14T03:28:51Z", "resets_text": "Oct 13",
                      "upstream_reported_at": iso(codex_age)},
        "codex_health": {"outcome": "ok", "attempted_at": iso(30), "upstream_reported_at": iso(codex_age)},
    }
    state["observations"] = {
        "claude": {"observed_at": iso(age_s), "account_id": account,
                   "collection": {"status": status, "attempted_at": iso(30), "error": error}},
        "codex": {"observed_at": iso(codex_age), "account_id": None,
                  "collection": {"status": codex_status, "attempted_at": iso(30),
                                 "error": "codex_usage_provider_error" if codex_status == "failed" else None}},
    } if observations else None
    return state


def pluck(cache_part=None, cadence_part=None):
    base = cache_part if cache_part is not None else {"oauth_account_uuid": ACCT, "oauth_organization_uuid": ORG}
    return {**base, "cadence": cadence_part}


def select(plucked, **kw):
    return ur.select_claude_row("amaterasu", plucked, now_ms=NOW_MS, max_age_s=900, **kw)


# --- AC3 rules --------------------------------------------------------------------------------

def test_fresh_ok_cadence_row_is_ok_from_the_cadence_file():
    row = select(pluck(cache(age_s=3000), cadence(age_s=120)))
    assert (row["outcome"], row["status"], row["source"], row["pct"]) == ("ok", "ok", "cadence-file", 78)
    assert row["observed_at"] == iso(120) and row["age_seconds"] == 120 and row["account_id"] == ORG
    assert row["collector"] == {"status": "ok", "attempted_at": iso(30), "error": None}


def test_cadence_older_than_900s_is_stale_not_ok():
    row = select(pluck({"oauth_account_uuid": ACCT, "oauth_organization_uuid": ORG}, cadence(age_s=901)))
    assert (row["outcome"], row["source"], row["age_seconds"]) == ("stale", "cadence-file", 901)
    boundary = select(pluck({"oauth_account_uuid": ACCT, "oauth_organization_uuid": ORG}, cadence(age_s=900)))
    assert boundary["outcome"] == "ok"


@pytest.mark.parametrize("status, error", [("failed", "claude_usage_provider_error"), ("no_update", None)])
def test_failed_or_no_update_collection_keeps_the_old_value_aging_and_never_ok(status, error):
    row = select(pluck(None, cadence(age_s=300, status=status, error=error)))
    assert row["outcome"] == "stale" and row["status"] == "stale"
    assert row["pct"] == 78 and row["observed_at"] == iso(300) and row["age_seconds"] == 300
    assert row["collector"]["status"] == status
    assert row["source"] == "cadence-file"


def test_old_account_cadence_row_is_never_selected_current_cache_wins():
    row = select(pluck(cache(age_s=60, pct=80), cadence(age_s=60, account=ORG_OLD, pct=55)))
    assert row["source"] == "claude-cache" and row["pct"] == 80 and row["outcome"] == "ok"
    assert row["account_id"] == ORG


def test_old_account_cadence_without_a_current_cache_selects_nothing_and_flags_mismatch():
    result = select(pluck(None, cadence(age_s=60, account=ORG_OLD, pct=55)))
    assert result == {"fallback": True, "reason": "account_mismatch", "collector": result["collector"]}


def test_null_account_cadence_cannot_be_matched():
    result = select(pluck(None, cadence(age_s=60, account=None)))
    assert result["fallback"] is True


def test_cache_newer_than_a_stale_cadence_row_is_used_with_honest_age_and_collector_failure():
    row = select(pluck(cache(age_s=100, pct=81), cadence(age_s=2000, status="failed", error="pinned_executable_missing")))
    assert row["source"] == "claude-cache" and row["pct"] == 81
    assert row["outcome"] == "ok" and row["age_seconds"] == 100 and row["observed_at"] == iso(100)
    assert row["collector"]["status"] == "failed" and row["collector"]["error"] == "pinned_executable_missing"


def test_stale_cache_newer_than_stale_cadence_is_stale_with_its_own_age():
    row = select(pluck(cache(age_s=1500, pct=81), cadence(age_s=4000, status="failed", error="x")))
    assert (row["source"], row["outcome"], row["age_seconds"]) == ("claude-cache", "stale", 1500)


def test_cadence_newer_than_cache_beats_a_stale_cache_even_when_collector_failed():
    row = select(pluck(cache(age_s=5000, pct=70), cadence(age_s=400, status="failed", error="x")))
    assert row["source"] == "cadence-file" and row["pct"] == 78 and row["outcome"] == "stale"


def test_no_cadence_file_falls_back_to_the_cache_as_before():
    row = select(pluck(cache(age_s=60), None))
    assert row["source"] == "claude-cache" and row["outcome"] == "ok" and row["collector"] is None
    assert row["observed_at"] == iso(60) and row["account_id"] == ORG


def test_cadence_source_is_printed_only_when_the_cadence_row_was_selected():
    rows = [
        select(pluck(cache(age_s=60), cadence(age_s=2000, status="failed", error="x"))),   # cache newer
        select(pluck(cache(age_s=60), cadence(age_s=60, account=ORG_OLD))),               # old account
        select(pluck(cache(age_s=60), None)),                                             # no file
    ]
    assert [row["source"] for row in rows] == ["claude-cache"] * 3


def test_a_failed_or_stale_reading_is_never_printed_ok_across_every_combination():
    for cad_age in (60, 500, 901, 5000):
        for status in ("ok", "failed", "no_update"):
            for cache_age in (None, 60, 1200, 8000):
                plucked = pluck(cache(age_s=cache_age) if cache_age is not None else None,
                                cadence(age_s=cad_age, status=status, error="x" if status == "failed" else None))
                row = select(plucked)
                if row.get("fallback"):
                    continue
                if row["outcome"] == "ok":
                    assert row["age_seconds"] <= 900, (cad_age, status, cache_age, row)
                    if row["source"] == "cadence-file":
                        assert row["collector"]["status"] == "ok"
                    assert row["observed_at"]


def test_legacy_cadence_without_observations_is_never_account_matched_on_a_satellite():
    result = select(pluck(None, cadence(age_s=60, observations=False)))
    assert result["fallback"] is True


# --- Codex ------------------------------------------------------------------------------------

def test_codex_fresh_cadence_row_is_ok_with_null_account():
    row = ur.select_codex_row("amaterasu", {"cadence": cadence(codex_age=200)}, now_ms=NOW_MS)
    assert (row["outcome"], row["source"], row["pct"], row["account_id"]) == ("ok", "cadence-file", 10, None)
    assert row["observed_at"] == iso(200) and row["collector"]["status"] == "ok"


@pytest.mark.parametrize("kw", [dict(codex_age=901), dict(codex_status="failed")])
def test_codex_stale_or_failed_cadence_is_not_selected_as_current(kw):
    assert ur.select_codex_row("amaterasu", {"cadence": cadence(**kw)}, now_ms=NOW_MS) is None


# --- end to end over the ssh seam --------------------------------------------------------------

def _runner(plucked, codex_stdout=None, calls=None):
    def run(target, command, *, input_text=None, **kw):
        if calls is not None:
            calls.append(command)
        if input_text == ur.CLAUDE_CACHE_PLUCK:
            return 0, json.dumps(plucked), ""
        if "check_codex_usage" in command:
            return (0, json.dumps(codex_stdout), "") if codex_stdout is not None else (1, "", "Codex usage unavailable")
        return 1, "", "claude probe must not run"
    return run


def test_remote_read_uses_one_ssh_pluck_and_no_probe_when_the_cadence_is_fresh():
    calls: list = []
    rows = ur.read_remote(MACHINE, runner=_runner(pluck(cache(age_s=5000), cadence()), calls=calls), now_ms=NOW_MS)
    claude, codex = rows
    assert (claude["source"], claude["outcome"]) == ("cadence-file", "ok")
    assert (codex["source"], codex["outcome"]) == ("cadence-file", "ok")
    assert len(calls) == 1  # one ssh round trip: the pluck


def test_codex_probe_runs_only_when_the_cadence_row_is_not_current_and_shows_collector():
    rows = ur.read_remote(MACHINE, runner=_runner(
        pluck(cache(), cadence(codex_status="failed")), codex_stdout={"pct": 12, "upstream_reported_at": iso(1)}),
        now_ms=NOW_MS)
    codex = rows[1]
    assert codex["source"] == "codex-app-server" and codex["pct"] == 12
    assert codex["collector"]["status"] == "failed" and codex["observed_at"]


def test_codex_probe_failure_shows_the_old_cadence_value_aged_not_fresh():
    rows = ur.read_remote(MACHINE, runner=_runner(pluck(cache(), cadence(codex_age=5000))), now_ms=NOW_MS)
    codex = rows[1]
    assert (codex["source"], codex["outcome"], codex["age_seconds"]) == ("cadence-file", "stale", 5000)


def test_probe_commands_pin_the_executables_by_env():
    calls: list = []
    ur.read_remote(MACHINE, runner=_runner(pluck(None, None), calls=calls), now_ms=NOW_MS)
    claude_cmd = next(c for c in calls if "check_claude_usage" in c)
    codex_cmd = next(c for c in calls if "check_codex_usage" in c)
    assert "PENTACLE_CLAUDE_BIN=/opt/tools/bin/claude" in claude_cmd
    assert "PENTACLE_CODEX_BIN=/opt/tools/bin/codex" in codex_cmd


def test_cadence_pluck_runs_on_host_allowlisted_and_carries_no_token(tmp_path):
    home = tmp_path / "home"
    state_dir = home / ".local/share/pentacle-stream"
    state_dir.mkdir(parents=True)
    state = cadence()
    state["schema_version"] = 2
    state["claude_fable_lkg"][0]["probed_at"] = "ignored"
    state["claude_fable_lkg"][0]["injected"] = "UNIQUE-SENTINEL-must-not-leak"
    state["observations"]["claude"]["injected"] = "UNIQUE-SENTINEL-must-not-leak"
    (state_dir / "usage_state.json").write_text(json.dumps(state))
    (home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"accountUuid": ACCT, "organizationUuid": ORG, "accessToken": "UNIQUE-SENTINEL-must-not-leak"},
        "cachedUsageUtilization": {"accountUuid": ACCT, "fetchedAtMs": NOW_MS, "utilization": {}},
    }))
    proc = sp.run(["python3", "-"], input=ur.CLAUDE_CACHE_PLUCK, text=True, capture_output=True,
                  env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
    assert proc.returncode == 0, proc.stderr
    assert "UNIQUE-SENTINEL" not in proc.stdout
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["cadence"]["observations"]["claude"]["account_id"] == ORG
    assert set(out["cadence"]["observations"]["claude"]) == {"observed_at", "account_id", "collection"}
    assert out["cadence"]["claude_fable_lkg"][0].keys() == {"id", "pct", "resets_at_iso", "resets_text"}


# --- table + json tuple ------------------------------------------------------------------------

def test_table_has_the_tuple_columns_and_json_carries_the_full_tuple():
    rows = ur.read_remote(MACHINE, runner=_runner(pluck(cache(age_s=5000), cadence())), now_ms=NOW_MS)
    table = ur.render_table(rows)
    header = table.splitlines()[0].split()
    assert header[:4] == ["HOST", "PROVIDER", "USAGE", "STATUS"]
    for column in ("SOURCE", "OBSERVED", "AGE", "ACCOUNT", "COLLECTOR", "RESETS"):
        assert column in header
    claude_line = next(line for line in table.splitlines() if " claude " in line)
    assert "cadence-file" in claude_line and ORG[:8] in claude_line and "ok" in claude_line
    for row in json.loads(json.dumps(rows)):
        assert {"observed_at", "age_seconds", "account_id", "status", "collector", "source"} <= set(row)


def test_default_max_age_is_900_seconds():
    assert ur.DEFAULT_MAX_AGE_SECONDS == 900


# --- the collector host reading its own cadence file ---------------------------------------------

def test_collector_host_rows_apply_the_same_age_and_status_rules():
    state = cadence(age_s=120)
    fresh = {r["provider"]: r for r in ur.parse_cadence_state("thoth", state, now_ms=NOW_MS)}
    assert [fresh[p]["outcome"] for p in ("claude", "fable", "codex")] == ["ok", "ok", "ok"]
    assert fresh["claude"]["observed_at"] == iso(120) and fresh["claude"]["account_id"] == ORG
    old = {r["provider"]: r for r in ur.parse_cadence_state("thoth", cadence(age_s=2000, status="failed", error="x"),
                                                           now_ms=NOW_MS)}
    assert old["claude"]["outcome"] == "stale" and old["claude"]["collector"]["status"] == "failed"
    assert old["fable"]["outcome"] == "stale" and old["claude"]["pct"] == 78


def test_pre_tuple_cadence_file_keeps_health_outcome_but_ages_by_its_stamp():
    legacy = cadence(age_s=2000, observations=False)
    rows = {r["provider"]: r for r in ur.parse_cadence_state("thoth", legacy, now_ms=NOW_MS)}
    assert rows["claude"]["outcome"] == "stale" and rows["claude"]["age_seconds"] == 2000
    recent = {r["provider"]: r for r in ur.parse_cadence_state("thoth", cadence(age_s=100, observations=False),
                                                              now_ms=NOW_MS)}
    assert recent["claude"]["outcome"] == "ok" and recent["claude"]["collector"]["status"] == "ok"
