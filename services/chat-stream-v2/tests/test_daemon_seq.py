"""Wire contract: every emitted event carries a monotonic per-service `daemon_seq`.

The shared reducer (`example-client-core/src/eventUtils.ts`,
`dedupeRecentEventsByStream`) DROPS every non-`client_origin` event whose
`daemon_seq` is not a finite number:

    const seq = Number(event?.daemon_seq);
    if (!Number.isFinite(seq) || seenSeq.has(seq)) continue;

So a daemon that emits events without a finite `daemon_seq` renders empty chats
on desktop AND mobile (both surfaces share this reducer). v1 stamped it; v2's
normalizer left it to "the daemon" and never built the stamping half. These
tests assert the daemon restores that contract on BOTH emit paths — the
`request_stream_events` backfill (`fetch_session_event_tail`) and the live
`chat.event` broadcast (`event.push` / local ingest) — and that the SAME durable
row carries the SAME value on both, so the reducer's dedupe survives a
fetch/live overlap across a reconnect.

Source of the sequence: `session_event_tail.event_id`
(`INTEGER PRIMARY KEY AUTOINCREMENT`) — the natural per-daemon monotonic id.
"""
from __future__ import annotations

import asyncio
import math
from pathlib import Path

from event_push import WIRE_VERSION, EventPush  # noqa: E402
from ingest import _identity_key  # noqa: E402
from store import Store  # noqa: E402


# --- client contract replica: the exact reducer drop rule, in Python ---------
# Mirrors `dedupeRecentEventsByStream`'s non-client_origin branch. An event the
# real client would drop must fail this predicate; an event it keeps must pass.
def _reducer_keeps(event: dict) -> bool:
    try:
        seq = float(event.get("daemon_seq"))  # JS Number(undefined | null) -> NaN
    except (TypeError, ValueError):
        return False
    return math.isfinite(seq)


def _is_wire_seq(value: object) -> bool:
    # `daemon_seq: number` in the reducer's type; the durable row id is an int.
    return isinstance(value, int) and not isinstance(value, bool)


def _ev(uuid: str, text: str = "hi", *, stream_id: str = "hostb:v2-abc") -> dict:
    return {
        "stream_id": stream_id, "provider": "claude", "kind": "USER", "text": text,
        "timestamp": "2026-08-05T00:00:00Z",
        "raw": {"jsonl_record_uuid": uuid, "jsonl_event_index": 0},
    }


def _sink(store: Store):
    broadcasts: list[dict] = []

    async def broadcast(frame: dict) -> None:
        broadcasts.append(frame)

    class _Alerts:
        def emit(self, *_a, **_k) -> None:
            pass

    ep = EventPush(store, broadcast, _Alerts(), recent_limit=500, enabled=True)

    async def _secret():
        return "TEST"

    ep._secret = _secret  # type: ignore[assignment]
    return ep, broadcasts


def _push(ep: EventPush, events):
    return {
        "type": "event.push", "request_id": 7, "push_secret": "TEST",
        "source_sha": "", "wire_version": WIRE_VERSION, "host": "hostb",
        "events": events, "high_water": {},
    }


def test_backfill_events_all_carry_finite_unique_increasing_daemon_seq() -> None:
    """`request_stream_events` backfill rows (`fetch_session_event_tail`) each
    carry a finite integer `daemon_seq`, unique and strictly increasing
    oldest-first — the reducer keeps every one instead of dropping the history."""
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            await store.open_session("hostb", "v2-abc", visibility="visible")
            for i in range(4):
                ev = _ev(f"u{i}", text=f"m{i}")
                await store.append_session_event(
                    "hostb:v2-abc", ev, identity=_identity_key(ev), limit=500,
                )
            rows = await store.fetch_session_event_tail("hostb:v2-abc", limit=500)
            assert len(rows) == 4, rows
            seqs = [r.get("daemon_seq") for r in rows]
            assert all(_is_wire_seq(s) for s in seqs), seqs
            assert all(_reducer_keeps(r) for r in rows), "reducer would drop a backfill row"
            assert seqs == sorted(seqs) and len(set(seqs)) == 4, seqs  # unique, increasing
        finally:
            store.stop()

    asyncio.run(_go())


def test_live_broadcast_events_carry_finite_daemon_seq() -> None:
    """Each live `chat.event` frame (here via the `event.push` ingest sink)
    carries a finite integer `daemon_seq`, so live turns render instead of
    being dropped by the reducer."""
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            await store.open_session("hostb", "v2-abc", visibility="visible")
            ep, casts = _sink(store)
            await ep.handle_push(_push(ep, [_ev(f"u{i}") for i in range(3)]))
            assert len(casts) == 3, casts
            events = [c["event"] for c in casts]
            assert all(c["type"] == "chat.event" for c in casts), casts
            assert all(_is_wire_seq(e.get("daemon_seq")) for e in events), events
            assert all(_reducer_keeps(e) for e in events), "reducer would drop a live frame"
        finally:
            store.stop()

    asyncio.run(_go())


def test_same_row_has_identical_daemon_seq_on_live_and_backfill() -> None:
    """The durable-dedup guarantee: a row broadcast live and the same row later
    served in backfill carry the SAME `daemon_seq`, so the reducer dedups the
    fetch/live overlap on reconnect instead of rendering duplicates."""
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            await store.open_session("hostb", "v2-abc", visibility="visible")
            ep, casts = _sink(store)
            await ep.handle_push(_push(ep, [_ev(f"u{i}", text=f"m{i}") for i in range(3)]))
            live_by_text = {c["event"]["text"]: c["event"]["daemon_seq"] for c in casts}
            rows = await store.fetch_session_event_tail("hostb:v2-abc", limit=500)
            back_by_text = {r["text"]: r["daemon_seq"] for r in rows}
            assert live_by_text == back_by_text, (live_by_text, back_by_text)
        finally:
            store.stop()

    asyncio.run(_go())


def test_backfill_page_uses_before_cursor_without_loading_the_older_tail() -> None:
    """A reconnect cursor walks fixed pages of the durable event tail."""
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            await store.open_session("hostb", "v2-abc", visibility="visible")
            for i in range(10):
                ev = _ev(f"u{i}", text=f"m{i}")
                await store.append_session_event(
                    "hostb:v2-abc", ev, identity=_identity_key(ev), limit=500,
                )
            newest = await store.fetch_session_event_page(
                "hostb:v2-abc", before_daemon_seq=None, limit=3,
            )
            assert [event["text"] for event in newest] == ["m7", "m8", "m9"]
            cursor = newest[0]["daemon_seq"]
            older = await store.fetch_session_event_page(
                "hostb:v2-abc", before_daemon_seq=cursor, limit=3,
            )
            assert [event["text"] for event in older] == ["m4", "m5", "m6"]
            assert all(event["daemon_seq"] < cursor for event in older)
        finally:
            store.stop()

    asyncio.run(_go())
