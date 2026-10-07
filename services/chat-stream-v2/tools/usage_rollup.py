#!/usr/bin/env python3
"""Read-only usage rollup and calibration over the Thoth ledger (Claude only).

Contract: docs/usage_accounting.md § Rollup and calibration. Reads
``sessions.db``, ``sessions_archive.db``, ``notifications.db`` and
``usage_history.jsonl`` with ``mode=ro`` and work-item frontmatter from shared
memory; the only write is ``--calibrate`` output (``calibration.json``, 0600).
Codex streams are listed as deferred (spec_pentacle__usage_codex_rollup_and_calibration_2026_10).
Stdlib only and Python 3.9 compatible (Thoth's system interpreter).

    python3 tools/usage_rollup.py --spec <spec_id> --json
    python3 tools/usage_rollup.py --project <epic_id|repo> --json
    python3 tools/usage_rollup.py --comparables --repo pentacle-mobile [--kind feature]
    python3 tools/usage_rollup.py --calibrate --json --redact
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from store_specs import normalize_spec_ids  # noqa: E402
from usage_history import HISTORY_FILENAME, dedupe_key  # noqa: E402

SCHEMA_VERSION = 1
DATA_DIR = '~/.local/share/pentacle-stream'
MEMORY_ROOT = '~/agent-workspace/triforce-memory'
CONFIG_NAME = 'calibration_config.json'
CALIBRATION_NAME = 'calibration.json'
PRICING_PATH = Path(__file__).resolve().with_name('usage_rollup_config') / 'pricing.json'
REPO_ROOT = Path(__file__).resolve().parents[3]

BUCKETS = ('uncached_input', 'cache_write', 'cache_read', 'output')
KINDS = ('feature', 'defect', 'infra', 'convention', 'analysis')
KIND_ALIASES = {'bug': 'defect'}
QUOTA = 'seven_day'
QUOTA_MINUTES = 10080
ACTIVITY_GAP_S = 600.0
MAX_INTERVAL_S = 86400.0
METHOD_A_MIN_COVERAGE = 0.90
SAMPLE_MIN_COVERAGE = 0.95
METHOD_B_MIN_SAMPLES = 10
METHOD_B_MIN_SPAN = 30
DEFAULT_UNPLACEABLE_THRESHOLD = 0.01
PROXY_LABEL = 'proxy: union of inter-response gaps <= 10 min from provenance observed_at (Claude records)'
DOLLARS_LABEL = 'API-equivalent weighted proxy, not billing'


class RollupError(Exception):
    pass


# --------------------------------------------------------------------------- time

_ISO = re.compile(r'^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?)?$')


def parse_ts(value: Any) -> float | None:
    """Epoch seconds for UTC ISO-8601 (``Z``/offset/naive-as-UTC; a bare date is 00:00Z) or an epoch number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    match = _ISO.match(str(value).strip())
    if not match:
        return None
    year, month, day, hour, minute, second, frac, zone = match.groups()
    try:
        moment = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0),
                          int(second or 0), tzinfo=timezone.utc)
    except ValueError:
        return None
    seconds = moment.timestamp() + (float('0.' + frac) if frac else 0.0)
    if zone and zone != 'Z':
        sign = 1 if zone[0] == '+' else -1
        digits = zone[1:].replace(':', '')
        seconds -= sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    return seconds


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


# --------------------------------------------------------------------------- pricing

class Pricing:
    def __init__(self, data: dict[str, Any]):
        self.version = str(data.get('version') or 'unversioned')
        classes = data.get('classes') or {}
        self.rates: dict[str, dict[str, float]] = {}
        for model, entry in (data.get('models') or {}).items():
            rates = classes.get(entry.get('class')) if isinstance(entry, dict) else None
            if not isinstance(rates, dict) or any(b not in rates for b in BUCKETS):
                raise RollupError(f'pricing: model {model!r} has no complete class rates')
            self.rates[model] = {b: float(rates[b]) for b in BUCKETS}

    @classmethod
    def load(cls, path: Path) -> 'Pricing':
        return cls(json.loads(Path(path).read_text()))

    def priced(self, model: str | None) -> bool:
        return model in self.rates

    def dollars(self, model: str | None, tokens: dict[str, int]) -> float | None:
        rates = self.rates.get(model) if model else None
        if rates is None:
            return None
        return sum(tokens.get(b, 0) * rates[b] for b in BUCKETS) / 1_000_000.0


# --------------------------------------------------------------------------- sources

class Seat:
    __slots__ = ('stream_id', 'host', 'provider', 'created', 'closed', 'status', 'specs',
                 'handoff_from', 'source')

    def __init__(self, row: dict[str, Any], source: str):
        self.stream_id = f"{row['host']}:{row['session_name']}"
        self.host = row['host']
        self.provider = row.get('provider')
        self.created = parse_ts(row.get('created_at'))
        self.closed = parse_ts(row.get('closed_at'))
        self.status = row.get('status') or ('closed' if self.closed else 'open')
        self.specs = normalize_spec_ids(row.get('spec_ids'), row.get('spec_id'))
        self.handoff_from = row.get('handoff_from_stream_id') or None
        self.source = source


class Rec:
    __slots__ = ('host', 'provider', 'native', 'key', 'stream_id', 'tokens', 'total', 'has_row',
                 'observed', 'model', 'account', 'account_state', 'cls', 'dollars')

    def __init__(self, row: sqlite3.Row, pricing: Pricing):
        self.host, self.provider, self.native = row['host'], row['provider'], row['native_session_id']
        self.key, self.stream_id = row['record_key'], row['stream_id']
        try:
            raw = json.loads(row['tokens'])
        except (TypeError, ValueError):
            raw = {}
        self.tokens = {k: int(v) for k, v in raw.items()
                       if isinstance(v, (int, float)) and not isinstance(v, bool)}
        self.total = sum(self.tokens.values())
        self.has_row = bool(row['has_row'])
        self.observed = parse_ts(row['observed_at']) if self.has_row else None
        self.model = row['model'] if self.has_row else None
        if row['account_source'] is None:
            self.account, self.account_state = None, 'unknown'
        elif row['conflict']:
            self.account, self.account_state = None, 'conflict'
        elif row['account_id']:
            self.account, self.account_state = row['account_id'], 'known'
        else:
            self.account, self.account_state = None, 'unknown'
        self.dollars = pricing.dollars(self.model, self.tokens) if self.provider == 'claude' else None
        # Exclusive partition (spec § Coverage): untimed first, then unpriced, then unknown account.
        if self.provider != 'claude':
            self.cls = 'deferred'
        elif self.observed is None:
            self.cls = 'untimed'
        elif self.dollars is None:
            self.cls = 'unpriced'
        elif self.account is None:
            self.cls = 'unknown_account'
        else:
            self.cls = 'measured'

    @property
    def account_label(self) -> str:
        return self.account if self.account else self.account_state


