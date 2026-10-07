"""`agent-orch usage rollup` passes through to the read-only rollup tool."""
import sys

from agent_orch import cli


def test_usage_rollup_tool_path_resolves_in_checkout():
    assert cli.USAGE_ROLLUP_TOOL.is_file()
    assert cli.USAGE_ROLLUP_TOOL.parts[-3:] == ("chat-stream-v2", "tools", "usage_rollup.py")


def test_usage_rollup_passes_flags_verbatim(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv: calls.append(argv) or 0)
    assert cli.main(["usage", "rollup", "--spec", "spec_x", "--json", "--help"]) == 0
    assert calls == [[sys.executable, str(cli.USAGE_ROLLUP_TOOL), "--spec", "spec_x", "--json", "--help"]]


def test_usage_rollup_exit_code_and_override(monkeypatch, tmp_path, capsys):
    tool = tmp_path / "tool.py"
    tool.write_text("import sys; sys.exit(3)\n")
    monkeypatch.setenv("PENTACLE_USAGE_ROLLUP_TOOL", str(tool))
    assert cli.main(["usage", "rollup", "--calibrate"]) == 3
    monkeypatch.setenv("PENTACLE_USAGE_ROLLUP_TOOL", str(tmp_path / "missing.py"))
    assert cli.main(["usage", "rollup"]) == 2
    assert "tool not found" in capsys.readouterr().err


def test_usage_host_readback_unchanged():
    args = cli.build_parser().parse_args(["usage", "--host", "thoth", "--json"])
    assert args.func is cli.usage and args.host == "thoth"
