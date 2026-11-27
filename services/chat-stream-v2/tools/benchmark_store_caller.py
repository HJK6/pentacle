"""Compare uncached Store caller capture with an empty function.

Run with the service interpreter: python tools/benchmark_store_caller.py
Reports local measurements; the review owner runs the criterion 1 gate on the deployment host.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import platform
from statistics import median
import sys
from time import perf_counter_ns

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store_call_diag import UNKNOWN, capture_caller  # noqa: E402

CALLS = 200_000
ROUNDS = 7


def empty(_store_classes):
    pass


class StoreFrames:
    def descend(self, depth, fn, classes):
        if depth:
            return self.descend(depth - 1, fn, classes)
        return fn(classes)

    async def outer(self, fn, classes):
        return await self.inner(fn, classes)

    async def inner(self, fn, classes):
        return fn(classes)


async def outside(stack, fn, classes):
    return await stack.outer(fn, classes)


async def main():
    stack = StoreFrames()
    classes = frozenset({"StoreFrames"})
    assert stack.descend(11, capture_caller, classes) == UNKNOWN
    assert await outside(stack, capture_caller, classes) == f"{__name__}.outside"

    async def worst(fn, n):
        for _ in range(n):
            stack.descend(11, fn, classes)

    async def typical(fn, n):
        for _ in range(n):
            await outside(stack, fn, classes)

    print(f"{platform.platform()}; {platform.python_implementation()} "
          f"{platform.python_version()}; {ROUNDS} × {CALLS:,} calls per function and case")
    for label, run in (("(a) 12 skipped frames", worst), ("(b) direct await, caller at frame 3", typical)):
        buffers = {empty: [], capture_caller: []}
        for fn in buffers:
            await run(fn, 2000)
        for i in range(ROUNDS):
            order = tuple(buffers)
            for fn in (order if i % 2 == 0 else order[::-1]):
                t0 = perf_counter_ns()
                await run(fn, CALLS)
                buffers[fn].append((perf_counter_ns() - t0) / CALLS / 1000)
        base, walk = buffers.values()
        overhead = median(w - b for b, w in zip(base, walk))
        print(label)
        print("  Empty samples:", ", ".join(f"{v:.3f}" for v in base))
        print("  Capture samples:", ", ".join(f"{v:.3f}" for v in walk))
        print(f"  Empty median {median(base):.3f}; capture median {median(walk):.3f}; "
              f"median paired overhead {overhead:.3f} µs/call")


if __name__ == "__main__":
    asyncio.run(main())
