"""``usage_provenance`` (payload version 1): validation, admission and export.

Contract: docs/usage_accounting.md § Provenance. The event is metadata only: it
never reads or writes token totals, and admission ignores stream open/closed
state and session generation, so closed and archived native sessions accept
it. One ``ProvenanceSink`` serves satellites (``event.push``), Thoth-local
ingest and the Thoth backfill tool. Stdlib only apart from the daemon Store
callable passed in, so the satellite imports the exporter half directly.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator

from usage_accounting import (
    IDENTITY_FIELDS, PROVENANCE_DATA_FIELDS, PROVENANCE_ITEM_FIELDS,
    PROVENANCE_KIND_PROVIDER, iso_utc, native_provenance,
)
from usage_history import HistoryLog, rollout_line

log = logging.getLogger('chat_streamd_v2.usage_provenance')

PAYLOAD_VERSION = 1
MAX_ITEMS = 2000
BACKFILL_BATCH = 500
_PAYLOAD_FIELDS = frozenset({'version', 'items', 'dry_run'})
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
#: Per-item outcomes that mean "not stored"; everything else was admitted.
REJECTIONS = frozenset({
    'bad_provenance', 'unsupported_kind', 'unsupported_kind_for_provider',
    'unknown_native_session', 'unknown_record',
})
_LEDGER_KINDS = frozenset({'claude_record', 'codex_response'})


def _now_iso() -> str:
    return iso_utc(datetime.now(timezone.utc).isoformat()) or ''


def _str(value: Any, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _opt_str(value: Any, limit: int) -> bool:
    return value is None or _str(value, limit)


def _count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _valid_identity(identity: Any) -> bool:
    if identity is None:
        return True
    if not isinstance(identity, dict) or set(identity) != IDENTITY_FIELDS:
        return False
    account, source, conflict = identity['account_id'], identity['account_source'], identity['conflict']
    if not _opt_str(account, 128) or not _opt_str(identity['cli_version'], 64):
        return False
    if type(conflict) is not int or conflict not in (0, 1) or source not in ('transcript', 'unknown'):
        return False
    if conflict:
        return account is None and source == 'transcript'
    return (account is None) == (source == 'unknown')


def validate_item(item: Any) -> str | None:
    """Exact per-kind key sets and types; None when admissible."""
    if not isinstance(item, dict) or set(item) != PROVENANCE_ITEM_FIELDS:
        return 'bad_provenance'
    kind, provider, data = item['kind'], item['provider'], item['data']
    if kind not in PROVENANCE_DATA_FIELDS:
        return 'unsupported_kind'
    if provider not in ('claude', 'codex'):
        return 'bad_provenance'
    if PROVENANCE_KIND_PROVIDER[kind] != provider:
        return 'unsupported_kind_for_provider'
    if not isinstance(data, dict) or set(data) != PROVENANCE_DATA_FIELDS[kind]:
        return 'bad_provenance'
    native, digest, identity = item['native_session_id'], item['source_file_identity_digest'], item['identity']
    if kind == 'rate_limit':
        ok = (
            native is None and digest is None and identity is None
            and _opt_str(data['account_id'], 128)
            and _str(data['window_kind'], 64)
            and type(data['window_minutes']) is int and data['window_minutes'] > 0
            and type(data['pct']) is int and 0 <= data['pct'] <= 100
            and iso_utc(data['resets_at']) is not None
            and isinstance(data['observed_at'], str) and iso_utc(data['observed_at']) is not None
        )
        return None if ok else 'bad_provenance'
    if not _str(native, 256) or not (digest is None or (isinstance(digest, str) and _HEX64.fullmatch(digest))):
        return 'bad_provenance'
    if not _valid_identity(identity):
        return 'bad_provenance'
    observed_at = data['observed_at']
    if observed_at is not None and (not isinstance(observed_at, str) or iso_utc(observed_at) is None):
        return 'bad_provenance'
    if not _opt_str(data['model'], 128):
        return 'bad_provenance'
    if kind == 'claude_record':
        return None if _str(data['record_key'], 256) else 'bad_provenance'
    counts = ('input', 'cached_input', 'cache_write_input', 'output', 'reasoning_output')
    if (
        not _str(data['response_id'], 256)
        or not all(_count(data[field]) for field in counts)
        or data['cached_input'] > data['input']
        or data['reasoning_output'] > data['output']
    ):
        return 'bad_provenance'
    return None


def _normalized(item: dict[str, Any]) -> dict[str, Any]:
    data = dict(item['data'])
    if data.get('observed_at') is not None:
        data['observed_at'] = iso_utc(data['observed_at'])
    return {**item, 'data': data}


class ProvenanceSink:
    """Admit one ``usage_provenance`` payload for an authenticated host."""

    def __init__(self, record: Callable[..., Awaitable[dict[str, Any]]],
                 history: HistoryLog | None, *, now: Callable[[], str] = _now_iso) -> None:
        self._record = record
        self.history = history
        self._now = now

    async def admit(self, host: str, payload: Any) -> dict[str, Any]:
        if (
            not isinstance(payload, dict) or not set(payload) <= _PAYLOAD_FIELDS
            or 'items' not in payload or not isinstance(payload['items'], list)
            or ('dry_run' in payload and not isinstance(payload['dry_run'], bool))
        ):
            return {'version': PAYLOAD_VERSION, 'error': 'bad_provenance'}
        if payload.get('version') != PAYLOAD_VERSION:
            return {'version': PAYLOAD_VERSION, 'error': 'unsupported_version'}
        items = payload['items']
        if len(items) > MAX_ITEMS:
            return {'version': PAYLOAD_VERSION, 'error': 'batch_too_large'}
        dry_run = bool(payload.get('dry_run'))
        received_at = self._now()
        outcomes: list[str | None] = [None] * len(items)
        ledger: list[tuple[int, dict[str, Any]]] = []
        limits: list[tuple[int, dict[str, Any]]] = []
        for index, item in enumerate(items):
            reason = validate_item(item)
            if reason is not None:
                outcomes[index] = reason
            elif item['kind'] in _LEDGER_KINDS:
                ledger.append((index, _normalized(item)))
            else:
                limits.append((index, item))
        known: list[str] = []
        unknown: list[str] = []
        identities = 0
        if ledger:
            stored = await self._record(host, [item for _, item in ledger], dry_run=dry_run)
            for (index, _item), outcome in zip(ledger, stored['outcomes']):
                outcomes[index] = outcome
            known, unknown = stored['known_native_sessions'], stored['unknown_native_sessions']
            identities = stored['identities_changed']
        if limits:
            if self.history is None:
                for index, _item in limits:
                    outcomes[index] = 'history_unavailable'
            else:
                lines = [rollout_line(item['data'], host=host, provider=item['provider'], probed_at=received_at)
                         for _, item in limits]
                written = await asyncio.to_thread(self.history.append, lines, dry_run=dry_run)
                for (index, _item), wrote in zip(limits, written):
                    outcomes[index] = 'recorded' if wrote else 'replayed'
        counts = Counter(outcome for outcome in outcomes if outcome)
        if counts.get('recorded') and not dry_run:
            log.info('usage provenance host=%s %s', host, dict(sorted(counts.items())),
                     extra={'subsystem': 'usage_provenance', 'bug_ref': 'usage_provenance_export_2026_10'})
        return {
            'version': PAYLOAD_VERSION,
            'dry_run': dry_run,
            'items': len(items),
            'counts': dict(sorted(counts.items())),
            'identities_changed': identities,
            'known_native_sessions': known,
            'unknown_native_sessions': unknown,
            'rejected': [
                {'index': index, 'reason': outcome}
                for index, outcome in enumerate(outcomes) if outcome in REJECTIONS
            ][:100],
        }


# --- exporter: transcript discovery and whole-file items --------------------

DEFAULT_CLAUDE_ROOT = '~/.claude/projects'
DEFAULT_CODEX_ROOT = '~/.codex/sessions'
_CLAUDE_MARKERS = (b'"assistant"', b'credential_org')
_CODEX_MARKERS = (b'"session_meta"', b'"turn_context"', b'"token_usage_record"', b'"token_count"')


def iter_transcripts(provider: str, root: str | os.PathLike[str]) -> Iterator[str]:
    """Bounded walk of one provider's transcript root, sorted for resumability.

    Claude: ``<root>/**/*.jsonl`` including ``subagents/``; Codex:
    ``<root>/**/rollout-*.jsonl``. Symlinked directories are not followed.
    """
    base = Path(root).expanduser()
    if not base.is_dir():
        return
    found = []
    for directory, subdirs, files in os.walk(base):
        subdirs.sort()
        for name in files:
            if not name.endswith('.jsonl'):
                continue
            if provider == 'codex' and not name.startswith('rollout-'):
                continue
            found.append(os.path.join(directory, name))
    yield from sorted(found)


def source_file_identity_digest(path: str) -> str:
    """Same digest the satellite uses for usage (path plus device/inode)."""
    import hashlib

    try:
        stat = os.stat(path)
        identity = f'{os.path.abspath(path)}\0{stat.st_dev}\0{stat.st_ino}'
    except OSError:
        identity = os.path.abspath(path)
    return hashlib.sha256(identity.encode('utf-8', 'surrogateescape')).hexdigest()


def codex_header_records(path: str, limit: int = 4 * 1024 * 1024) -> list[dict[str, Any]]:
    """The ``session_meta`` record(s) at a rollout's head (the line can be large)."""
    records = []
    try:
        with open(path, 'rb') as handle:
            read = 0
            for raw in handle:
                read += len(raw)
                if b'"session_meta"' in raw:
                    try:
                        record = json.loads(raw)
                    except ValueError:
                        record = None
                    if isinstance(record, dict) and record.get('type') == 'session_meta':
                        records.append(record)
                        break
                if read >= limit:
                    break
    except OSError:
        return []
    return records


