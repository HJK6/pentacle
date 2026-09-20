"""Typed CLI coverage for assistant-composite backend replies/operations."""

from __future__ import annotations

import asyncio

from agent_orch import cli


def test_assistant_publish_uses_typed_existing_socket_request(monkeypatch, capsys) -> None:
    parser = cli.build_parser()
    args = parser.parse_args([
        "assistant", "publish", "--dispatch-id", "dispatch-1",
        "--reply-to-message-id", "input-1", "--request-id", "pub-1",
        "--composite-stream-id", "fixture-host-chat:assistant",
        "--publish-kind", "prose", "--message", "done",
    ])
    captured = {}

    async def fake_once(_config, payload, *, timeout):
        captured.update(payload=payload, timeout=timeout)
        return {"type": "assistant.publish.ok", "event_id": 9}

    monkeypatch.setattr(cli, "assistant_once", fake_once)
    monkeypatch.setattr(cli, "load_config", lambda: object())
    assert args.func(args) == 0
    assert captured["payload"] == {
        "type": "assistant.publish", "request_id": "pub-1",
        "composite_stream_id": "fixture-host-chat:assistant", "dispatch_id": "dispatch-1",
        "reply_to_message_id": "input-1", "reply_to_question_id": None,
        "publish_kind": "prose", "message": "done", "attachment_ids": [], "evidence_refs": [],
    }
    assert "assistant.publish.ok" in capsys.readouterr().out


def test_assistant_operation_refuses_non_object_payload(capsys) -> None:
    parser = cli.build_parser()
    args = parser.parse_args([
        "assistant", "operation", "--operation", "lane.admit", "--request-id", "op-1",
        "--composite-stream-id", "fixture-host-chat:assistant", "--dispatch-id", "dispatch-1", "--payload", "[]",
    ])
    assert args.func(args) == 2
    assert "JSON object" in capsys.readouterr().err


def test_authority_request_uses_existing_closed_operation_transport(monkeypatch):
    args = cli.build_parser().parse_args([
        "assistant", "operation", "--operation", "authority.request", "--request-id", "request-once",
        "--composite-stream-id", "fixture-host-chat:assistant", "--dispatch-id", "dispatch-1",
        "--payload", '{"reason":"Needs authority coordination"}',
    ])
    captured = {}
    async def fake_once(_config, payload, *, timeout):
        captured.update(payload)
        return {"type": "assistant.operation.ok"}
    monkeypatch.setattr(cli, "assistant_once", fake_once)
    monkeypatch.setattr(cli, "load_config", lambda: object())
    assert args.func(args) == 0
    assert captured["operation"] == "authority.request"
    assert captured["payload"] == {"reason": "Needs authority coordination"}
    assert captured["dispatch_id"] == "dispatch-1" and captured.get("lane_id") is None
