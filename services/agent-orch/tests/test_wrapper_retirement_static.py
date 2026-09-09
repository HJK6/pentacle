from __future__ import annotations

import ast
import inspect

from agent_orch import cli


def _function_source_tree(func) -> ast.FunctionDef:
    tree = ast.parse(inspect.getsource(func))
    node = tree.body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


def test_start_stop_are_deprecation_stubs_only() -> None:
    for func in (cli.start, cli.stop):
        node = _function_source_tree(func)
        assert len(node.body) == 2
        assert isinstance(node.body[0], ast.Expr)
        assert isinstance(node.body[0].value, ast.Call)
        assert getattr(node.body[0].value.func, "id", None) == "print"
        assert isinstance(node.body[1], ast.Return)
        assert isinstance(node.body[1].value, ast.Constant)
        assert node.body[1].value.value == 0


def test_cli_has_no_legacy_workspace_option() -> None:
    parser = cli.build_parser()
    for argv in (
        ["spawn", "--objective", "Exercise the spawn contract", "--workspace", "/tmp/ws", "--provider", "codex"],
        ["send", "--workspace", "/tmp/ws", "host:session", "1", "hello"],
        ["await", "--workspace", "/tmp/ws", "host:session", "1"],
        ["close", "--workspace", "/tmp/ws", "host:session"],
    ):
        try:
            parser.parse_args(argv)
        except SystemExit as exc:
            assert exc.code == 2
        else:  # pragma: no cover
            raise AssertionError(f"parser accepted the legacy --workspace option: {argv}")
