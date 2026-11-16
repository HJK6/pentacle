#!/usr/bin/env python3
"""Assert that a checkout has no tracked or untracked workspace residue."""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def status(root: Path) -> str:
    """Return porcelain status, including every untracked file, for *root*."""
    return subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        text=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        residue = status(root)
    except (OSError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    if residue:
        print("source_integrity FAIL: dirty_worktree")
        print(residue, end="")
        return 125
    print("source_integrity PASS: clean_worktree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
