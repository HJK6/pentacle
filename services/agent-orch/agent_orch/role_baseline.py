from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _existing_directory(path: Path) -> Path | None:
    try:
        resolved = path.expanduser()
        if resolved.exists() and resolved.is_dir():
            return resolved
    except OSError:
        return None
    return None


def resolve_memory_repo_path(config: Any) -> Path | None:
    """Resolve only explicit public configuration; never probe fleet paths."""
    configured = os.environ.get("AGENT_ORCH_MEMORY_REPO")
    if configured is None:
        configured = getattr(config, "memory_repo_path", None)
    if not configured:
        return None
    return _existing_directory(Path(configured))


def _strip_front_matter(text: str) -> str:
    lines = text.splitlines(keepends=True)
    first_content_index = None
    for index, line in enumerate(lines):
        if line.strip():
            first_content_index = index
            break
    if first_content_index is None:
        return text

    delimiter_values = {"---", "---\n", "---\r\n"}
    if lines[first_content_index] not in delimiter_values:
        return text

    for index in range(first_content_index + 1, len(lines)):
        if lines[index] in delimiter_values:
            body = "".join(lines[index + 1 :])
            if body.startswith("\r\n"):
                return body[2:]
            if body.startswith("\n"):
                return body[1:]
            return body
    return text


def load_role_baseline(config: Any, role: str) -> dict[str, str] | None:
    try:
        memory_repo_path = resolve_memory_repo_path(config)
        if memory_repo_path is None:
            return None
        baseline_path = memory_repo_path / "agents" / f"{role}_baseline.md"
        if not baseline_path.exists() or not baseline_path.is_file():
            return None
        text = baseline_path.read_text(encoding="utf-8")
        return {
            "role": role,
            "source_path": str(baseline_path.resolve()),
            "content": _strip_front_matter(text),
        }
    except Exception:
        return None
