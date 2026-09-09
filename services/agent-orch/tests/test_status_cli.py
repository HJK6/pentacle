"""CLI tests for the public status-card command."""
from __future__ import annotations

import json
from argparse import Namespace

from agent_orch import cli
from agent_orch.config import Config


def _args(**overrides):
    data = {
        "goal": None,
        "plan": None,
        "step_done": None,
        "update": None,
        "handoff_planned": None,
        "timeout": 1.0,
    }
    data.update(overrides)
    return Namespace(**data)


def _patch_env(monkeypatch, tmp_path, stream_id="hosta:claude-hosta-1"):
    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", stream_id)
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hosta", tmp_path))


def test_status_parser_accepts_each_flag():
    args = cli.build_parser().parse_args(
        [
            "status",
            "--goal", "Ship the card",
            "--plan", "daemon",
            "--plan", "cli",
            "--step-done", "1",
            "--update", "milestone",
            "--handoff-planned",
        ]
    )
    assert args.goal == "Ship the card"
    assert args.plan == ["daemon", "cli"]
    assert args.step_done == [[1]]
    assert args.update == "milestone"
    assert args.handoff_planned is True
    assert cli.build_parser().parse_args(["status", "--no-handoff-planned"]).handoff_planned is False


def test_status_sends_only_provided_fields(monkeypatch, capsys, tmp_path):
    calls = []

    async def fake_status_card_once(config, stream_id, fields, *, timeout):
        calls.append({"stream_id": stream_id, "fields": fields, "timeout": timeout})
        return {"type": "status_card.ok", "session": {"stream_id": stream_id}}

    _patch_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "status_card_once", fake_status_card_once)

    assert cli.status(_args(update="daemon merged")) == 0

    assert calls == [
        {"stream_id": "hosta:claude-hosta-1", "fields": {"update": "daemon merged"}, "timeout": 1.0}
    ]
    assert json.loads(capsys.readouterr().out)["type"] == "status_card.ok"


def test_status_full_payload_and_handoff_false(monkeypatch, tmp_path):
    calls = []

    async def fake_status_card_once(_config, stream_id, fields, *, timeout):
        calls.append(fields)
        return {"type": "status_card.ok"}

    _patch_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "status_card_once", fake_status_card_once)

    args = _args(
        goal="Ship the card",
        plan=["daemon", "cli"],
        step_done=1,
        update="starting",
        handoff_planned=False,
    )
    assert cli.status(args) == 0
    assert calls == [
        {
            "goal": "Ship the card",
            "plan": ["daemon", "cli"],
            "step_done": 1,
            "update": "starting",
            "handoff_planned": False,
        }
    ]


def test_status_applies_repeated_and_comma_list_steps(monkeypatch, capsys, tmp_path):
    calls = []

    async def fake_status_card_once(_config, _stream_id, fields, *, timeout):
        calls.append((fields, timeout))
        return {"type": "status_card.ok"}

    _patch_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "status_card_once", fake_status_card_once)
    args = cli.build_parser().parse_args(
        ["status", "--step-done", "1", "--step-done", "2,3", "--timeout", "2"]
    )

    assert cli.status(args) == 0
    assert calls == [({"step_done": 1}, 2.0), ({"step_done": 2}, 2.0), ({"step_done": 3}, 2.0)]
    assert json.loads(capsys.readouterr().out)["type"] == "status_card.ok"


def test_status_no_fields_exits_2_without_network(monkeypatch, capsys, tmp_path):
    async def fake_status_card_once(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("must not reach the network")

    _patch_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "status_card_once", fake_status_card_once)

    assert cli.status(_args()) == 2
    assert "no_fields" in capsys.readouterr().err


def test_status_requires_resolved_stream_id(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hosta", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: None)

    assert cli.status(_args(goal="g")) == 2
    assert "stream_id_unknown" in capsys.readouterr().err


def test_status_daemon_error_exits_1(monkeypatch, capsys, tmp_path):
    async def fake_status_card_once(*_args, **_kwargs):
        return {"type": "status_card.error", "error_code": "step_out_of_range", "error": "step_out_of_range"}

    _patch_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "status_card_once", fake_status_card_once)

    assert cli.status(_args(step_done=9)) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "step_out_of_range"


def test_status_transport_error_exit_codes(monkeypatch, capsys, tmp_path):
    _patch_env(monkeypatch, tmp_path)

    for exc, expected in ((TimeoutError("t"), 67), (PermissionError("p"), 66), (OSError("o"), 64), (RuntimeError("r"), 65)):
        async def fake_status_card_once(*_args, _exc=exc, **_kwargs):
            raise _exc

        monkeypatch.setattr(cli, "status_card_once", fake_status_card_once)
        assert cli.status(_args(goal="g")) == expected
        capsys.readouterr()

