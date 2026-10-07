"""`agent-orch todo` parser, payloads and rendering."""

from __future__ import annotations

import json

import pytest

from agent_orch import cli
from agent_orch.config import Config

ITEM = {
    "item_id": "11111111-1111-4111-8111-111111111111",
    "text": "Buy milk",
    "priority": "high",
    "state": "open",
    "position": 1,
    "created_at": "2026-10-07T00:00:00.000000Z",
    "updated_at": "2026-10-07T00:00:00.000000Z",
}


@pytest.fixture
def rpc(monkeypatch, tmp_path):
    calls: list[dict] = []
    replies: dict[str, dict] = {}

    async def fake_todo_once(_config, payload, timeout):
        calls.append({"payload": dict(payload), "timeout": timeout})
        return replies.get(payload["type"], {"type": f"{payload['type']}.ok", "item": ITEM})

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hosta", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hosta:bart")
    monkeypatch.setattr(cli, "todo_once", fake_todo_once)
    return calls, replies


def run(*argv: str) -> int:
    args = cli.build_parser().parse_args(["todo", *argv])
    return args.func(args)


def test_list_sends_todo_list_and_prints_json(rpc, capsys):
    calls, replies = rpc
    replies["todo.list"] = {"type": "todo.list.ok", "items": [ITEM]}
    assert run("list", "--json") == 0
    payload = calls[0]["payload"]
    assert payload["type"] == "todo.list"
    assert payload["from_stream_id"] == "hosta:bart"
    assert "include_done" not in payload or payload["include_done"] is False
    assert json.loads(capsys.readouterr().out) == replies["todo.list"]


def test_list_include_done_flag(rpc):
    calls, replies = rpc
    replies["todo.list"] = {"type": "todo.list.ok", "items": []}
    assert run("list", "--include-done", "--json") == 0
    assert calls[0]["payload"]["include_done"] is True


def test_list_human_table_shows_priority_state_and_text(rpc, capsys):
    _, replies = rpc
    replies["todo.list"] = {"type": "todo.list.ok", "items": [ITEM]}
    assert run("list") == 0
    out = capsys.readouterr().out
    assert ITEM["item_id"] in out and "high" in out and "open" in out and "Buy milk" in out


def test_add_with_and_without_priority(rpc):
    calls, _ = rpc
    assert run("add", "Buy milk", "--priority", "high", "--json") == 0
    assert calls[0]["payload"]["type"] == "todo.add"
    assert calls[0]["payload"]["text"] == "Buy milk"
    assert calls[0]["payload"]["priority"] == "high"
    assert run("add", "Walk dog", "--json") == 0
    assert "priority" not in calls[1]["payload"]


def test_set_requires_priority_and_sends_priority_only(rpc, capsys):
    calls, _ = rpc
    with pytest.raises(SystemExit):
        run("set", ITEM["item_id"])
    with pytest.raises(SystemExit):
        run("set", ITEM["item_id"], "--priority", "urgent")
    with pytest.raises(SystemExit):
        run("set", ITEM["item_id"], "--priority", "low", "--text", "edit")
    capsys.readouterr()
    assert run("set", ITEM["item_id"], "--priority", "low", "--json") == 0
    payload = calls[0]["payload"]
    assert (payload["type"], payload["item_id"], payload["priority"]) == ("todo.set", ITEM["item_id"], "low")
    assert "text" not in payload


@pytest.mark.parametrize("verb", ["check", "remove"])
def test_check_and_remove_send_item_id(rpc, verb):
    calls, replies = rpc
    replies["todo.remove"] = {"type": "todo.remove.ok", "item_id": ITEM["item_id"]}
    assert run(verb, ITEM["item_id"], "--json") == 0
    assert calls[0]["payload"]["type"] == f"todo.{verb}"
    assert calls[0]["payload"]["item_id"] == ITEM["item_id"]


def test_from_and_timeout_are_forwarded(rpc):
    calls, _ = rpc
    assert run("check", ITEM["item_id"], "--from", "hosta:other", "--timeout", "5", "--json") == 0
    assert calls[0]["payload"]["from_stream_id"] == "hosta:other"
    assert calls[0]["timeout"] == 5.0


def test_mutation_human_output_is_one_line(rpc, capsys):
    assert run("add", "Buy milk") == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and ITEM["item_id"] in out[0]


def test_daemon_error_exits_nonzero_and_prints_the_error(rpc, capsys):
    _, replies = rpc
    replies["todo.add"] = {"type": "todo.add.error", "error_code": "duplicate", "error": "duplicate"}
    assert run("add", "Buy milk", "--json") == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "duplicate"


def test_unauthorized_error_exits_nonzero(rpc, capsys):
    _, replies = rpc
    replies["todo.list"] = {"type": "todo.list.error", "error_code": "unauthorized"}
    assert run("list") == 1
    assert "unauthorized" in capsys.readouterr().out


def test_transport_timeout_maps_to_exit_67(monkeypatch, tmp_path, capsys):
    async def boom(_config, _payload, timeout):
        raise TimeoutError("slow")

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hosta", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hosta:bart")
    monkeypatch.setattr(cli, "todo_once", boom)
    assert run("list", "--json") == 67
    assert json.loads(capsys.readouterr().out)["type"] == "todo.error"
