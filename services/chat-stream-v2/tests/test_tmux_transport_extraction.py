"""Transport extraction boundaries remain one-way."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import tmux_transport


SERVICE_DIR = Path(__file__).resolve().parents[1]
MOVED_EXPORTS = frozenset(
    {
        "POLL_INTERVAL_S",
        "RECEIPT_TIMEOUT_S",
        "TRANSCRIPT_DIRS",
        "Tmux",
        "_ACTIVE_LAUNCH_TMUX",
        "_exec",
        "_intent",
        "_prompt_stage_path",
        "_search_transcripts",
        "_stage_timeout",
        "_target",
        "assert_injectable",
        "collapse_ws",
        "has_collapsed_paste",
        "new_since",
        "open_fields",
        "receipt_needle",
        "sanitize_injectable",
    }
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def test_transport_is_not_a_spawnctl_export() -> None:
    """Every consumer names the lower transport module, never spawnctl."""
    spawnctl_tree = _tree(SERVICE_DIR / "spawnctl.py")
    spawnctl_bindings = {
        node.name
        for node in spawnctl_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "Tmux" not in spawnctl_bindings
    assert tmux_transport.Tmux.__module__ == "tmux_transport"

    for path in (*SERVICE_DIR.glob("*.py"), *(SERVICE_DIR / "tests").rglob("*.py")):
        if path.name == "spawnctl.py":
            continue
        tree = _tree(path)
        spawnctl_aliases = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name == "spawnctl"
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "spawnctl":
                assert MOVED_EXPORTS.isdisjoint(alias.name for alias in node.names), path
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in spawnctl_aliases
            ):
                assert node.attr not in MOVED_EXPORTS, path


class _NoCopyModeTmux(tmux_transport.Tmux):
    async def run(self, *args: str, **kwargs: object) -> tuple[int, str]:
        return 0, "0"


def test_cancel_copy_mode_returns_false_when_pane_is_not_in_copy_mode() -> None:
    assert asyncio.run(_NoCopyModeTmux()._cancel_copy_mode("pane")) is False
