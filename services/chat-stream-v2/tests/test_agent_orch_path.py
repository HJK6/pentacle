"""Spawned provider panes must inherit the target host's agent-orch PATH."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import launch
import pytest
from machines import _machine_from_dict, load_machines
from main import _local_machine_for_spawn, parse_args


def test_machine_profile_preserves_explicit_agent_orch_bin_dir() -> None:
    machine = _machine_from_dict(
        {
            "name": "hosta",
            "ssh_target": None,
            "claude_bin": "/Users/example/.local/bin/claude",
            "tmux_bin": "/opt/homebrew/bin/tmux",
            "agent_orch_bin_dir": "/Users/example/.local/bin",
        }
    )

    assert machine.agent_orch_bin_dir == "/Users/example/.local/bin"


def test_profile_default_follows_provider_install_dir_not_tmux_dir() -> None:
    profile = _machine_from_dict(
        {
            "name": "hostd",
            "ssh_target": "user@hostd",
            "claude_bin": "/Users/example/.local/bin/claude",
            "codex_bin": "/Users/example/.local/bin/codex",
            "tmux_bin": "/opt/homebrew/bin/tmux",
            "projects_root": "/Users/example/.claude/projects",
            "cwd": "/Users/example/agent-workspace",
        }
    )

    machine = launch.machine_from_config(profile)

    assert machine.agent_orch_bin_dir == "/Users/example/.local/bin"


def test_codex_only_remote_profile_uses_codex_install_dir() -> None:
    profile = _machine_from_dict(
        {
            "name": "legacy-codex",
            "ssh_target": "user@legacy-codex",
            "codex_bin": "/remote/home/.npm-global/bin/codex",
            "projects_root": "/remote/home/.codex/projects",
            "cwd": "/remote/home/agent-workspace",
        }
    )

    assert profile.claude_bin == ""
    machine = launch.machine_from_config(profile, provider="codex")

    assert machine.codex_bin == "/remote/home/.npm-global/bin/codex"
    assert machine.agent_orch_bin_dir == "/remote/home/.npm-global/bin"


def test_remote_profile_rejects_relative_active_provider_without_explicit_orch_dir() -> None:
    profile = _machine_from_dict(
        {
            "name": "ambiguous",
            "ssh_target": "user@ambiguous",
            "codex_bin": "codex",
            "projects_root": "/remote/home/.codex/projects",
            "cwd": "/remote/home/agent-workspace",
        }
    )

    with pytest.raises(ValueError, match="absolute codex_bin/cwd/projects_root"):
        launch.machine_from_config(profile, provider="codex")


@pytest.mark.parametrize("field", ("cwd", "projects_root"))
def test_remote_profile_rejects_relative_working_paths(field: str) -> None:
    values = {
        "name": "relative-path",
        "ssh_target": "user@relative-path",
        "claude_bin": "/remote/home/.local/bin/claude",
        "projects_root": "/remote/home/.claude/projects",
        "cwd": "/remote/home/agent-workspace",
    }
    values[field] = "relative"
    profile = _machine_from_dict(values)

    with pytest.raises(ValueError, match="absolute claude_bin/cwd/projects_root"):
        launch.machine_from_config(profile)


def test_remote_profile_rejects_tilde_and_blank_ssh_target() -> None:
    with pytest.raises(ValueError, match="absolute claude_bin"):
        _machine_from_dict(
            {
                "name": "tilde-path",
                "ssh_target": "user@tilde-path",
                "claude_bin": "~/.local/bin/claude",
                "projects_root": "/remote/home/.claude/projects",
                "cwd": "/remote/home/agent-workspace",
            }
        )
    with pytest.raises(ValueError, match="blank ssh_target"):
        _machine_from_dict({"name": "blank-target", "ssh_target": "  "})


def test_local_fallback_follows_active_provider_install_dir(tmp_path: Path) -> None:
    capture = tmp_path / "capture.txt"
    claude_dir = tmp_path / "claude-bin"
    codex_dir = tmp_path / "codex-bin"
    claude_dir.mkdir()
    codex_dir.mkdir()
    for provider, provider_dir in (("claude", claude_dir), ("codex", codex_dir)):
        agent_orch = provider_dir / "agent-orch"
        agent_orch.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        agent_orch.chmod(0o755)
        provider_bin = provider_dir / provider
        provider_bin.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$(command -v agent-orch)\" > {shlex.quote(str(capture))}\n",
            encoding="utf-8",
        )
        provider_bin.chmod(0o755)

    machine = launch.local_machine(
        "local-fallback",
        cwd=str(tmp_path),
        claude_bin=str(claude_dir / "claude"),
        codex_bin=str(codex_dir / "codex"),
        projects_root=str(tmp_path / "projects"),
    )
    for provider, provider_dir in (("claude", claude_dir), ("codex", codex_dir)):
        plan = launch.build_launch(
            machine,
            provider=provider,
            tmux_session=f"{provider}-fallback",
            launch_model=None,
            launch_effort=None,
        )
        completed = subprocess.run(
            ["/bin/sh", "-c", plan.command],
            cwd=str(tmp_path),
            env={"PATH": "/usr/bin:/bin", "PENTACLE_CODEX_ENABLE_APPS": "1"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert capture.read_text(encoding="utf-8").strip() == str(provider_dir / "agent-orch")


def test_bare_local_provider_commands_resolve_before_minimal_child_path(
    tmp_path: Path, monkeypatch,
) -> None:
    capture = tmp_path / "bare-capture.txt"
    claude_dir = tmp_path / "bare-claude"
    codex_dir = tmp_path / "bare-codex"
    claude_dir.mkdir()
    codex_dir.mkdir()
    for provider, provider_dir in (("claude", claude_dir), ("codex", codex_dir)):
        agent_orch = provider_dir / "agent-orch"
        agent_orch.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        agent_orch.chmod(0o755)
        provider_bin = provider_dir / provider
        provider_bin.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$(command -v agent-orch)\" > {shlex.quote(str(capture))}\n",
            encoding="utf-8",
        )
        provider_bin.chmod(0o755)
    monkeypatch.setenv("PATH", f"{claude_dir}:{codex_dir}:/usr/bin:/bin")

    machine = launch.local_machine(
        "bare-local",
        cwd=str(tmp_path),
        claude_bin="claude",
        codex_bin="codex",
        projects_root=str(tmp_path / "projects"),
    )
    for provider, provider_dir in (("claude", claude_dir), ("codex", codex_dir)):
        plan = launch.build_launch(
            machine,
            provider=provider,
            tmux_session=f"{provider}-bare",
            launch_model=None,
            launch_effort=None,
        )
        completed = subprocess.run(
            ["/bin/sh", "-c", plan.command],
            cwd=str(tmp_path),
            env={"PATH": "/usr/bin:/bin", "PENTACLE_CODEX_ENABLE_APPS": "1"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert capture.read_text(encoding="utf-8").strip() == str(provider_dir / "agent-orch")


def test_local_fallback_handles_claude_path_with_spaces(tmp_path: Path) -> None:
    provider_dir = tmp_path / "claude dir 'safe'"
    provider_dir.mkdir()
    agent_orch = provider_dir / "agent-orch"
    agent_orch.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    agent_orch.chmod(0o755)
    capture = tmp_path / "space-capture.txt"
    claude = provider_dir / "claude"
    claude.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$(command -v agent-orch)\" > {shlex.quote(str(capture))}\n",
        encoding="utf-8",
    )
    claude.chmod(0o755)

    machine = launch.local_machine(
        "space-local",
        cwd=str(tmp_path),
        claude_bin=str(claude),
        codex_bin="codex",
        projects_root=str(tmp_path / "projects"),
    )
    plan = launch.build_launch(
        machine,
        provider="claude",
        tmux_session="space-claude",
        launch_model=None,
        launch_effort=None,
    )
    completed = subprocess.run(
        ["/bin/sh", "-c", plan.command],
        cwd=str(tmp_path),
        env={"PATH": "/usr/bin:/bin", "PENTACLE_CODEX_ENABLE_APPS": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert capture.read_text(encoding="utf-8").strip() == str(agent_orch)


def test_unresolved_bare_local_provider_fails_before_child_launch(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty-provider-path"))
    machine = launch.local_machine(
        "unresolved-local",
        cwd=str(tmp_path),
        claude_bin=str(tmp_path / "claude"),
        codex_bin="codex",
        projects_root=str(tmp_path / "projects"),
    )

    with pytest.raises(ValueError, match="not discoverable on the daemon PATH"):
        launch.build_launch(
            machine,
            provider="codex",
            tmux_session="unresolved-codex",
            launch_model=None,
            launch_effort=None,
        )


def test_codex_absolute_path_with_arguments_keeps_provider_and_agent_orch(
    tmp_path: Path,
) -> None:
    provider_dir = tmp_path / "codex dir 'safe'"
    provider_dir.mkdir()
    agent_orch = provider_dir / "agent-orch"
    agent_orch.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    agent_orch.chmod(0o755)
    capture = tmp_path / "codex-args-capture.txt"
    codex = provider_dir / "codex"
    codex.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$(command -v agent-orch)\" \"$1\" > {shlex.quote(str(capture))}\n",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    machine = launch.local_machine(
        "codex-args",
        cwd=str(tmp_path),
        claude_bin=str(tmp_path / "claude"),
        codex_bin=f"{codex} --profile qa",
        projects_root=str(tmp_path / "projects"),
    )

    plan = launch.build_launch(
        machine,
        provider="codex",
        tmux_session="codex-args",
        launch_model=None,
        launch_effort=None,
    )
    completed = subprocess.run(
        ["/bin/sh", "-c", plan.command],
        cwd=str(tmp_path),
        env={"PATH": "/usr/bin:/bin", "PENTACLE_CODEX_ENABLE_APPS": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert capture.read_text(encoding="utf-8").splitlines() == [str(agent_orch), "--profile"]


def test_relative_local_provider_is_absolutized_before_child_launch(
    tmp_path: Path, monkeypatch,
) -> None:
    provider_dir = tmp_path / "tools"
    provider_dir.mkdir()
    agent_orch = provider_dir / "agent-orch"
    agent_orch.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    agent_orch.chmod(0o755)
    capture = tmp_path / "relative-capture.txt"
    codex = provider_dir / "codex"
    codex.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$(command -v agent-orch)\" > {shlex.quote(str(capture))}\n",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    machine = launch.local_machine(
        "relative-local",
        cwd=str(tmp_path),
        claude_bin=str(tmp_path / "claude"),
        codex_bin="tools/codex",
        projects_root=str(tmp_path / "projects"),
    )

    plan = launch.build_launch(
        machine,
        provider="codex",
        tmux_session="relative-codex",
        launch_model=None,
        launch_effort=None,
    )
    completed = subprocess.run(
        ["/bin/sh", "-c", plan.command],
        cwd=str(tmp_path),
        env={"PATH": "/usr/bin:/bin", "PENTACLE_CODEX_ENABLE_APPS": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert capture.read_text(encoding="utf-8").strip() == str(agent_orch)


def test_generated_provider_shell_reaches_agent_orch_with_minimal_path(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent-bin"
    agent_dir.mkdir()
    agent_orch = agent_dir / "agent-orch"
    agent_orch.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    agent_orch.chmod(0o755)

    capture = tmp_path / "capture.txt"
    provider_dir = tmp_path / "provider-bin"
    provider_dir.mkdir()
    for provider in ("claude", "codex"):
        provider_bin = provider_dir / provider
        provider_bin.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$(command -v agent-orch)\" \"$PENTACLE_STREAM_ID\" > {capture!s}\n",
            encoding="utf-8",
        )
        provider_bin.chmod(0o755)
        machine = launch.LocalMachine(
            name="target",
            cwd=str(tmp_path),
            codex_cwd=str(tmp_path),
            claude_bin=str(provider_dir / "claude"),
            codex_bin=str(provider_dir / "codex"),
            projects_root=str(tmp_path / "projects"),
            agent_orch_bin_dir=str(agent_dir),
        )
        plan = launch.build_launch(
            machine,
            provider=provider,
            tmux_session=f"{provider}-shell",
            launch_model=None,
            launch_effort=None,
        )
        env = {"PATH": "/usr/bin:/bin", "PENTACLE_CODEX_ENABLE_APPS": "1"}
        completed = subprocess.run(
            ["/bin/sh", "-c", plan.command],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr
        lines = capture.read_text(encoding="utf-8").splitlines()
        assert lines[0] == str(agent_orch)
        assert lines[1] == f"target:{provider}-shell"


def test_both_provider_launches_export_the_configured_target_dir() -> None:
    profile = _machine_from_dict(
        {
            "name": "hosta",
            "ssh_target": None,
            "claude_bin": "/Users/example/.local/bin/claude",
            "codex_bin": "/opt/homebrew/bin/codex",
            "tmux_bin": "/opt/homebrew/bin/tmux",
            "agent_orch_bin_dir": "/Users/example/.local/bin",
            "projects_root": "/Users/example/.claude/projects",
            "cwd": "/Users/example/agent-workspace",
        }
    )
    machine = launch.machine_from_config(profile)

    for provider in ("claude", "codex"):
        command = launch.build_launch(
            machine,
            provider=provider,
            tmux_session=f"{provider}-path",
            launch_model=None,
            launch_effort=None,
        ).command
        assert (
            "export PATH=/Users/example/.local/bin:$PATH && "
        ) in command


def test_daemon_local_spawn_uses_local_host_profile(monkeypatch) -> None:
    monkeypatch.setenv(
        "PENTACLE_MACHINES_JSON",
        '{"machines":[{"name":"hosta","ssh_target":null,'
        '"claude_bin":"/Users/example/.local/bin/claude",'
        '"codex_bin":"/opt/homebrew/bin/codex",'
        '"tmux_bin":"/opt/homebrew/bin/tmux",'
        '"agent_orch_bin_dir":"/Users/example/.local/bin",'
        '"projects_root":"/Users/example/.claude/projects",'
        '"cwd":"/Users/example/agent-workspace"}]}',
    )
    args = parse_args(["--local-host", "hosta"])

    machine = _local_machine_for_spawn(args, load_machines())

    assert machine.agent_orch_bin_dir == "/Users/example/.local/bin"
