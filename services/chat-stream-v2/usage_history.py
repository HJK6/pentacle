"""Append-only percent-of-limit history (``usage_history.jsonl``).

Contract: docs/usage_accounting.md § Percent history. Every line has exactly
``HISTORY_FIELDS`` and every value in a line comes from one observation. Writers
are the Thoth daemon (Codex ``rollout`` lines from provenance pushes), the usage
collector (``cache`` and ``probe`` lines) and the agent-orch readback (``cache``
lines; agent-orch keeps its own stdlib copy in ``agent_orch/usage_readback.py``,
pinned to this format by a parity test). Stdlib only: the satellite and the
collector import it outside the daemon.
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path
from typing import Any, Iterable

from usage_accounting import iso_from_ms, iso_utc

HISTORY_FIELDS = (
    'observed_at', 'probed_at', 'host', 'provider', 'account_id',
    'window_kind', 'window_minutes', 'pct', 'resets_at', 'source',
)
WEEK_MINUTES = 10080
HISTORY_FILENAME = 'usage_history.jsonl'


def _pct(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    pct = int(round(value))
    return pct if 0 <= pct <= 100 else None


def history_line(*, observed_at: str, probed_at: str, host: str, provider: str,
                 account_id: str | None, window_kind: str, window_minutes: int,
                 pct: int, resets_at: str | None, source: str) -> dict[str, Any]:
    return {
        'observed_at': observed_at, 'probed_at': probed_at, 'host': host,
        'provider': provider, 'account_id': account_id, 'window_kind': window_kind,
        'window_minutes': window_minutes, 'pct': pct, 'resets_at': resets_at,
        'source': source,
    }


def dedupe_key(line: dict[str, Any]) -> tuple | None:
    """Identity of the observation a line records; None never dedupes.

    ``rollout`` uses the spec tuple (first observed_at wins, across hosts);
    ``cache`` is one snapshot of one host's cache; each ``probe`` is distinct.
    """
    source = line.get('source')
    tuple_key = (line.get('provider'), line.get('account_id'), line.get('window_kind'),
                 line.get('window_minutes'), line.get('resets_at'), line.get('pct'))
    if source == 'rollout':
        return ('rollout', *tuple_key)
    if source == 'cache':
        return ('cache', line.get('host'), line.get('observed_at'), *tuple_key)
    return None


def _fable_window(utilization: dict[str, Any]) -> tuple[Any, Any]:
    """The weekly Fable window: ``seven_day_fable`` or the scoped ``limits`` row."""
    direct = utilization.get('seven_day_fable')
    if isinstance(direct, dict):
        return direct.get('utilization'), direct.get('resets_at')
    for entry in utilization.get('limits') or ():
        if not isinstance(entry, dict) or entry.get('kind') != 'weekly_scoped':
            continue
        scope = entry.get('scope') if isinstance(entry.get('scope'), dict) else {}
        model = scope.get('model') if isinstance(scope.get('model'), dict) else {}
        if str(model.get('display_name') or '').strip().casefold() == 'fable':
            return entry.get('percent'), entry.get('resets_at')
    return None, None


def claude_cache_lines(data: Any, *, host: str, probed_at: str) -> list[dict[str, Any]]:
    """Lines from ONE read of ``~/.claude.json`` (source ``cache``).

    The account is the OAuth organization only when the cache and the OAuth
    login name the same ``accountUuid`` in this same read (C1); otherwise null.
    A missing/invalid ``fetchedAtMs`` writes nothing.
    """
    if not isinstance(data, dict):
        return []
    cache = data.get('cachedUsageUtilization')
    oauth = data.get('oauthAccount')
    cache = cache if isinstance(cache, dict) else {}
    oauth = oauth if isinstance(oauth, dict) else {}
    observed_at = iso_from_ms(cache.get('fetchedAtMs'))
    if observed_at is None:
        return []
    cache_account = cache.get('accountUuid')
    org = oauth.get('organizationUuid')
    account_id = org if (
        isinstance(cache_account, str) and cache_account
        and cache_account == oauth.get('accountUuid')
        and isinstance(org, str) and org
    ) else None
    utilization = cache.get('utilization') if isinstance(cache.get('utilization'), dict) else {}
    seven = utilization.get('seven_day') if isinstance(utilization.get('seven_day'), dict) else {}
    windows = (
        ('seven_day', seven.get('utilization'), seven.get('resets_at')),
        ('seven_day_fable', *_fable_window(utilization)),
    )
    lines = []
    for window_kind, raw_pct, resets_at in windows:
        pct = _pct(raw_pct)
        if pct is None:
            continue
        lines.append(history_line(
            observed_at=observed_at, probed_at=probed_at, host=host, provider='claude',
            account_id=account_id, window_kind=window_kind, window_minutes=WEEK_MINUTES,
            pct=pct, resets_at=iso_utc(resets_at), source='cache',
        ))
    return lines


def claude_probe_lines(payload: dict[str, Any], *, host: str, probed_at: str) -> list[dict[str, Any]]:
    """Display-path scrape lines (source ``probe``); excluded from fitting."""
    lines = []
    for window_kind, pct_key, resets_key in (
        ('seven_day', 'week_all_pct', 'week_all_resets'),
        ('seven_day_fable', 'week_fable_pct', 'week_fable_resets'),
    ):
        pct = _pct(payload.get(pct_key))
        if pct is None:
            continue
        lines.append(history_line(
            observed_at=probed_at, probed_at=probed_at, host=host, provider='claude',
            account_id=None, window_kind=window_kind, window_minutes=WEEK_MINUTES,
            pct=pct, resets_at=iso_utc(payload.get(resets_key)), source='probe',
        ))
    return lines


def codex_probe_lines(payload: dict[str, Any], *, host: str, probed_at: str) -> list[dict[str, Any]]:
    pct = _pct(payload.get('pct'))
    if pct is None:
        return []
    return [history_line(
        observed_at=probed_at, probed_at=probed_at, host=host, provider='codex',
        account_id=None, window_kind='codex', window_minutes=WEEK_MINUTES,
        pct=pct, resets_at=iso_utc(payload.get('resets_at_iso')), source='probe',
    )]


def rollout_line(data: dict[str, Any], *, host: str, provider: str, probed_at: str) -> dict[str, Any]:
    """Map one validated ``rate_limit`` item (spec Target State §2 table)."""
    return history_line(
        observed_at=iso_utc(data['observed_at']), probed_at=probed_at, host=host,
        provider=provider, account_id=data['account_id'], window_kind=data['window_kind'],
        window_minutes=data['window_minutes'], pct=data['pct'],
        resets_at=iso_utc(data['resets_at']), source='rollout',
    )


class HistoryLog:
    """Locked append-only writer with whole-file observation dedupe.

    Several processes append (daemon, collector, readback, Thoth backfill), so
    every append takes an exclusive ``flock``, first reads any lines appended
    since this instance last looked, then appends only unseen observations.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()
        self._keys: set[tuple] = set()
        self._offset = 0
        self._file_id: tuple[int, int] | None = None

    def _sync(self, handle) -> None:
        stat = os.fstat(handle.fileno())
        file_id = (stat.st_dev, stat.st_ino)
        if file_id != self._file_id or stat.st_size < self._offset:
            self._keys.clear()
            self._offset = 0
            self._file_id = file_id
        if stat.st_size <= self._offset:
            return
        handle.seek(self._offset)
        chunk = handle.read(stat.st_size - self._offset)
        complete = chunk[: chunk.rfind(b'\n') + 1]
        for raw in complete.splitlines():
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            key = dedupe_key(line) if isinstance(line, dict) else None
            if key is not None:
                self._keys.add(key)
        self._offset += len(complete)

    def append(self, lines: Iterable[dict[str, Any]], *, dry_run: bool = False) -> list[bool]:
        """Append unseen lines; return one ``written`` flag per input line."""
        lines = list(lines)
        if not lines:
            return []
        with self._lock:
            if dry_run and not self.path.exists():
                seen: set[tuple] = set()
                flags = []
                for line in lines:
                    key = dedupe_key(line)
                    flags.append(key is None or key not in seen)
                    if key is not None:
                        seen.add(key)
                return flags
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, 'a+b') as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    self._sync(handle)
                    seen = set(self._keys) if dry_run else self._keys
                    written: list[bool] = []
                    out: list[str] = []
                    for line in lines:
                        key = dedupe_key(line)
                        if key is not None and key in seen:
                            written.append(False)
                            continue
                        if key is not None:
                            seen.add(key)
                        out.append(json.dumps({field: line[field] for field in HISTORY_FIELDS},
                                              separators=(',', ':')) + '\n')
                        written.append(True)
                    if out and not dry_run:
                        size = os.fstat(handle.fileno()).st_size
                        prefix = ''
                        if size:
                            handle.seek(size - 1)
                            if handle.read(1) != b'\n':
                                prefix = '\n'  # never glue onto a torn line
                        handle.seek(0, os.SEEK_END)
                        handle.write((prefix + ''.join(out)).encode('utf-8'))
                        handle.flush()
                        self._sync(handle)
                    return written
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
