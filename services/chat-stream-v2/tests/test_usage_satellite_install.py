"""Rendered collector jobs: pinned release/executable paths, cadence, no secrets (AC6, AC8)."""
from __future__ import annotations

import importlib.util
import json
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from .test_deploy_script import deploy_mod

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
_SPEC = importlib.util.spec_from_file_location("install_usage_collector", DEPLOY / "install_usage_collector.py")
installer = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = installer
_SPEC.loader.exec_module(installer)


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


@pytest.fixture()
def params(tmp_path: Path):
    release = tmp_path / "release"
    collector = release / installer.COLLECTOR
    collector.parent.mkdir(parents=True)
    collector.write_text("")
    home = tmp_path / "home"
    (home).mkdir()
    return installer.Params(
        host="amaterasu", release_checkout=release, python=_exe(tmp_path / "bin/python3"),
        claude_bin=_exe(home / ".local/bin/claude"), codex_bin=_exe(tmp_path / "npm/bin/codex"),
        tmux_bin=_exe(tmp_path / "usr/bin/tmux"), usage_cwd=home, home=home,
    )


def _unit_env(service: bytes) -> dict[str, str]:
    env = {}
    for line in service.decode().splitlines():
        if line.startswith("Environment="):
            key, _, value = line[len("Environment="):].partition("=")
            env[key] = value
    return env


def _assert_pinned(env: dict[str, str], program: list[str], params) -> None:
    assert Path(env["PENTACLE_CLAUDE_BIN"]).is_absolute() and env["PENTACLE_CLAUDE_BIN"] == str(params.claude_bin)
    assert Path(env["PENTACLE_CODEX_BIN"]).is_absolute() and env["PENTACLE_CODEX_BIN"] == str(params.codex_bin)
    assert Path(env["PENTACLE_USAGE_TMUX_BIN"]).is_absolute()
    assert env["PENTACLE_USAGE_CLAUDE_OAUTH"] == "0"
    assert env["PENTACLE_HOST_ID"] == params.host
    assert Path(env["PENTACLE_USAGE_CWD"]).is_dir()
    assert not [key for key in env if any(word in key.upper() for word in ("TOKEN", "SECRET", "KEY"))]
    collector = Path(program[1])
    assert collector.is_relative_to(params.release_checkout)
    assert collector == params.release_checkout / installer.COLLECTOR
    assert Path(program[0]).is_absolute()
    assert Path(program[program.index("--shared-scripts") + 1]).is_relative_to(params.release_checkout)


def test_amaterasu_systemd_unit_is_pinned_and_timer_is_600s(params):
    rendered = installer.render_systemd(params)
    service = rendered[installer.SERVICE]
    assert b"__PENTACLE_" not in service
    env = _unit_env(service)
    exec_start = next(line for line in service.decode().splitlines() if line.startswith("ExecStart="))
    _assert_pinned(env, exec_start[len("ExecStart="):].split(), params)
    assert "Type=oneshot" in service.decode()
    timer = rendered[installer.TIMER].decode()
    assert "OnUnitActiveSec=600" in timer
    assert [line for line in timer.splitlines() if line.startswith("OnUnitActiveSec=")] == ["OnUnitActiveSec=600"]
    assert f"Unit={installer.SERVICE}" in timer


def test_merlin_plist_is_pinned_and_600s(params):
    plist = plistlib.loads(installer.render_launchd(params)[f"{installer.LABEL}.plist"])
    assert plist["StartInterval"] == 600
    assert plist["Label"] == "com.pentacle.usage-state-collector"
    _assert_pinned(plist["EnvironmentVariables"], plist["ProgramArguments"], params)