class WorkItem:
    __slots__ = ('id', 'status', 'completed_at', 'epic', 'tags', 'kind', 'path')

    def __init__(self, meta: dict[str, Any], path: Path):
        self.id = normalize_spec_ids(meta.get('id'))[0] if meta.get('id') else None
        self.status = str(meta.get('status') or '')
        self.completed_at = parse_ts(meta.get('completed_at'))
        self.epic = meta.get('epic') or None
        tags = meta.get('tags') if isinstance(meta.get('tags'), list) else []
        self.tags = [str(t) for t in tags]
        self.kind = next((KIND_ALIASES.get(t, t) for t in self.tags if KIND_ALIASES.get(t, t) in KINDS), None)
        self.path = str(path)

    @property
    def completed(self) -> bool:
        return self.status == 'completed' and self.completed_at is not None


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Flat YAML frontmatter: ``key: value`` scalars and ``- item`` lists."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        return {}
    meta: dict[str, Any] = {}
    key = None
    for line in lines[1:]:
        if line.strip() == '---':
            break
        if line.startswith('- ') or line.startswith('  - '):
            if key is not None:
                if not isinstance(meta.get(key), list):
                    meta[key] = []
                meta[key].append(_scalar(line.split('- ', 1)[1]))
            continue
        if ':' in line and not line.startswith((' ', '\t')):
            key, _, value = line.partition(':')
            key = key.strip()
            meta[key] = _scalar(value) if value.strip() else []
    return meta


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '\'"':
        return value[1:-1]
    return value


def load_work_items(work_root: Path) -> dict[str, WorkItem]:
    items: dict[str, WorkItem] = {}
    if not work_root.is_dir():
        return items
    for pattern in ('*/spec.md', '*/*/spec.md'):
        for path in sorted(work_root.glob(pattern)):
            try:
                meta = parse_frontmatter(path.read_text(errors='replace'))
            except OSError:
                continue
            item = WorkItem(meta, path)
            if item.id and (item.id not in items or item.completed):
                items[item.id] = item
    return items


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path).absolute().as_uri() + '?mode=ro', uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


RECORDS_SQL = """
SELECT r.host, r.provider, r.native_session_id, r.record_key, r.stream_id, r.tokens,
       p.record_key IS NOT NULL AS has_row, p.observed_at, p.model,
       i.account_id, i.account_source, i.conflict
FROM v2_usage_records r
LEFT JOIN v2_usage_provenance p ON p.host=r.host AND p.provider=r.provider
     AND p.native_session_id=r.native_session_id AND p.record_key=r.record_key
LEFT JOIN v2_usage_identity i ON i.host=r.host AND i.provider=r.provider
     AND i.native_session_id=r.native_session_id
"""


class Sources:
    """Everything the rollup reads, loaded once, read-only."""

    def __init__(self, *, data_dir: Path, work_root: Path, pricing: Pricing, now: float):
        self.now = now
        self.pricing = pricing
        self.seats: dict[str, Seat] = {}
        self.records: list[Rec] = []
        self.reports: list[tuple[str, str | None, float | None]] = []
        self.cards: list[tuple[str | None, str | None, float, float]] = []
        self.holds: list[tuple[str, float, float]] = []
        self.history: list[dict[str, Any]] = []
        live = data_dir / 'sessions.db'
        if not live.exists():
            raise RollupError(f'no ledger at {live}')
        for name, source in (('sessions_archive.db', 'archive'), ('sessions.db', 'live')):
            path = data_dir / name
            if path.exists():
                self._load_db(_ro(path), source)
        notifications = data_dir / 'notifications.db'
        if notifications.exists():
            self._load_cards(_ro(notifications))
        history = data_dir / HISTORY_FILENAME
        if history.exists():
            for line in history.read_text(errors='replace').splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    self.history.append(entry)
        self.work_items = load_work_items(work_root)

    def _load_db(self, conn: sqlite3.Connection, source: str) -> None:
        tables = _tables(conn)
        if 'sessions' in tables:
            for row in conn.execute('SELECT * FROM sessions'):
                seat = Seat(dict(row), source)
                self.seats[seat.stream_id] = seat  # live wins over archive (loaded second)
        if {'v2_usage_records', 'v2_usage_provenance', 'v2_usage_identity'} <= tables:
            self.records.extend(Rec(row, self.pricing) for row in conn.execute(RECORDS_SQL))
        if 'v2_reports' in tables:
            for row in conn.execute('SELECT from_stream_id, qa_verdict, ingested_at, created_at FROM v2_reports'):
                at = parse_ts(row['ingested_at']) or parse_ts(row['created_at'])
                self.reports.append((row['from_stream_id'], row['qa_verdict'], at))
        if 'holds' in tables:
            for row in conn.execute('SELECT owner_stream, acquired_at, expires_at, released_at FROM holds'):
                start = parse_ts(row['acquired_at'])
                if start is None:
                    continue
                end = parse_ts(row['released_at'])
                if end is None:
                    expires = parse_ts(row['expires_at'])
                    end = min(expires, self.now) if expires is not None else self.now
                self.holds.append((row['owner_stream'], start, end))
        conn.close()

    def _load_cards(self, conn: sqlite3.Connection) -> None:
        if 'agent_questions' in _tables(conn):
            for row in conn.execute('SELECT producer_stream_id, spec_id, created_at, updated_at, answered_at, state '
                                    'FROM agent_questions'):
                start = parse_ts(row['created_at'])
                if start is None:
                    continue
                end = parse_ts(row['answered_at'])
                if end is None:
                    end = self.now if row['state'] == 'open' else (parse_ts(row['updated_at']) or self.now)
                spec = normalize_spec_ids(row['spec_id'])[0] if row['spec_id'] else None
                self.cards.append((row['producer_stream_id'], spec, start, end))
        conn.close()


