"""Deterministic end-to-end proof for the Codex usage probe + collector.

These tests replace reliance on a live Codex account: the probe's real
subprocess boundary is exercised against a temporary fake ``codex app-server``
placed on ``PATH`` (the same discovery mechanism production uses). They pin the two runtime outcomes the
spec promises — a fresh ``codex_health.outcome=ok`` seven-key row on success, and
an accurate ``provider_error`` that byte-preserves the prior Codex LKG, retains
its receipt stamps, advances ``attempted_at``, and leaves Claude/Fable untouched.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))

REPO_ROOT = Path(__file__).resolve().parents[3]
PROBE_PATH = REPO_ROOT / "scripts/check_codex_usage.py"

_spec = importlib.util.spec_from_file_location("public_codex_usage_e2e", PROBE_PATH)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

from usage_collector import UsageStateCollector, _ok, _row  # noqa: E402
from usage_state import UsageStateStore, canonical_state_v2  # noqa: E402

CODEX_JSON = (sys.executable, str(PROBE_PATH), "--json")
CLAUDE_NO_UPDATE = (sys.executable, "-c", "print('{\"status\":\"no_update\"}')")


def _write_fake_codex(tmp_path: Path, *, used_percent=41, resets_at=1_789_362_000, fail=False, burst=False) -> str:
    """A minimal ``codex app-server`` stand-in speaking the probe's JSON-RPC."""
    if fail:
        body = "import sys\nsys.exit(3)\n"
    elif burst:
        # Emit an unrelated notification and the expected response together in one
        # write, to prove the reader drains buffered lines rather than stalling.
        rate = {"secondary": {"usedPercent": used_percent, "resetsAt": resets_at, "windowDurationMins": 10080}}
        body = (
            "import sys, json\n"
            "for line in sys.stdin:\n"
            "    line = line.strip()\n"
            "    if not line:\n        continue\n"
            "    msg = json.loads(line); mid = msg.get('id'); method = msg.get('method')\n"
            "    if method == 'initialize':\n"
            "        sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':mid,'result':{}})+chr(10)); sys.stdout.flush()\n"
            "    elif method == 'account/rateLimits/read':\n"
            "        note = json.dumps({'jsonrpc':'2.0','method':'notifications/sessionUpdate'})\n"
            f"        resp = json.dumps({{'jsonrpc':'2.0','id':mid,'result':{{'rateLimits':{json.dumps(rate)}}}}})\n"
            "        sys.stdout.write(note+chr(10)+resp+chr(10)); sys.stdout.flush()\n"
        )
    else:
        rate = {"secondary": {"usedPercent": used_percent, "resetsAt": resets_at, "windowDurationMins": 10080}}
        body = (
            "import sys, json\n"
            "for line in sys.stdin:\n"
            "    line = line.strip()\n"
            "    if not line:\n        continue\n"
            "    msg = json.loads(line)\n"
            "    mid = msg.get('id'); method = msg.get('method')\n"
            "    if method == 'initialize':\n"
            "        sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':mid,'result':{}})+chr(10)); sys.stdout.flush()\n"
            "    elif method == 'account/rateLimits/read':\n"
            f"        sys.stdout.write(json.dumps({{'jsonrpc':'2.0','id':mid,'result':{{'rateLimits':{json.dumps(rate)}}}}})+chr(10)); sys.stdout.flush()\n"
        )
    fake = tmp_path / "codex"
    fake.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(fake)


def _put_fake_on_path(monkeypatch, tmp_path, **kw):
    """Write a fake ``codex`` and prepend its dir to PATH — the same discovery
    mechanism production uses (no dedicated env knob)."""
    _write_fake_codex(tmp_path, **kw)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])


