"""Contract + unit tests for the public Codex weekly-usage probe.

The probe drives ``codex app-server`` (stdio JSON-RPC) and emits the canonical
wire object the v2 usage collector consumes. These tests pin: the collector
actually invokes a shipped ``scripts/check_codex_usage.py``; the weekly window
is classified by duration (fail-closed on ambiguity); the wire shape matches
``usage_collector._CODEX_USAGE_FIELDS``; and the source carries no private/user
residue (hardcoded Homebrew path, Homebrew PATH injection, or a hardcoded TZ).
"""
from __future__ import annotations

import importlib.util
import json
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PROBE_PATH = REPO_ROOT / "scripts/check_codex_usage.py"

_spec = importlib.util.spec_from_file_location("public_codex_usage", PROBE_PATH)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, data: str) -> None:
        self.writes.append(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeStdout:
    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""


class _FakeProc:
    def __init__(self, lines: list[str]) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines)
        self.stderr = _FakeStdout([])

    def wait(self, timeout=None) -> int:
        return 0

    def kill(self) -> None:
        pass


def _weekly(pct, resets_at, *, duration=10080):
    return {"usedPercent": pct, "resetsAt": resets_at, "windowDurationMins": duration}


# ---- Collector contract -----------------------------------------------------

def test_collector_invokes_a_shipped_codex_script(monkeypatch, tmp_path):
    """collect_usage_state must wire an on-disk check_codex_usage.py under --json."""
    import collect_usage_state

    seen: dict = {}

    class _Collector:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def run_once(self):
            pass

    monkeypatch.setattr(collect_usage_state, "UsageStateCollector", _Collector)
    collect_usage_state.main(["--state", str(tmp_path / "state.json")])
    codex_command = seen["codex_command"]
    script = Path(codex_command[1])
    assert script.name == "check_codex_usage.py"
    assert script.is_file(), f"collector wires a missing script: {script}"
    assert "--json" in codex_command


def test_json_payload_keys_match_the_collector_contract():
    from usage_collector import _CODEX_USAGE_FIELDS

    payload = probe.build_usage_payload({
        "pct": 42,
        "resets_at_iso": "2026-09-14T05:00:00Z",
        "resets_text": "Sep 14 at 12am (UTC)",
        "upstream_reported_at": "2026-09-11T19:00:00Z",
    })
    assert set(payload) == set(_CODEX_USAGE_FIELDS)


# ---- Weekly-window classification -------------------------------------------

def test_weekly_window_is_classified_by_duration_not_position():
    result = {"rateLimits": {
        "primary": _weekly(10, 1_760_000_000, duration=300),      # 5h daily
        "secondary": _weekly(37, 1_760_400_000, duration=10080),  # weekly
    }}
    parsed = probe._parse_rate_limits(result)
    assert parsed["pct"] == 37
    assert parsed["resets_at_iso"] is not None


def test_missing_weekly_window_is_a_valid_all_null_observation():
    parsed = probe._parse_rate_limits({"rateLimits": {"primary": _weekly(80, 1, duration=300)}})
    assert parsed == {"pct": None, "resets_at_iso": None, "resets_text": None}


def test_two_weekly_windows_fail_closed():
    result = {"rateLimits": {
        "primary": _weekly(10, 1_760_000_000),
        "secondary": _weekly(37, 1_760_400_000),
    }}
    with pytest.raises(ValueError):
        probe._parse_rate_limits(result)


@pytest.mark.parametrize("bad", ["soon", True, float("inf"), float("nan"), [], {}])
def test_numeric_pct_with_malformed_non_null_resets_at_fails_closed(bad):
    result = {"rateLimits": {"secondary": {"usedPercent": 40, "resetsAt": bad, "windowDurationMins": 10080}}}
    with pytest.raises(ValueError):
        probe._parse_rate_limits(result)


def test_pct_rounds_and_rejects_out_of_range():
    assert probe._pct(36.6) == 37
    assert probe._pct(None) is None
    for bad in (-1, 101, float("nan"), True):
        with pytest.raises(ValueError):
            probe._pct(bad)


# ---- Timezone genericization ------------------------------------------------

def test_reset_text_uses_injected_timezone_and_utc_iso():
    # 2026-09-14T05:00:00Z == 2026-09-14 00:00 America/Chicago (CDT, UTC-5).
    ts = 1_789_362_000
    iso_utc, _ = probe._fmt_reset(ts, tz=timezone.utc)
    assert iso_utc == "2026-09-14T05:00:00Z"
    _, text_chicago = probe._fmt_reset(ts, tz=ZoneInfo("America/Chicago"))
    assert "12am" in text_chicago and "Sep 14" in text_chicago
    # The ISO stamp is timezone-invariant; only display text follows the tz.
    iso_other, _ = probe._fmt_reset(ts, tz=ZoneInfo("America/Chicago"))
    assert iso_other == iso_utc


# ---- Payload shape ----------------------------------------------------------

def test_build_usage_payload_requires_exact_weekly_fields():
    with pytest.raises(ValueError):
        probe.build_usage_payload({"pct": 1, "resets_at_iso": None, "resets_text": None})  # missing stamp
    with pytest.raises(ValueError):
        probe.build_usage_payload({
            "pct": 1, "resets_at_iso": None, "resets_text": None,
            "upstream_reported_at": "not-a-timestamp",
        })


# ---- app-server RPC drive ---------------------------------------------------

def test_collect_usage_drives_initialize_then_rate_limits_read():
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 0, "result": {}}) + "\n",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/somethingElse"}) + "\n",
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
            "rateLimits": {"secondary": _weekly(37, 1_789_628_400)}}}) + "\n",
    ]
    proc = _FakeProc(lines)
    usage = probe.collect_usage(
        codex_bin="/fake/codex",
        popen=lambda *a, **k: proc,
        now_fn=lambda: "2026-09-11T19:00:00Z",
    )
    assert usage["pct"] == 37
    assert usage["upstream_reported_at"] == "2026-09-11T19:00:00Z"
    sent = [json.loads(w) for w in proc.stdin.writes]
    methods = [m.get("method") for m in sent]
    assert methods == ["initialize", "notifications/initialized", "account/rateLimits/read"]


# ---- No private / user residue ----------------------------------------------

def test_source_has_no_private_or_user_residue():
    text = PROBE_PATH.read_text(encoding="utf-8")
    assert "/opt/homebrew" not in text, "no hardcoded Homebrew path/PATH injection"
    assert "America/Chicago" not in text, "no hardcoded user timezone"
    assert "shutil.which" in text, "binary must be discovered generically"
