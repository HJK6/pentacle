from __future__ import annotations

import json
from argparse import Namespace
import errno
from pathlib import Path

from agent_orch import cli


def _args(workspace: Path, *, retry: bool = False, prompt_text: str = "hello") -> Namespace:
    return Namespace(
        workspace=str(workspace),
        stream_id="hosta:codex-worker",
        msg_id=18,
        prompt_text=prompt_text,
        retry=retry,
        inline_inbox=False,
        quiet=False,
        timeout=7.0,
    )


def _install_send_fakes(monkeypatch, captured: dict, *, leader_stream_id: str | None = "hosta:leader") -> None:
    monkeypatch.setattr(cli, "load_config", lambda: object())
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: leader_stream_id)

    async def fake_send_once(config, request, timeout=30.0):
        captured["config"] = config
        captured["request"] = request
        captured["timeout"] = timeout
        return {
            "type": "send.result",
            "request_id": request.get("request_id", "send-test"),
            "delivery": "landed",
            "attempt": 1,
        }

    monkeypatch.setattr(cli, "send_once", fake_send_once)


def test_agent_orch_send_receipt_prints_one_durable_projection(monkeypatch, capsys) -> None:
    captured: dict = {}
    monkeypatch.setattr(cli, "load_config", lambda: object())

    async def fake_send_receipt_once(config, stream_id, request_id, timeout=30.0):
        captured.update({"config": config, "stream_id": stream_id, "request_id": request_id, "timeout": timeout})
        return {
            "type": "send.receipt.get.ok", "found": True,
            "receipts": [{"request_id": request_id, "state": "landed"}],
        }

    monkeypatch.setattr(cli, "send_receipt_once", fake_send_receipt_once)
    result = cli.send_receipt(Namespace(stream_id="hostc:target", request_id="send-cli-1", timeout=9.0))

    assert result == 0
    assert captured["stream_id"] == "hostc:target"
    assert captured["request_id"] == "send-cli-1"
    assert captured["timeout"] == 9.0
    assert json.loads(capsys.readouterr().out)["receipts"][0]["state"] == "landed"


def test_agent_orch_send_builds_inline_inbox_without_workspace_file(monkeypatch, tmp_path, capsys) -> None:
    captured: dict = {}
    _install_send_fakes(monkeypatch, captured)

    result = cli.send(_args(tmp_path / "workspace", prompt_text="review this change"))

    assert result == 0
    assert json.loads(capsys.readouterr().out)["type"] == "send.result"
    request = captured["request"]
    assert request["host"] == "hosta"
    assert request["session_name"] == "codex-worker"
    assert request["from_stream_id"] == "hosta:leader"
    assert request["msg_id"] == 18
    assert request["retry"] is False
    assert request["inbox"] == {
        "schema_version": "v1",
        "msg_id": 18,
        "from": "hosta:leader",
        "to": "hosta:codex-worker",
        "phase": None,
        "role_hint": None,
        "task": "review this change",
        "inputs": {},
        "extras": {},
    }
    assert not (tmp_path / "workspace" / "inbox" / "msg_18.json").exists()


def test_agent_orch_send_retry_uses_same_inline_path(monkeypatch, tmp_path, capsys) -> None:
    captured: dict = {}
    _install_send_fakes(monkeypatch, captured)

    result = cli.send(_args(tmp_path / "workspace", retry=True, prompt_text="retry body"))

    assert result == 0
    assert json.loads(capsys.readouterr().out)["delivery"] == "landed"
    assert captured["request"]["retry"] is True
    assert captured["request"]["inbox"]["task"] == "retry body"
    assert not (tmp_path / "workspace" / "inbox" / "msg_18.json").exists()


def test_agent_orch_send_not_landed_queued_exits_deferred(monkeypatch, tmp_path, capsys) -> None:
    _install_send_fakes(monkeypatch, {})

    async def fake_send_once(_config, request, timeout=30.0):
        return {
            "type": "send.result",
            "request_id": "send-test",
            "msg_id": request["msg_id"],
            "delivery": "not_landed",
            "reason": "pane_not_ready",
            "queued_for_redelivery": True,
        }

    monkeypatch.setattr(cli, "send_once", fake_send_once)

    result = cli.send(_args(tmp_path / "workspace"))

    assert result == 75
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["queued_for_redelivery"] is True
    assert "DEFERRED" in captured.err
    assert "Do NOT resend" in captured.err
    assert "agent-orch await --msg-id 18" in captured.err


def test_agent_orch_send_deduped_queued_result_still_exits_deferred(monkeypatch, tmp_path, capsys) -> None:
    _install_send_fakes(monkeypatch, {})

    async def fake_send_once(_config, request, timeout=30.0):
        return {
            "type": "send.result",
            "request_id": "send-test",
            "msg_id": request["msg_id"],
            "delivery": "not_landed",
            "reason": "queued_for_redelivery",
            "queued_for_redelivery": True,
            "deduped": True,
        }

    monkeypatch.setattr(cli, "send_once", fake_send_once)

    result = cli.send(_args(tmp_path / "workspace"))

    assert result == 75
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["deduped"] is True
    assert payload["queued_for_redelivery"] is True
    assert "DEFERRED" in captured.err