# --------------------------------------------------------------------------- attribution

def repo_of(spec_id: str) -> str:
    body = spec_id[len('spec_'):] if spec_id.startswith('spec_') else spec_id
    return body.split('__', 1)[0].replace('-', '_')


def norm_repo(value: str) -> str:
    return value.strip().replace('-', '_')


def attribute(seats: dict[str, Seat]) -> dict[str, tuple[str | None, bool]]:
    """stream -> (spec, folded): first spec id; an unattributed predecessor folds into its successor's spec."""
    successors: dict[str, list[Seat]] = {}
    for seat in seats.values():
        if seat.handoff_from:
            successors.setdefault(seat.handoff_from, []).append(seat)
    resolved: dict[str, tuple[str | None, bool]] = {}

    def resolve(stream_id: str, stack: frozenset) -> str | None:
        seat = seats.get(stream_id)
        if seat is not None and seat.specs:
            return seat.specs[0]
        for succ in sorted(successors.get(stream_id, ()), key=lambda s: (s.created or 0, s.stream_id)):
            if succ.stream_id in stack:
                continue
            spec = resolve(succ.stream_id, stack | {succ.stream_id})
            if spec:
                return spec
        return None

    for stream_id, seat in seats.items():
        if seat.specs:
            resolved[stream_id] = (seat.specs[0], False)
        else:
            spec = resolve(stream_id, frozenset({stream_id}))
            resolved[stream_id] = (spec, spec is not None)
    return resolved


# --------------------------------------------------------------------------- calibration lookup

class Calibration:
    def __init__(self, data: dict[str, Any] | None, path: Path | None):
        self.path = path
        self.coefficients: dict[str, dict[str, Any]] = {}
        for entry in (data or {}).get('entries') or []:
            if entry.get('provider') == 'claude' and entry.get('quota') == QUOTA and entry.get('account_id'):
                self.coefficients[entry['account_id']] = entry

    def weekly_pct(self, account: str, dollars: float) -> dict[str, Any]:
        entry = self.coefficients.get(account)
        if entry is None:
            reason = 'no calibration' if self.path is None else 'account not in calibration'
            return {'value': None, 'quota': QUOTA, 'reason': reason}
        if entry.get('coefficient') is None:
            return {'value': None, 'quota': QUOTA,
                    'reason': entry.get('reason') or f"calibration {entry.get('status') or 'unavailable'}"}
        return {'value': round(dollars / entry['coefficient'], 4), 'quota': QUOTA,
                'basis': entry.get('basis'), 'usd_per_pct': entry['coefficient']}


# --------------------------------------------------------------------------- rollup

class Context:
    def __init__(self, sources: Sources, calibration: Calibration, *,
                 since: float | None = None, until: float | None = None):
        self.src = sources
        self.cal = calibration
        self.since, self.until = since, until
        self.attribution = attribute(sources.seats)
        self.records_by_stream: dict[str, list[Rec]] = {}
        for rec in sources.records:
            self.records_by_stream.setdefault(rec.stream_id, []).append(rec)
        self.streams_by_spec: dict[str, list[str]] = {}
        for stream_id, (spec, _folded) in self.attribution.items():
            if spec:
                self.streams_by_spec.setdefault(spec, []).append(stream_id)

    def stream_spec(self, stream_id: str) -> str | None:
        return self.attribution.get(stream_id, (None, False))[0]

    def in_range(self, rec: Rec) -> bool:
        if self.since is None and self.until is None:
            return True
        if rec.observed is None:
            return False
        return ((self.since is None or rec.observed >= self.since)
                and (self.until is None or rec.observed < self.until))

    def range_excluded(self, recs: list[Rec]) -> int:
        if self.since is None and self.until is None:
            return 0
        return sum(1 for r in recs if r.observed is None)


def summarize_claude(recs: list[Rec], ctx: Context) -> dict[str, Any]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    partition = dict.fromkeys(('measured', 'untimed', 'unpriced', 'unknown_account'), 0)
    tokens = dict.fromkeys(BUCKETS, 0)
    accounts: dict[str, dict[str, Any]] = {}
    dollars = 0.0
    with_row = 0
    for rec in recs:
        model = rec.model or 'unknown'
        group = groups.setdefault((model, rec.account_label), {
            'model': model, 'account_id': rec.account_label, 'records': 0,
            'tokens': dict.fromkeys(BUCKETS, 0), 'dollars': 0.0, 'priced': rec.dollars is not None})
        group['records'] += 1
        for bucket in BUCKETS:
            group['tokens'][bucket] += rec.tokens.get(bucket, 0)
            tokens[bucket] += rec.tokens.get(bucket, 0)
        partition[rec.cls] += rec.total
        with_row += rec.has_row
        acct = accounts.setdefault(rec.account_label, {'account_id': rec.account_label, 'tokens': 0,
                                                       'dollars': 0.0, 'unpriced_tokens': 0})
        acct['tokens'] += rec.total
        if rec.dollars is None:
            acct['unpriced_tokens'] += rec.total
        else:
            group['dollars'] += rec.dollars
            acct['dollars'] += rec.dollars
            dollars += rec.dollars
    for group in groups.values():
        group['dollars'] = round(group['dollars'], 4) if group['priced'] else None
        if not group['priced']:
            group['bucket'] = 'unpriced'
    for acct in accounts.values():
        acct['dollars'] = round(acct['dollars'], 4)
        if acct['account_id'] in ('unknown', 'conflict'):
            acct['weekly_pct'] = {'value': None, 'quota': QUOTA, 'reason': f"account {acct['account_id']}"}
        else:
            acct['weekly_pct'] = ctx.cal.weekly_pct(acct['account_id'], acct['dollars'])
    total = sum(partition.values())
    return {
        'provider': 'claude',
        'records': len(recs),
        'tokens': tokens,
        'total_tokens': total,
        'dollars': round(dollars, 4),
        'dollars_label': DOLLARS_LABEL,
        'unpriced_tokens': sum(r.total for r in recs if r.dollars is None),
        'partition_tokens': partition,
        'provenance_row_coverage': round(with_row / len(recs), 6) if recs else None,
        # Scope-level coverage: untimed records belong to the scope by stream, so they stay in the denominator.
        'measured_coverage': round(partition['measured'] / total, 6) if total else None,
        'by_model_account': sorted(groups.values(), key=lambda g: (g['model'], g['account_id'])),
        'by_account': sorted(accounts.values(), key=lambda a: a['account_id']),
    }


