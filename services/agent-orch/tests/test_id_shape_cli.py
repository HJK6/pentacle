from __future__ import annotations

from argparse import Namespace

import pytest

from agent_orch import cli


@pytest.mark.parametrize(
    ("command", "invoke"),
    [
        ("await", lambda: cli.await_command(Namespace(stream_id="report-123"))),
        ("inspect", lambda: cli.inspect(Namespace(stream_id="report-123"))),
        ("close", lambda: cli.close(Namespace(stream_id="report-123"))),
        ("send", lambda: cli.send(Namespace(stream_id="report-123"))),
        ("tell", lambda: cli.tell(Namespace(peer_stream_id="report-123"))),
    ],
)
def test_stream_verbs_reject_wrong_id_shape_before_network(command, invoke, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: pytest.fail("network configuration should not be loaded"))

    assert invoke() == 2
    error = capsys.readouterr().err
    assert f"agent-orch {command}: validation failed" in error
    assert "report/request id 'report-123'" in error
    assert "<host>:<session>" in error


def test_opaque_report_or_tell_uuid_gets_actionable_stream_hint(capsys):
    value = "8367fbe6-6e95-43f9-a411-8f446ce798da"

    assert cli._require_stream_id("await", value) is False
    error = capsys.readouterr().err
    assert "opaque UUID (commonly a report or tell id)" in error
    assert "canonical <host>:<session> stream id" in error


@pytest.mark.parametrize(
    ("value", "shape"),
    [
        ("spec_pentacle__cli_fix", "spec id"),
        ("tell-123", "tell id"),
        ("spawn-123", "request id"),
        ("host:", "malformed stream id"),
    ],
)
def test_each_known_wrong_id_shape_is_named(value, shape, capsys):
    assert cli._require_stream_id("inspect", value) is False
    assert shape in capsys.readouterr().err
