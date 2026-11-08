"""Authoritative time arithmetic shared by offline rollups and lane projections."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

ACTIVITY_GAP_S = 600.0
PROXY_LABEL = 'proxy: union of inter-response gaps <= 10 min from provenance observed_at (Claude records)'
CODEX_PROXY_LABEL = 'proxy: union of inter-response gaps <= 10 min from placed Codex response observed_at'

def iso(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def hours(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds / 3600.0, 4)


def union(intervals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted((s, e) for s, e in intervals if e > s):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def span(intervals: Iterable[tuple[float, float]]) -> float:
    return sum(e - s for s, e in union(intervals))


def intersect(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = []
    for s1, e1 in union(a):
        for s2, e2 in union(b):
            s, e = max(s1, s2), min(e1, e2)
            if e > s:
                out.append((s, e))
    return union(out)


def time_metrics(ctx: Any, spec_id: str, streams: list[str]) -> dict[str, Any]:
    src = ctx.src
    seats = [src.seats[s] for s in streams if s in src.seats]
    item = src.work_items.get(spec_id)
    out: dict[str, Any] = {'seats': len(seats)}
    starts = [s.created for s in seats if s.created is not None]
    first_open = min(starts) if starts else None
    open_seats = [s for s in seats if s.status == 'open' and s.closed is None]
    no_close = [s for s in seats if s.closed is None and s not in open_seats]
    closes = [s.closed for s in seats if s.closed is not None]
    if not seats or first_open is None:
        delivery: dict[str, Any] = {'censored': True, 'elapsed_so_far_h': None, 'reason': 'no_seats'}
    else:
        reason = None
        if item is None or not item.completed:
            reason = 'not_completed'
        elif open_seats:
            reason = 'open_seat'
        elif no_close:
            reason = 'no_close'
        if reason:
            last = src.now if (open_seats or no_close) else max(closes)
            delivery = {'censored': True, 'elapsed_so_far_h': hours(last - first_open), 'reason': reason}
        else:
            delivery = {'censored': False, 'value_h': hours(max(closes) - first_open), 'endpoint': 'close',
                        'first_open': iso(first_open), 'last_close': iso(max(closes))}
    out['elapsed_delivery_h'] = delivery
    seat_spans = union((s.created, s.closed if s.closed is not None else src.now)
                       for s in seats if s.created is not None)
    stream_set = set(streams)
    waits = [(a, b) for producer, spec, a, b in src.cards if producer in stream_set or spec == spec_id]
    waits += [(a, b) for owner, a, b in src.holds if owner in stream_set]
    out['known_wait_h'] = hours(span(intersect(waits, seat_spans))) if seat_spans else None
    claude = [r for s in streams for r in ctx.records_by_stream.get(s, ()) if r.provider == 'claude']
    if not claude or any(r.observed is None for r in claude):
        out['activity_proxy_h'] = {'value': None, 'label': PROXY_LABEL,
                                   'reason': 'no Claude records' if not claude else 'provenance missing'}
    else:
        gaps = []
        for stream in streams:
            times = sorted(r.observed for r in ctx.records_by_stream.get(stream, ()) if r.provider == 'claude')
            gaps += [(a, b) for a, b in zip(times, times[1:]) if b - a <= ACTIVITY_GAP_S]
        out['activity_proxy_h'] = {'value': hours(span(gaps)), 'label': PROXY_LABEL}
    codex = [s for stream in streams for s in ctx.codex_by_stream.get(stream, ())]
    if not codex:
        out['codex_activity_proxy_h'] = {'value': None, 'label': CODEX_PROXY_LABEL, 'reason': 'no Codex sessions'}
    elif any(s.masses()['placed'] != s.total for s in codex):
        out['codex_activity_proxy_h'] = {'value': None, 'label': CODEX_PROXY_LABEL,
                                         'reason': 'Codex mass outside placed responses'}
    else:
        gaps = []
        for stream in streams:
            times = sorted(r.observed for s in ctx.codex_by_stream.get(stream, ()) for r in s.placed)
            gaps += [(a, b) for a, b in zip(times, times[1:]) if b - a <= ACTIVITY_GAP_S]
        out['codex_activity_proxy_h'] = {'value': hours(span(gaps)), 'label': CODEX_PROXY_LABEL}
    qa = sorted(at for stream, verdict, at in src.reports if stream in stream_set and verdict and at is not None)
    out['qa_rounds'] = sum(1 for stream, verdict, _ in src.reports if stream in stream_set and verdict)
    out['time_to_first_qa_h'] = hours(qa[0] - first_open) if qa and first_open is not None else None
    return out