def test_agent_orch_send_not_landed_unqueued_exits_hard_failure(monkeypatch, tmp_path, capsys) -> None:
    _install_send_fakes(monkeypatch, {})

    async def fake_send_once(_config, request, timeout=30.0):
        return {
            "type": "send.result",
            "request_id": "send-test",
            "msg_id": request["msg_id"],
            "delivery": "not_landed",
            "reason": "readback_unconfirmed_post_enter",
        }

    monkeypatch.setattr(cli, "send_once", fake_send_once)

    result = cli.send(_args(tmp_path / "workspace"))

    assert result == 76
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert "NOT DELIVERED (readback_unconfirmed_post_enter) and NOT queued" in captured.err


def test_agent_orch_send_committed_pending_proof_exits_zero_non_fatal(
    monkeypatch, tmp_path, capsys,
) -> None:
    """A durably-committed send whose USER-event proof is late must exit 0 (not
    the not_landed=76 hard failure): the paste left the composer, so the caller
    must not re-evaluate or resend; it reconciles the receipt asynchronously."""
    _install_send_fakes(monkeypatch, {})

    async def fake_send_once(_config, request, timeout=30.0):
        return {
            "type": "send.result",
            "request_id": "send-test",
            "msg_id": request["msg_id"],
            "delivery": "committed_pending_proof",
            "reason": "submit_unconfirmed",
            "action_status": "committed",
            "confirmation_status": "pending",
            "action_committed": True,
            "confirmation_pending": True,
            "do_not_resubmit": True,
            "to_stream_id": "hosta:codex-worker",
            "reconcile_command": "agent-orch send-receipt hosta:codex-worker send-test",
        }

    monkeypatch.setattr(cli, "send_once", fake_send_once)

    result = cli.send(_args(tmp_path / "workspace"))

    assert result == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["delivery"] == "committed_pending_proof"
    assert "COMMITTED" in captured.err
    assert "Do NOT resend" in captured.err


def test_agent_orch_send_committed_pending_proof_without_commitment_is_not_zero(
    monkeypatch, tmp_path, capsys,
) -> None:
    """The exit-0 path is gated on the do_not_resubmit commitment; a bare
    committed_pending_proof status without it is not silently treated as ok."""
    _install_send_fakes(monkeypatch, {})

    async def fake_send_once(_config, request, timeout=30.0):
        return {
            "type": "send.result",
            "request_id": "send-test",
            "msg_id": request["msg_id"],
            "delivery": "committed_pending_proof",
        }

    monkeypatch.setattr(cli, "send_once", fake_send_once)

    result = cli.send(_args(tmp_path / "workspace"))

    assert result != 0
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_agent_orch_send_workspace_argument_is_noop(monkeypatch, tmp_path) -> None:
    captured: dict = {}
    _install_send_fakes(monkeypatch, captured)

    result = cli.send(_args(tmp_path / "does-not-exist", prompt_text="ping"))

    assert result == 0
    assert captured["request"]["inbox"]["from"] == "hosta:leader"


def test_agent_orch_send_requires_caller_stream_id(monkeypatch, tmp_path, capsys) -> None:
    captured: dict = {}
    _install_send_fakes(monkeypatch, captured, leader_stream_id=None)

    result = cli.send(_args(tmp_path / "workspace"))

    assert result == 2
    assert "stream_id_unknown" in capsys.readouterr().err
    assert captured == {}


def test_agent_orch_send_timeout_prints_json_before_exit(monkeypatch, tmp_path, capsys) -> None:
    _install_send_fakes(monkeypatch, {})

    async def fake_send_once(*_args, **_kwargs):
        raise TimeoutError("send_timeout")

    monkeypatch.setattr(cli, "send_once", fake_send_once)

    result = cli.send(_args(tmp_path / "workspace"))

    assert result == 67
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["type"] == "send.error"
    assert payload["ok"] is False
    assert payload["error_code"] == "timeout"
    assert "timeout waiting" in captured.err


def test_agent_orch_send_transport_errors_print_json(monkeypatch, tmp_path, capsys) -> None:
    _install_send_fakes(monkeypatch, {})

    async def fake_send_once(*_args, **_kwargs):
        raise OSError(errno.ECONNREFUSED, "refused")

    monkeypatch.setattr(cli, "send_once", fake_send_once)

    result = cli.send(_args(tmp_path / "workspace"))

    assert result == 64
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["type"] == "send.error"
    assert payload["ok"] is False
    assert payload["error_code"] == "chat_streamd_unreachable"
    assert "chat_streamd unreachable" in captured.err


