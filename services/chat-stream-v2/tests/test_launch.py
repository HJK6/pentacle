"""Launch-path contracts that do not require a running daemon."""

from pathlib import Path

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
