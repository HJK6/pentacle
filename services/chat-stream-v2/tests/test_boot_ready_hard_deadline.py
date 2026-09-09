"""Codex readiness uses one fixed attempt deadline."""

from __future__ import annotations

import asyncio
import time as _real_time

import pytest

import spawnctl as spawnctl_mod
from sessions import Sessions, VerbError
from spawnctl import BootReadyOutcome, SpawnCtl
from store import Store


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class _ClockShim:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock

    def monotonic(self) -> float:
        return self.clock.monotonic()

    def __getattr__(self, name: str):
        return getattr(_real_time, name)


class _ChangingTmux:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.polls = 0

    async def capture(self, _name: str) -> str:
        self.polls += 1
        self.clock.now += 10.0
        return f"still loading {self.polls}"

    async def session_state(self, _name: str) -> str:
        return "alive"

    async def run(self, *_args, **_kwargs) -> tuple[int, str]:
        return 0, ""


def _ctl(tmux: object) -> SpawnCtl:
    store = Store(":memory:")
    return SpawnCtl(store, Sessions(store, tmux=tmux, local_host="localhost"), tmux=tmux)


async def _no_sleep(*_args, **_kwargs) -> None:
    return None


def test_changing_pane_cannot_extend_fixed_boot_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(spawnctl_mod, "time", _ClockShim(clock))
    monkeypatch.setattr(spawnctl_mod.asyncio, "sleep", _no_sleep)
    tmux = _ChangingTmux(clock)

    outcome = asyncio.run(_ctl(tmux)._await_provider_ready("s", "claude", tmux))

    assert outcome.ready is False
    assert spawnctl_mod.BOOT_READY_HARD_DEADLINE_S <= outcome.elapsed_s < 191.0


def test_boot_not_ready_reports_elapsed_and_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    class Tmux:
        async def new_session(self, *_args, **_kwargs) -> None:
            return None

        async def pane_pid(self, _name: str) -> str:
            return "123"

        async def pane_identity(self, name: str) -> dict[str, str]:
            return {"pane_id": "%1", "pane_pid": "123", "session_name": name}

    async def run() -> str:
        store = Store(":memory:")
        store.start()
        try:
            tmux = Tmux()
            ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host="localhost"), tmux=tmux)

            async def not_ready(*_args, **_kwargs) -> BootReadyOutcome:
                return BootReadyOutcome(False, 147.3, 17)

            monkeypatch.setattr(ctl, "_await_provider_ready", not_ready)
            # Production reserves + records intent before _spawn_fenced; the
            # cancel-fence bind commit is fail-closed on a missing reservation.
            await store.reserve_stream_id(
                "localhost", "s", ttl_s=600, request_id="r",
                nonce="n-boot", owner_instance_id="inst",
            )
            await store.record_spawn_intent("localhost", "s", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": ""})
            with pytest.raises(VerbError) as exc:
                await ctl._spawn_fenced(
                    "localhost", "s", "r", "run", "", "", {}, [False], [False],
                    {"provider": "claude"}, {"resolved_launch_tuple": {"provider": "claude"}},
                    tmux, {"state": "not_requested"}, nonce="n-boot",
                )
            return str(exc.value)
        finally:
            store.stop()

    message = asyncio.run(run())
    assert "147.3s" in message
    assert "budget 180s" in message
