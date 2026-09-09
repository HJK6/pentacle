from __future__ import annotations

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