def test_thoth_plist_keeps_300s_and_pins_the_clis(tmp_path):
    template = DEPLOY / "com.pentacle.usage-state-collector.plist"
    repo = tmp_path / "release"
    destination = repo / "services/chat-stream-v2/deploy/com.pentacle.usage-state-collector.plist"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(template.read_bytes())
    rendered = plistlib.loads(deploy_mod._render_v2_usage_collector_plist(repo))
    assert rendered["StartInterval"] == 300  # AC8: Thoth cadence unchanged
    env = rendered["EnvironmentVariables"]
    assert Path(env["PENTACLE_CLAUDE_BIN"]).is_absolute()
    assert Path(env["PENTACLE_CODEX_BIN"]).is_absolute()
    # Thoth keeps its CLIs under the user home (there is no /opt/homebrew/bin/codex there): a pin
    # to a path that does not exist fails the probe closed, so the template must bind to the home.
    home = str(Path.home())
    assert env["PENTACLE_CLAUDE_BIN"] == f"{home}/.local/bin/claude"
    assert env["PENTACLE_CODEX_BIN"] == f"{home}/.local/bin/codex"
    assert env["PENTACLE_USAGE_CLAUDE_OAUTH"] == "0"
    assert not [key for key in env if any(word in key.upper() for word in ("TOKEN", "SECRET"))]
    assert Path(rendered["ProgramArguments"][1]).is_relative_to(repo)


def test_cadences_are_pinned_across_the_three_templates():
    assert plistlib.loads((DEPLOY / "com.pentacle.usage-state-collector.plist").read_bytes().replace(
        b"__PENTACLE_RELEASE_CHECKOUT__", b"/r").replace(b"__PENTACLE_USER_HOME__", b"/h"))["StartInterval"] == 300
    assert b"<integer>600</integer>" in (DEPLOY / "satellite/com.pentacle.usage-state-collector.plist").read_bytes()
    assert b"OnUnitActiveSec=600" in (DEPLOY / "satellite" / installer.TIMER).read_bytes()


def test_installer_refuses_non_absolute_or_missing_pins(params, tmp_path):
    import argparse
    base = dict(host="amaterasu", release_checkout=str(params.release_checkout), python=str(params.python),
                claude_bin=str(params.claude_bin), codex_bin=str(params.codex_bin), tmux_bin=str(params.tmux_bin),
                usage_cwd=str(params.usage_cwd))
    installer.build_params(argparse.Namespace(**base), home=params.home)
    for key, bad in (("codex_bin", "codex"), ("claude_bin", "/nonexistent/claude"), ("python", "python3"),
                     ("tmux_bin", "tmux"), ("codex_bin", None), ("release_checkout", str(tmp_path / "elsewhere"))):
        with pytest.raises(installer.InstallError):
            installer.build_params(argparse.Namespace(**{**base, key: bad}), home=params.home)


def test_install_keeps_preimage_does_not_activate_and_rolls_back(params, monkeypatch):
    monkeypatch.setattr(installer, "_systemd", lambda: True)
    calls: list[tuple[str, ...]] = []

    def runner(argv):
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    unit_dir = params.home / ".config/systemd/user"
    unit_dir.mkdir(parents=True)
    (unit_dir / installer.SERVICE).write_text("original service")
    installer.install(params, runner)
    assert (unit_dir / installer.TIMER).exists()
    assert not any("enable" in call or "start" in call for call in calls), "install must not activate"
    assert calls == [("systemctl", "--user", "daemon-reload")]
    # A second install must keep the FIRST preimage.
    installer.install(params, runner)
    manifest = json.loads((installer.preimage_dir(params) / "manifest.json").read_text())
    assert manifest[str(unit_dir / installer.SERVICE)] == installer.SERVICE
    assert manifest[str(unit_dir / installer.TIMER)] == "absent"

    installer.enable(params, runner)
    assert ("systemctl", "--user", "enable", "--now", installer.TIMER) in calls

    installer.rollback(params, runner)
    assert (unit_dir / installer.SERVICE).read_text() == "original service"
    assert not (unit_dir / installer.TIMER).exists()
    assert ("systemctl", "--user", "disable", "--now", installer.TIMER) in calls


def test_verify_runs_the_rendered_command_with_the_rendered_environment(params, tmp_path, monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(command=command, env=kwargs["env"], cwd=kwargs["cwd"])
        params.state_path.parent.mkdir(parents=True, exist_ok=True)
        params.state_path.write_text(json.dumps({"observations": {"claude": {"collection": {"status": "ok"}}}}))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-leak")
    report = installer.verify(params)
    assert seen["command"] == params.command
    assert seen["env"]["PENTACLE_CLAUDE_BIN"] == str(params.claude_bin)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen["env"]
    assert report["exit_code"] == 0 and report["observations"]["claude"]["collection"]["status"] == "ok"
    assert {"wall_seconds", "child_cpu_user_seconds", "child_cpu_system_seconds"} <= set(report)