def file_items(provider: str, path: str) -> list[dict[str, Any]]:
    """Every provenance item for one whole transcript, read line by line.

    Only lines that can carry provenance are parsed; the rest are skipped by a
    byte-marker check so multi-GB backfills stay cheap.
    """
    markers = _CLAUDE_MARKERS if provider == 'claude' else _CODEX_MARKERS
    records = []
    with open(path, 'rb') as handle:
        for raw in handle:
            if not any(marker in raw for marker in markers):
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return native_provenance(provider, records, complete=True,
                             source_file_identity_digest=source_file_identity_digest(path))


class BackfillCursor:
    """Resumable per-file cursor: a file whose (size, mtime) is unchanged is skipped."""

    def __init__(self, path: str | os.PathLike[str] | None) -> None:
        self.path = Path(path).expanduser() if path else None
        self.done: dict[str, list[float]] = {}
        if self.path and self.path.exists():
            try:
                loaded = json.loads(self.path.read_text())
                if isinstance(loaded, dict) and isinstance(loaded.get('done'), dict):
                    self.done = loaded['done']
            except (OSError, ValueError):
                self.done = {}

    @staticmethod
    def _stamp(path: str) -> list[float] | None:
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return [stat.st_size, stat.st_mtime]

    def is_done(self, path: str) -> bool:
        stamp = self._stamp(path)
        return stamp is not None and self.done.get(path) == stamp

    def mark(self, path: str, stamp: list[float] | None) -> None:
        if stamp is None or self.path is None:
            return
        self.done[path] = stamp
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'version': 1, 'done': self.done}, sort_keys=True))
        os.replace(tmp, self.path)


