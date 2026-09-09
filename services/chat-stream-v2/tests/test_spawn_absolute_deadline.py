"""Caller-supplied absolute-deadline clamps in the spawn readiness path.

These tests exercise the deadline substrate directly, without depending on an
external manifest or deployment environment.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import spawnctl as spawnctl_mod
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store
from submission_events import EventProof, EventWatermark, PROOF_TERMINAL_BOUND_S


HOST = "localhost"
NAME = "codex-release-order"


class ReadyTmux:
    def __init__(self) -> None:
        self.alive = False
        self.created = 0
        self.pastes = 0
        self.killed = False

    async def new_session(self, _name, _command, cwd=None, env=None) -> None:
        self.alive = True
        self.created += 1

    async def has_session(self, _name) -> bool:
        return self.alive

    async def session_state(self, _name) -> str:
        return "alive" if self.alive else "gone"

    async def capture(self, _name) -> str:
        return "READY\n› "

    async def pane_pid(self, _name) -> str:
        return "4321"

    async def pane_identity(self, name) -> dict[str, str] | None:
        if not self.alive:
            return None
        return {
            "pane_pid": "4321",
            "pane_id": "%42",
            "tty": "/dev/ttys042",
            "tmux_socket": "/tmp/tmux-test",
            "session_name": name,
        }

    async def paste(self, _name, _text) -> None:
        self.pastes += 1

    async def kill_session(self, _name) -> None:
        self.alive = False
        self.killed = True

    async def kill_pane(self, _pane_id) -> None:
        self.alive = False
        self.killed = True

    async def run(self, *_args, **_kwargs):
        return 0, ""


async def _resolved_codex(_msg, _host, _name):
    return (
        "run",
        {"resolved_launch_tuple": {"provider": "codex"}},
        {
            "provider": "codex",
        },
    )


async def _confirmed_delivery(self, name, brief, before, provider, tmux, **_kwargs):
    return True


def _message(request_id: str) -> dict:
    return {"objective": "Exercise the existing spawn contract",
        "provider": "codex",
        "prompt": "first durable prompt",
        "session_name": NAME,
        "request_id": request_id,
        "idempotency_key": request_id,
        "ready_marker": "READY",
    }


def test_submission_proof_uses_only_remaining_release_budget() -> None:
    class Proof:
        timeout = None

        async def wait(self, stream_id, *, expected_text, watermark, timeout_s):
            self.timeout = timeout_s
            return EventProof("proven", stream_id, watermark.daemon_seq, event_id=1)

    async def go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = ReadyTmux()
            proof = Proof()
            ctl = SpawnCtl(
                store,
                Sessions(store, tmux=tmux, local_host=HOST),
                tmux=tmux,
                submission_proof=proof,
            )
            deadline = time.monotonic() + 0.5
            confirmed = await ctl._confirm_submission(
                NAME,
                "brief",
                "",
                "codex",
                tmux,
                watermark=EventWatermark(f"{HOST}:{NAME}", 0, "reachable"),
                absolute_deadline=deadline,
            )
            return confirmed, proof.timeout
        finally:
            store.stop()

    confirmed, timeout = asyncio.run(go())
    assert confirmed is True
    assert 0 < timeout <= 0.5 < PROOF_TERMINAL_BOUND_S


def test_provider_ready_poll_cannot_extend_past_release_deadline(monkeypatch) -> None:
    class Clock:
        now = 100.0

        def monotonic(self) -> float:
            return self.now

        async def sleep(self, delay: float) -> None:
            self.now += delay

    class LoadingTmux(ReadyTmux):
        async def capture(self, _name) -> str:
            return "still loading"

    clock = Clock()
    monkeypatch.setattr(spawnctl_mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(spawnctl_mod.asyncio, "sleep", clock.sleep)

    async def go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = LoadingTmux()
            tmux.alive = True
            ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=HOST), tmux=tmux)
            return await ctl._await_provider_ready(
                NAME,
                "codex",
                tmux,
                absolute_deadline=100.5,
            )
        finally:
            store.stop()

    outcome = asyncio.run(go())
    assert outcome.ready is False
    assert clock.now == 100.5


def test_boot_marker_poll_cannot_extend_past_release_deadline(monkeypatch) -> None:
    class Clock:
        now = 100.0

        def monotonic(self) -> float:
            return self.now

        async def sleep(self, delay: float) -> None:
            self.now += delay

    class LoadingTmux(ReadyTmux):
        def __init__(self) -> None:
            super().__init__()
            self.captures = 0

        async def capture(self, _name) -> str:
            self.captures += 1
            return f"still loading {self.captures}"

    clock = Clock()
    monkeypatch.setattr(spawnctl_mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(spawnctl_mod.asyncio, "sleep", clock.sleep)

    async def go() -> bool:
        store = Store(":memory:")
        store.start()
        try:
            tmux = LoadingTmux()
            tmux.alive = True
            ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=HOST), tmux=tmux)
            return await ctl._await_marker(
                NAME, "READY", 30, tmux=tmux, absolute_deadline=100.5,
            )
        finally:
            store.stop()

    assert asyncio.run(go()) is False
    assert clock.now == 100.5
