"""Failed live spawns remain observable without daemon remediation."""

from __future__ import annotations

import asyncio

import pytest

import spawnctl as spawnctl_mod
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store

HOST = "hosta"
TRUST_DIALOG = "Quick safety check\nYes, I trust this folder\nEnter to confirm"


class Tmux:
    def __init__(self, screen: str = "model: loading") -> None:
        self.alive: set[str] = set()
        self.screen = screen
        self.keys: list[tuple[str, ...]] = []

    async def has_session(self, name: str) -> bool:
        return name in self.alive

    async def session_state(self, name: str) -> str:
        return "alive" if name in self.alive else "gone"

    async def capture(self, _name: str) -> str:
        return self.screen

    async def pane_pid(self, _name: str) -> str:
        return ""

    async def run(self, *args: str, **_kwargs: object) -> tuple[int, str]:
        self.keys.append(args)
        return 0, ""


def test_agent_never_started_failure_leaves_live_pane_open() -> None:
    async def run() -> tuple[dict, dict, Tmux, dict]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = Tmux()
            tmux.alive.add("child")
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            await sessions.open(HOST, "child", provider="codex", pane_status="pane_alive")
            await store.set_spawn_outcome(
                HOST, "child", "failed", request_id="spawn-child",
                delivery_evidence="agent_never_started", reason="boot deadline expired",
            )
            assert not hasattr(ctl, "bind_failed_spawn_routing")
            assert not hasattr(ctl, "_route_failed_live_spawn")
            row = await store.fetch_session(HOST, "child") or {}
            outcome = await store.get_spawn_outcome(HOST, "child") or {}
            reply = await ctl.await_spawn({"stream_id": f"{HOST}:child"})
            return row, outcome, tmux, reply
        finally:
            store.stop()

    row, outcome, tmux, reply = asyncio.run(run())
    assert row["status"] == "open"
    assert outcome["state"] == "failed"
    assert "child" in tmux.alive
    assert reply["type"] == "await_spawn.error"
    assert reply["delivery_evidence"] == "agent_never_started"


def test_claude_trust_dialog_uses_existing_readiness_failure_without_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> tuple[object, Tmux]:
        tmux = Tmux(TRUST_DIALOG)
        tmux.alive.add("child")
        store = Store(":memory:")
        try:
            ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=HOST), tmux=tmux)
            return await ctl._await_provider_ready("child", "claude", tmux), tmux
        finally:
            store.stop()

    monkeypatch.setattr(spawnctl_mod, "BOOT_READY_HARD_DEADLINE_S", 0.01)
    outcome, tmux = asyncio.run(run())
    assert outcome.ready is False
    assert tmux.keys == []