def summarize_codex(recs: list[Rec]) -> dict[str, Any]:
    totals: dict[str, int] = {}
    for rec in recs:
        for key, value in rec.tokens.items():
            totals[key] = totals.get(key, 0) + value
    return {'provider': 'codex', 'status': 'deferred', 'priced': False, 'windows': None,
            'streams': sorted({r.stream_id for r in recs}), 'records': len(recs),
            'ledger_cumulative_tokens': totals,
            'follow_up': 'spec_pentacle__usage_codex_rollup_and_calibration_2026_10'}


def time_metrics(ctx: Context, spec_id: str, streams: list[str]) -> dict[str, Any]:
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
    qa = sorted(at for stream, verdict, at in src.reports if stream in stream_set and verdict and at is not None)
    out['qa_rounds'] = sum(1 for stream, verdict, _ in src.reports if stream in stream_set and verdict)
    out['time_to_first_qa_h'] = hours(qa[0] - first_open) if qa and first_open is not None else None
    return out


def spec_rollup(ctx: Context, spec_id: str) -> dict[str, Any]:
    spec_id = normalize_spec_ids(spec_id)[0]
    streams = sorted(ctx.streams_by_spec.get(spec_id, ()))
    recs = [r for s in streams for r in ctx.records_by_stream.get(s, ())]
    item = ctx.src.work_items.get(spec_id)
    return {
        'spec_id': spec_id,
        'work_item': None if item is None else {'status': item.status, 'completed_at': iso(item.completed_at),
                                                'epic': item.epic, 'kind': item.kind, 'path': item.path},
        'streams': streams,
        'folded_streams': sorted(s for s in streams if ctx.attribution[s][1]),
        'claude': summarize_claude([r for r in recs if r.provider == 'claude' and ctx.in_range(r)], ctx),
        'codex': summarize_codex([r for r in recs if r.provider == 'codex']),
        'untimed_outside_range_records': ctx.range_excluded([r for r in recs if r.provider == 'claude']),
        'completeness': round(sum(r.has_row for r in recs if r.provider == 'claude')
                              / max(1, sum(1 for r in recs if r.provider == 'claude')), 6),
        'time': time_metrics(ctx, spec_id, streams),
    }


def fleet_totals(ctx: Context) -> dict[str, Any]:
    """Attributed vs unattributed Claude totals per account (always reported)."""
    rows: dict[str, dict[str, Any]] = {}
    for rec in ctx.src.records:
        if rec.provider != 'claude' or not ctx.in_range(rec):
            continue
        row = rows.setdefault(rec.account_label, {
            'account_id': rec.account_label,
            'attributed': {'tokens': 0, 'dollars': 0.0, 'records': 0},
            'unattributed': {'tokens': 0, 'dollars': 0.0, 'records': 0}})
        side = row['attributed' if ctx.stream_spec(rec.stream_id) else 'unattributed']
        side['tokens'] += rec.total
        side['records'] += 1
        side['dollars'] += rec.dollars or 0.0
    for row in rows.values():
        for side in ('attributed', 'unattributed'):
            row[side]['dollars'] = round(row[side]['dollars'], 4)
    return {'provider': 'claude', 'by_account': sorted(rows.values(), key=lambda r: r['account_id'])}


def project_members(ctx: Context, project: str) -> list[str]:
    if project.startswith('epic_'):
        return sorted(i.id for i in ctx.src.work_items.values() if i.epic == project)
    repo = norm_repo(project)
    specs = set(ctx.streams_by_spec) | set(ctx.src.work_items)
    return sorted(s for s in specs if repo_of(s) == repo)


def project_rollup(ctx: Context, project: str) -> dict[str, Any]:
    members = project_members(ctx, project)
    streams = sorted({s for spec in members for s in ctx.streams_by_spec.get(spec, ())})
    recs = [r for s in streams for r in ctx.records_by_stream.get(s, ())]
    return {
        'project': project,
        'kind': 'epic' if project.startswith('epic_') else 'repo',
        'member_specs': members,
        'streams': len(streams),
        'claude': summarize_claude([r for r in recs if r.provider == 'claude' and ctx.in_range(r)], ctx),
        'codex': summarize_codex([r for r in recs if r.provider == 'codex']),
        'specs': [spec_rollup(ctx, spec) for spec in members],
    }


def _quartiles(values: list[float]) -> dict[str, float] | None:
    if len(values) < 2:
        return None
    p25, p50, p75 = statistics.quantiles(sorted(values), n=4, method='inclusive')
    return {'p25': round(p25, 4), 'median': round(p50, 4), 'p75': round(p75, 4)}


def comparables(ctx: Context, repo: str, kind: str | None, limit: int) -> dict[str, Any]:
    kind = KIND_ALIASES.get(kind, kind) if kind else None
    candidates = [i for i in ctx.src.work_items.values()
                  if i.completed and repo_of(i.id) == norm_repo(repo) and (kind is None or i.kind == kind)]
    candidates.sort(key=lambda i: (-(i.completed_at or 0), i.id))
    rows, excluded = [], []
    for item in candidates:
        roll = spec_rollup(ctx, item.id)
        delivery = roll['time']['elapsed_delivery_h']
        if delivery.get('censored'):
            excluded.append({'spec_id': item.id, 'reason': delivery['reason']})
            continue
        pcts = {a['account_id']: a['weekly_pct']['value'] for a in roll['claude']['by_account']}
        row = {'spec_id': item.id, 'kind': item.kind, 'completed_at': iso(item.completed_at),
               'elapsed_delivery_h': delivery['value_h'], 'dollars': roll['claude']['dollars'],
               'weekly_pct': pcts}
        if not roll['claude']['records']:  # no Claude evidence: a zero would bias the quantiles low
            row['dollars'] = None
            row['dollars_reason'] = 'codex_only_deferred' if roll['codex']['records'] else 'no_usage_records'
        rows.append(row)
        if len(rows) >= limit:
            break
    out: dict[str, Any] = {'repo': repo, 'kind': kind, 'count': len(rows), 'rows': rows, 'excluded': excluded}
    if len(rows) < 3:
        out['status'] = 'insufficient_comparables'
        return out
    out['status'] = 'ok'
    out['elapsed_delivery_h'] = _quartiles([r['elapsed_delivery_h'] for r in rows])
    out['dollars'] = _quartiles([r['dollars'] for r in rows if r['dollars'] is not None])
    accounts = sorted({a for r in rows for a in r['weekly_pct']})
    out['weekly_pct'] = {a: _quartiles([r['weekly_pct'][a] for r in rows if r['weekly_pct'].get(a) is not None])
                         for a in accounts}
    return out


