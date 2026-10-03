"""Tests for the on-demand per-host usage readback (agent-orch usage --host).

Covers the spec's named cases: Claude-cache ok/stale/account-mismatch/missing,
fallback classes, codex ok, token-never-emitted, no-fabricated-zero, host
isolation, and the thoth cadence-file path. No live provider or network.
"""

from __future__ import annotations

import io
import json
import shlex
from argparse import Namespace
from contextlib import redirect_stdout, redirect_stderr

import pytest

from agent_orch import cli, usage_readback as ur

NOW_MS = 1_791_000_000_000  # fixed "now"
# A distinctive non-credential-shaped sentinel (public-residue safe): the test
# only needs a unique string to prove the OAuth token is never surfaced.
SENTINEL_TOKEN = "UNIQUE-SENTINEL-VALUE-MUST-NOT-LEAK-7f3a9c"


def _cache(**over):
    base = {
        "oauth_account_uuid": "acct-A",
        "cache_account_uuid": "acct-A",
        "fetched_at_ms": NOW_MS - 60_000,  # 60s old
        "seven_day_pct": 85,
        "seven_day_resets": "2026-10-04T07:00:00Z",
        "five_hour_pct": 11,
        "five_hour_resets": "2026-10-03T21:00:00Z",
    }
    base.update(over)
    return base


# --- C1: active-account anchor ---------------------------------------------
def test_usage_cli_claude_cache_ok():
    row = ur.classify_claude_cache("merlin", _cache(), now_ms=NOW_MS, max_age_s=600)
    assert row["outcome"] == ur.OUTCOME_OK
    assert row["pct"] == 85
    assert row["source"] == "claude-cache"
    assert row["five_hour_pct"] == 11
    assert row["age_seconds"] == 60


def test_usage_cli_claude_account_mismatch_then_no_fallback_number():
    # cache is for a different account than currently logged in -> fallback signal
    sig = ur.classify_claude_cache("merlin", _cache(cache_account_uuid="acct-OLD"),
                                   now_ms=NOW_MS, max_age_s=600)
    assert sig == {"fallback": True, "reason": "account_mismatch"}
    # fallback probe yields no weekly number -> account_mismatch (never the stale %)
    row = ur.classify_claude_fallback(
        "merlin", 1, "", "Claude usage unavailable: ... did not provide labeled weekly ...",
        mismatch=True, now_ms=NOW_MS)
    assert row["outcome"] == ur.OUTCOME_ACCOUNT_MISMATCH
    assert row["pct"] is None


# --- C2: freshness cutoff ---------------------------------------------------
def test_usage_cli_claude_cache_stale_boundary():
    just_under = ur.classify_claude_cache(
        "merlin", _cache(fetched_at_ms=NOW_MS - 600_000), now_ms=NOW_MS, max_age_s=600)
    assert just_under["outcome"] == ur.OUTCOME_OK  # age == cutoff is still ok
    just_over = ur.classify_claude_cache(
        "merlin", _cache(fetched_at_ms=NOW_MS - 601_000), now_ms=NOW_MS, max_age_s=600)
    assert just_over["outcome"] == ur.OUTCOME_STALE
    assert just_over["pct"] == 85  # value still shown
    assert just_over["age_seconds"] == 601


# --- cache missing / malformed -> fallback ---------------------------------
def test_usage_cli_claude_cache_missing_or_malformed():
    assert ur.classify_claude_cache("merlin", {}, now_ms=NOW_MS)["fallback"] is True
    assert ur.classify_claude_cache("merlin", _cache(seven_day_pct=None),
                                    now_ms=NOW_MS)["reason"] == "cache_miss"


