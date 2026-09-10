import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("public_claude_usage", Path(__file__).parents[3] / "scripts/check_claude_usage.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_weekly_labels_do_not_confuse_session_or_other_model_limits():
    value = probe.parse_weekly_usage("Current session\n3% used\nCurrent week (all models)\n42% used\nResets tomorrow\nCurrent week (Sonnet)\n77% used\nResets later")
    assert value == {"week_all_pct": 42, "week_all_resets": "tomorrow", "week_fable_pct": None, "week_fable_resets": None}
    assert probe.parse_weekly_usage("Current session\n2% used\n$0.20\n99% used") is None
    assert probe.parse_weekly_usage("Current week (all models)\n101% used") is None


def test_only_explicit_weekly_fable_label_populates_fable():
    value = probe.parse_weekly_usage("Current week (all models)\n0% used\nCurrent week (Fable)\n20% used\nResets Friday")
    assert value["week_all_pct"] == 0
    assert value["week_fable_pct"] == 20
    assert value["week_fable_resets"] == "Friday"


def test_probe_sends_only_usage_and_cleans_its_dedicated_socket():
    calls = []
    screens = iter(["Claude Code\n❯ ", "Current week (all models)\n42% used\nResets tomorrow"])

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=next(screens) if "capture-pane" in command else "", returncode=0)

    assert probe.collect(claude="claude", tmux="tmux", cwd="/tmp", run=run, sleep=lambda _: None)["week_all_pct"] == 42
    assert len({tuple(c[:3]) for c in calls}) == 1
    assert calls[0][2].startswith("pentacle-usage-")
    assert calls[-1][3:] == ["kill-server"]
    assert [c[3:] for c in calls if "send-keys" in c] == [["send-keys", "-t", "probe", "-l", "/usage"], ["send-keys", "-t", "probe", "Enter"]]


def test_trust_prompt_is_not_accepted_and_cleanup_still_runs():
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="Do you trust this folder?", returncode=0)

    with pytest.raises(RuntimeError, match="trust"):
        probe.collect(claude="claude", tmux="tmux", cwd="/tmp", run=run, sleep=lambda _: None)
    assert calls[-1][3:] == ["kill-server"]
    assert not any("send-keys" in c for c in calls)


def test_skip_codex_uses_no_update_instead_of_missing_helper(monkeypatch, tmp_path):
    import collect_usage_state
    seen = {}

    class Collector:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def run_once(self):
            pass

    monkeypatch.setattr(collect_usage_state, "UsageStateCollector", Collector)
    assert collect_usage_state.main(["--state", str(tmp_path / "usage_state.json"), "--skip-codex"]) == 0
    import subprocess
    import json
    assert json.loads(subprocess.check_output(seen["codex_command"], text=True)) == {"status": "no_update"}
