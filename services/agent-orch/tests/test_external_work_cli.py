"""`agent-orch external-work` wire shapes and the narrow RPC helper."""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_orch import cli, wsclient


def _wire(monkeypatch, reply):
    sent = []

    async def fake_once(_config, payload, *, timeout):
        sent.append(payload)
        return reply

    monkeypatch.setattr(cli, "external_work_once", fake_once)
    monkeypatch.setattr(cli, "load_config", lambda: object())
    return sent


def _run(argv):
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


def test_show_and_record_send_only_the_typed_payload(monkeypatch, tmp_path, capsys):
    sent = _wire(monkeypatch, {"type": "external_work.show.ok", "ok": True, "version": 3})
    assert _run(["external-work", "show"]) == 0
    assert sent[-1] == {"type": "external_work.show"}
    assert json.loads(capsys.readouterr().out)["version"] == 3

    record = {"request_id": "check-1", "state": "unknown"}
    path = tmp_path / "record.json"
    path.write_text(json.dumps(record))
    sent = _wire(monkeypatch, {"type": "external_work.error", "ok": False, "error_code": "invalid_request"})
    assert _run(["external-work", "record", "--file", str(path)]) == 1
    # The client forwards the file untouched: it never repairs or guesses a field.
    assert sent[-1] == {"type": "external_work.record", "record": record, "request_id": "check-1"}
    assert json.loads(capsys.readouterr().out)["error_code"] == "invalid_request"


def test_unreadable_record_file_sends_nothing(monkeypatch, tmp_path, capsys):
    sent = _wire(monkeypatch, {"type": "external_work.record.ok"})
    path = tmp_path / "record.json"
    path.write_text("{broken")
    assert _run(["external-work", "record", "--file", str(path)]) == 2
    assert _run(["external-work", "record", "--file", str(tmp_path / "absent.json")]) == 2
    assert sent == [] and "unreadable record file" in capsys.readouterr().err


def test_rpc_helper_attaches_seat_identity_and_refuses_other_verbs(monkeypatch):
    calls = []

    async def fake_rpc(_config, payload, **kwargs):
        calls.append((payload, kwargs))
        return {"type": "external_work.show.ok"}

    monkeypatch.setattr(wsclient, "_one_shot_rpc", fake_rpc)
    monkeypatch.setattr(wsclient, "_stream_token_from_env", lambda: "synthetic-token")
    monkeypatch.delenv("AGENT_ORCH_INTERNAL_LEADER_STREAM_ID", raising=False)
    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "test:desk")
    asyncio.run(wsclient.external_work_once(object(), {"type": "external_work.show"}))
    payload, kwargs = calls[0]
    assert payload["from_stream_id"] == "test:desk" and payload["stream_token"] == "synthetic-token"
    assert payload["request_id"].startswith("external-work-")
    assert kwargs["prefix"] == "external_work" and kwargs["from_stream_id"] == "test:desk"
    with pytest.raises(ValueError, match="external_work_verb_invalid"):
        asyncio.run(wsclient.external_work_once(object(), {"type": "tell"}))
    assert {"external_work.show", "external_work.record"} <= wsclient.RPC_RETRY_ELIGIBLE_TYPES
