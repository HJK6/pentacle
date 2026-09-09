"""A tmux_created reservation whose host stops matching --local-host is released,
not wedged forever (QA #19).

The TTL sweep exempts tmux_created=1 reservations (they are the sole record of a
live orphan pane). If --local-host changes under such a reservation, the
reconciler used to skip it (foreign host) while the sweep still exempted it, so
that stream id was reserved permanently. Boot reconciliation now releases a
foreign-host reservation — without touching tmux — so the id becomes reusable.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from sessions import Sessions  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from store import Store  # noqa: E402

OLD_HOST = "oldhost"
NAME = "s"


class FakeTmux:
    async def has_session(self, name: str) -> bool:
        return False

    async def session_state(self, name: str) -> str:
        return "gone"

    async def kill_session(self, name: str) -> None:  # must never be called here
        raise AssertionError("reconcile must not touch tmux for a foreign host")


def test_foreign_host_reservation_is_released_and_the_id_is_reusable() -> None:
    async def _go() -> tuple[dict, bool, dict | None]:
        store = Store(":memory:")
        store.start()
        try:
            assert await store.reserve_stream_id(OLD_HOST, NAME, ttl_s=0.01, request_id="r1")
            await store.mark_tmux_created(OLD_HOST, NAME)  # exempts it from the TTL sweep
            await asyncio.sleep(0.05)  # TTL lapses; the row survives because tmux_created=1

            # A daemon that now identifies as a DIFFERENT host reconciles.
            sessions = Sessions(store, tmux=FakeTmux(), local_host="newhost")
            spawnctl = SpawnCtl(store, sessions, tmux=FakeTmux())
            result = await spawnctl.reconcile_spawn_intents()

            outcome = await store.get_spawn_outcome(OLD_HOST, NAME)
            # The strongest signal: the id is reservable again. On revert the
            # exempt row persists and this INSERT hits the UNIQUE constraint.
            reusable = await store.reserve_stream_id(OLD_HOST, NAME, ttl_s=10, request_id="r2")
            return result, reusable, outcome
        finally:
            store.stop()

    result, reusable, outcome = asyncio.run(_go())
    assert result["released"] >= 1
    assert reusable is True, "the wedged stream id must be reservable again (QA #19)"
    assert outcome is not None and outcome["state"] == "failed"
