from __future__ import annotations

import re

import pytest

from agent_orch import cli


def test_repo_cli_is_not_exposed_during_control_plane_rollback(capsys) -> None:
    parser = cli.build_parser()
    parser.print_help()
    assert re.search(r"\brepo\b", capsys.readouterr().out) is None
    with pytest.raises(SystemExit):
        parser.parse_args(["repo", "metrics"])
