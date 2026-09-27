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


def test_assistant_rebind_reads_revision_then_sends_one_typed_mutation(monkeypatch, capsys) -> None:
    args = cli.build_parser().parse_args([
        "assistant", "rebind", "--target", "fixture-host:new",
        "--request-id", "move-1",
    ])
    calls = []

    async def fake_once(_config, payload, *, timeout):
        calls.append(payload)
        if payload["type"] == "assistant.binding":
            return {"type": "assistant.binding.ok", "revision": 7}
        return {"type": "assistant.rebind.ok", "new_binding": {
            "stream_id": "fixture-host:new", "revision": 8,
        }}

    monkeypatch.setattr(cli, "assistant_once", fake_once)
    monkeypatch.setattr(cli, "load_config", lambda: object())
    assert args.func(args) == 0
    assert [item["type"] for item in calls] == ["assistant.binding", "assistant.rebind"]
    assert calls[1] == {
        "type": "assistant.rebind", "request_id": "move-1",
        "expected_revision": 7, "clear": False,
        "target_stream_id": "fixture-host:new",
    }
    assert "assistant.rebind.ok" in capsys.readouterr().out


def test_assistant_rebind_retry_reads_new_revision_and_replays_receipt(monkeypatch, capsys) -> None:
    args = cli.build_parser().parse_args([
        "assistant", "rebind", "--target", "fixture-host:new", "--request-id", "move-once",
    ])
    revision = 0
    receipt = {"type": "assistant.rebind.ok", "request_id": "move-once",
               "new_binding": {"stream_id": "fixture-host:new", "revision": 1}}
    requests = []

    async def fake_once(_config, payload, *, timeout):
        nonlocal revision
        if payload["type"] == "assistant.binding":
            return {"type": "assistant.binding.ok", "revision": revision}
        requests.append(payload)
        if len(requests) == 1:
            revision = 1
            raise TimeoutError("reply lost after commit")
        return {**receipt, "duplicate": True}

    monkeypatch.setattr(cli, "assistant_once", fake_once)
    monkeypatch.setattr(cli, "load_config", lambda: object())
    assert args.func(args) != 0
    assert args.func(args) == 0
    assert [item["expected_revision"] for item in requests] == [0, 1]
    assert requests[0]["request_id"] == requests[1]["request_id"] == "move-once"
    assert '"duplicate":true' in capsys.readouterr().out


def test_assistant_publish_serializes_explicit_final_response_marker(monkeypatch, capsys) -> None:
    parser = cli.build_parser()
    args = parser.parse_args([
        "assistant", "publish", "--dispatch-id", "dispatch-1",
        "--reply-to-message-id", "input-1", "--request-id", "pub-final",
        "--composite-stream-id", "fixture-host-chat:assistant",
        "--publish-kind", "result", "--response-state", "final", "--message", "done",
    ])
    captured = {}

    async def fake_once(_config, payload, *, timeout):
        captured.update(payload=payload, timeout=timeout)
        return {"type": "assistant.publish.ok", "event_id": 10}

    monkeypatch.setattr(cli, "assistant_once", fake_once)
    monkeypatch.setattr(cli, "load_config", lambda: object())
    assert args.func(args) == 0
    assert captured["payload"]["publish_kind"] == "result"
    assert captured["payload"]["response_state"] == "final"
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
