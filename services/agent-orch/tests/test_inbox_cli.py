from __future__ import annotations

import pytest
from agent_orch import cli
def test_inbox_is_not_a_command() -> None:
    with pytest.raises(SystemExit) as exited:
        cli.build_parser().parse_args(["inbox"])

    assert exited.value.code == 2
