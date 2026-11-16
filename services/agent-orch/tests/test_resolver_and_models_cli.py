from __future__ import annotations

import json

import pytest

from agent_orch import cli
from agent_orch.config import Config


def test_v2_rejects_retired_commands_and_noncanonical_stream_ids_locally(capsys):
    parser = cli.build_parser()
    for argv in (
        ["resolve", "agent-uuid"],
        ["recover", "--stream", "hosta:worker", "--msg-id", "7"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)

    args = parser.parse_args(["inspect", "agent-uuid", "--json"])
    assert cli.inspect(args) == 2
    args = parser.parse_args(["await", "--from", "agent-uuid"])
    assert cli.await_command(args) == 2
    assert "canonical <host>:<session> stream id" in capsys.readouterr().err


def test_models_cli_surfaces_daemon_catalog(monkeypatch, capsys, tmp_path):
    async def fake_catalog(_config, *, timeout):
        assert timeout == 2.0
        return {
            "type": "spawn_catalog_get.ok",
            "catalog_version": "spawn-catalog-v2",
            "models": {"codex": {"gpt-5.6-sol": {"efforts": ["high"]}}},
            "profiles": {"agent_orch": {"codex": ["gpt-5.6-sol", "high"]}},
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hosta", tmp_path))
    monkeypatch.setattr(cli, "spawn_catalog_get_once", fake_catalog)

    args = cli.build_parser().parse_args(["models", "--timeout", "2"])
    assert cli.models(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["catalog_version"] == "spawn-catalog-v2"
    assert "profiles" in payload


def test_handoff_explicit_tuple_reaches_spawn_wire(monkeypatch, capsys, tmp_path):
    captured = {}

    async def fake_spawn(_config, payload, timeout):
        captured.update(payload)
        return {"type": "spawn.ok", "session": {"stream_id": "hosta:codex-successor"}}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hosta", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hosta:codex-retiring")
    monkeypatch.setattr(
        cli,
        "_handoff_source_row",
        lambda *_args, **_kwargs: {
            "provider": "codex",
            "effective_model": "gpt-5.6-sol",
            "effective_effort": "high",
        },
    )
    monkeypatch.setattr(cli, "spawn_once", fake_spawn)

    args = cli.build_parser().parse_args(
        ["spawn", "--objective", "Exercise the existing spawn contract", "--handoff", "--model", "gpt-5.6-sol", "--effort", "high"]
    )
    assert cli.spawn(args) == 0
    assert json.loads(capsys.readouterr().out)["type"] == "spawn.ok"
    assert captured["handoff"] is True
    assert captured["model"] == "gpt-5.6-sol"
    assert captured["effort"] == "high"
