"""Ordered timestamp classifier for wire-v2 usage admission (refusal only).

One function decides every v2 usage record, fenced or unfenced; the two paths
differ only in the candidate set they pass (docs/usage_accounting.md
§ Unfenced spans). Nothing here widens an admission interval: every
uncertainty term only widens the refusal zone, and there is no tolerance
constant that admits.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from usage_accounting import iso_utc

#: Boundary precision in seconds: `'s'` for a backfilled second-resolution
#: `created_at`, `'ms'` for a stamp the history table wrote itself.
PRECISION_S = {'s': 1.0, 'ms': 0.001}
#: Millisecond truncation of `transcript_ts` (0.001) plus `server_now` (0.001).
TRUNCATION_S = 0.002
#: A send sample older than this, by the daemon's own clock, is no evidence.
CLOCK_MAX_AGE_S = 600.0

TERMINAL_REASONS = frozenset({
    'timestamp_missing', 'no_candidate_generation', 'outside_all_generations',
    'generation_overlap', 'boundary_uncertain',
})
TRANSIENT_REASONS = frozenset({'clock_unavailable'})


@dataclass(frozen=True)
class Generation:
    """One `v2_session_generation_history` row as classifier input."""
    generation: str
    created_at: float
    created_precision: str = 'ms'
    closed_at: float | None = None
    closed_precision: str = 'ms'


@dataclass(frozen=True)
class ClockSample:
    """`offset_s = server_now - (t_send_wall + rtt_s/2)` on the satellite."""
    offset_s: float
    rtt_s: float
    server_now: float


def epoch(value: Any) -> float | None:
    """Epoch seconds (ms exact) of an offset-bearing timestamp; naive -> None."""
    text = iso_utc(value)
    if text is None:
        return None
    return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()


def iso_ms(seconds: float) -> str:
    """Fixed-width millisecond UTC stamp (``...SS.fffZ``) written by the history
    table itself, so SQL string order is time order."""
    from datetime import timezone

    millis = int(round(seconds * 1000))
    moment = datetime.fromtimestamp(millis // 1000, tz=timezone.utc)
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{millis % 1000:03d}Z"


def _finite(value: Any) -> float | None:
    """A finite float from a JSON number; bool, oversized or non-finite -> None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def parse_clock(value: Any) -> ClockSample | None:
    """Validate a wire clock sample; anything malformed is no sample."""
    if not isinstance(value, dict):
        return None
    offset, rtt = _finite(value.get('offset_s')), _finite(value.get('rtt_s'))
    server_now = epoch(value.get('server_now'))
    if (
        server_now is None or not math.isfinite(server_now)
        or offset is None or rtt is None
        or rtt < 0 or abs(offset) > 10 ** 7 or rtt > 3600
    ):
        return None
    return ClockSample(offset, rtt, server_now)


def generation_from_row(row: Any) -> Generation | None:
    """History row -> classifier input; an unparseable stamp is no candidate."""
    created = epoch(row['created_at'])
    if created is None:
        return None
    closed = epoch(row['closed_at']) if row['closed_at'] else None
    if row['closed_at'] and closed is None:
        return None
    precision = row['precision'] if row['precision'] in PRECISION_S else 's'
    return Generation(str(row['generation']), created, precision, closed, 'ms')


def clock_band(capture: ClockSample | None, send: ClockSample) -> tuple[float, float]:
    """Hull of the capture and send offset intervals -> (midpoint, half-width).

    The band bounds the offset only at the two sampled instants; using it for
    the unobserved capture time is a stated assumption, not protection.
    """
    samples = [sample for sample in (capture, send) if sample is not None]
    low = min(sample.offset_s - sample.rtt_s / 2 for sample in samples)
    high = max(sample.offset_s + sample.rtt_s / 2 for sample in samples)
    return (low + high) / 2, (high - low) / 2


def _status(ts_c: float, generation: Generation, u_clock: float) -> str:
    """OUT, NEAR (within U of a boundary) or IN; an open row has no end."""
    u_start = u_clock + PRECISION_S[generation.created_precision] + TRUNCATION_S
    if ts_c < generation.created_at - u_start:
        return 'OUT'
    near = ts_c <= generation.created_at + u_start
    if generation.closed_at is not None:
        u_end = u_clock + PRECISION_S[generation.closed_precision] + TRUNCATION_S
        if ts_c > generation.closed_at + u_end:
            return 'OUT'
        near = near or ts_c >= generation.closed_at - u_end
    return 'NEAR' if near else 'IN'


def classify(
    ts: float | None,
    candidates: list[Generation],
    send: ClockSample | None,
    receipt_now: float,
    capture: ClockSample | None = None,
) -> tuple[str, str | None]:
    """First match wins, identical on both v2 paths -> (outcome, generation)."""
    ts = _finite(ts)
    if ts is None:
        return 'timestamp_missing', None
    if not candidates:
        return 'no_candidate_generation', None
    if send is None or receipt_now - send.server_now > CLOCK_MAX_AGE_S:
        return 'clock_unavailable', None
    offset, u_clock = clock_band(capture, send)
    ts_c = ts + offset
    status = {generation.generation: _status(ts_c, generation, u_clock) for generation in candidates}
    possible = [name for name, value in status.items() if value != 'OUT']
    if not possible:
        return 'outside_all_generations', None
    if len(possible) > 1:
        # Before boundary: the record could belong to more than one
        # generation, so it is never credited to either.
        return 'generation_overlap', None
    if status[possible[0]] == 'NEAR':
        return 'boundary_uncertain', None
    return 'credited', possible[0]
