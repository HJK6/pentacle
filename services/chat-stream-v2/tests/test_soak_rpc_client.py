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
