from __future__ import annotations

import asyncio

import pytest

import spawnctl as spawnctl_mod
from spawnctl import SpawnCtl


class _UnreadableTmux:
    async def session_state(self, _name: str) -> str:
        return "unknown"


def test_timeout_returns_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spawnctl_mod, "CREATION_PROBE_TIMEOUT_S", 0)
    assert asyncio.run(SpawnCtl(None, None)._wait_for_created_session("s", _UnreadableTmux())) == "unknown"
