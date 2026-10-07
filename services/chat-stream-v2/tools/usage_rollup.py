#!/usr/bin/env python3
"""Read-only usage rollup and calibration over the Thoth ledger (Claude and Codex).

Contract: docs/usage_accounting.md § Rollup and calibration. Reads
``sessions.db``, ``sessions_archive.db``, ``notifications.db`` and
``usage_history.jsonl`` with ``mode=ro`` and work-item frontmatter from shared
memory; the only write is ``--calibrate`` output (``calibration.json``, 0600).
Codex sessions are reconciled per native session (cumulative ledger row vs per-response detail) before any
window logic (spec_pentacle__usage_codex_rollup_and_calibration_2026_10).
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
RETIRED_QUIET_S = 7 * 86400.0
PROXY_LABEL = 'proxy: union of inter-response gaps <= 10 min from provenance observed_at (Claude records)'
CODEX_PROXY_LABEL = 'proxy: union of inter-response gaps <= 10 min from placed Codex response observed_at'
CODEX_QUOTA = 'codex'
CODEX_QUOTA_MINUTES = 10080
CODEX_SOURCE = 'rollout'
RECON_CLASSES = ('reconciled', 'partial', 'unverifiable')
CODEX_OUTSIDE = ('retired', 'unverifiable', 'unreconciled', 'untimed')
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


def _ints(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    return {k: int(v) for k, v in raw.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def codex_ledger_buckets(raw: dict[str, int]) -> dict[str, int]:
    """Codex cumulative ledger counters -> the four buckets (cached input is a subset of input_total)."""
    inp, cached = raw.get('input_total', 0), raw.get('cached_input', 0)
    return {'uncached_input': inp - cached, 'cache_read': cached, 'cache_write': raw.get('cache_write', 0),
            'output': raw.get('output', 0)}


def _identity(account_source: Any, conflict: Any, account_id: Any) -> tuple[str | None, str]:
    if account_source is None:
        return None, 'unknown'
    if conflict:
        return None, 'conflict'
    if account_id:
        return account_id, 'known'
    return None, 'unknown'


class Rec:
    __slots__ = ('host', 'provider', 'native', 'key', 'stream_id', 'tokens', 'total', 'has_row',
                 'observed', 'model', 'account', 'account_state', 'cls', 'dollars', 'reasoning')

    def __init__(self, row: sqlite3.Row, pricing: Pricing):
        self.host, self.provider, self.native = row['host'], row['provider'], row['native_session_id']
        self.key, self.stream_id = row['record_key'], row['stream_id']
        try:
            raw = _ints(json.loads(row['tokens']))
        except (TypeError, ValueError):
            raw = {}
        self.reasoning = raw.get('reasoning', 0) if self.provider == 'codex' else 0
        self.tokens = codex_ledger_buckets(raw) if self.provider == 'codex' else raw
        self.total = sum(self.tokens.values())
        self.has_row = bool(row['has_row'])
        self.observed = parse_ts(row['observed_at']) if self.has_row else None
        self.model = row['model'] if self.has_row else None
        self.account, self.account_state = _identity(row['account_source'], row['conflict'], row['account_id'])
        self.dollars = pricing.dollars(self.model, self.tokens) if self.provider == 'claude' else None
        # Exclusive partition (spec § Coverage): untimed first, then unpriced, then unknown account.
        # A Codex cumulative row is classified per session by reconciliation (CodexSession), not here.
        if self.provider != 'claude':
            self.cls = 'codex_session'
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


def _zero() -> dict[str, int]:
    return dict.fromkeys(BUCKETS, 0)


def _add(into: dict[str, int], tokens: dict[str, int]) -> dict[str, int]:
    for bucket in BUCKETS:
        into[bucket] += tokens.get(bucket, 0)
    return into


class CodexRow:
    """One v2_usage_codex_responses row in the four ledger buckets; reasoning is a labelled subset of output."""
    __slots__ = ('host', 'provider', 'native', 'stream_id', 'observed', 'model', 'tokens', 'total', 'reasoning',
                 'account', 'account_state', 'dollars', 'cls')

    def __init__(self, row: sqlite3.Row, pricing: Pricing):
        self.host, self.provider, self.native = row['host'], 'codex', row['native_session_id']
        self.stream_id = None
        self.observed = parse_ts(row['observed_at'])
        self.model = row['model']
        self.tokens = {'uncached_input': int(row['input']) - int(row['cached_input']),
                       'cache_read': int(row['cached_input']), 'cache_write': int(row['cache_write_input']),
                       'output': int(row['output'])}
        self.total = sum(self.tokens.values())
        self.reasoning = int(row['reasoning_output'])
        self.account, self.account_state = None, 'unknown'
        self.dollars = pricing.dollars(self.model, self.tokens)
        self.cls = 'untimed'

    def classify(self) -> None:
        """Placed rows only (inside a reconciled/partial session): same order as Claude."""
        if self.observed is None:
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


class CodexSession:
    """Per-native-session reconciliation of the cumulative ledger row C_n with the response rows R_n.

    The class is decided first over every response (timed or not); rows are labelled only inside
    reconciled/partial sessions. Invariant: placed + untimed + unreconciled == C_n per bucket, or the whole
    mass is unverifiable and appears nowhere else.
    """

    def __init__(self, host: str, native: str, cumulative: Rec | None, responses: list[CodexRow],
                 identity: tuple[str | None, str]):
        self.host, self.native = host, native
        self.stream_id = cumulative.stream_id if cumulative is not None else None
        self.cumulative = dict(cumulative.tokens) if cumulative is not None else None
        self.responses = responses
        self.account, self.account_state = identity
        self.received = _zero()
        for row in responses:
            _add(self.received, row.tokens)
        c = self.cumulative
        self.placed: list[CodexRow] = []
        self.untimed = _zero()
        self.unreconciled = _zero()
        self.unverifiable = _zero()
        if c is None or any(self.received[b] > c.get(b, 0) for b in BUCKETS):
            self.cls = 'unverifiable'
            self.unverifiable = ({b: max(self.received[b], c.get(b, 0)) for b in BUCKETS} if c is not None
                                 else dict(self.received))
        else:
            self.cls = 'reconciled' if all(self.received[b] == c.get(b, 0) for b in BUCKETS) else 'partial'
            self.unreconciled = {b: c.get(b, 0) - self.received[b] for b in BUCKETS}
            for row in responses:
                if row.observed is None:
                    _add(self.untimed, row.tokens)
                else:
                    self.placed.append(row)
        self.set_account(self.account, self.account_state)

    def set_account(self, account: str | None, state: str) -> None:
        self.account, self.account_state = account, state
        for row in self.responses:
            row.stream_id = self.stream_id
            row.account, row.account_state = account, state
            row.classify()

    @property
    def account_label(self) -> str:
        return self.account if self.account else self.account_state

    def masses(self) -> dict[str, int]:
        placed = sum(r.total for r in self.placed)
        return {'placed': placed, 'untimed': sum(self.untimed.values()),
                'unreconciled': sum(self.unreconciled.values()), 'unverifiable': sum(self.unverifiable.values())}

    @property
    def total(self) -> int:
        return sum(self.masses().values())


def build_codex_sessions(cumulative: dict[tuple[str, str], Rec], responses: dict[tuple[str, str, str], CodexRow],
                         identity: dict[tuple[str, str], tuple[str | None, str]]) -> list[CodexSession]:
    grouped: dict[tuple[str, str], list[CodexRow]] = {}
    for (host, native, _rid), row in sorted(responses.items()):
        grouped.setdefault((host, native), []).append(row)
    sessions = []
    for key in sorted(set(cumulative) | set(grouped)):
        sessions.append(CodexSession(key[0], key[1], cumulative.get(key), grouped.get(key, []),
                                     identity.get(key, (None, 'unknown'))))
    return sessions


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
        self._codex_cumulative: dict[tuple[str, str], Rec] = {}
        self._codex_responses: dict[tuple[str, str, str], CodexRow] = {}
        self._codex_identity: dict[tuple[str, str], tuple[str | None, str]] = {}
        self.codex_alias_report: list[dict[str, Any]] = []
        self.usage_state: list[tuple[str, str, float | None]] = []  # (collection_host, stream_id, updated_at)
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
        self.codex = build_codex_sessions(self._codex_cumulative, self._codex_responses, self._codex_identity)

    def apply_codex_aliases(self, aliases: dict[str, str]) -> None:
        """Justified private-config aliases fold one creator account into another (basis: aliased)."""
        for session in self.codex:
            if session.account in aliases:
                session.set_account(aliases[session.account], 'known')

    def _load_db(self, conn: sqlite3.Connection, source: str) -> None:
        tables = _tables(conn)
        if 'sessions' in tables:
            for row in conn.execute('SELECT * FROM sessions'):
                seat = Seat(dict(row), source)
                self.seats[seat.stream_id] = seat  # live wins over archive (loaded second)
        if {'v2_usage_records', 'v2_usage_provenance', 'v2_usage_identity'} <= tables:
            for row in conn.execute(RECORDS_SQL):
                rec = Rec(row, self.pricing)
                self.records.append(rec)
                if rec.provider == 'codex':  # one cumulative row per native session; live wins over archive
                    self._codex_cumulative[(rec.host, rec.native)] = rec
            for row in conn.execute("SELECT host, native_session_id, account_id, account_source, conflict "
                                    "FROM v2_usage_identity WHERE provider='codex'"):
                self._codex_identity[(row['host'], row['native_session_id'])] = _identity(
                    row['account_source'], row['conflict'], row['account_id'])
        if 'v2_usage_codex_responses' in tables:
            for row in conn.execute('SELECT * FROM v2_usage_codex_responses'):
                key = (row['host'], row['native_session_id'], row['response_id'])
                self._codex_responses[key] = CodexRow(row, self.pricing)
        if 'v2_usage_state' in tables:
            for row in conn.execute('SELECT collection_host, stream_id, updated_at FROM v2_usage_state'):
                self.usage_state.append((row['collection_host'], row['stream_id'], parse_ts(row['updated_at'])))
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
        self.coefficients: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in (data or {}).get('entries') or []:
            if entry.get('provider') == 'claude' and entry.get('quota') == QUOTA and entry.get('account_id'):
                self.coefficients[('claude', entry['account_id'])] = entry
        for entry in ((data or {}).get('codex') or {}).get('entries') or []:
            if entry.get('provider') == 'codex' and entry.get('quota') == CODEX_QUOTA and entry.get('account_id'):
                self.coefficients[('codex', entry['account_id'])] = entry

    def weekly_pct(self, account: str, dollars: float, provider: str = 'claude') -> dict[str, Any]:
        quota = QUOTA if provider == 'claude' else CODEX_QUOTA
        entry = self.coefficients.get((provider, account))
        if entry is None:
            reason = 'no calibration' if self.path is None else 'account not in calibration'
            return {'value': None, 'quota': quota, 'reason': reason}
        if entry.get('coefficient') is None:
            return {'value': None, 'quota': quota,
                    'reason': entry.get('reason') or f"calibration {entry.get('status') or 'unavailable'}"}
        return {'value': round(dollars / entry['coefficient'], 4), 'quota': quota,
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
        self.codex_by_stream: dict[str, list[CodexSession]] = {}
        for session in sources.codex:
            if session.stream_id:
                self.codex_by_stream.setdefault(session.stream_id, []).append(session)
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


def _recon_block(sessions: list[CodexSession]) -> dict[str, Any]:
    """Session counts and token masses per reconciliation class (the per-bucket split is in `tokens`)."""
    out: dict[str, Any] = {}
    for cls in RECON_CLASSES:
        mine = [s for s in sessions if s.cls == cls]
        block: dict[str, Any] = {'sessions': len(mine), 'tokens': sum(s.total for s in mine)}
        if cls == 'partial':
            block['unreconciled_tokens'] = sum(s.masses()['unreconciled'] for s in mine)
        out[cls] = block
    return out


def _mass_by_host_account(sessions: list[CodexSession], kind: str) -> list[dict[str, Any]]:
    mass: dict[tuple[str, str], dict[str, int]] = {}
    for session in sessions:
        tokens = getattr(session, kind)
        if sum(tokens.values()):
            _add(mass.setdefault((session.host, session.account_label), _zero()), tokens)
    return [{'host': h, 'account_id': a, 'tokens': sum(t.values()), 'by_bucket': t} for (h, a), t in sorted(mass.items())]


def summarize_codex(sessions: list[CodexSession], ctx: Context) -> dict[str, Any]:
    """Codex scope summary: placed response rows are priced and windowed; everything else is reported, never priced."""
    ranged = ctx.since is not None or ctx.until is not None
    placed = [r for s in sessions for r in s.placed
              if not ranged or ((ctx.since is None or r.observed >= ctx.since)
                                and (ctx.until is None or r.observed < ctx.until))]
    tokens, untimed, unreconciled, unverifiable = _zero(), _zero(), _zero(), _zero()
    for row in placed:
        _add(tokens, row.tokens)
    for session in sessions:
        _add(untimed, session.untimed)
        _add(unreconciled, session.unreconciled)
        _add(unverifiable, session.unverifiable)
    partition = dict.fromkeys(('measured', 'unpriced', 'unknown_account'), 0)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    accounts: dict[str, dict[str, Any]] = {}
    dollars = 0.0
    for row in placed:
        partition[row.cls] += row.total
        group = groups.setdefault((row.model or 'unknown', row.account_label), {
            'model': row.model or 'unknown', 'account_id': row.account_label, 'responses': 0,
            'tokens': _zero(), 'reasoning_output': 0, 'dollars': 0.0, 'priced': row.dollars is not None})
        group['responses'] += 1
        _add(group['tokens'], row.tokens)
        group['reasoning_output'] += row.reasoning
        acct = accounts.setdefault(row.account_label, {'account_id': row.account_label, 'tokens': 0,
                                                       'dollars': 0.0, 'unpriced_tokens': 0})
        acct['tokens'] += row.total
        if row.dollars is None:
            acct['unpriced_tokens'] += row.total
        else:
            group['dollars'] += row.dollars
            acct['dollars'] += row.dollars
            dollars += row.dollars
    for group in groups.values():
        group['dollars'] = round(group['dollars'], 4) if group['priced'] else None
        if not group['priced']:
            group['bucket'] = 'unpriced'
    for acct in accounts.values():
        acct['dollars'] = round(acct['dollars'], 4)
        if acct['account_id'] in ('unknown', 'conflict'):
            acct['weekly_pct'] = {'value': None, 'quota': CODEX_QUOTA, 'reason': f"account {acct['account_id']}"}
        else:
            acct['weekly_pct'] = ctx.cal.weekly_pct(acct['account_id'], acct['dollars'], 'codex')
    outside = {'untimed': sum(untimed.values()), 'unreconciled': sum(unreconciled.values()),
               'unverifiable': sum(unverifiable.values())}
    placed_total = sum(partition.values())
    out: dict[str, Any] = {
        'provider': 'codex',
        'streams': sorted({s.stream_id for s in sessions if s.stream_id}),
        'records': sum(1 for s in sessions if s.cumulative is not None),
        'sessions': len(sessions),
        'responses': sum(len(s.responses) for s in sessions),
        'tokens': tokens,
        'reasoning_output': sum(r.reasoning for r in placed),
        'placed_tokens': placed_total,
        'dollars': round(dollars, 4),
        'dollars_label': DOLLARS_LABEL,
        'unpriced_tokens': partition['unpriced'],
        'partition_tokens': {**partition, **outside},
        'outside_window_tokens': {'untimed': untimed, 'unreconciled': unreconciled, 'unverifiable': unverifiable},
        'reconciliation': _recon_block(sessions),
        'unreconciled_mass': _mass_by_host_account(sessions, 'unreconciled'),
        'unverifiable_mass': _mass_by_host_account(sessions, 'unverifiable'),
        'by_model_account': sorted(groups.values(), key=lambda g: (g['model'], g['account_id'])),
        'by_account': sorted(accounts.values(), key=lambda a: a['account_id']),
    }
    denom = placed_total + sum(outside.values())
    if ranged:  # unplaceable mass has no time, so it cannot be assigned to (or kept out of) a range
        out.update(completeness=None, measured_coverage=None,
                   completeness_withheld='--since/--until set; untimed, unreconciled and unverifiable mass has no time')
    else:
        out['completeness'] = round(placed_total / denom, 6) if denom else None
        out['measured_coverage'] = round(partition['measured'] / denom, 6) if denom else None
    return out


def codex_by_stream(sessions: list[CodexSession]) -> list[dict[str, Any]]:
    rows = []
    for stream in sorted({s.stream_id for s in sessions if s.stream_id}):
        mine = [s for s in sessions if s.stream_id == stream]
        placed = sum(s.masses()['placed'] for s in mine)
        total = sum(s.total for s in mine)
        rows.append({'stream_id': stream, 'sessions': len(mine), 'placed_tokens': placed, 'total_tokens': total,
                     'dollars': round(sum(r.dollars or 0.0 for s in mine for r in s.placed), 4),
                     'completeness': round(placed / total, 6) if total else None,
                     'reconciliation': {c: sum(1 for s in mine if s.cls == c) for c in RECON_CLASSES}})
    return rows


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


def spec_rollup(ctx: Context, spec_id: str) -> dict[str, Any]:
    spec_id = normalize_spec_ids(spec_id)[0]
    streams = sorted(ctx.streams_by_spec.get(spec_id, ()))
    recs = [r for s in streams for r in ctx.records_by_stream.get(s, ())]
    codex = [c for s in streams for c in ctx.codex_by_stream.get(s, ())]
    item = ctx.src.work_items.get(spec_id)
    return {
        'spec_id': spec_id,
        'work_item': None if item is None else {'status': item.status, 'completed_at': iso(item.completed_at),
                                                'epic': item.epic, 'kind': item.kind, 'path': item.path},
        'streams': streams,
        'folded_streams': sorted(s for s in streams if ctx.attribution[s][1]),
        'claude': summarize_claude([r for r in recs if r.provider == 'claude' and ctx.in_range(r)], ctx),
        'codex': {**summarize_codex(codex, ctx), 'by_stream': codex_by_stream(codex)},
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
    codex = [c for s in streams for c in ctx.codex_by_stream.get(s, ())]
    return {
        'project': project,
        'kind': 'epic' if project.startswith('epic_') else 'repo',
        'member_specs': members,
        'streams': len(streams),
        'claude': summarize_claude([r for r in recs if r.provider == 'claude' and ctx.in_range(r)], ctx),
        'codex': summarize_codex(codex, ctx),
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
               'weekly_pct': pcts, 'codex_dollars': roll['codex']['dollars'] if roll['codex']['sessions'] else None,
               'codex_completeness': roll['codex']['completeness']}
        if not roll['claude']['records']:  # no Claude evidence: a zero would bias the quantiles low
            row['dollars'] = None
            row['dollars_reason'] = 'codex_only' if roll['codex']['sessions'] else 'no_usage_records'
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
    out['codex_dollars'] = _quartiles([r['codex_dollars'] for r in rows if r['codex_dollars'] is not None])
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


def retired_hosts(src: Sources, config: dict[str, Any]) -> tuple[set[str], list[dict[str, Any]]]:
    """Honour a configured retired host only if nothing live points at it: no open seat, no recent usage push."""
    listed = config.get('retired_hosts') or []
    if not isinstance(listed, list) or not all(isinstance(h, str) and h for h in listed):
        raise RollupError('calibration config: retired_hosts must be a list of host names')
    honoured: set[str] = set()
    report: list[dict[str, Any]] = []
    for host in sorted(set(listed)):
        # Composite assistant rows (e.g. bart:assistant) are routing aliases with no usage of their own.
        open_rows = [s for s in src.seats.values() if s.host == host and s.status == 'open']
        open_seats = [s for s in open_rows if s.provider != 'composite']
        composite = len(open_rows) - len(open_seats)
        # An unparseable updated_at cannot prove the host quiet, so it counts as recent.
        recent = [u for u in src.usage_state if (u[0] == host or u[1].startswith(host + ':'))
                  and (u[2] is None or u[2] >= src.now - RETIRED_QUIET_S)]
        if open_seats:
            reason = f'open seat in sessions ({len(open_seats)})'
        elif recent:
            reason = 'v2_usage_state row updated within 7 days'
        else:
            honoured.add(host)
            report.append({'host': host, 'status': 'honoured',
                           **({'open_composite_rows_not_counted': composite} if composite else {})})
            continue
        print(f'usage rollup: warning: retired_hosts entry {host!r} ignored: {reason}', file=sys.stderr)
        report.append({'host': host, 'status': 'ignored', 'reason': reason})
    return honoured, report


class Timeline:
    """Timed records of one provider ordered by observed_at, for window and interval slices."""

    def __init__(self, records: Iterable[Any], provider: str = 'claude'):
        timed = sorted((r for r in records if r.provider == provider and r.observed is not None),
                       key=lambda r: r.observed)
        self.recs = timed
        self.times = [r.observed for r in timed]

    def window(self, start: float, end: float) -> list[Rec]:  # [start, end)
        return self.recs[bisect.bisect_left(self.times, start):bisect.bisect_left(self.times, end)]

    def interval(self, start: float, end: float) -> list[Rec]:  # (start, end]
        return self.recs[bisect.bisect_right(self.times, start):bisect.bisect_right(self.times, end)]


WINDOW_BUCKETS = ('measured', 'unpriced', 'unknown_account')


def _bucket_for(acct: Account | None, rec: Rec) -> str | None:
    """The window bucket a timed record adds to this account's denominator (None: not this account's)."""
    if acct is None:  # fleet-wide view: every timed Claude record by its class
        return rec.cls if rec.cls in WINDOW_BUCKETS else None
    if rec.cls == 'measured':
        return 'measured' if rec.account == acct.id else None
    if rec.cls == 'unpriced':
        return 'unpriced' if rec.account == acct.id or (rec.account is None and acct.may_own(rec.host)) else None
    if rec.cls == 'unknown_account':
        return 'unknown_account' if acct.may_own(rec.host) else None
    return None


def account_partition(acct: Account | None, recs: Iterable[Rec]) -> dict[str, Any]:
    """Per-(provider, account, quota) partition; ambiguous mass sits in every candidate account's denominator.

    `by_host` splits the same three buckets by record host (conflict is a sub-count of unknown_account),
    so the host rows always sum to `tokens` exactly.
    """
    tokens = dict.fromkeys(WINDOW_BUCKETS, 0)
    by_host: dict[str, dict[str, int]] = {}
    dollars = 0.0
    for rec in recs:
        bucket = _bucket_for(acct, rec)
        if bucket is None:
            continue
        tokens[bucket] += rec.total
        host = by_host.setdefault(rec.host, {**dict.fromkeys(WINDOW_BUCKETS, 0), 'conflict': 0})
        host[bucket] += rec.total
        if bucket == 'unknown_account' and rec.account_state == 'conflict':
            host['conflict'] += rec.total
        if bucket == 'measured':
            dollars += rec.dollars or 0.0
    denom = sum(tokens.values())
    return {'tokens': tokens, 'by_host': by_host,
            'dollars': round(dollars, 6), 'coverage': (tokens['measured'] / denom) if denom else None}


def identity_mass(by_host: dict[str, dict[str, int]], outside: dict[str, dict[str, int]],
                  provider: str = 'claude') -> list[dict[str, Any]]:
    """Per-host identity mass: window buckets (summing to the parent denominator) plus the outside-window classes."""
    zero = {**dict.fromkeys(WINDOW_BUCKETS, 0), 'conflict': 0}
    keys = ('untimed', 'retired') if provider == 'claude' else CODEX_OUTSIDE
    return [{'provider': provider, 'host': host, 'window': dict(by_host.get(host) or zero),
             'outside_window_sum': dict(outside.get(host) or dict.fromkeys(keys, 0))}
            for host in sorted(set(by_host) | set(outside))]


def _coverage_fields(part: dict[str, Any], eligible: bool) -> dict[str, Any]:
    cov = part['coverage']
    if eligible:
        return {'measured_coverage': None if cov is None else round(cov, 6)}
    return {'measured_coverage': None,
            'measured_coverage_withheld': 'unplaceable_mass above threshold (bounded-coverage assumption fails)',
            'measured_coverage_if_bounded': None if cov is None else round(cov, 6)}


def unplaceable_summary(records: list[Rec], threshold: float, retired: set[str] = frozenset()) -> dict[str, Any]:
    """Untimed share of Claude tokens; honoured retired hosts leave numerator and denominator."""
    retired_mass: dict[str, int] = {}
    claude = []
    for rec in records:
        if rec.provider != 'claude':
            continue
        if rec.host in retired:
            retired_mass[rec.host] = retired_mass.get(rec.host, 0) + rec.total
        else:
            claude.append(rec)
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
            'by_host_account': [{'host': h, 'account_id': a, 'tokens': t} for (h, a), t in sorted(mass.items())],
            'retired_mass': [{'provider': 'claude', 'host': h, 'tokens': t} for h, t in sorted(retired_mass.items())]}


def codex_outside(sessions: list[CodexSession], retired: set[str]) -> dict[str, dict[str, int]]:
    """Per-host Codex mass outside every window, classified once: retired -> unverifiable -> unreconciled -> untimed."""
    outside: dict[str, dict[str, int]] = {}
    for session in sessions:
        host = outside.setdefault(session.host, dict.fromkeys(CODEX_OUTSIDE, 0))
        if session.host in retired:
            host['retired'] += session.total
            continue
        masses = session.masses()
        for cls in ('unverifiable', 'unreconciled', 'untimed'):
            host[cls] += masses[cls]
    return {h: v for h, v in outside.items() if any(v.values())}


def codex_unplaceable_summary(sessions: list[CodexSession], threshold: float,
                              retired: set[str] = frozenset()) -> dict[str, Any]:
    """Unverifiable + unreconciled + untimed share of all Codex ledger mass; honoured retired hosts leave both sides."""
    retired_mass: dict[str, int] = {}
    live = []
    for session in sessions:
        if session.host in retired:
            retired_mass[session.host] = retired_mass.get(session.host, 0) + session.total
        else:
            live.append(session)
    total = sum(s.total for s in live)
    mass: dict[tuple[str, str], dict[str, int]] = {}
    for session in live:
        m = session.masses()
        if m['unverifiable'] or m['unreconciled'] or m['untimed']:
            row = mass.setdefault((session.host, session.account_label),
                                  {'unverifiable': 0, 'unreconciled': 0, 'untimed': 0})
            for cls in row:
                row[cls] += m[cls]
    unplaceable = sum(sum(v.values()) for v in mass.values())
    ratio = (unplaceable / total) if total else 0.0
    return {'provider': 'codex', 'unplaceable_tokens': unplaceable, 'provider_total_tokens': total,
            'ratio': round(ratio, 6), 'threshold': threshold, 'passes': ratio <= threshold,
            'by_class': {c: sum(v[c] for v in mass.values()) for c in ('unverifiable', 'unreconciled', 'untimed')},
            'by_host_account': [{'host': h, 'account_id': a, 'tokens': sum(v.values()), **v}
                                for (h, a), v in sorted(mass.items())],
            'retired_mass': [{'provider': 'codex', 'host': h, 'tokens': t} for h, t in sorted(retired_mass.items())]}


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
             now: float, outside: dict[str, dict[str, int]]) -> dict[str, Any]:
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
                                 'eligibility': 'eligible' if eligible else 'unknown',
                                 'identity_mass_by_host': identity_mass(part['by_host'], outside)}
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


def history_lines(history: list[dict[str, Any]], account: str, window_kind: str, provider: str = 'claude',
                  window_minutes: int | None = None, source: str | None = None) -> tuple[list[dict], list[dict]]:
    """(deduped non-probe lines sorted by observed_at, probe/invalid exclusions) for one account and quota.

    Codex passes window_minutes and source so only the one weekly rollout quota is ever read.
    """
    seen, lines, probes = set(), [], []
    for line in history:
        if line.get('provider') != provider or line.get('window_kind') != window_kind:
            continue
        if window_minutes is not None and line.get('window_minutes') != window_minutes:
            continue
        if line.get('account_id') != account:
            continue
        if line.get('source') == 'probe':
            probes.append({'observed_at': line.get('observed_at'), 'reason': 'probe_source'})
            continue
        if source is not None and line.get('source') != source:
            probes.append({'observed_at': line.get('observed_at'), 'reason': 'source_not_' + source})
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


def pair_samples(acct: Account, timeline: Timeline, lines: list[dict], probes: list[dict], eligible: bool,
                 outside: dict[str, dict[str, int]], provider: str = 'claude') -> dict[str, Any]:
    """Pair each positive pct change with the previous pct-change observation.

    Delta = 0 observations extend the open interval instead of closing it, so their tokens stay in the next
    sample; a pct decrease starts a new base and forms no sample. Exclusions judge the whole interval.
    """
    samples, exclusions = [], list(probes)
    union_recs: list[Rec] = []
    formed = 0
    base, run = (lines[0], []) if lines else (None, [])
    for line in lines[1:]:
        delta = line['pct'] - base['pct']
        if delta == 0:
            run.append(line)
            continue
        if delta < 0:
            exclusions.append({'from': base['observed_at'], 'to': line['observed_at'], 'delta_pct': delta,
                               'reason': 'pct_decrease_new_base'})
            base, run = line, []
            continue
        t1, t2 = parse_ts(base['observed_at']), parse_ts(line['observed_at'])
        resets = [_minute(l.get('resets_at')) for l in (base, *run, line)]
        recs = timeline.interval(t1, t2)
        union_recs.extend(recs)
        formed += 1
        part = account_partition(acct, recs)
        sample = {'from': base['observed_at'], 'to': line['observed_at'],
                                  'delta_pct': delta, 'interval_s': round(t2 - t1, 3),
                                  'observations': len(run) + 2, 'tokens': part['tokens'],
                                  'dollars': round(part['dollars'], 6)}
        sample.update(_coverage_fields(part, eligible))
        if any(r is None for r in resets):
            reason = 'reset_unknown'
        elif len(set(resets)) > 1:
            reason = 'reset_crossing'
        elif t2 == t1:
            reason = 'stale'
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
        base, run = line, []
    union = account_partition(acct, union_recs)
    return {'valid_samples': len(samples), 'span_pct': sum(s['delta_pct'] for s in samples), 'samples': samples,
            'exclusions': exclusions, 'min_samples': METHOD_B_MIN_SAMPLES,
            'min_span_pct': METHOD_B_MIN_SPAN, 'min_coverage': SAMPLE_MIN_COVERAGE,
            'interval_union': {'intervals': formed, 'tokens': union['tokens'],
                               'identity_mass_by_host': identity_mass(union['by_host'], outside, provider)}}


def fit_origin(samples: list[dict[str, Any]], value: Any) -> tuple[float, float] | None:
    """Least squares through the origin of value(sample) on delta_pct -> (k, median abs % error of delta_pct)."""
    sxx = sum(s['delta_pct'] ** 2 for s in samples)
    sxy = sum(s['delta_pct'] * value(s) for s in samples)
    if sxx == 0 or sxy <= 0:
        return None
    k = sxy / sxx
    errors = [abs(s['delta_pct'] - value(s) / k) / s['delta_pct'] for s in samples]
    return k, statistics.median(errors) * 100


def method_b(acct: Account, timeline: Timeline, history: list[dict[str, Any]], eligible: bool,
             outside: dict[str, dict[str, int]]) -> dict[str, Any]:
    lines, probes = history_lines(history, acct.id, QUOTA)
    out: dict[str, Any] = {'method': 'history_regression', 'quota': QUOTA,
                           **pair_samples(acct, timeline, lines, probes, eligible, outside)}
    samples = out['samples']
    fit = fit_origin(samples, lambda s: s['dollars'])
    if len(samples) < METHOD_B_MIN_SAMPLES or out['span_pct'] < METHOD_B_MIN_SPAN or fit is None:
        out.update(status='insufficient', coefficient=None,
                   reason=f"needs >= {METHOD_B_MIN_SAMPLES} valid samples spanning >= {METHOD_B_MIN_SPAN} pts")
        return out
    out.update(status='fitted', coefficient=round(fit[0], 4), residual_mape_pct=round(fit[1], 4))
    return out


def method_c(acct: Account, timeline: Timeline, history: list[dict[str, Any]], eligible: bool,
             outside: dict[str, dict[str, int]]) -> dict[str, Any]:
    """Codex: Method B sampling on the one weekly rollout quota only; fits $ and tokens per 1 %."""
    lines, probes = history_lines(history, acct.id, CODEX_QUOTA, 'codex', CODEX_QUOTA_MINUTES, CODEX_SOURCE)
    out: dict[str, Any] = {'method': 'history_regression', 'quota': CODEX_QUOTA,
                           'window_minutes': CODEX_QUOTA_MINUTES, 'source': CODEX_SOURCE,
                           **pair_samples(acct, timeline, lines, probes, eligible, outside, 'codex')}
    samples = out['samples']
    usd = fit_origin(samples, lambda s: s['dollars'])
    tok = fit_origin(samples, lambda s: s['tokens']['measured'])
    reasons = []
    if not eligible:
        reasons.append('provider unplaceable gate fails (every interval eligibility_unknown)')
    if len(samples) < METHOD_B_MIN_SAMPLES or out['span_pct'] < METHOD_B_MIN_SPAN:
        reasons.append(f"{len(samples)} valid samples spanning {out['span_pct']} pts; needs >= "
                       f"{METHOD_B_MIN_SAMPLES} spanning >= {METHOD_B_MIN_SPAN}")
    elif usd is None or tok is None:
        reasons.append('no positive measured mass in the valid samples')
    if reasons:
        out.update(status='insufficient', coefficient=None, tokens_per_pct=None, reason='; '.join(reasons))
        return out
    out.update(status='fitted', coefficient=round(usd[0], 4), residual_mape_pct=round(usd[1], 4),
               tokens_per_pct=round(tok[0], 1), tokens_residual_mape_pct=round(tok[1], 4))
    return out


def _window_observed_pct(history: list[dict[str, Any]], account: str, start: float, end: float) -> int | None:
    lines, _ = history_lines(history, account, QUOTA)
    inside = [l['pct'] for l in lines if start <= parse_ts(l['observed_at']) < end]
    return max(inside) if inside else None


def calibrate(src: Sources, config: dict[str, Any] | None, config_path: Path) -> dict[str, Any]:
    config = config or {}
    threshold = float(config.get('unplaceable_threshold', DEFAULT_UNPLACEABLE_THRESHOLD))
    retired, retired_report = retired_hosts(src, config)
    unplaceable = unplaceable_summary(src.records, threshold, retired)
    eligible = unplaceable['passes']
    # Partition order: retired host -> untimed -> window buckets; retired records never enter a window.
    timeline = Timeline([r for r in src.records if r.host not in retired])
    outside: dict[str, dict[str, int]] = {}
    for rec in src.records:
        if rec.provider != 'claude' or (rec.host not in retired and rec.cls != 'untimed'):
            continue
        host = outside.setdefault(rec.host, {'untimed': 0, 'retired': 0})
        host['retired' if rec.host in retired else 'untimed'] += rec.total
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
                                     'dollars_label': DOLLARS_LABEL},
                 'identity_mass_by_host': identity_mass(part['by_host'], outside)}
        entry.update(_coverage_fields(part, eligible))
        return entry

    for acct in accounts:
        if acct.role != 'fleet_only':
            continue
        entry = base(acct)
        a = method_a(acct, timeline, config, eligible, src.now, outside)
        b = method_b(acct, timeline, src.history, eligible, outside)
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
        'retired_hosts': retired_report,
        'identity_mass_by_host': identity_mass(account_partition(None, timeline.recs)['by_host'], outside),
        'entries': entries,
        'reported_only': reported_only,
        'codex': calibrate_codex(src, config, threshold, retired, retired_report),
    }


def codex_aliases(config: dict[str, Any] | None) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Private-config `account_aliases`: an alias merges two Codex creator ids only with a written justification."""
    listed = (config or {}).get('account_aliases') or []
    if not isinstance(listed, list):
        raise RollupError('calibration config: account_aliases must be a list')
    mapping: dict[str, str] = {}
    report: list[dict[str, Any]] = []
    for item in listed:
        if not isinstance(item, dict) or not item.get('account_id') or not item.get('alias_of'):
            raise RollupError('calibration config: each account_aliases entry needs account_id and alias_of')
        row = {'provider': item.get('provider') or 'codex', 'account_id': item['account_id'],
               'alias_of': item['alias_of']}
        justification = item.get('justification')
        if row['provider'] != 'codex':
            reason = 'only codex aliases are supported'
        elif not (isinstance(justification, str) and justification.strip()):
            reason = 'alias requires a justification'
        elif item['account_id'] == item['alias_of'] or item['alias_of'] in mapping or item['account_id'] in mapping:
            reason = 'alias is self-referential or chained'
        else:
            mapping[item['account_id']] = item['alias_of']
            report.append({**row, 'status': 'applied', 'basis': 'aliased', 'justification': justification})
            continue
        print(f'usage rollup: warning: account_aliases entry rejected: {reason}', file=sys.stderr)
        report.append({**row, 'status': 'rejected', 'reason': reason})
    return mapping, report