# --------------------------------------------------------------------------- calibration

class Account:
    def __init__(self, entry: dict[str, Any]):
        self.id = entry.get('account_id')
        self.label = entry.get('label')
        self.provider = entry.get('provider') or 'claude'
        self.role = entry.get('role')  # explicit only: a missing role is never fitted
        hosts = entry.get('hosts')
        self.hosts = set(hosts) if isinstance(hosts, list) else None
        self.transfer_from = entry.get('transfer_from')
        self.justification = entry.get('justification')

    def may_own(self, host: str) -> bool:
        return self.hosts is None or host in self.hosts


def load_config(path: Path) -> dict[str, Any] | None:
    path = Path(path).expanduser()
    if not path.exists():
        return None
    resolved = path.resolve()
    if REPO_ROOT in resolved.parents:
        raise RollupError('calibration config must live outside the repository (private, on Thoth)')
    if os.stat(resolved).st_mode & 0o077:
        raise RollupError(f'calibration config {path} must be mode 0600')
    data = json.loads(resolved.read_text())
    if not isinstance(data, dict) or not isinstance(data.get('accounts', []), list):
        raise RollupError('calibration config: accounts must be a list')
    return data


class Timeline:
    """Timed Claude records ordered by observed_at, for window and interval slices."""

    def __init__(self, records: list[Rec]):
        timed = sorted((r for r in records if r.provider == 'claude' and r.observed is not None),
                       key=lambda r: r.observed)
        self.recs = timed
        self.times = [r.observed for r in timed]

    def window(self, start: float, end: float) -> list[Rec]:  # [start, end)
        return self.recs[bisect.bisect_left(self.times, start):bisect.bisect_left(self.times, end)]

    def interval(self, start: float, end: float) -> list[Rec]:  # (start, end]
        return self.recs[bisect.bisect_right(self.times, start):bisect.bisect_right(self.times, end)]


def account_partition(acct: Account, recs: Iterable[Rec]) -> dict[str, Any]:
    """Per-(provider, account, quota) partition; ambiguous mass sits in every candidate account's denominator."""
    measured = unpriced = unknown = 0
    dollars = 0.0
    for rec in recs:
        if rec.cls == 'measured':
            if rec.account == acct.id:
                measured += rec.total
                dollars += rec.dollars or 0.0
        elif rec.cls == 'unpriced':
            if rec.account == acct.id or (rec.account is None and acct.may_own(rec.host)):
                unpriced += rec.total
        elif rec.cls == 'unknown_account' and acct.may_own(rec.host):
            unknown += rec.total
    denom = measured + unpriced + unknown
    return {'tokens': {'measured': measured, 'unpriced': unpriced, 'unknown_account': unknown},
            'dollars': round(dollars, 6), 'coverage': (measured / denom) if denom else None}


def _coverage_fields(part: dict[str, Any], eligible: bool) -> dict[str, Any]:
    cov = part['coverage']
    if eligible:
        return {'measured_coverage': None if cov is None else round(cov, 6)}
    return {'measured_coverage': None,
            'measured_coverage_withheld': 'unplaceable_mass above threshold (bounded-coverage assumption fails)',
            'measured_coverage_if_bounded': None if cov is None else round(cov, 6)}


def unplaceable_summary(records: list[Rec], threshold: float) -> dict[str, Any]:
    claude = [r for r in records if r.provider == 'claude']
    total = sum(r.total for r in claude)
    mass: dict[tuple[str, str], int] = {}
    for rec in claude:
        if rec.cls == 'untimed':
            key = (rec.host, rec.account_label)
            mass[key] = mass.get(key, 0) + rec.total
    unplaceable = sum(mass.values())
    ratio = (unplaceable / total) if total else 0.0
    return {'provider': 'claude', 'unplaceable_tokens': unplaceable, 'provider_total_tokens': total,
            'ratio': round(ratio, 6), 'threshold': threshold, 'passes': ratio <= threshold,
            'by_host_account': [{'host': h, 'account_id': a, 'tokens': t} for (h, a), t in sorted(mass.items())]}


