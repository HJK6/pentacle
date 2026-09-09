from __future__ import annotations

import json
from argparse import Namespace

from agent_orch import cli
from agent_orch.config import Config


def _args(**overrides):
    data = {
        "message": "scrape done, 142 leads",
        "ask": None,
        "title": None,
        "producer": None,
        "severity": "info",
        "ttl": None,
        "dedup_key": None,
        "button": [],
        "actions": None,
        "await_answer": False,
        "resolve_dedup_key": None,
        "timeout": 1.0,
        "from_stream_id": None,
    }
    data.update(overrides)
    return Namespace(**data)


def test_notify_parser_accepts_message_and_actionable_question():
    parser = cli.build_parser()

    message = parser.parse_args(["notify", "--message", "scrape done", "--dedup-key", "scrape:done"])
    question = parser.parse_args(
        ["notify", "--ask", "Re-run scrape?", "--button", "Yes", "--button", "No"]
    )

    assert message.message == "scrape done"
    assert message.dedup_key == "scrape:done"
    assert question.ask == "Re-run scrape?"
    assert question.button == ["Yes", "No"]


def test_notify_message_payload_defaults_producer_to_caller():
    payload = cli._notification_create_payload_from_args(
        _args(title="Scrape finished", ttl=60),
        caller_stream_id="hostb:codex-a",
    )

    assert payload == {
        "type": "notification.create",
        "producer": "hostb:codex-a",
        "severity": "info",
        "actions": [],
        "title": "Scrape finished",
        "body": "scrape done, 142 leads",
        "ttl_seconds": 60,
    }


def test_notify_actionable_question_payload_builds_yes_no_buttons():
    payload = cli._notification_create_payload_from_args(
        _args(
            message=None,
            ask="Re-run scrape?",
            button=["Yes", "No"],
            await_answer=True,
        ),
        caller_stream_id="hostb:claude-a",
    )

    assert payload["answer_to_stream_id"] == "hostb:claude-a"
    assert payload["title"] == "Re-run scrape?"
    assert payload["body"] == "Re-run scrape?"
    assert payload["actions"] == [
        {"kind": "yes_no", "action_id": "a0", "label": "Yes", "choice": True},
        {"kind": "yes_no", "action_id": "a1", "label": "No", "choice": False},
    ]


def test_notify_message_payload_accepts_dedup_key_and_actions_json():
    actions = [{"kind": "ack", "label": "Mute 7d"}]
    payload = cli._notification_create_payload_from_args(
        _args(dedup_key="cadence:validation:path", actions=json.dumps(actions)),
        caller_stream_id="hosta:codex-a",
    )

    assert payload["dedup_key"] == "cadence:validation:path"
    assert payload["actions"] == [{"kind": "ack", "label": "Mute 7d", "action_id": "a0"}]


def test_notify_await_answer_prints_typed_answer(monkeypatch, capsys, tmp_path):
    calls = []

    async def fake_create(config, payload, timeout):
        calls.append(("create", payload, timeout))
        return {
            "type": "notification.create.ok",
            "notification": {"notification_id": "notif-1"},
        }

    async def fake_await(config, notification_id, timeout):
        calls.append(("await", notification_id, timeout))
        return {
            "type": "notification.await.ok",
            "answer": {
                "notification_id": "notif-1",
                "action_id": "a0",
                "action_kind": "yes_no",
                "label": "Yes",
                "choice": True,
                "by": "operator",
                "at": "2026-06-19T00:00:00+00:00",
            },
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-a")
    monkeypatch.setattr(cli, "notification_create_once", fake_create)
    monkeypatch.setattr(cli, "notification_await_once", fake_await)

    assert cli.notify(
        _args(message=None, ask="Re-run scrape?", button=["Yes", "No"], await_answer=True)
    ) == 0

    assert calls[0][0] == "create"
    assert calls[0][1]["answer_to_stream_id"] == "hostb:codex-a"
    assert calls[1] == ("await", "notif-1", 1.0)
    assert json.loads(capsys.readouterr().out)["action_id"] == "a0"


def test_notify_resolve_dedup_key_calls_dedup_rpc(monkeypatch, capsys, tmp_path):
    calls = []

    async def fake_resolve(config, payload, timeout):
        calls.append((payload, timeout))
        return {"type": "notification.resolve_by_dedup.ok", "resolved": True}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hosta", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hosta:codex-a")
    monkeypatch.setattr(cli, "notification_resolve_by_dedup_once", fake_resolve)

    assert cli.notify(_args(message=None, resolve_dedup_key="auto-heal:x", producer="memory-cadence")) == 0
    assert calls == [
        (
            {
                "type": "notification.resolve_by_dedup",
                "producer": "memory-cadence",
                "dedup_key": "auto-heal:x",
                "by": "hosta:codex-a",
            },
            1.0,
        )
    ]
    assert json.loads(capsys.readouterr().out)["resolved"] is True


def test_notify_resolve_dedup_key_targets_agent_questions_by_default(
    monkeypatch, capsys, tmp_path
):
    calls = []

    async def fake_resolve(config, payload, timeout):
        calls.append((payload, timeout))
        return {"type": "notification.resolve_by_dedup.ok", "resolved": True}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hosta", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hosta:codex-a")
    monkeypatch.setattr(cli, "notification_resolve_by_dedup_once", fake_resolve)

    assert cli.notify(
        _args(message=None, resolve_dedup_key="agent-question:hosta:claude-a:q-1")
    ) == 0
    assert calls[0][0]["producer"] == "agent_question.v1"
    assert json.loads(capsys.readouterr().out)["resolved"] is True


def test_notify_payload_is_provider_agnostic():
    claude_payload = cli._notification_create_payload_from_args(
        _args(message=None, ask="Continue?", button=["Yes", "No"], await_answer=True),
        caller_stream_id="hostb:claude-a",
    )
    codex_payload = cli._notification_create_payload_from_args(
        _args(message=None, ask="Continue?", button=["Yes", "No"], await_answer=True),
        caller_stream_id="hostb:codex-a",
    )

    for payload in (claude_payload, codex_payload):
        payload.pop("producer")
        payload.pop("answer_to_stream_id")
    assert claude_payload == codex_payload