def codex_reported_only(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every Codex history line outside the one fitted quota (weekly `codex` rollout with an account)."""
    groups: dict[tuple, list[dict]] = {}
    for line in history:
        if line.get('provider') != 'codex':
            continue
        fitted = (line.get('source') == CODEX_SOURCE and line.get('window_kind') == CODEX_QUOTA
                  and line.get('window_minutes') == CODEX_QUOTA_MINUTES and line.get('account_id'))
        if fitted:
            continue
        key = (str(line.get('window_kind')), line.get('window_minutes') if isinstance(line.get('window_minutes'), int)
               else -1, str(line.get('source')), line.get('account_id') or 'null')
        groups.setdefault(key, []).append(line)
    out = []
    for (kind, minutes, source, account), lines in sorted(groups.items()):
        if account == 'null':
            reason = 'no account to fit'
        elif source != CODEX_SOURCE:
            reason = f'source {source} is not {CODEX_SOURCE}'
        else:
            reason = 'auxiliary limit window; one quota per calibration (weekly codex)'
        out.append({'provider': 'codex', 'window_kind': kind, 'window_minutes': None if minutes == -1 else minutes,
                    'source': source, 'account_id': account, 'observations': len(lines),
                    'latest_pct': lines[-1].get('pct'), 'reason': reason})
    return out


def calibrate_codex(src: Sources, config: dict[str, Any], threshold: float, retired: set[str],
                    retired_report: list[dict[str, Any]]) -> dict[str, Any]:
    """Method C per (creator account, codex, weekly); no Method A, no probe, measurement of retained evidence only."""
    sessions = src.codex
    unplaceable = codex_unplaceable_summary(sessions, threshold, retired)
    eligible = unplaceable['passes']
    live = [s for s in sessions if s.host not in retired]
    timeline = Timeline((r for s in live for r in s.placed), 'codex')
    outside = codex_outside(sessions, retired)
    configured = {a.get('account_id'): Account(a) for a in config.get('accounts') or []
                  if a.get('provider') == 'codex' and a.get('account_id')}
    aliased: dict[str, list[str]] = {}
    aliases_of: dict[str, str] = {}
    for row in src.codex_alias_report:
        if row['status'] == 'applied':
            aliased.setdefault(row['alias_of'], []).append(row['account_id'])
            aliases_of[row['account_id']] = row['alias_of']
    history_ids = {aliases_of.get(l['account_id'], l['account_id']) for l in src.history
                   if l.get('provider') == 'codex' and l.get('source') == CODEX_SOURCE
                   and l.get('window_kind') == CODEX_QUOTA and l.get('window_minutes') == CODEX_QUOTA_MINUTES
                   and isinstance(l.get('account_id'), str) and l.get('account_id')}
    ids = sorted({s.account for s in sessions if s.account} | set(configured) | history_ids)
    history = [({**l, 'account_id': aliases_of[l['account_id']]} if l.get('provider') == 'codex'
                and l.get('account_id') in aliases_of else l) for l in src.history]
    reported_only = codex_reported_only(src.history)
    entries = []
    for account_id in ids:
        acct = configured.get(account_id) or Account({'account_id': account_id, 'label': None,
                                                      'provider': 'codex', 'role': None})
        own = [s for s in sessions if s.account == account_id]
        own_live = [s for s in own if s.host not in retired]
        part = account_partition(acct, timeline.recs)
        masses = {k: sum(s.masses()[k] for s in own_live) for k in ('placed', 'untimed', 'unreconciled', 'unverifiable')}
        denom = sum(masses.values())
        entry: dict[str, Any] = {
            'account_id': account_id, 'label': acct.label, 'role': acct.role, 'provider': 'codex',
            'quota': CODEX_QUOTA, 'window_minutes': CODEX_QUOTA_MINUTES, 'unit': 'usd_per_pct',
            'basis': 'aliased' if account_id in aliased else 'creator_account',
            'unplaceable': {k: unplaceable[k] for k in ('ratio', 'threshold', 'passes')},
            'reconciliation': _recon_block(own_live),
            'reconciliation_masses': masses,
            'completeness': round(masses['placed'] / denom, 6) if denom else None,
            'measured_rollup': {'tokens': part['tokens'], 'dollars': round(part['dollars'], 4),
                                'untimed_tokens': masses['untimed'], 'unreconciled_tokens': masses['unreconciled'],
                                'unverifiable_tokens': masses['unverifiable'],
                                'retired_tokens': sum(s.total for s in own if s.host in retired),
                                'dollars_label': DOLLARS_LABEL},
            'identity_mass_by_host': identity_mass(part['by_host'], outside, 'codex'),
            'reported_only': [r for r in reported_only if r['account_id'] == account_id],
        }
        if account_id in aliased:
            entry['aliased_from'] = sorted(aliased[account_id])
        entry.update(_coverage_fields(part, eligible))
        c = method_c(acct, timeline, history, eligible, outside)
        entry['methods'] = {'history_regression': c}
        entry['method'] = 'history_regression'
        if c['coefficient'] is not None:
            entry.update(status='fitted', coefficient=c['coefficient'], tokens_per_pct=c['tokens_per_pct'],
                         residual_mape_pct=c['residual_mape_pct'],
                         tokens_residual_mape_pct=c['tokens_residual_mape_pct'], points=c['valid_samples'])
        else:
            reason = c['reason']
            if not eligible:
                reason += f" (unplaceable_mass {unplaceable['ratio']:.4f} > {threshold})"
            entry.update(status='insufficient', coefficient=None, tokens_per_pct=None, reason=reason)
        entry['exclusions'] = [{'from': x.get('from') or x.get('observed_at'), 'reason': x['reason']}
                               for x in c['exclusions']]
        entries.append(entry)
    fleet = account_partition(None, timeline.recs)
    return {
        'provider': 'codex', 'quota': CODEX_QUOTA, 'window_minutes': CODEX_QUOTA_MINUTES, 'source': CODEX_SOURCE,
        'methods': ['history_regression'], 'full_week_100': 'not applicable to Codex (no 100 % rule)',
        'unplaceable': unplaceable,
        'retired_hosts': retired_report,
        'reconciliation': _recon_block([s for s in sessions if s.host not in retired]),
        'identity_mass_by_host': identity_mass(fleet['by_host'], outside, 'codex'),
        'account_aliases': src.codex_alias_report,
        'entries': entries,
        'reported_only': reported_only,
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
    seen = ({r.account for r in src.records if r.account} | {s.account for s in src.codex if s.account}
            | {a.get(k) for a in (config or {}).get('account_aliases') or [] if isinstance(a, dict)
               for k in ('account_id', 'alias_of') if isinstance(a.get(k), str) and a.get(k)})
    others = sorted(a for a in seen if a not in mapping)
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
        cx = spec['codex']
        if cx['sessions']:
            rc = cx['reconciliation']
            out.append(f"  codex: sessions={cx['sessions']} placed={cx['placed_tokens']:,} dollars~{cx['dollars']:,.2f}"
                       f" completeness={cx['completeness']} reconciled/partial/unverifiable="
                       f"{rc['reconciled']['sessions']}/{rc['partial']['sessions']}/{rc['unverifiable']['sessions']}")
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
        out.append(f"calibration: unplaceable ratio={u['ratio']} threshold={u['threshold']} passes={u['passes']}"
                   f" retired_mass={sum(r['tokens'] for r in u.get('retired_mass', []))}")
        for e in cal['entries']:
            out.append(f"  {e['account_id']} ({e['role']}): status={e['status']} coefficient={e['coefficient']}"
                       f" measured_cov={e.get('measured_coverage')} reason={e.get('reason', '')}")
        cx = cal['codex']
        out.append(f"codex calibration: unplaceable ratio={cx['unplaceable']['ratio']}"
                   f" passes={cx['unplaceable']['passes']} reported_only={len(cx['reported_only'])}")
        for e in cx['entries']:
            out.append(f"  {e['account_id']}: status={e['status']} usd_per_pct={e['coefficient']}"
                       f" tokens_per_pct={e['tokens_per_pct']} completeness={e['completeness']}"
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
    aliases, src.codex_alias_report = codex_aliases(config)
    src.apply_codex_aliases(aliases)
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