# --- fallback failure classes ----------------------------------------------
def test_usage_cli_claude_fallback_classes():
    auth = ur.classify_claude_fallback("merlin", 1, "", "Please sign in to continue",
                                       mismatch=False, now_ms=NOW_MS)
    assert auth["outcome"] == ur.OUTCOME_AUTH_ERROR
    # the probe's own deadline message is a timeout, not "no weekly limits"
    deadline = ur.classify_claude_fallback(
        "merlin", 1, "", "Claude usage unavailable: ... did not provide labeled weekly ...",
        mismatch=False, now_ms=NOW_MS)
    assert deadline["outcome"] == ur.OUTCOME_TIMEOUT
    missing_bin = ur.classify_claude_fallback(
        "merlin", 1, "", "Claude and tmux must be installed", mismatch=False, now_ms=NOW_MS)
    assert missing_bin["outcome"] == ur.OUTCOME_PROVIDER_ERROR
    ssh255 = ur.classify_claude_fallback("merlin", 255, "", "ssh connect failed (255)",
                                         mismatch=False, now_ms=NOW_MS)
    assert ssh255["outcome"] == ur.OUTCOME_TRANSPORT_ERROR
    timeout = ur.classify_claude_fallback("merlin", 124, "", "", mismatch=False, now_ms=NOW_MS)
    assert timeout["outcome"] == ur.OUTCOME_TIMEOUT
    ok = ur.classify_claude_fallback(
        "merlin", 0, json.dumps({"week_all_pct": 42, "week_all_resets": "soon"}), "",
        mismatch=False, now_ms=NOW_MS)
    assert ok["outcome"] == ur.OUTCOME_OK and ok["pct"] == 42
    # after a cache account-mismatch, a failed live read reports account_mismatch
    # (never the stale account's number), keeping the specific cause as a note.
    # (exit 1 + deadline message goes through the lower branch where mismatch wraps.)
    mm = ur.classify_claude_fallback(
        "merlin", 1, "", "Claude usage unavailable: ... did not provide labeled weekly ...",
        mismatch=True, now_ms=NOW_MS)
    assert mm["outcome"] == ur.OUTCOME_ACCOUNT_MISMATCH and mm["pct"] is None
    assert mm["note"] == ur.OUTCOME_TIMEOUT
    # but a pure transport/timeout at the ssh layer is reported as-is (not masked
    # as account_mismatch) even under mismatch, since the account couldn't be read
    assert ur.classify_claude_fallback("merlin", 124, "", "", mismatch=True,
                                       now_ms=NOW_MS)["outcome"] == ur.OUTCOME_TIMEOUT


def test_usage_cli_claude_cache_anchor_fail_closed():
    # oauth anchor absent -> cannot verify current account -> must fall back
    sig = ur.classify_claude_cache("merlin", _cache(oauth_account_uuid=None),
                                   now_ms=NOW_MS, max_age_s=600)
    assert sig == {"fallback": True, "reason": "account_mismatch"}
    # empty-string anchors are also treated as unverifiable
    sig2 = ur.classify_claude_cache("merlin", _cache(oauth_account_uuid=""),
                                    now_ms=NOW_MS, max_age_s=600)
    assert sig2["fallback"] is True


def test_usage_cli_claude_cache_bad_stamp_fail_closed():
    # missing / non-numeric / non-positive fetchedAtMs -> stale (never ok), pct kept
    for bad in (None, "nope", 0, -5, True):
        row = ur.classify_claude_cache("merlin", _cache(fetched_at_ms=bad),
                                       now_ms=NOW_MS, max_age_s=600)
        assert row["outcome"] == ur.OUTCOME_STALE, bad
        assert row["pct"] == 85 and row["age_seconds"] is None


def test_usage_cli_claude_cache_fractional_boundary():
    # 600.9s old must be stale at a 600s cutoff (ms comparison, not floored secs)
    row = ur.classify_claude_cache("merlin", _cache(fetched_at_ms=NOW_MS - 600_900),
                                   now_ms=NOW_MS, max_age_s=600)
    assert row["outcome"] == ur.OUTCOME_STALE and row["age_seconds"] == 600


