from __future__ import annotations

import json
from argparse import Namespace

from agent_orch import cli
from agent_orch.config import Config


def _args(**overrides):
    data = {
        "peer_stream_id": "hostb:codex-b",
        "text": "heads up",
        "from_stream_id": None,
        "ttl": 300,
        "tell_id": None,
        "urgent": False,
        "timeout": 1.0,
    }
    data.update(overrides)
    return Namespace(**data)


def test_tell_parser_accepts_positional_peer_text_and_from_flag():
    args = cli.build_parser().parse_args(["tell", "hostb:codex-b", "hello", "--from", "hostb:codex-a"])

    assert args.peer_stream_id == "hostb:codex-b"
    assert args.text == "hello"
    assert args.from_stream_id == "hostb:codex-a"
    assert args.ttl == 300


def test_tell_parser_accepts_urgent_flag():
    args = cli.build_parser().parse_args(["tell", "hostb:codex-b", "hello", "--urgent"])

    assert args.urgent is True


def test_tell_parser_rejects_ttl_out_of_bounds():
    parser = cli.build_parser()

    try:
        parser.parse_args(["tell", "hostb:codex-b", "hello", "--ttl", "3601"])
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover
        raise AssertionError("argparse accepted an out-of-bounds ttl")


def test_tell_uses_from_self_discovery_and_generates_tell_id(monkeypatch, capsys, tmp_path):
    calls = []

    async def fake_tell_once(config, request, timeout):
        calls.append({"config": config, "request": request, "timeout": timeout})
        return {
            "type": "tell.ok",
            "request_id": request.get("request_id", "tell-test"),
            "tell_id": request["tell_id"],
            "ledger_row_id": 3,
            "delivery_status": "queued",
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-a")
    monkeypatch.setattr(cli, "tell_once", fake_tell_once)

    assert cli.tell(_args()) == 0

    request = calls[0]["request"]
    assert request["from_stream_id"] == "hostb:codex-a"
    assert request["to_stream_id"] == "hostb:codex-b"
    assert request["ttl_seconds"] == 300
    assert isinstance(request["tell_id"], str) and request["tell_id"]
    assert json.loads(capsys.readouterr().out)["type"] == "tell.ok"


def test_tell_includes_urgent_request_flag(monkeypatch, capsys, tmp_path):
    calls = []

    async def fake_tell_once(_config, request, timeout):
        calls.append(dict(request))
        return {
            "type": "tell.ok",
            "request_id": "tell-urgent",
            "tell_id": request["tell_id"],
            "ledger_row_id": 4,
            "delivery_status": "queued",
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "tell_once", fake_tell_once)

    assert cli.tell(_args(from_stream_id="hostb:codex-a", tell_id="tell-urgent", urgent=True)) == 0

    assert calls[0]["urgent"] is True
    assert json.loads(capsys.readouterr().out)["type"] == "tell.ok"


def test_park_and_unpark_commands_call_direct_rpc(monkeypatch, capsys, tmp_path):
    requests = []

    async def fake_park_once(_config, request, timeout):
        requests.append(dict(request))
        return {
            "type": f"{request['type']}.ok",
            "request_id": request.get("request_id", "park-test"),
            "stream_id": request["stream_id"],
            "turn_state": "parked" if request["type"] == "park" else "idle",
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:leader")
    monkeypatch.setattr(cli, "park_once", fake_park_once)

    parser = cli.build_parser()
    assert cli.park(parser.parse_args(["park", "hostb:child", "--reason", "paused"])) == 0
    assert cli.unpark(parser.parse_args(["unpark", "hostb:child"])) == 0

    assert requests[0]["type"] == "park"
    assert requests[0]["from_stream_id"] == "hostb:leader"
    assert requests[0]["reason"] == "paused"
    assert requests[1]["type"] == "unpark"
    assert "operator_confirm" not in requests[1]
    assert capsys.readouterr().out.count('"stream_id":"hostb:child"') == 2


def test_tell_reuses_supplied_tell_id_for_retry(monkeypatch, capsys, tmp_path):
    requests = []

    async def fake_tell_once(_config, request, timeout):
        requests.append(dict(request))
        return {
            "type": "tell.ok",
            "request_id": request.get("request_id", "tell-test"),
            "tell_id": request["tell_id"],
            "ledger_row_id": 9,
            "delivery_status": "queued",
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "tell_once", fake_tell_once)

    args = _args(from_stream_id="hostb:codex-a", tell_id="tell-fixed")
    assert cli.tell(args) == 0
    assert cli.tell(args) == 0

    assert [request["tell_id"] for request in requests] == ["tell-fixed", "tell-fixed"]
    assert capsys.readouterr().out.count('"tell_id":"tell-fixed"') == 2


def test_tell_uses_direct_path_even_when_wrapper_would_be_available(monkeypatch, capsys, tmp_path):
    requests = []

    async def fake_direct_tell(_config, request, timeout):
        requests.append({"request": request, "timeout": timeout})
        return {
            "type": "tell.ok",
            "request_id": "tell-direct",
            "tell_id": request["tell_id"],
            "ledger_row_id": 10,
            "delivery_status": "queued",
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "tell_once", fake_direct_tell)

    assert cli.tell(_args(from_stream_id="hostb:codex-a", tell_id="tell-wrapper")) == 0

    assert requests == [
        {
            "request": {
                "type": "tell",
                "tell_id": "tell-wrapper",
                "from_stream_id": "hostb:codex-a",
                "to_stream_id": "hostb:codex-b",
                "text": "heads up",
                "ttl_seconds": 300,
            },
            "timeout": 1.0,
        }
    ]
    assert json.loads(capsys.readouterr().out)["type"] == "tell.ok"


def test_tell_wrapper_less_mode_uses_direct_tell_without_wrapper_replay(monkeypatch, capsys, tmp_path):
    requests = []

    async def fake_tell_once(_config, request, timeout):
        requests.append(dict(request))
        return {
            "type": "tell.ok",
            "request_id": request.get("request_id", "tell-direct-request"),
            "tell_id": request["tell_id"],
            "ledger_row_id": 11,
            "delivery_status": "delivered",
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "tell_once", fake_tell_once)

    assert cli.tell(_args(from_stream_id="hostb:codex-a", tell_id="tell-direct")) == 0

    assert requests[0]["type"] == "tell"
    assert "command" not in requests[0]
    assert requests[0]["tell_id"] == "tell-direct"
    assert json.loads(capsys.readouterr().out)["type"] == "tell.ok"


def test_tell_requires_text_positional():
    parser = cli.build_parser()

    try:
        parser.parse_args(["tell", "hostb:codex-b"])
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover
        raise AssertionError("argparse accepted tell without text")