def _seed_v2_state(path: Path) -> dict:
    """Write a valid v2 state with populated Claude/Fable and Codex rows."""
    t = "2026-09-11T18:00:00Z"
    claude_lkg = [
        _row("claude", "Claude", pct=12, resets_text="Fri"),
        _row("fable", "Fable", pct=3, resets_text="Sat"),
    ]
    codex_lkg = _row("codex", "Codex", pct=77, resets_text="Sep 20 at 5pm (UTC)",
                     resets_at_iso="2026-09-20T22:00:00Z", probed_at=t, upstream_reported_at=t)
    payload = canonical_state_v2(claude_lkg, _ok(t), codex_lkg=codex_lkg, codex_health=_ok(t))
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return payload


def test_success_via_fake_app_server_records_fresh_seven_key_codex_row(tmp_path, monkeypatch):
    _put_fake_on_path(monkeypatch, tmp_path)
    state = tmp_path / "usage_state.json"
    UsageStateCollector(
        state_path=state, claude_command=CLAUDE_NO_UPDATE, codex_command=CODEX_JSON,
    ).run_once()

    loaded = json.loads(state.read_text())
    row = loaded["codex_lkg"]
    assert set(row) == {"id", "label", "pct", "resets_at_iso", "resets_text",
                        "upstream_reported_at", "probed_at"}
    assert row["id"] == "codex" and row["pct"] == 41 and row["resets_at_iso"] is not None
    health = loaded["codex_health"]
    assert health["outcome"] == "ok" and health["error"] is None
    # The persisted Codex row carries its own receipt (T1) and completion (T2)
    # stamps; assert the ordering on the row itself, not on the health record.
    assert row["upstream_reported_at"] is not None and row["probed_at"] is not None
    assert _parse(row["upstream_reported_at"]) <= _parse(row["probed_at"])


def test_failure_retains_codex_lkg_and_leaves_claude_untouched(tmp_path, monkeypatch):
    state = tmp_path / "usage_state.json"
    seed = _seed_v2_state(state)
    _put_fake_on_path(monkeypatch, tmp_path, fail=True)  # codex on PATH but exits nonzero
    UsageStateCollector(
        state_path=state, claude_command=CLAUDE_NO_UPDATE, codex_command=CODEX_JSON,
    ).run_once()

    loaded = json.loads(state.read_text())
    health = loaded["codex_health"]
    assert health["outcome"] == "provider_error"
    assert health["error"]["code"] == "codex_usage_provider_error"
    assert health["error"]["message"].strip()
    # Prior LKG byte-preserved (serialization identity, not just value equality);
    # receipt/completion stamps retained; attempt strictly advanced.
    assert json.dumps(loaded["codex_lkg"], sort_keys=True) == json.dumps(seed["codex_lkg"], sort_keys=True)
    assert health["probed_at"] == seed["codex_health"]["probed_at"]
    assert health["upstream_reported_at"] == seed["codex_health"]["upstream_reported_at"]
    assert health["attempted_at"] is not None
    assert _parse(health["attempted_at"]) > _parse(seed["codex_health"]["attempted_at"])
    # Claude/Fable rows and health untouched by the Codex failure.
    assert loaded["claude_fable_lkg"] == seed["claude_fable_lkg"]
    assert loaded["claude_health"] == seed["claude_health"]


def test_probe_json_stdout_is_pure_and_stderr_quiet(tmp_path):
    _write_fake_codex(tmp_path)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"])
    result = subprocess.run(CODEX_JSON, capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)  # exactly one JSON object, no diagnostics
    assert set(payload) == {"pct", "resets_at_iso", "resets_text", "upstream_reported_at"}
    assert payload["pct"] == 41


def test_burst_notification_and_response_are_parsed(tmp_path, monkeypatch):
    """A notification and the expected response arriving in one write must be
    parsed by draining buffered lines (the plain-readline contract)."""
    _put_fake_on_path(monkeypatch, tmp_path, burst=True)
    state = tmp_path / "usage_state.json"
    UsageStateCollector(
        state_path=state, claude_command=CLAUDE_NO_UPDATE, codex_command=CODEX_JSON,
    ).run_once()
    loaded = json.loads(state.read_text())
    assert loaded["codex_health"]["outcome"] == "ok"
    assert loaded["codex_lkg"]["pct"] == 41