def test_usage_cli_codex_ok_and_errors():
    ok = ur.classify_codex_probe("merlin", 0, json.dumps(
        {"pct": 15, "resets_at_iso": "2026-10-09T21:13:24Z", "resets_text": "Oct 9"}), "",
        now_ms=NOW_MS)
    assert ok["outcome"] == ur.OUTCOME_OK and ok["pct"] == 15
    missing = ur.classify_codex_probe("merlin", 1, "", "Codex CLI not found on PATH", now_ms=NOW_MS)
    assert missing["outcome"] == ur.OUTCOME_PROVIDER_ERROR and missing["pct"] is None
    transport = ur.classify_codex_probe("merlin", 127, "", "ssh transport failure", now_ms=NOW_MS)
    assert transport["outcome"] == ur.OUTCOME_TRANSPORT_ERROR


# --- no fabricated zero -----------------------------------------------------
def test_usage_cli_no_fabricated_zero():
    for outcome in (ur.OUTCOME_ACCOUNT_MISMATCH, ur.OUTCOME_NO_WEEKLY_LIMITS,
                    ur.OUTCOME_AUTH_ERROR, ur.OUTCOME_PROVIDER_ERROR,
                    ur.OUTCOME_TRANSPORT_ERROR, ur.OUTCOME_TIMEOUT):
        row = ur._row("merlin", "claude", outcome, pct=0)  # even if a 0 is passed
        assert row["pct"] is None, f"{outcome} must null pct, never 0"
    # a genuine 0% stays 0 for ok/stale
    assert ur._row("thoth", "codex", ur.OUTCOME_OK, pct=0)["pct"] == 0


# --- host isolation ---------------------------------------------------------
def test_usage_cli_host_isolation(monkeypatch):
    def fake_runner(target, command, **kw):
        if "merlin" in target:  # merlin unreachable
            return 127, "", "ssh transport failure (OSError)"
        return 0, json.dumps({"pct": 7}), ""  # (unused here)
    machine = {"name": "merlin", "ssh_target": "u@merlin", "codex_bin": "/x/codex"}
    rows = ur.read_remote(machine, runner=fake_runner, now_ms=NOW_MS)
    assert all(r["host"] == "merlin" for r in rows)
    assert {r["provider"] for r in rows} == {"claude", "codex"}
    # merlin's transport failure does not fabricate a value for either provider
    assert all(r["pct"] is None for r in rows)


