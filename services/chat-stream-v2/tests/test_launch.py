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
    assert "--disable plugins" in plan.command
    assert "--dangerously-bypass-approvals-and-sandbox" not in plan.command
    assert "asks one concise question in the active chat" in plan.command
    assert "Do not use request_user_input or agent-orch prompt ask" in plan.command
    assert "run agent-orch prompt ask" not in plan.command