def test_agent_orch_send_auth_and_indeterminate_errors_print_json(monkeypatch, tmp_path, capsys) -> None:
    _install_send_fakes(monkeypatch, {})

    async def fake_permission(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(cli, "send_once", fake_permission)
    assert cli.send(_args(tmp_path / "workspace")) == 66
    first = capsys.readouterr()
    assert json.loads(first.out)["error_code"] == "auth_failed"

    async def fake_dropped(*_args, **_kwargs):
        raise RuntimeError("dropped")

    monkeypatch.setattr(cli, "send_once", fake_dropped)
    assert cli.send(_args(tmp_path / "workspace")) == 65
    second = capsys.readouterr()
    assert json.loads(second.out)["error_code"] == "connection_dropped"



ALIAS = cli.ASSISTANT_ALIAS

def _capture_send_transport(monkeypatch, tmp_path, responses):
    """Mock only the one-shot RPC transport; the real parser, handler,
    inline-INBOX builder and `send_once` request-id minting run unchanged."""
    from agent_orch import wsclient
    from agent_orch.config import Config

    sent: list[dict] = []
    queue = list(responses)

    async def fake_one_shot(_config, payload, *, prefix, timeout=30.0, from_stream_id=None, **_kwargs):
        sent.append({"prefix": prefix, "payload": json.loads(json.dumps(payload)), "from": from_stream_id})
        return {**queue.pop(0), "request_id": payload.get("request_id")}

    monkeypatch.setattr(wsclient, "_one_shot_rpc", fake_one_shot)
    monkeypatch.setattr(wsclient, "_stream_token_from_env", lambda: "synthetic-seat-token")
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "example-host-s", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "example-sender:seat-s")
    return sent


_LANDED_AT_A = {
    "type": "send.result", "delivery": "landed", "host": "example-host-a", "session_name": "seat-a",
    "to_stream_id": "example-host-a:seat-a", "receipt_id": "receipt-example-a",
    "reconcile_command": "agent-orch send-receipt example-host-a:seat-a send-x",
}


def test_send_literal_alias_emits_raw_target_and_inbox_to(monkeypatch, tmp_path, capsys) -> None:
    sent = _capture_send_transport(monkeypatch, tmp_path, [_LANDED_AT_A, _LANDED_AT_A])
    parser = cli.build_parser()

    first = parser.parse_args(["send", ALIAS, "101", "synthetic commissioned task"])
    assert first.func(first) == 0
    retry = parser.parse_args(["send", ALIAS, "101", "synthetic commissioned task", "--retry"])
    assert retry.func(retry) == 0

    assert [item["prefix"] for item in sent] == ["send", "send"]
    for item, retry_flag in zip(sent, (False, True)):
        payload = item["payload"]
        assert payload["type"] == "send"
        assert payload["to_stream_id"] == ALIAS
        assert payload["inbox"]["to"] == ALIAS
        assert payload["inbox"]["from"] == "example-sender:seat-s"
        assert payload["inbox"]["msg_id"] == 101
        assert payload["from_stream_id"] == "example-sender:seat-s"
        assert payload["msg_id"] == 101
        assert payload["text"] == "synthetic commissioned task"
        assert payload["retry"] is retry_flag
        assert payload["stream_token"] == "synthetic-seat-token"
        # No colon split: no host/session/stream_id alternative, and no
        # synthetic optimistic identity is added for the alias.
        assert not {"host", "session_name", "stream_id", "optimistic_id"} & set(payload)
        assert payload["request_id"].startswith("send-")
    # Separate CLI invocations (including --retry) rotate the request id.
    assert sent[0]["payload"]["request_id"] != sent[1]["payload"]["request_id"]
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert out[0]["to_stream_id"] == "example-host-a:seat-a"


def test_send_alias_displays_ordinary_committed_pending_guidance(monkeypatch, tmp_path, capsys) -> None:
    pending = {
        "type": "send.result", "delivery": "committed_pending_proof", "do_not_resubmit": True,
        "to_stream_id": "example-host-a:seat-a",
        "reconcile_command": "agent-orch send-receipt example-host-a:seat-a send-x",
    }
    _capture_send_transport(monkeypatch, tmp_path, [pending])
    args = cli.build_parser().parse_args(["send", ALIAS, "101", "synthetic commissioned task"])

    assert args.func(args) == 0
    err = capsys.readouterr().err
    assert "Do NOT resend" in err
    assert "agent-orch send-receipt example-host-a:seat-a send-x" in err


def test_send_alias_is_exact_literal_and_other_targets_unchanged(monkeypatch, tmp_path, capsys) -> None:
    sent = _capture_send_transport(monkeypatch, tmp_path, [_LANDED_AT_A])
    parser = cli.build_parser()
    for target in (ALIAS.capitalize(), "daff", ALIAS + ":"):
        args = parser.parse_args(["send", target, "101", "synthetic commissioned task"])
        assert args.func(args) == 2, target
    assert sent == []
    explicit = parser.parse_args(["send", "example-host-a:seat-a", "101", "synthetic commissioned task"])
    assert explicit.func(explicit) == 0
    payload = sent[0]["payload"]
    assert payload["host"] == "example-host-a" and payload["session_name"] == "seat-a"
    assert payload["inbox"]["to"] == "example-host-a:seat-a"
    assert "to_stream_id" not in payload
