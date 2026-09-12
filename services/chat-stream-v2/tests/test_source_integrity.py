from __future__ import annotations

import subprocess
import sys
from pathlib import Path


CHECK = Path(__file__).resolve().parents[1] / "tools" / "source_integrity.py"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "source-integrity-test")
    (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-qm", "initial")
    return root


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK), "--root", str(root)],
        capture_output=True,
        text=True,
    )


def test_source_integrity_accepts_clean_checkout(tmp_path: Path) -> None:
    result = _run(_repo(tmp_path))

    assert result.returncode == 0
    assert result.stdout == "source_integrity PASS: clean_worktree\n"


def test_source_integrity_rejects_tracked_and_nested_untracked_residue(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "tracked.txt").write_text("changed\n", encoding="utf-8")
    nested = root / "residue" / "nested"
    nested.mkdir(parents=True)
    (nested / "chrome.deb").write_text("residue\n", encoding="utf-8")

    result = _run(root)

    assert result.returncode == 125
    assert "source_integrity FAIL: dirty_worktree\n" in result.stdout
    assert " M tracked.txt\n" in result.stdout
    assert "?? residue/nested/chrome.deb\n" in result.stdout