# --- token never emitted (C3) ----------------------------------------------
def test_usage_cli_token_never_emitted(monkeypatch, tmp_path):
    # The pluck snippet, run locally against a fixture ~/.claude.json holding a
    # sentinel token, must emit only the allowlisted fields — never the token.
    import subprocess as sp
    fixture = {
        "oauthAccount": {"accountUuid": "acct-A", "emailAddress": "x@y.z", "accessToken": SENTINEL_TOKEN},
        "claudeAiOauth": {"accessToken": SENTINEL_TOKEN},
        "cachedUsageUtilization": {
            "accountUuid": "acct-A", "fetchedAtMs": NOW_MS,
            "utilization": {"seven_day": {"utilization": 85, "resets_at": "z"},
                            "five_hour": {"utilization": 11, "resets_at": "z"}},
        },
    }
    home = tmp_path
    (home / ".claude.json").write_text(json.dumps(fixture))
    proc = sp.run(["python3", "-"], input=ur.CLAUDE_CACHE_PLUCK, text=True,
                  capture_output=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
    assert proc.returncode == 0, proc.stderr
    assert SENTINEL_TOKEN not in proc.stdout
    assert SENTINEL_TOKEN not in proc.stderr
    plucked = json.loads(proc.stdout.strip().splitlines()[-1])
    assert set(plucked) == {"oauth_account_uuid", "cache_account_uuid", "fetched_at_ms",
                            "seven_day_pct", "seven_day_resets", "five_hour_pct", "five_hour_resets"}
    assert plucked["seven_day_pct"] == 85
    # And the full readback path never surfaces the token either.
    row = ur.classify_claude_cache("merlin", plucked, now_ms=NOW_MS + 1000)
    assert SENTINEL_TOKEN not in json.dumps(row)


# --- thoth cadence-file path ------------------------------------------------
CADENCE_STATE = {
    "schema_version": 2,
    "claude_fable_lkg": [
        {"id": "claude", "label": "Claude", "pct": 97, "resets_at_iso": None,
         "resets_text": "Oct 7", "probed_at": "2026-10-03T18:59:46Z"},
        {"id": "fable", "label": "Fable", "pct": 13, "resets_text": "Oct 7"},
    ],
    "claude_health": {"outcome": "ok", "probed_at": "2026-10-03T18:59:46Z"},
    "codex_lkg": {"id": "codex", "label": "Codex", "pct": 15,
                  "resets_at_iso": "2026-10-09T21:13:24Z", "probed_at": "2026-10-03T18:59:47Z"},
    "codex_health": {"outcome": "ok"},
}


def test_usage_cli_thoth_cadence_file(monkeypatch, tmp_path):
    state_path = tmp_path / "usage_state.json"
    state_path.write_text(json.dumps(CADENCE_STATE))
    env = {"PENTACLE_USAGE_STATE_PATH": str(state_path),
           "PENTACLE_MACHINES_JSON": json.dumps([{"name": "thoth", "ssh_target": None}])}
    rows = ur.read_host("thoth", env=env)
    by = {r["provider"]: r for r in rows}
    assert by["claude"]["pct"] == 97 and by["claude"]["source"] == "cadence-file"
    assert by["fable"]["pct"] == 13
    assert by["codex"]["pct"] == 15 and by["codex"]["outcome"] == "ok"
    assert all(r["source"] == "cadence-file" for r in rows)


def test_usage_cli_handler_json_and_table(monkeypatch):
    env_machines = json.dumps([{"name": "merlin", "ssh_target": "u@merlin",
                                "codex_bin": "/opt/homebrew/bin/codex",
                                "claude_bin": "/opt/homebrew/bin/claude",
                                "tmux_bin": "/opt/homebrew/bin/tmux"}])
    monkeypatch.setenv("PENTACLE_MACHINES_JSON", env_machines)

    def fake_runner(target, command, *, input_text=None, **kw):
        if input_text and "cachedUsageUtilization" in input_text:  # claude cache pluck
            return 0, json.dumps(_cache()), ""
        if "check_codex_usage" in command:
            return 0, json.dumps({"pct": 15, "resets_text": "Oct 9"}), ""
        return 127, "", "unexpected"
    monkeypatch.setattr(ur, "run_remote", fake_runner)

    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli.usage(Namespace(host="merlin", json=True, max_age_seconds=600))
    assert rc == 0
    rows = json.loads(out.getvalue())
    by = {r["provider"]: r for r in rows}
    assert by["claude"]["pct"] == 85 and by["claude"]["source"] == "claude-cache"
    assert by["codex"]["pct"] == 15
    assert SENTINEL_TOKEN not in out.getvalue()

    out2 = io.StringIO()
    with redirect_stdout(out2):
        rc = cli.usage(Namespace(host="merlin", json=False, max_age_seconds=600))
    assert rc == 0 and "merlin" in out2.getvalue() and "85%" in out2.getvalue()


def test_remote_script_expands_home_safely():
    # ~-path: quoted $HOME prefix + shlex-quoted remainder. shlex.quote leaves a
    # clean remainder bare (still safe); only $HOME expands remotely.
    tok = ur._remote_script("~/repos/pentacle-public-runtime", "check_codex_usage.py")
    assert tok == '"$HOME"/repos/pentacle-public-runtime/scripts/check_codex_usage.py'
    assert "'~" not in tok
    # an absolute clean path is returned bare (shlex.quote), still one shell token
    tok2 = ur._remote_script("/opt/pentacle", "check_codex_usage.py")
    assert shlex.split(tok2) == ["/opt/pentacle/scripts/check_codex_usage.py"]


def test_remote_script_blocks_injection():
    # A crafted runtime dir must NOT allow command substitution / backticks: the
    # dangerous remainder is shlex-quoted into exactly one literal shell token.
    for evil in ("~/x$(touch /tmp/pwn)", "~/`id`", "/a b;rm -rf /", "~/x';echo bad;'"):
        tok = ur._remote_script(evil, "check_codex_usage.py")
        rest = tok[len('"$HOME"/'):] if tok.startswith('"$HOME"/') else tok
        # the shell parses the remainder as exactly ONE literal token (no exec)
        parsed = shlex.split(rest)
        assert len(parsed) == 1 and parsed[0].endswith("/scripts/check_codex_usage.py")


def test_codex_command_expands_home_not_tilde_literal(monkeypatch):
    captured = {}

    def fake_runner(target, command, *, input_text=None, **kw):
        captured["cmd"] = command
        return 0, json.dumps({"pct": 15}), ""
    machine = {"name": "merlin", "ssh_target": "u@merlin",
               "codex_bin": "/opt/homebrew/bin/codex"}
    ur.read_remote_codex(machine, now_ms=NOW_MS, runner=fake_runner,
                         env={"PENTACLE_USAGE_RUNTIME_DIR": "~/repos/pentacle-public-runtime"})
    assert '"$HOME"/repos/pentacle-public-runtime/scripts/check_codex_usage.py' in captured["cmd"]
    assert "'~" not in captured["cmd"]


def test_usage_cli_two_host_isolation(monkeypatch):
    # One host's transport failure must not alter the other host's rows or values.
    env = {"PENTACLE_MACHINES_JSON": json.dumps([
        {"name": "thoth", "ssh_target": None},
        {"name": "merlin", "ssh_target": "u@merlin", "codex_bin": "/x/codex"},
        {"name": "amaterasu", "ssh_target": "u@amaterasu", "codex_bin": "/x/codex"},
    ])}

    def fake_runner(target, command, *, input_text=None, **kw):
        if "merlin" in target:
            return 255, "", "ssh: connect to host merlin port 22: No route to host"
        if input_text and "cachedUsageUtilization" in input_text:
            return 0, json.dumps(_cache()), ""
        return 0, json.dumps({"pct": 15}), ""

    merlin = ur.read_host("merlin", env=env, runner=fake_runner, now_ms=NOW_MS)
    amaterasu = ur.read_host("amaterasu", env=env, runner=fake_runner, now_ms=NOW_MS)
    assert all(r["outcome"] == ur.OUTCOME_TRANSPORT_ERROR and r["pct"] is None for r in merlin)
    # amaterasu is unaffected: real values come through
    by = {r["provider"]: r for r in amaterasu}
    assert by["claude"]["pct"] == 85 and by["codex"]["pct"] == 15
    assert all(r["host"] == "amaterasu" for r in amaterasu)


def test_usage_cli_injected_cache_field_never_surfaces(monkeypatch):
    # Even if a remote returned extra/hostile fields, only the allowlisted row
    # fields reach CLI output — unknown fields (incl. a token) are dropped.
    env_machines = json.dumps([{"name": "merlin", "ssh_target": "u@merlin", "codex_bin": "/x/codex"}])
    monkeypatch.setenv("PENTACLE_MACHINES_JSON", env_machines)

    def fake_runner(target, command, *, input_text=None, **kw):
        if input_text and "cachedUsageUtilization" in input_text:
            poisoned = _cache()
            poisoned["accessToken"] = SENTINEL_TOKEN  # hostile extra field
            poisoned["note"] = SENTINEL_TOKEN
            return 0, json.dumps(poisoned), ""
        return 0, json.dumps({"pct": 15}), ""
    monkeypatch.setattr(ur, "run_remote", fake_runner)

    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli.usage(Namespace(host="merlin", json=True, max_age_seconds=600))
    assert rc == 0
    assert SENTINEL_TOKEN not in out.getvalue()
    rows = json.loads(out.getvalue())
    assert {r["provider"] for r in rows} == {"claude", "codex"}


def test_usage_cli_unknown_host():
    err = io.StringIO()
    with redirect_stderr(err):
        rc = cli.usage(Namespace(host="nope", json=False, max_age_seconds=None))
    assert rc == 1 and "unknown host" in err.getvalue()
