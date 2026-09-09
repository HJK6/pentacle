"""Spawn — and only spawn — waits for boot reconciliation to finish (test #18).

`bind()` serves before init, so a spawn arriving in the boot window could have
its reservation adopted or released underneath it. The server gates spawn on a
`spawn_ready` event that `main.py` clears before bind and sets once
`reconcile_spawn_intents()` returns. Every other verb must keep serving
immediately (port-bind-first is spec constraint 1, non-negotiable).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from server import Server  # noqa: E402


class StubSpawn:
    def __init__(self) -> None:
        self.calls = 0

    async def spawn(self, msg: dict, local_host: str) -> dict:
        self.calls += 1
        return {"type": "spawn.ok", "ok": True, "stream_id": "localhost:x"}


def test_spawn_waits_for_the_gate_but_other_verbs_do_not() -> None:
    async def _go() -> None:
        spawn = StubSpawn()
        server = Server(spawnctl=spawn, local_host="localhost")
        server.spawn_ready.clear()  # boot window: gate closed

        task = asyncio.create_task(
            server._dispatch('{"type": "spawn", "request_id": "s1"}')
        )

        # A non-spawn verb serves immediately even while the gate is closed —
        # this is what proves ONLY spawn is gated (port-bind-first is intact).
        pong = await asyncio.wait_for(
            server._dispatch('{"type": "ping", "request_id": "p1"}'), timeout=1
        )
        assert pong[0]["type"] == "pong"

        # The spawn is parked: give it ample loop turns, it must not run. On
        # revert (no `await self.spawn_ready.wait()`) it runs here and fails this.
        await asyncio.sleep(0.05)
        assert spawn.calls == 0, "spawn ran during the boot window"
        assert not task.done()

        # Reconciliation done → open the gate → the spawn proceeds.
        server.spawn_ready.set()
        frames = await asyncio.wait_for(task, timeout=1)
        assert spawn.calls == 1
        assert frames[0]["type"] == "spawn.ok"

    asyncio.run(_go())
