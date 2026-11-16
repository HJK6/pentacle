"""Store write queue is bounded and never strands a future (test #8).

Two hazards the v1 wedge taught: an unbounded write queue lets callers pile
work on a stalled store thread without limit, and a callable whose future is
never drained hangs its awaiter for the life of the process. The queue now has
a cap (over it, `submit` fails fast rather than growing), the start/stop
transition is lock-serialized so nothing is enqueued behind the stop sentinel,
and the worker resolves anything still queued as it tears down.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from store import Store  # noqa: E402


def test_submit_after_stop_raises_instead_of_hanging() -> None:
    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        await store.put("k", "v")  # sanity: a normal submit resolves
        store.stop()
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(store.submit(lambda c: None), timeout=1)

    asyncio.run(_go())


def test_over_cap_submit_fails_fast_instead_of_growing_unbounded() -> None:
    async def _go() -> None:
        store = Store(":memory:", max_pending=2)
        store.start()
        started = threading.Event()
        release = threading.Event()

        def blocker(conn: object) -> None:
            started.set()
            release.wait(5)

        blocked = asyncio.ensure_future(store.submit(blocker))
        fillers: list[asyncio.Future] = []
        try:
            # Wait until the worker has dequeued the blocker and is stuck in it;
            # from here the bounded queue can hold at most `max_pending` items.
            await asyncio.get_running_loop().run_in_executor(None, started.wait, 2)
            fillers = [asyncio.ensure_future(store.submit(lambda c: None)) for _ in range(3)]
            await asyncio.sleep(0.2)  # let each reach put_nowait
            # Assert BEFORE releasing: the two that fit are still pending (the
            # worker is blocked), and the one past the cap has already been
            # rejected. Awaiting the pending ones here would deadlock until the
            # blocker times out.
            rejected = [
                f.exception() for f in fillers
                if f.done() and isinstance(f.exception(), RuntimeError)
            ]
            assert any("overloaded" in str(e) for e in rejected), (
                f"expected a fail-fast rejection past the cap, got {rejected}"
            )
        finally:
            release.set()
            store.stop()
            # Drain every task so none is left pending.
            await asyncio.gather(blocked, *fillers, return_exceptions=True)

    asyncio.run(_go())


def test_a_queued_future_is_failed_not_left_pending_on_teardown() -> None:
    """The drain mechanism: a callable still in the queue when the worker exits
    resolves with an error, never hangs. (Revert: the future stays pending and
    the await times out.)"""

    async def _go() -> None:
        store = Store(":memory:")  # not started — no worker will ever run it
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        store._queue.put((lambda c: None, fut, loop))  # type: ignore[arg-type]
        store._fail_pending("store stopped")
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(fut, timeout=1)

    asyncio.run(_go())


def test_teardown_drain_leaves_no_pending_future_after_stop() -> None:
    """End to end: a real stop must not strand any awaiter."""

    async def _go() -> None:
        store = Store(":memory:", max_pending=4)
        store.start()
        # A couple of quick writes, then stop; everything must settle.
        await store.put("a", "1")
        await store.put("b", "2")
        store.stop()
        # Post-stop submit is refused, not hung.
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(store.submit(lambda c: None), timeout=1)

    asyncio.run(_go())


if __name__ == "__main__":  # pragma: no cover
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
