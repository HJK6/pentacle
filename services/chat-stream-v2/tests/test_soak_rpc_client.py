from __future__ import annotations

import json
from typing import Any

from tests.soak import harness
from tests.soak.harness import LatencyStats, RpcClient


class _WebSocket:
    def close(self) -> None:
        return None


def test_soak_rpc_client_accepts_synthetic_frames(monkeypatch: Any) -> None:
    connection: dict[str, Any] = {}

    def connect(url: str, **kwargs: Any) -> _WebSocket:
        connection.update(url=url, **kwargs)
        return _WebSocket()

    monkeypatch.setattr(harness, "connect", connect)
    client = RpcClient("ws://soak.test", LatencyStats(), "probe")
    client.close()

    assert connection["max_size"] is None


class _EchoWebSocket(_WebSocket):
    """Replies to each sent frame with a frame carrying its request id."""

    def __init__(self, sent: list[dict[str, Any]]) -> None:
        self._sent = sent

    def send(self, raw: str) -> None:
        self._sent.append(json.loads(raw))

    def recv(self, timeout: float | None = None) -> str:
        return json.dumps({"type": "spawn.ok", "request_id": self._sent[-1]["request_id"]})


def test_reconnected_clients_never_reuse_request_ids(monkeypatch: Any) -> None:
    # The daemon keys spawn idempotency on request_id when no explicit key is
    # sent, and keeps that binding across a daemon restart. A client rebuilt
    # after the soak's forced restart must not replay an earlier id with a
    # different spawn payload (idempotency_key_conflict).
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(harness, "connect", lambda url, **_: _EchoWebSocket(sent))
    for _ in range(2):
        client = RpcClient("ws://soak.test", LatencyStats(), "churn")
        client.call({"type": "spawn", "session_name": f"soak-churn-{len(sent)}"}, record=False)
        client.close()

    request_ids = [frame["request_id"] for frame in sent]
    assert len(set(request_ids)) == len(request_ids), request_ids
    assert all(rid.startswith("churn-") for rid in request_ids)


class _ScriptedWebSocket(_WebSocket):
    """Replies with scripted frames per verb, echoing each request id."""

    def __init__(self, sent: list[dict[str, Any]], replies: dict[str, dict[str, Any]]) -> None:
        self._sent = sent
        self._replies = replies

    def send(self, raw: str) -> None:
        self._sent.append(json.loads(raw))

    def recv(self, timeout: float | None = None) -> str:
        frame = self._sent[-1]
        return json.dumps({**self._replies[frame["type"]], "request_id": frame["request_id"]})


def test_fixture_token_grant_error_carries_the_daemon_error_code(monkeypatch: Any, tmp_path: Any) -> None:
    # The daemon's grant_token errors use the `error` field; the harness must
    # surface that code (e.g. token_already_set after a lost grant reply).
    sent: list[dict[str, Any]] = []
    replies = {"grant_token": {"type": "grant_token.error", "error": "token_already_set"}}
    monkeypatch.setattr(harness, "connect", lambda url, **_: _ScriptedWebSocket(sent, replies))
    monkeypatch.setattr(harness, "_seat_token_path", lambda _cwd, sid: tmp_path / sid.replace(":", "_"))
    client = RpcClient("ws://soak.test", LatencyStats(), "churn", spawn_cwd=tmp_path)

    try:
        client.call({"type": "close", "stream_id": "soakhost:soak-churn-0-1"}, record=False)
    except harness.FixtureTokenGrantError as exc:
        assert exc.stream_id == "soakhost:soak-churn-0-1"
        assert exc.error == "token_already_set"
        assert isinstance(exc, AssertionError)
    else:
        raise AssertionError("a refused grant must raise FixtureTokenGrantError")
    assert [frame["type"] for frame in sent] == ["grant_token"]


def test_operator_close_uses_a_fresh_unbound_connection(monkeypatch: Any, tmp_path: Any) -> None:
    sent: list[dict[str, Any]] = []
    connections: list[_ScriptedWebSocket] = []
    replies = {
        "grant_token": {"type": "grant_token.ok", "stream_token": "seat-token"},
        "close": {"type": "close.ok"},
    }

    def connect(url: str, **_: Any) -> _ScriptedWebSocket:
        connections.append(_ScriptedWebSocket(sent, replies))
        return connections[-1]

    monkeypatch.setattr(harness, "connect", connect)
    monkeypatch.setattr(harness, "_seat_token_path", lambda _cwd, sid: tmp_path / sid.replace(":", "_"))
    client = RpcClient("ws://soak.test", LatencyStats(), "churn", spawn_cwd=tmp_path)
    client.call({"type": "close", "stream_id": "soakhost:seat-a"}, record=False)
    seat_connections = len(connections)

    reply = client.operator_close("soakhost:seat-b", reason="lost grant reply")

    assert reply["type"] == "close.ok"
    assert len(connections) == seat_connections + 1
    operator_frame = sent[-1]
    assert operator_frame["type"] == "close"
    assert operator_frame["stream_id"] == "soakhost:seat-b"
    assert operator_frame["operator_confirm"] is True
    assert operator_frame["reason"] == "lost grant reply"
    assert "from_stream_id" not in operator_frame and "stream_token" not in operator_frame