def test_collector_bounds_hung_probe_via_subprocess_timeout(tmp_path):
    """Production hang-safety is the collector's 75s subprocess timeout, not a
    standalone RPC deadline: a timed-out probe yields provider_error + retained LKG."""
    state = tmp_path / "usage_state.json"
    seed = _seed_v2_state(state)

    def run(command, **kwargs):
        if "check_codex_usage" in " ".join(map(str, command)):
            assert kwargs.get("timeout") == 75  # the hard production bound
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
        return subprocess.CompletedProcess(command, 0, '{"status":"no_update"}', "")

    UsageStateCollector(
        state_path=state, claude_command=CLAUDE_NO_UPDATE, codex_command=CODEX_JSON, run=run,
    ).run_once()
    loaded = json.loads(state.read_text())
    assert loaded["codex_health"]["outcome"] == "provider_error"
    assert loaded["codex_health"]["error"]["code"] == "codex_usage_provider_error"
    assert json.dumps(loaded["codex_lkg"], sort_keys=True) == json.dumps(seed["codex_lkg"], sort_keys=True)
    # The Codex timeout must not disturb Claude/Fable rows or health.
    assert loaded["claude_fable_lkg"] == seed["claude_fable_lkg"]
    assert loaded["claude_health"] == seed["claude_health"]


def test_default_local_tz_is_dst_aware():
    """The default (no explicit tz) path must resolve DST per the reset instant,
    not a single fixed offset captured at process start."""
    prev = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    try:
        _, summer = probe._fmt_reset(1_752_566_400)  # 2025-07-15T08:00:00Z -> 1am PDT
        _, winter = probe._fmt_reset(1_736_935_200)  # 2025-01-15T10:00:00Z -> 2am PST
        assert "1am" in summer and "PDT" in summer
        assert "2am" in winter and "PST" in winter
    finally:
        if prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev
        time.tzset()


def test_resolve_codex_bin_uses_path_only(monkeypatch):
    monkeypatch.setattr(probe.shutil, "which", lambda name: "/resolved/" + name if name == "codex" else None)
    assert probe._resolve_codex_bin() == "/resolved/codex"


def test_reset_text_follows_dst_and_iso_stays_utc():
    la = ZoneInfo("America/Los_Angeles")
    summer = 1_752_566_400  # 2025-07-15T08:00:00Z -> PDT (UTC-7)
    winter = 1_736_935_200  # 2025-01-15T10:00:00Z -> PST (UTC-8)
    iso_s, text_s = probe._fmt_reset(summer, tz=la)
    iso_w, text_w = probe._fmt_reset(winter, tz=la)
    assert iso_s == "2025-07-15T08:00:00Z" and "1am" in text_s   # 08:00Z - 7h = 01:00 PDT
    assert iso_w == "2025-01-15T10:00:00Z" and "2am" in text_w   # 10:00Z - 8h = 02:00 PST
    # Same-instant ISO is timezone-invariant; only the label localizes.
    assert probe._fmt_reset(summer, tz=ZoneInfo("UTC"))[0] == iso_s


def test_null_used_percent_is_all_null_observation():
    result = {"rateLimits": {"secondary": {"usedPercent": None, "resetsAt": 1, "windowDurationMins": 10080}}}
    assert probe._parse_rate_limits(result) == {"pct": None, "resets_at_iso": None, "resets_text": None}


def test_missing_resets_at_with_numeric_pct_raises():
    result = {"rateLimits": {"secondary": {"usedPercent": 40, "windowDurationMins": 10080}}}
    with pytest.raises(ValueError):
        probe._parse_rate_limits(result)
