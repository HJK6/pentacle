"""Handoff-lineage routing contract for the session router.
`tests/test_route_reliability.py` (`resolve_route_target`, bounded depth) and
adapted to v2's `comms.resolve_route_target` + the `sessions` handoff link.
Routing stays on the active target while following closed handoff links.

Coverage:
  - a LIVE target with progeny still delivers to the live target (no forward)
  - a handoff chain is followed THROUGH closed intermediates to the live tail,
    with the full hop list and forwarded provenance
  - a cycle is a bounded `route_loop`, never an unbounded walk
  - depth is bounded (`route_depth_exceeded`)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from comms import Comms  # noqa: E402
from sessions import Sessions  # noqa: E402
from store import Store  # noqa: E402

HOST = "h"


async def _seed(store: Store, name: str, *, handoff_from: str | None = None, closed: bool = False) -> None:
    await store.open_session(HOST, name, visibility="visible", handoff_from_stream_id=handoff_from)
    if closed:
        await store.update_session(HOST, name, status="closed")


async def _comms(seed) -> Comms:
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, tmux=None, local_host=HOST)
    await seed(store)
    await sessions.refresh()
    return Comms(store, sessions, spawnctl=None), store


def test_live_target_with_progeny_delivers_to_live_target() -> None:
    async def go() -> None:
        async def seed(store: Store) -> None:
            await _seed(store, "old")  # OPEN
            await _seed(store, "new", handoff_from=f"{HOST}:old")  # a successor exists...
        comms, store = await _comms(seed)
        try:
            route = await comms.resolve_route_target(f"{HOST}:old")
            # ...but the target is still live, so delivery stays on it.
            assert route["ok"] is True
            assert route["final_target"] == f"{HOST}:old"
            assert route["forwarded"] is False
            assert route["hops"] == [f"{HOST}:old"]
        finally:
            store.stop()

    asyncio.run(go())


def test_chain_is_followed_through_closed_intermediates_to_the_live_tail() -> None:
    async def go() -> None:
        async def seed(store: Store) -> None:
            await _seed(store, "old", closed=True)
            await _seed(store, "mid", handoff_from=f"{HOST}:old", closed=True)
            await _seed(store, "live", handoff_from=f"{HOST}:mid")  # OPEN tail
        comms, store = await _comms(seed)
        try:
            route = await comms.resolve_route_target(f"{HOST}:old")
            assert route["ok"] is True
            assert route["final_target"] == f"{HOST}:live"
            assert route["forwarded"] is True
            assert route["hops"] == [f"{HOST}:old", f"{HOST}:mid", f"{HOST}:live"]
        finally:
            store.stop()

    asyncio.run(go())


def test_a_handoff_cycle_is_a_bounded_route_loop() -> None:
    async def go() -> None:
        async def seed(store: Store) -> None:
            # old <-> mid, both retired: a cycle the walk must break, not chase.
            await _seed(store, "old", handoff_from=f"{HOST}:mid", closed=True)
            await _seed(store, "mid", handoff_from=f"{HOST}:old", closed=True)
        comms, store = await _comms(seed)
        try:
            route = await comms.resolve_route_target(f"{HOST}:old")
            assert route["ok"] is False
            assert route["reason"] == "route_loop"
        finally:
            store.stop()

    asyncio.run(go())


def test_depth_is_bounded() -> None:
    async def go() -> None:
        async def seed(store: Store) -> None:
            # A chain longer than max_depth, every hop retired: bounded refusal,
            # never an unbounded walk.
            prev: str | None = None
            for i in range(6):
                await _seed(store, f"n{i}", handoff_from=(f"{HOST}:{prev}" if prev else None), closed=True)
                prev = f"n{i}"
        comms, store = await _comms(seed)
        try:
            route = await comms.resolve_route_target(f"{HOST}:n0", max_depth=3)
            assert route["ok"] is False
            assert route["reason"] == "route_depth_exceeded"
        finally:
            store.stop()

    asyncio.run(go())
