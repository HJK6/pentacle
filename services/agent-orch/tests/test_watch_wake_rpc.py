"""Exercise watch/wake replies through the real RPC reader, not a CLI stub."""
import asyncio
import json

import pytest

from agent_orch import wsclient
from agent_orch.config import Config


class ReplySocket:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []
        self.closed = False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if self.frames:
            return json.dumps(self.frames.pop(0))
        await asyncio.Future()

    async def close(self):
        self.closed = True


def configure(monkeypatch, tmp_path, sockets, attempts=1):
    connected = []

    async def connect(config, **kwargs):
        socket = sockets[len(connected)]
        connected.append(socket)
        return socket

    monkeypatch.setattr(wsclient, "_connect_rpc_ready", connect)
    monkeypatch.setattr(wsclient, "_stream_token_from_env", lambda: "synthetic-token")
    monkeypatch.setattr(wsclient, "_retry_backoff_s", lambda policy, attempt: 0.0)
    monkeypatch.setenv("AGENT_ORCH_RPC_RETRY_MAX_ATTEMPTS", str(attempts))
    monkeypatch.delenv("AGENT_ORCH_RPC_RETRY_DEADLINE_S", raising=False)
    return Config(ws_url="ws://127.0.0.1:1", token="synthetic", host_id="test",
                  runtime_dir=tmp_path), connected


@pytest.mark.parametrize("family", ["watch", "wake"])
@pytest.mark.parametrize("verb", ["register", "list", "cancel"])
@pytest.mark.parametrize("outcome", ["ok", "error"])
def test_watch_wake_consumes_correlated_reply(monkeypatch, tmp_path, family, verb, outcome):
    request = {"type": f"{family}.{verb}", "request_id": "owned-request",
               "from_stream_id": "test:owner"}
    # WatchWake.handle emits family.error for failures, and family.verb.ok for success.
    reply_type = f"{family}.{verb}.ok" if outcome == "ok" else f"{family}.error"
    expected = {"type": reply_type, "request_id": "owned-request",
                "ok": outcome == "ok"}
    socket = ReplySocket([
        {"type": "ready"},
        {**expected, "request_id": "someone-else"},
        {"type": "schedule.list.ok", "request_id": "owned-request"},
        expected,
    ])
    config, connected = configure(monkeypatch, tmp_path, [socket])
    result = asyncio.run(wsclient.coordination_once(config, request, timeout=0.03))
    assert result == expected
    assert len(connected) == 1
    assert socket.closed
    assert socket.sent[0]["request_id"] == "owned-request"


@pytest.mark.parametrize("verb", ["coordination.hold.acquire", "coordination.hold.list",
                                  "coordination.hold.release"])
def test_unrelated_coordination_reply_is_preserved(monkeypatch, tmp_path, verb):
    expected = {"type": verb + ".ok", "request_id": "coordination-request"}
    socket = ReplySocket([expected])
    config, _ = configure(monkeypatch, tmp_path, [socket])
    result = asyncio.run(wsclient.coordination_once(
        config, {"type": verb, "request_id": "coordination-request"}, timeout=0.03))
    assert result == expected
    assert socket.closed


@pytest.mark.parametrize("family", ["watch", "wake"])
def test_lost_reply_retry_keeps_registration_identity(monkeypatch, tmp_path, family):
    expected = {"type": family + ".register.ok", "request_id": "same-registration",
                "id": "same-trigger"}
    sockets = [ReplySocket([]), ReplySocket([expected])]
    config, connected = configure(monkeypatch, tmp_path, sockets, attempts=2)
    result = asyncio.run(wsclient.coordination_once(
        config, {"type": family + ".register", "request_id": "same-registration",
                 "from_stream_id": "test:owner"}, timeout=0.03))
    assert result == expected
    assert len(connected) == 2
    assert [s.sent[0]["request_id"] for s in sockets] == ["same-registration"] * 2
    assert all(s.closed for s in sockets)


@pytest.mark.parametrize("family", ["watch", "wake"])
def test_auth_failure_still_refuses_without_retry(monkeypatch, tmp_path, family):
    socket = ReplySocket([{"type": "auth.error", "error": "synthetic denial"}])
    config, connected = configure(monkeypatch, tmp_path, [socket])
    with pytest.raises(PermissionError, match="synthetic denial"):
        asyncio.run(wsclient.coordination_once(
            config, {"type": family + ".list", "request_id": "denied"}, timeout=0.03))
    assert len(connected) == 1
    assert socket.closed
