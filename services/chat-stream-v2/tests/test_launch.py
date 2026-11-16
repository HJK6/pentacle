"""Launch-path contracts that do not require a running daemon."""

from pathlib import Path
import shlex

import launch


def test_claude_transcript_project_dir_resolves_symlinked_cwd(tmp_path: Path) -> None:
    real_cwd = tmp_path / "real-cwd"
    real_cwd.mkdir()
    symlinked_cwd = tmp_path / "linked-cwd"
    symlinked_cwd.symlink_to(real_cwd, target_is_directory=True)

    machine = launch.local_machine(
        "hosta",
        cwd=str(symlinked_cwd),
        claude_bin="/bin/echo",
        projects_root=str(tmp_path / "projects"),
    )
    plan = launch.build_launch(
        machine,
        provider="claude",
        tmux_session="symlinked-cwd",
        launch_model=None,
        launch_effort=None,
    )

    expected_project = tmp_path / "projects" / launch.slugify_cwd(str(real_cwd))
    assert Path(plan.jsonl_path).parent == expected_project


def test_codex_launch_changes_to_configured_cwd(tmp_path: Path) -> None:
    cwd = tmp_path / "workspace with spaces"
    cwd.mkdir()
    machine = launch.local_machine(
        "local",
        cwd=str(cwd),
        codex_bin="/bin/echo",
        projects_root=str(tmp_path / "projects"),
    )

    plan = launch.build_launch(
        machine,
        provider="codex",
        tmux_session="codex-cwd",
        launch_model="gpt-5.6-sol",
        launch_effort="high",
    )

    assert plan.command.startswith(f"cd {shlex.quote(str(cwd))} && ")
    assert "--approve-for-me" in plan.command
    assert "check_for_update_on_startup=false" in plan.command
    assert "sandbox_workspace_write.network_access=true" in plan.command
    assert "--disable plugins" in plan.command
    assert "--dangerously-bypass-approvals-and-sandbox" not in plan.command
    # The portable public default routes operator questions through prompt ask,
    # not an active-chat fleet convention.
    assert "run agent-orch prompt ask" in plan.command
    assert "asks one concise question in the active chat" not in plan.command
    # The sandbox-loopback escalation note travels with the --approve-for-me change.
    assert "retry only that agent-orch command with require_escalated" in plan.command


def test_visible_codex_launch_requires_title_and_status(tmp_path: Path) -> None:
    machine = launch.local_machine(
        "local",
        cwd=str(tmp_path),
        codex_bin="/bin/echo",
        projects_root=str(tmp_path / "projects"),
    )

    visible = launch.build_launch(
        machine,
        provider="codex",
        tmux_session="visible-codex",
        launch_model="gpt-5.6-sol",
        launch_effort="high",
        operator_facing=True,
    )
    worker = launch.build_launch(
        machine,
        provider="codex",
        tmux_session="worker-codex",
        launch_model="gpt-5.6-sol",
        launch_effort="high",
    )

    assert "Pentacle visible-session setup" in visible.command
    assert "agent-orch title" in visible.command
    assert "agent-orch status" in visible.command
    assert "Pentacle visible-session setup" not in worker.command


def test_visible_claude_launch_appends_title_and_status_instruction(tmp_path: Path) -> None:
    machine = launch.local_machine(
        "local",
        cwd=str(tmp_path),
        claude_bin="/bin/echo",
        projects_root=str(tmp_path / "projects"),
    )

    visible = launch.build_launch(
        machine,
        provider="claude",
        tmux_session="visible-claude",
        launch_model="claude-opus-4-8",
        launch_effort="high",
        operator_facing=True,
    )
    worker = launch.build_launch(
        machine,
        provider="claude",
        tmux_session="worker-claude",
        launch_model="claude-opus-4-8",
        launch_effort="high",
    )

    assert "--append-system-prompt" in visible.command
    assert "Pentacle visible-session setup" in visible.command
    assert "--append-system-prompt" not in worker.command


def _codex_machine(tmp_path: Path) -> "launch.LocalMachine":
    # codex_cwd is the machine default; launch_cwd (spawn --cwd) must override it.
    return launch.local_machine(
        "hostc", cwd=str(tmp_path / "machine-default"),
        codex_bin="/bin/echo", projects_root=str(tmp_path / "projects"),
    )


def test_codex_command_honors_launch_cwd_and_trusts_it(tmp_path: Path) -> None:
    machine = _codex_machine(tmp_path)
    worktree = "/tmp/fresh-worktree"
    cmd = launch._codex_command(
        machine, "sess", "/tmp/tok", launch_model=None, launch_effort=None,
        launch_cwd=worktree,
    )
    assert f"cd {shlex.quote(worktree)} &&" in cmd
    assert f'projects."{worktree}".trust_level="trusted"' in cmd
    # The machine default must NOT be the cd target when a launch cwd is given.
    assert f"cd {shlex.quote(machine.codex_cwd)} &&" not in cmd


def test_codex_command_falls_back_to_machine_codex_cwd(tmp_path: Path) -> None:
    machine = _codex_machine(tmp_path)
    cmd = launch._codex_command(
        machine, "sess", "/tmp/tok", launch_model=None, launch_effort=None,
    )
    assert f"cd {shlex.quote(machine.codex_cwd)} &&" in cmd
    assert f'projects."{machine.codex_cwd}".trust_level="trusted"' in cmd


def test_codex_initial_prompt_branch_honors_launch_cwd(tmp_path: Path) -> None:
    machine = _codex_machine(tmp_path)
    worktree = "/tmp/fresh-worktree-ip"
    cmd = launch._codex_command(
        machine, "sess", "/tmp/tok", launch_model=None, launch_effort=None,
        initial_prompt_file="/tmp/prompt", launch_cwd=worktree,
    )
    assert f"cd {shlex.quote(worktree)} &&" in cmd
    assert f'projects."{worktree}".trust_level="trusted"' in cmd


def test_build_launch_threads_spawn_cwd_to_codex(tmp_path: Path) -> None:
    machine = _codex_machine(tmp_path)
    worktree = "/tmp/spawn-cwd-wt"
    plan = launch.build_launch(
        machine, provider="codex", tmux_session="s",
        launch_model=None, launch_effort=None, cwd=worktree,
    )
    assert f"cd {shlex.quote(worktree)} &&" in plan.command
    assert f'projects."{worktree}".trust_level="trusted"' in plan.command
