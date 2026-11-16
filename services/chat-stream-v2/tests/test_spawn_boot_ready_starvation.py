"""A fresh ready capture wins even when the loop was starved to the deadline."""

from __future__ import annotations

import asyncio
import time as _real_time

import pytest

import spawnctl as spawnctl_mod
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store


READY = "⏵⏵ bypass permissions on (shift+tab to cycle)\n❯ "


class Clock:
    now = 0.0

    def monotonic(self) -> float:
        return self.now


class ClockShim:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    def monotonic(self) -> float:
        return self.clock.monotonic()

    def __getattr__(self, name: str):
        return getattr(_real_time, name)


class StarvedTmux:
    def __init__(self, clock: Clock, ready: bool) -> None:
        self.clock, self.ready = clock, ready
        self.captures = 0

    async def capture(self, _name: str) -> str:
        self.captures += 1
        self.clock.now = spawnctl_mod.BOOT_READY_HARD_DEADLINE_S + 5
        if self.captures == 1:
            return "Loading…"
        return READY if self.ready else "Loading…"

    async def session_state(self, _name: str) -> str:
        return "alive"


def _outcome(monkeypatch: pytest.MonkeyPatch, ready: bool):
    clock = Clock()
    monkeypatch.setattr(spawnctl_mod, "time", ClockShim(clock))
    tmux = StarvedTmux(clock, ready)
    store = Store(":memory:")
    ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host="localhost"), tmux=tmux)
    return tmux, asyncio.run(ctl._await_provider_ready("s", "claude", tmux))


def test_ready_pane_survives_event_loop_starvation(monkeypatch: pytest.MonkeyPatch) -> None:
    tmux, outcome = _outcome(monkeypatch, True)
    assert outcome.ready is True
    assert tmux.captures == 2


def test_genuinely_unready_pane_still_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    tmux, outcome = _outcome(monkeypatch, False)
    assert outcome.ready is False
    assert tmux.captures == 2
