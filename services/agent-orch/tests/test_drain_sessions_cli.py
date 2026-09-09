from __future__ import annotations

import asyncio
import json
from argparse import Namespace

from agent_orch import cli, wsclient
from agent_orch.config import Config


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        request_id = self.sent[-1]["request_id"]
        return json.dumps(
            {
                "type": "drain_sessions.ok",
                "request_id": request_id,
                "result": {"closed": [], "already_closed": [], "unknown": []},
            }
        )

    async def close(self) -> None:
        return None


def _args(**overrides):
    data = {
        "stream_ids": ["hosta:claude-hosta-stale"],
        "operator_confirm": True,
        "timeout": 5.0,
    }
    data.update(overrides)
    return Namespace(**data)


def test_drain_sessions_once_builds_explicit_operator_confirmed_frame(monkeypatch, tmp_path):
    fake = _FakeWS()

    async def fake_connect_ready(_config, from_stream_id=None):
        return fake

    monkeypatch.setattr(wsclient, "_connect_rpc_ready", fake_connect_ready)

    response = asyncio.run(
        wsclient.drain_sessions_once(
            Config("ws://test", "tok", "hosta", tmp_path),
            ["hosta:claude-hosta-stale"],
            operator_confirm=True,
        )
    )

    assert response["type"] == "drain_sessions.ok"
    assert fake.sent == [
        {
            "type": "drain_sessions",
            "request_id": fake.sent[0]["request_id"],
            "keys": [{"host": "hosta", "session_name": "claude-hosta-stale"}],
            "operator_confirm": True,
        }
    ]


def test_drain_sessions_cli_refuses_without_operator_confirm(monkeypatch, tmp_path, capsys):
    calls: list[object] = []

    async def fake_drain_sessions_once(*args, **kwargs):
        calls.append((args, kwargs))
        return {"type": "drain_sessions.ok", "request_id": "drain-1"}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hosta", tmp_path))
    monkeypatch.setattr(cli, "drain_sessions_once", fake_drain_sessions_once)

    rc = cli.drain_sessions(_args(operator_confirm=False))

    assert rc == 2
    assert calls == []
    assert "--operator-confirm is required" in capsys.readouterr().err


def test_drain_sessions_cli_accepts_with_operator_confirm(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []

    async def fake_drain_sessions_once(config, stream_ids, *, timeout=30.0, operator_confirm=False):
        calls.append(
            {
                "stream_ids": stream_ids,
                "timeout": timeout,
                "operator_confirm": operator_confirm,
            }
        )
        return {"type": "drain_sessions.ok", "request_id": "drain-1"}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hosta", tmp_path))
    monkeypatch.setattr(cli, "drain_sessions_once", fake_drain_sessions_once)

    rc = cli.drain_sessions(_args())

    assert rc == 0
    assert calls == [
        {
            "stream_ids": ["hosta:claude-hosta-stale"],
            "timeout": 5.0,
            "operator_confirm": True,
        }
    ]
    assert json.loads(capsys.readouterr().out)["type"] == "drain_sessions.ok"


def test_drain_sessions_cli_surfaces_operator_trust_refusal_without_retry(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []

    async def fake_drain_sessions_once(config, stream_ids, *, timeout=30.0, operator_confirm=False):
        calls.append(
            {
                "stream_ids": stream_ids,
                "timeout": timeout,
                "operator_confirm": operator_confirm,
            }
        )
        return {
            "type": "drain_sessions.error",
            "request_id": "drain-1",
            "error": "operator_trust_required",
            "error_code": "operator_trust_required",
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hosta", tmp_path))
    monkeypatch.setattr(cli, "drain_sessions_once", fake_drain_sessions_once)

    rc = cli.drain_sessions(_args())

    assert rc == 1
    assert calls == [
        {
            "stream_ids": ["hosta:claude-hosta-stale"],
            "timeout": 5.0,
            "operator_confirm": True,
        }
    ]
    assert json.loads(capsys.readouterr().out)["error_code"] == "operator_trust_required"
