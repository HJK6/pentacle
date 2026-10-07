from __future__ import annotations

import asyncio
import json

import pytest

from agent_orch import cli, wsclient


def _fake_answer(seen, response_type="voice_answer.answer.ok"):
    async def _answer(_config, payload, *, timeout):
        seen.append({"payload": dict(payload), "timeout": timeout})
        return {
            "type": response_type,
            "ok": response_type.endswith(".ok"),
            "recording_id": payload["recording_id"],
            "question_id": payload["question_id"],
            "outcome": "answered" if response_type.endswith(".ok") else "stale",
        }

    return _answer


def test_voice_answer_answer_posts_the_binding_scoped_verb(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: object())
    seen = []
    monkeypatch.setattr(cli, "voice_answer_once", _fake_answer(seen))
    parser = cli.build_parser()

    cases = [
        (
            ["voice-answer", "answer", "q-1", "--recording-id", "rec-1", "--select", "yes", "--timeout", "9"],
            {"type": "voice_answer.answer", "recording_id": "rec-1", "question_id": "q-1", "selections": ["yes"]},
        ),
        (
            ["voice-answer", "answer", "q-2", "--recording-id", "rec-1", "--text", "skip the six I edited"],
            {"type": "voice_answer.answer", "recording_id": "rec-1", "question_id": "q-2", "text": "skip the six I edited"},
        ),
        (
            ["voice-answer", "answer", "q-3", "--recording-id", "rec-2", "--selection", "a", "--selection", "b", "--text", "both"],
            {"type": "voice_answer.answer", "recording_id": "rec-2", "question_id": "q-3", "selections": ["a", "b"], "text": "both"},
        ),
    ]
    for argv, expected in cases:
        args = parser.parse_args(argv)
        assert args.func is cli.voice_answer_answer
        assert cli.voice_answer_answer(args) == 0
        assert json.loads(capsys.readouterr().out)["outcome"] == "answered"
        assert seen[-1]["payload"] == expected
    assert seen[0]["timeout"] == 9.0


def test_voice_answer_answer_requires_recording_id_and_an_answer(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: object())

    async def _answer(_config, payload, *, timeout):  # pragma: no cover - must not run
        raise AssertionError("an incomplete voice answer must be rejected before the wire")

    monkeypatch.setattr(cli, "voice_answer_once", _answer)
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["voice-answer", "answer", "q", "--text", "x"])
    assert cli.voice_answer_answer(parser.parse_args(["voice-answer", "answer", "q", "--recording-id", "r"])) == 2
    assert cli.voice_answer_answer(parser.parse_args(["voice-answer", "answer", "q", "--recording-id", "r", "--text", "  "])) == 2


def test_voice_answer_answer_exits_nonzero_on_a_daemon_error(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: object())
    seen = []
    monkeypatch.setattr(cli, "voice_answer_once", _fake_answer(seen, "voice_answer.answer.error"))
    args = cli.build_parser().parse_args(["voice-answer", "answer", "q-9", "--recording-id", "rec-9", "--select", "x"])
    assert cli.voice_answer_answer(args) == 1
    assert json.loads(capsys.readouterr().out)["type"] == "voice_answer.answer.error"


def test_voice_answer_once_carries_seat_identity_and_is_retry_eligible(monkeypatch):
    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "seat-host:front-seat")
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN_FILE", raising=False)
    monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN", "front-token")
    seen = []

    async def fake_one_shot(_config, payload, *, prefix, timeout, from_stream_id=None, **kwargs):
        seen.append((prefix, dict(payload), from_stream_id))
        return {"type": f"{prefix}.answer.ok"}

    monkeypatch.setattr(wsclient, "_one_shot_rpc", fake_one_shot)
    asyncio.run(wsclient.voice_answer_once(
        object(), {"type": "voice_answer.answer", "recording_id": "r", "question_id": "q"}))
    prefix, payload, from_stream_id = seen[0]
    assert prefix == "voice_answer"
    assert from_stream_id == "seat-host:front-seat"
    assert payload["request_id"].startswith("voice-answer-")
    assert payload.get("from_stream_id") == "seat-host:front-seat"
    assert payload.get("stream_token") == "front-token"
    assert wsclient._is_rpc_retry_eligible({"type": "voice_answer.answer", "request_id": "r"})
