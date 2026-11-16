#!/usr/bin/env python3
"""Measure real Store history paging on an owned, explicitly captured extract.

Never point this tool at a live database: Store startup and the inter-page
writes mutate the supplied extract. The capture manifest must travel with the
results. Queue waiting is excluded, just as in the store_call_slow boundary.
Works unchanged on the baseline and on the connection-shared delivery reader.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
from store import Store


class TimedStore(Store):
    recording = False

    async def submit(self, fn, *args, **kwargs):
        if not self.recording:
            return await super().submit(fn, *args, **kwargs)

        def timed(conn):
            start = time.monotonic()
            try:
                return fn(conn)
            finally:
                self.timings.append((time.monotonic() - start) * 1000)

        return await super().submit(timed, *args, **kwargs)


async def walk(store, stream, case, limit=500):
    remaining, cursor, output = limit, None, []
    cache = {}
    baseline = "deliveries_cache" in inspect.signature(store.fetch_session_event_page).parameters
    store.timings = []
    while remaining:
        quota = min(8, remaining)
        store.recording = True
        try:
            rows = await store.fetch_session_event_page(
                stream, before_daemon_seq=cursor,
                limit=quota + (1 if remaining > quota else 0),
                **({"deliveries_cache": cache} if baseline else {}),
            )
        finally:
            store.recording = False
        has_more = remaining > quota and len(rows) > quota
        page = rows[-quota:] if has_more else rows[:quota]
        if not page:
            break
        output[:0] = page
        remaining -= len(page)
        cursor = min(row["daemon_seq"] for row in page)
        if not has_more:
            break
        if case != "no_writes":
            def write(conn):
                if case == "unrelated_write":
                    conn.execute("UPDATE history_benchmark_writes SET value=value+1")
                else:
                    conn.execute("UPDATE v2_tell_deliveries SET created_at=created_at+1 "
                                 "WHERE tell_id=(SELECT tell_id FROM v2_tell_deliveries LIMIT 1)")
                    assert conn.execute("SELECT changes()").fetchone()[0] == 1
                conn.commit()
            await store.submit(write)
    return {"total_ms": sum(store.timings), "calls_ms": store.timings,
            "events": len(output), "calls": len(store.timings),
            "output_sha256": hashlib.sha256(json.dumps(output, sort_keys=True).encode()).hexdigest()}


async def measure(args):
    store = TimedStore(str(args.database), archive_path=str(args.archive))
    store.start()
    try:
        def setup(conn):
            conn.execute("CREATE TABLE IF NOT EXISTS history_benchmark_writes(value INTEGER)")
            conn.execute("DELETE FROM history_benchmark_writes")
            conn.execute("INSERT INTO history_benchmark_writes VALUES(0)")
            conn.commit()
        await store.submit(setup)
        cases = {}
        for case in ("unrelated_write", "delivery_write", "no_writes"):
            await walk(store, args.stream, case)  # one unrecorded warm-up
            runs = [await walk(store, args.stream, case) for _ in range(5)]
            assert all(run["events"] == 500 for run in runs), "extract cannot cover 500 events"
            assert len({run["output_sha256"] for run in runs}) == 1
            cases[case] = {"median_total_ms": statistics.median(r["total_ms"] for r in runs),
                           "max_call_ms": max(max(r["calls_ms"]) for r in runs), "runs": runs}
        return {"evidence_kind": "extract", "stream": args.stream, "cases": cases}
    finally:
        store.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--stream", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--owned-extract", action="store_true", required=True)
    args = parser.parse_args()
    result = asyncio.run(measure(args))
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: {f: v[f] for f in ("median_total_ms", "max_call_ms")}
                      for k, v in result["cases"].items()}, indent=2))


if __name__ == "__main__":
    main()