async def run_backfill(push: Callable[[list[dict[str, Any]], bool], Awaitable[dict[str, Any]]], *,
                       roots: dict[str, str], cursor: BackfillCursor, dry_run: bool,
                       batch: int = BACKFILL_BATCH) -> dict[str, Any]:
    """Walk local transcripts and push their provenance in bounded batches.

    ``push(items, dry_run)`` returns a sink acknowledgement. A dry run never
    advances the cursor. Returns the summary printed by both CLIs.
    """
    summary: dict[str, Any] = {'dry_run': dry_run, 'providers': {}}
    for provider, root in roots.items():
        stats: Counter = Counter()
        found: set[str] = set()
        known: set[str] = set()
        unknown: set[str] = set()
        for path in iter_transcripts(provider, root):
            stats['files'] += 1
            if not dry_run and cursor.is_done(path):
                stats['files_skipped_cursor'] += 1
                continue
            stamp = BackfillCursor._stamp(path)
            try:
                items = await asyncio.to_thread(file_items, provider, path)
            except OSError as exc:
                stats['files_unreadable'] += 1
                log.warning('backfill skipped unreadable transcript: %s', exc)
                continue
            found.update(f"{item['provider']}:{item['native_session_id']}" for item in items if item['native_session_id'])
            complete = True
            for start in range(0, len(items), batch):
                ack = await push(items[start:start + batch], dry_run)
                if not isinstance(ack, dict) or ack.get('error'):
                    stats['batches_failed'] += 1
                    complete = False
                    log.warning('backfill batch failed path_digest=%s ack=%s',
                                source_file_identity_digest(path)[:12], (ack or {}).get('error') if isinstance(ack, dict) else ack)
                    break
                stats['batches'] += 1
                for outcome, count in (ack.get('counts') or {}).items():
                    stats[outcome] += count
                known.update(ack.get('known_native_sessions') or ())
                unknown.update(ack.get('unknown_native_sessions') or ())
            if complete and not dry_run:
                cursor.mark(path, stamp)
        stats['native_sessions_found'] = len(found)
        stats['native_sessions_known_in_ledger'] = len(known)
        stats['native_sessions_unknown_to_ledger'] = len(unknown - known)
        summary['providers'][provider] = dict(sorted(stats.items()))
    return summary