def windows_for(config: dict[str, Any], first: float, now: float) -> list[tuple[float, float]]:
    spec = config.get('windows') or {}
    anchor = parse_ts(spec.get('anchor'))
    if anchor is None:
        raise RollupError('calibration config: windows.anchor is required (UTC ISO)')
    length = float(spec.get('days', 7)) * 86400.0
    k0 = int((first - anchor) // length)
    k1 = int((now - anchor) // length)
    return [(anchor + k * length, anchor + (k + 1) * length) for k in range(k0, k1 + 1)]


def method_a(acct: Account, timeline: Timeline, config: dict[str, Any], eligible: bool,
             now: float) -> dict[str, Any]:
    if not timeline.times:
        return {'method': 'full_week_100', 'status': 'insufficient', 'coefficient': None,
                'reason': 'no timed records', 'points': []}
    excluded = {parse_ts(v) for v in (config.get('windows') or {}).get('excluded') or []}
    points = []
    for start, end in windows_for(config, timeline.times[0], now):
        part = account_partition(acct, timeline.window(start, end))
        point: dict[str, Any] = {'window_start': iso(start), 'window_end': iso(end),
                                 'completed': end <= now, 'dollars': round(part['dollars'], 4),
                                 'tokens': part['tokens'], 'bias': 'floor',
                                 'usd_per_pct': round(part['dollars'] / 100.0, 4),
                                 'eligibility': 'eligible' if eligible else 'unknown'}
        point.update(_coverage_fields(part, eligible))
        if start in excluded:
            reason = 'excluded_by_config'
        elif end > now:
            reason = 'incomplete_window'
        elif not eligible:
            reason = 'eligibility_unknown'
        elif part['coverage'] is None or part['tokens']['measured'] == 0:
            reason = 'no_measured_tokens'
        elif part['coverage'] < METHOD_A_MIN_COVERAGE:
            reason = 'coverage_below_0.90'
        else:
            reason = None
        point['used'] = reason is None
        if reason:
            point['reason'] = reason
        points.append(point)
    used = [p['usd_per_pct'] for p in points if p['used']]
    out: dict[str, Any] = {'method': 'full_week_100', 'quota': QUOTA, 'points': points,
                           'min_coverage': METHOD_A_MIN_COVERAGE, 'bias': 'floor'}
    if used:
        out.update(status='fitted', coefficient=round(statistics.median(used), 4), used_points=len(used))
    else:
        out.update(status='insufficient', coefficient=None, reason='no eligible window')
    return out


def _minute(value: Any) -> int | None:
    ts = parse_ts(value)
    return None if ts is None else int(round(ts / 60.0))


def history_lines(history: list[dict[str, Any]], account: str, window_kind: str) -> tuple[list[dict], list[dict]]:
    """(deduped non-probe lines sorted by observed_at, probe/invalid exclusions) for one account and quota."""
    seen, lines, probes = set(), [], []
    for line in history:
        if line.get('provider') != 'claude' or line.get('window_kind') != window_kind:
            continue
        if line.get('account_id') != account:
            continue
        if line.get('source') == 'probe':
            probes.append({'observed_at': line.get('observed_at'), 'reason': 'probe_source'})
            continue
        if parse_ts(line.get('observed_at')) is None or not isinstance(line.get('pct'), (int, float)):
            probes.append({'observed_at': line.get('observed_at'), 'reason': 'invalid_line'})
            continue
        key = dedupe_key(line)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        lines.append(line)
    lines.sort(key=lambda l: parse_ts(l['observed_at']))
    return lines, probes


def method_b(acct: Account, timeline: Timeline, history: list[dict[str, Any]], eligible: bool) -> dict[str, Any]:
    lines, probes = history_lines(history, acct.id, QUOTA)
    samples, exclusions = [], list(probes)
    for first, second in zip(lines, lines[1:]):
        t1, t2 = parse_ts(first['observed_at']), parse_ts(second['observed_at'])
        delta = second['pct'] - first['pct']
        r1, r2 = _minute(first.get('resets_at')), _minute(second.get('resets_at'))
        sample: dict[str, Any] = {'from': first['observed_at'], 'to': second['observed_at'],
                                  'delta_pct': delta, 'interval_s': round(t2 - t1, 3)}
        part = account_partition(acct, timeline.interval(t1, t2))
        sample['dollars'] = round(part['dollars'], 6)
        sample.update(_coverage_fields(part, eligible))
        if r1 is None or r2 is None:
            reason = 'reset_unknown'
        elif r1 != r2:
            reason = 'reset_crossing'
        elif t2 == t1:
            reason = 'stale'
        elif delta <= 0:
            reason = 'non_positive_delta'
        elif t2 - t1 > MAX_INTERVAL_S:
            reason = 'interval_over_24h'
        elif not eligible:
            reason = 'eligibility_unknown'
        elif part['coverage'] is None:
            reason = 'no_measured_tokens'
        elif part['coverage'] < SAMPLE_MIN_COVERAGE:
            reason = 'coverage_below_0.95'
        else:
            reason = None
        if reason:
            sample['reason'] = reason
            exclusions.append(sample)
        else:
            samples.append(sample)
    out: dict[str, Any] = {'method': 'history_regression', 'quota': QUOTA, 'valid_samples': len(samples),
                           'span_pct': sum(s['delta_pct'] for s in samples), 'samples': samples,
                           'exclusions': exclusions, 'min_samples': METHOD_B_MIN_SAMPLES,
                           'min_span_pct': METHOD_B_MIN_SPAN, 'min_coverage': SAMPLE_MIN_COVERAGE}
    sxx = sum(s['delta_pct'] ** 2 for s in samples)
    sxy = sum(s['delta_pct'] * s['dollars'] for s in samples)
    if len(samples) < METHOD_B_MIN_SAMPLES or out['span_pct'] < METHOD_B_MIN_SPAN or sxx == 0 or sxy <= 0:
        out.update(status='insufficient', coefficient=None,
                   reason=f"needs >= {METHOD_B_MIN_SAMPLES} valid samples spanning >= {METHOD_B_MIN_SPAN} pts")
        return out
    k = sxy / sxx
    errors = [abs(s['delta_pct'] - s['dollars'] / k) / s['delta_pct'] for s in samples]
    out.update(status='fitted', coefficient=round(k, 4), residual_mape_pct=round(statistics.median(errors) * 100, 4))
    return out


def _window_observed_pct(history: list[dict[str, Any]], account: str, start: float, end: float) -> int | None:
    lines, _ = history_lines(history, account, QUOTA)
    inside = [l['pct'] for l in lines if start <= parse_ts(l['observed_at']) < end]
    return max(inside) if inside else None


def calibrate(src: Sources, config: dict[str, Any] | None, config_path: Path) -> dict[str, Any]:
    config = config or {}
    threshold = float(config.get('unplaceable_threshold', DEFAULT_UNPLACEABLE_THRESHOLD))
    unplaceable = unplaceable_summary(src.records, threshold)
    eligible = unplaceable['passes']
    timeline = Timeline(src.records)
    accounts = [Account(a) for a in config.get('accounts') or [] if (a.get('provider') or 'claude') == 'claude']
    by_label = {a.label: a for a in accounts}
    seen_ids = sorted({r.account for r in src.records if r.provider == 'claude' and r.account})
    claude = [r for r in src.records if r.provider == 'claude']
    entries: list[dict[str, Any]] = []
    fitted: dict[str, dict[str, Any]] = {}

    def base(acct: Account) -> dict[str, Any]:
        own = [r for r in claude if r.account == acct.id]
        part = account_partition(acct, timeline.recs)
        entry = {'account_id': acct.id, 'label': acct.label, 'role': acct.role, 'provider': 'claude',
                 'quota': QUOTA, 'window_minutes': QUOTA_MINUTES, 'unit': 'usd_per_pct',
                 'unplaceable': {k: unplaceable[k] for k in ('ratio', 'threshold', 'passes')},
                 'provenance_row_coverage': round(sum(r.has_row for r in own) / len(own), 6) if own else None,
                 'measured_rollup': {'tokens': part['tokens'], 'dollars': round(part['dollars'], 4),
                                     'untimed_tokens': sum(r.total for r in own if r.cls == 'untimed'),
                                     'dollars_label': DOLLARS_LABEL}}
        entry.update(_coverage_fields(part, eligible))
        return entry

    for acct in accounts:
        if acct.role != 'fleet_only':
            continue
        entry = base(acct)
        a = method_a(acct, timeline, config, eligible, src.now)
        b = method_b(acct, timeline, src.history, eligible)
        entry['methods'] = {'full_week_100': a, 'history_regression': b}
        if b['coefficient'] is not None:
            entry.update(status='fitted', coefficient=b['coefficient'], basis='history_regression',
                         residual_mape_pct=b['residual_mape_pct'], points=b['valid_samples'])
        elif a['coefficient'] is not None:
            entry.update(status='fitted', coefficient=a['coefficient'], basis='full_week_100', bias='floor',
                         points=a['used_points'])
        else:
            entry.update(status='insufficient', coefficient=None,
                         reason='no eligible window or regression sample' + ('' if eligible else
                                 f" (unplaceable_mass {unplaceable['ratio']:.4f} > {threshold})"))
        entry['exclusions'] = ([{'window_start': p['window_start'], 'reason': p['reason']}
                                for p in a.get('points', []) if p.get('reason')]
                               + [{'from': s.get('from') or s.get('observed_at'), 'reason': s['reason']}
                                  for s in b['exclusions']])
        entries.append(entry)
        fitted[acct.label] = entry

    for acct in accounts:
        if acct.role in ('fleet_only', 'shared'):
            continue
        entry = base(acct)
        entry.update(status='not_fitted', coefficient=None, conversion=None,
                     reason=f'config role {acct.role!r} is not fleet_only or shared; not fitted')
        entries.append(entry)

    for acct in accounts:
        if acct.role != 'shared':
            continue
        entry = base(acct)
        source = by_label.get(acct.transfer_from) if acct.transfer_from else None
        if not acct.transfer_from:
            entry.update(status='not_fitted', coefficient=None, conversion=None, reason='shared account; not fitted')
        elif not (isinstance(acct.justification, str) and acct.justification.strip()):
            entry.update(status='not_fitted', coefficient=None, conversion=None,
                         reason='transfer_from requires a justification')
        elif source is None or fitted.get(source.label, {}).get('coefficient') is None:
            entry.update(status='insufficient', coefficient=None, conversion=None, basis='transferred',
                         reason=f'transfer source {acct.transfer_from!r} has no coefficient')
        else:
            coefficient = fitted[source.label]['coefficient']
            entry.update(status='transferred', coefficient=coefficient, conversion=coefficient,
                         basis='transferred', transfer_from=acct.transfer_from,
                         justification=acct.justification)
            residuals = []
            if timeline.times:
                for start, end in windows_for(config, timeline.times[0], src.now):
                    part = account_partition(acct, timeline.window(start, end))
                    observed = _window_observed_pct(src.history, acct.id, start, end)
                    predicted = part['dollars'] / coefficient
                    residuals.append({'window_start': iso(start), 'window_end': iso(end),
                                      'observed_pct': observed, 'predicted_pct': round(predicted, 4),
                                      'external_residual': None if observed is None else round(observed - predicted, 4),
                                      'label': 'estimate of non-fleet use'})
            entry['external_residual'] = residuals
        entries.append(entry)

    configured = {a.id for a in accounts}
    for account_id in seen_ids:
        if account_id not in configured:
            entry = base(Account({'account_id': account_id, 'label': None, 'role': 'unconfigured'}))
            entry.update(status='not_configured', coefficient=None,
                         reason='account not in calibration_config.json')
            entries.append(entry)

    reported_only = []
    for kind in sorted({l.get('window_kind') for l in src.history if l.get('provider') == 'claude'} - {QUOTA}):
        lines = [l for l in src.history if l.get('provider') == 'claude' and l.get('window_kind') == kind]
        for account in sorted({l.get('account_id') or 'null' for l in lines}):
            mine = [l for l in lines if (l.get('account_id') or 'null') == account]
            reported_only.append({'provider': 'claude', 'window_kind': kind, 'account_id': account,
                                  'observations': len(mine), 'latest_pct': mine[-1].get('pct'),
                                  'reason': 'reported only; not fitted (one quota per calibration)'})
    total = len(claude)
    return {
        'schema_version': SCHEMA_VERSION, 'provider': 'claude', 'quota': QUOTA,
        'fitted_at': iso(src.now), 'pricing_version': src.pricing.version,
        'config_path': str(config_path), 'config_loaded': bool(config),
        'thresholds': {'method_a_min_coverage': METHOD_A_MIN_COVERAGE, 'sample_min_coverage': SAMPLE_MIN_COVERAGE,
                       'method_b_min_samples': METHOD_B_MIN_SAMPLES, 'method_b_min_span_pct': METHOD_B_MIN_SPAN,
                       'max_interval_s': MAX_INTERVAL_S, 'unplaceable_threshold': threshold},
        'provenance_row_coverage': round(sum(r.has_row for r in claude) / total, 6) if total else None,
        'unplaceable': unplaceable,
        'entries': entries,
        'reported_only': reported_only,
        'codex': {'status': 'deferred', 'follow_up': 'spec_pentacle__usage_codex_rollup_and_calibration_2026_10'},
    }


# --------------------------------------------------------------------------- output

def redact(obj: Any, mapping: dict[str, str]) -> Any:
    if isinstance(obj, dict):
        return {mapping.get(k, k) if isinstance(k, str) else k: redact(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, mapping) for v in obj]
    if isinstance(obj, str):
        return mapping.get(obj, obj)
    return obj


def redaction_map(src: Sources, config: dict[str, Any] | None) -> dict[str, str]:
    mapping = {a['account_id']: a.get('label') or 'configured_account'
               for a in (config or {}).get('accounts') or [] if a.get('account_id')}
    others = sorted({r.account for r in src.records if r.account and r.account not in mapping})
    others += sorted({l.get('account_id') for l in src.history
                      if l.get('account_id') and l.get('account_id') not in mapping and l.get('account_id') not in others})
    for index, account in enumerate(others, 1):
        mapping[account] = f'unconfigured_account_{index}'
    return mapping


def write_private(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write('\n')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def render_text(result: dict[str, Any]) -> str:
    out = []

    def claude_line(name: str, c: dict[str, Any]) -> str:
        return (f"{name}: claude tokens={c['total_tokens']:,} dollars~{c['dollars']:,.2f} "
                f"unpriced={c['unpriced_tokens']:,} provenance_row_cov={c['provenance_row_coverage']} "
                f"measured_cov={c['measured_coverage']}")

    for spec in result.get('specs', []):
        out.append(claude_line(spec['spec_id'], spec['claude']))
        t = spec['time']
        d = t['elapsed_delivery_h']
        out.append(f"  seats={t['seats']} delivery={'censored:' + d['reason'] if d.get('censored') else d['value_h']}"
                   f" wait_h={t['known_wait_h']} activity_proxy_h={t['activity_proxy_h']['value']}"
                   f" qa_rounds={t['qa_rounds']}")
        for acct in spec['claude']['by_account']:
            out.append(f"  account {acct['account_id']}: tokens={acct['tokens']:,} dollars~{acct['dollars']:,.2f}"
                       f" weekly_pct={acct['weekly_pct'].get('value')} ({acct['weekly_pct'].get('reason', '')})")
        if spec['codex']['records']:
            out.append(f"  codex: deferred ({spec['codex']['records']} cumulative records, unpriced)")
    if 'project' in result:
        p = result['project']
        out.append(claude_line(f"project {p['project']} ({len(p['member_specs'])} specs, {p['streams']} streams)",
                               p['claude']))
    if 'comparables' in result:
        c = result['comparables']
        out.append(f"comparables repo={c['repo']} kind={c['kind']} count={c['count']} status={c['status']}")
        for key in ('elapsed_delivery_h', 'dollars'):
            if c.get(key):
                out.append(f"  {key}: {c[key]}")
    if 'calibration' in result:
        cal = result['calibration']
        u = cal['unplaceable']
        out.append(f"calibration: unplaceable ratio={u['ratio']} threshold={u['threshold']} passes={u['passes']}")
        for e in cal['entries']:
            out.append(f"  {e['account_id']} ({e['role']}): status={e['status']} coefficient={e['coefficient']}"
                       f" measured_cov={e.get('measured_coverage')} reason={e.get('reason', '')}")
    if 'fleet_totals' in result:
        for row in result['fleet_totals']['by_account']:
            out.append(f"fleet {row['account_id']}: attributed={row['attributed']['tokens']:,}"
                       f" unattributed={row['unattributed']['tokens']:,}")
    return '\n'.join(out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='agent-orch usage rollup', description=__doc__.splitlines()[0])
    parser.add_argument('--spec', nargs='+', default=[])
    parser.add_argument('--project')
    parser.add_argument('--since')
    parser.add_argument('--until')
    parser.add_argument('--comparables', action='store_true')
    parser.add_argument('--repo')
    parser.add_argument('--kind', choices=KINDS + tuple(KIND_ALIASES))
    parser.add_argument('--n', type=int, default=20)
    parser.add_argument('--calibrate', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--redact', action='store_true', help='replace account ids with config labels')
    parser.add_argument('--data-dir', default=DATA_DIR)
    parser.add_argument('--memory-root', default=MEMORY_ROOT)
    parser.add_argument('--work-root', help='work-item root (default <memory-root>/work)')
    parser.add_argument('--pricing', default=str(PRICING_PATH))
    parser.add_argument('--config', help=f'private calibration config (default <data-dir>/{CONFIG_NAME})')
    parser.add_argument('--calibration', help=f'calibration to read for weekly percent (default <data-dir>/{CALIBRATION_NAME})')
    parser.add_argument('--calibration-out', help=f'--calibrate output (default <data-dir>/{CALIBRATION_NAME})')
    parser.add_argument('--now', help=argparse.SUPPRESS)
    return parser


def run(argv: list[str] | None = None) -> tuple[int, dict[str, Any]]:
    args = build_parser().parse_args(argv)
    if args.comparables and not args.repo:
        raise RollupError('--comparables requires --repo')
    data_dir = Path(args.data_dir).expanduser()
    work_root = Path(args.work_root).expanduser() if args.work_root else Path(args.memory_root).expanduser() / 'work'
    now = parse_ts(args.now) if args.now else time.time()
    since = parse_ts(args.since) if args.since else None
    until = parse_ts(args.until) if args.until else None
    if (args.since and since is None) or (args.until and until is None):
        raise RollupError('--since/--until must be UTC ISO-8601')
    pricing = Pricing.load(Path(args.pricing))
    src = Sources(data_dir=data_dir, work_root=work_root, pricing=pricing, now=now)
    config_path = Path(args.config).expanduser() if args.config else data_dir / CONFIG_NAME
    config = load_config(config_path)
    result: dict[str, Any] = {'schema_version': SCHEMA_VERSION, 'generated_at': iso(now),
                              'pricing_version': pricing.version, 'dollars_label': DOLLARS_LABEL,
                              'since': iso(since), 'until': iso(until)}
    if args.calibrate:
        calibration = calibrate(src, config, config_path)
        out_path = Path(args.calibration_out).expanduser() if args.calibration_out else data_dir / CALIBRATION_NAME
        try:
            write_private(out_path, calibration)
        except OSError as exc:
            raise RollupError(f'cannot write {out_path}: {exc}') from exc
        result['calibration'] = calibration
        result['calibration_written'] = str(out_path)
        cal = Calibration(calibration, out_path)
    else:
        cal_path = Path(args.calibration).expanduser() if args.calibration else data_dir / CALIBRATION_NAME
        cal = Calibration(json.loads(cal_path.read_text()), cal_path) if cal_path.exists() else Calibration(None, None)
    ctx = Context(src, cal, since=since, until=until)
    if args.spec:
        result['specs'] = [spec_rollup(ctx, spec) for spec in args.spec]
    if args.project:
        result['project'] = project_rollup(ctx, args.project)
    if args.comparables:
        result['comparables'] = comparables(ctx, args.repo, args.kind, args.n)
    result['fleet_totals'] = fleet_totals(ctx)
    if args.redact:
        result = redact(result, redaction_map(src, config))
    return 0, result


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        code, result = run(argv)
    except RollupError as exc:
        print(f'usage rollup: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True) if '--json' in argv else render_text(result))
    return code


if __name__ == '__main__':
    sys.exit(main())
