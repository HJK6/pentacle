import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("isolated_tmux_env")

spec = importlib.util.spec_from_file_location("public_claude_usage", Path(__file__).parents[3] / "scripts/check_claude_usage.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_weekly_labels_do_not_confuse_session_or_other_model_limits():
    value = probe.parse_weekly_usage("Current session\n3% used\nCurrent week (all models)\n42% used\nResets tomorrow\nCurrent week (Sonnet)\n77% used\nResets later")
    assert value == {"week_all_pct": 42, "week_all_resets": "tomorrow", "week_fable_pct": None, "week_fable_resets": None}
    assert probe.parse_weekly_usage("Current session\n2% used\n$0.20\n99% used") is None
    assert probe.parse_weekly_usage("Current week (all models)\n101% used") is None


def test_monthly_all_models_label_populates_the_account_period_row():
    value = probe.parse_weekly_usage(
        "Current session\n3% used\nCurrent month (all models)\n24% used\nResets Nov 1\n"
    )
    assert value == {
        "week_all_pct": 24,
        "week_all_resets": "Nov 1",
        "week_fable_pct": None,
        "week_fable_resets": None,
    }


def test_enterprise_monthly_spend_populates_the_account_period_row():
    assert probe.parse_oauth_usage({"spend": {"enabled": True, "percent": 24}}) == {
        "week_all_pct": 24,
        "week_all_resets": None,
        "week_fable_pct": None,
        "week_fable_resets": None,
    }


@pytest.mark.parametrize("spend", [
    None,
    {"enabled": False, "percent": 24},
    {"enabled": True, "percent": True},
    {"enabled": True, "percent": 1.5},
    {"enabled": True, "percent": 101},
])
def test_enterprise_monthly_spend_rejects_non_limits(spend):
    assert probe.parse_oauth_usage({"spend": spend}) is None


def test_environment_oauth_token_precedes_keychain(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", " protected-token ")
    assert probe.oauth_token(run=lambda *args, **kwargs: pytest.fail("keychain read")) == "protected-token"


def test_macos_oauth_token_uses_claude_code_keychain(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(probe.sys, "platform", "darwin")
    monkeypatch.setattr(probe.shutil, "which", lambda executable: "/usr/bin/security")

    def run(command, **kwargs):
        assert command[-1] == "Claude Code-credentials"
        return SimpleNamespace(
            returncode=0,
            stdout='{"claudeAiOauth":{"accessToken":"secure-value"}}',
        )

    assert probe.oauth_token(run=run) == "secure-value"


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


def test_trust_error_names_the_configured_cwd_and_the_remedy():
    def run(command, **kwargs):
        return SimpleNamespace(stdout="Quick safety check: Yes, I trust this folder", returncode=0)

    with pytest.raises(RuntimeError) as exc:
        probe.collect(claude="claude", tmux="tmux", cwd="/opt/probe-home", run=run, sleep=lambda _: None)
    message = str(exc.value)
    assert "/opt/probe-home" in message          # operator-configured cwd surfaced for diagnosis
    assert "PENTACLE_USAGE_CWD" in message        # remedy: repoint at a trusted folder
    assert "trust" in message                     # remedy also covers re-trusting that folder


def test_login_prompt_is_distinct_from_trust():
    def run(command, **kwargs):
        return SimpleNamespace(stdout="Please sign in to continue", returncode=0)

    with pytest.raises(RuntimeError, match="logged in"):
        probe.collect(claude="claude", tmux="tmux", cwd="/tmp", run=run, sleep=lambda _: None)


def test_default_cwd_is_home_not_process_cwd(monkeypatch):
    # isolated_tmux_env / conftest does not clear this, so an inherited value would
    # pollute the assertion; clear it to check the fallback.
    monkeypatch.delenv("PENTACLE_USAGE_CWD", raising=False)
    from pathlib import Path
    assert probe.default_cwd() == str(Path.home())
    assert probe.default_cwd() != "/"
    monkeypatch.setenv("PENTACLE_USAGE_CWD", "/srv/trusted-workspace")
    assert probe.default_cwd() == "/srv/trusted-workspace"


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


def test_oauth_path_is_opt_in_and_defaults_off(monkeypatch):
    # With the opt-in unset, the probe performs NO Keychain lookup and NO usage
    # network call: neither oauth_token nor collect_oauth may run. main() then
    # falls through to the local CLI path (short-circuited here via which->None).
    monkeypatch.delenv("PENTACLE_USAGE_CLAUDE_OAUTH", raising=False)
    assert probe.oauth_enabled() is False

    def forbidden(*args, **kwargs):
        raise AssertionError("OAuth path ran while the opt-in was disabled")

    monkeypatch.setattr(probe, "oauth_token", forbidden)
    monkeypatch.setattr(probe, "collect_oauth", forbidden)
    monkeypatch.setattr(probe.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(probe.sys, "argv", ["check_claude_usage.py", "--json"])
    with pytest.raises(SystemExit):
        probe.main()


def test_oauth_path_runs_only_when_opted_in(monkeypatch, capsys):
    import json

    monkeypatch.setenv("PENTACLE_USAGE_CLAUDE_OAUTH", "1")
    assert probe.oauth_enabled() is True
    monkeypatch.setattr(probe, "oauth_token", lambda: "secure-value")
    captured = {}

    def fake_collect_oauth(token):
        captured["token"] = token
        return {
            "week_all_pct": 7, "week_all_resets": None,
            "week_fable_pct": None, "week_fable_resets": None,
        }

    monkeypatch.setattr(probe, "collect_oauth", fake_collect_oauth)
    # which() must never be consulted: the opted-in OAuth path returns first.
    monkeypatch.setattr(probe.shutil, "which",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("CLI path ran")))
    monkeypatch.setattr(probe.sys, "argv", ["check_claude_usage.py", "--json"])
    assert probe.main() == 0
    assert captured["token"] == "secure-value"
    assert json.loads(capsys.readouterr().out)["week_all_pct"] == 7
