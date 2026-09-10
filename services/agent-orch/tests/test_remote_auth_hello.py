import asyncio
import json
import time

import pytest

from agent_orch.config import Config
from agent_orch.wsclient import WebsocketClient, _read_rpc_frame


def test_persistent_client_hello_carries_actual_seat_token(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_ORCH_INTERNAL_LEADER_STREAM_ID", raising=False)
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN_FILE", raising=False)
    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "local:seat")
    monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN", "synthetic-seat-token")
    hello = WebsocketClient(Config("ws://unused", "", "local", tmp_path))._hello()
    assert hello["from_stream_id"] == "local:seat"
    assert hello["stream_token"] == "synthetic-seat-token"


def test_hello_rejection_fails_rpc_immediately():
    class Socket:
        async def recv(self):
            return json.dumps({"type": "hello.error", "error_code": "authentication_required"})
    async def run():
        with pytest.raises(PermissionError, match="authentication_required"):
            await _read_rpc_frame(Socket(), "req", deadline=time.monotonic() + .1,
                                  matches=lambda frame: frame.get("type") == "send.ok",
                                  timeout_message="should not time out")
    asyncio.run(run())
