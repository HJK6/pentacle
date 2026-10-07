"""Usage persistence extension of Store, executed only on its SQLite worker."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from typing import Any

from usage_accounting import FIELDS, native_usage
from usage_admission import (
    TERMINAL_REASONS, classify, epoch, generation_from_row, iso_ms, parse_clock,
)
from v2_runtime import iso_now

log = logging.getLogger('chat_streamd_v2.store_usage')

DDL = (
    '''CREATE TABLE IF NOT EXISTS v2_usage_state (
        stream_id TEXT NOT NULL, generation TEXT NOT NULL,
        collection_host TEXT NOT NULL, collected_since TEXT NOT NULL,
        updated_at TEXT NOT NULL, revision INTEGER NOT NULL,
        tokens TEXT NOT NULL, reasons TEXT NOT NULL,
        PRIMARY KEY(stream_id,generation))''',
    '''CREATE TABLE IF NOT EXISTS v2_usage_records (
        host TEXT NOT NULL, provider TEXT NOT NULL, native_session_id TEXT NOT NULL,
        record_key TEXT NOT NULL, stream_id TEXT NOT NULL, generation TEXT NOT NULL,
        tokens TEXT NOT NULL,
        PRIMARY KEY(host,provider,native_session_id,record_key))''',
    # Provenance (docs/usage_accounting.md § Provenance): metadata only, joined
    # to v2_usage_records by the identical key and never read by admission.
    '''CREATE TABLE IF NOT EXISTS v2_usage_provenance (
        host TEXT NOT NULL, provider TEXT NOT NULL, native_session_id TEXT NOT NULL,
        record_key TEXT NOT NULL, observed_at TEXT, model TEXT,
        PRIMARY KEY(host,provider,native_session_id,record_key))''',
    '''CREATE TABLE IF NOT EXISTS v2_usage_identity (
        host TEXT NOT NULL, provider TEXT NOT NULL, native_session_id TEXT NOT NULL,
        account_id TEXT, account_source TEXT NOT NULL, first_observed_at TEXT,
        conflict INTEGER NOT NULL DEFAULT 0, cli_version TEXT,
        PRIMARY KEY(host,provider,native_session_id))''',
    '''CREATE TABLE IF NOT EXISTS v2_usage_codex_responses (
        host TEXT NOT NULL, native_session_id TEXT NOT NULL, response_id TEXT NOT NULL,
        observed_at TEXT, model TEXT, input INTEGER NOT NULL, cached_input INTEGER NOT NULL,
        cache_write_input INTEGER NOT NULL, output INTEGER NOT NULL,
        reasoning_output INTEGER NOT NULL,
        PRIMARY KEY(host,native_session_id,response_id))''',
    # Thread proof (docs/usage_accounting.md § Thread proof): one row per response, written in the response
    # row's transaction; read only by the rollup's reconciled_by_rows predicate.
    '''CREATE TABLE IF NOT EXISTS v2_usage_codex_thread_proof (
        host TEXT NOT NULL, native_session_id TEXT NOT NULL, response_id TEXT NOT NULL,
        transcript_seq INTEGER NULL CHECK(transcript_seq IS NULL OR transcript_seq >= 1),
        thread_input INTEGER NOT NULL CHECK(thread_input >= 0),
        thread_cached_input INTEGER NOT NULL CHECK(thread_cached_input >= 0),
        thread_cache_write_input INTEGER NOT NULL CHECK(thread_cache_write_input >= 0),
        thread_output INTEGER NOT NULL CHECK(thread_output >= 0),
        thread_reasoning_output INTEGER NOT NULL CHECK(thread_reasoning_output >= 0),
        source TEXT NOT NULL CHECK(source IN ('backfill','live')), observed_at TEXT,
        PRIMARY KEY(host,native_session_id,response_id))''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS v2_usage_codex_thread_proof_seq
        ON v2_usage_codex_thread_proof(host,native_session_id,transcript_seq) WHERE transcript_seq IS NOT NULL''',
    # Adverse evidence: insert-or-ignore, never deleted by any merge.
    '''CREATE TABLE IF NOT EXISTS v2_usage_codex_thread_flags (
        host TEXT NOT NULL, native_session_id TEXT NOT NULL, flag TEXT NOT NULL, response_id TEXT NOT NULL,
        detail TEXT, first_seen_at TEXT,
        PRIMARY KEY(host,native_session_id,flag,response_id))''',
    # Per-generation lifecycle history (docs/usage_accounting.md § Unfenced
    # spans): one row per open generation, never overwritten by a same-name
    # reopen and never archived; pruned 30 days after close by retention.py.
    '''CREATE TABLE IF NOT EXISTS v2_session_generation_history (
        host TEXT NOT NULL, session_name TEXT NOT NULL, generation TEXT NOT NULL,
        pane_pid TEXT, provider TEXT, created_at TEXT NOT NULL,
        precision TEXT NOT NULL CHECK (precision IN ('ms','s')),
        closed_at TEXT, native_session_id TEXT, source_file_identity_digest TEXT,
        PRIMARY KEY(host,session_name,generation))''',
    # Usage that has no generation to live in: refused records and held-span
    # loss/conflict rows. Never counted as measured, placed or priced.
    '''CREATE TABLE IF NOT EXISTS v2_usage_unplaced (
        host TEXT NOT NULL, provider TEXT NOT NULL, native_session_id TEXT NOT NULL,
        record_key TEXT NOT NULL, stream_id TEXT, source_pane_pid TEXT, transcript_ts TEXT,
        reason TEXT NOT NULL, first_seen_at TEXT NOT NULL, detail TEXT,
        PRIMARY KEY(host,provider,native_session_id,record_key))''',
)
THREAD_COLUMNS = ('thread_input', 'thread_cached_input', 'thread_cache_write_input', 'thread_output',
                  'thread_reasoning_output')
THREAD_FIELDS = ('input', 'cached_input', 'cache_write_input', 'output', 'reasoning_output')
SOURCE_FIELDS = ('jsonl_path', 'claude_session_id', 'observer_binding', 'pane_pid')


def _derived_projection(provider: str | None, tokens: dict[str, Any]) -> tuple[dict[str, int], dict[str, str]]:
    """Expose only the labelled Codex difference; native counters stay intact."""
    if (
        provider == 'codex'
        and type(tokens.get('input_total')) is int
        and type(tokens.get('cached_input')) is int
        and tokens['input_total'] >= tokens['cached_input'] >= 0
    ):
        return (
            {'uncached_input': tokens['input_total'] - tokens['cached_input']},
            {'uncached_input': 'input_total - cached_input'},
        )
    return {}, {}


def snapshot_conn(conn, row: dict[str, Any]) -> dict[str, Any]:
    """Project current associations over immutable-generation accounting state."""
    from store_specs import normalize_spec_ids

    stream_id = str(row.get('stream_id') or f"{row['host']}:{row['session_name']}")
    generation = str(row.get('session_generation') or '')
    provider = row.get('provider')
    state = conn.execute('SELECT * FROM v2_usage_state WHERE stream_id=? AND generation=?', (stream_id, generation)).fetchone()
    specs = normalize_spec_ids(row.get('spec_ids'), row.get('spec_id'))
    derived_tokens, derived_fields = _derived_projection(provider, json.loads(state['tokens']) if state else {})
    result = {
        'schema_version': 1, 'scope': 'stream', 'stream_id': stream_id,
        'session_generation': generation, 'host': row.get('host'), 'provider': provider,
        'collection_host': state['collection_host'] if state else None,
        'collected_since': state['collected_since'] if state else None,
        'updated_at': state['updated_at'] if state else None,
        'revision': state['revision'] if state else 0,
        'incomplete': True,
        'incomplete_reasons': json.loads(state['reasons']) if state else ['history_not_verified'],
        'tokens': json.loads(state['tokens']) if state else dict.fromkeys(FIELDS.get(provider, {}).values()),
        'spec_ids': specs,
        'attribution': {'mode': 'exclusive_primary' if specs else 'unattributed', 'spec_id': specs[0] if specs else None},
        'handoff_from_stream_id': row.get('handoff_from_stream_id'),
    }
    if derived_tokens:
        result['derived_tokens'] = derived_tokens
        result['derived_fields'] = derived_fields
    return result


class UsageStoreMixin:
    async def record_provenance(self, host: str, items: list[dict[str, Any]], *,
                                dry_run: bool = False, version: int = 1) -> dict[str, Any]:
        """Persist validated provenance items for ``host`` on the Store worker."""
        return await self.submit(lambda conn: record_provenance_conn(conn, host, items, dry_run=dry_run,
                                                                     version=version))

    async def usage_provenance_summary(self, stream_id: str, generation: str) -> dict[str, Any]:
        return await self.submit(lambda conn: provenance_summary_conn(conn, stream_id, generation))

    async def record_usage(self, expected: dict[str, Any], records: list[dict[str, Any]], *,
                           native_session_id: str, collection_host: str,
                           malformed: bool = False,
                           source_file_identity_digest: str | None = None) -> dict[str, Any] | None:
        """Record one native span, retaining the historic snapshot-only API."""
        outcome = await self.record_usage_checked(
            expected, records, native_session_id=native_session_id,
            collection_host=collection_host, malformed=malformed,
            source_file_identity_digest=source_file_identity_digest,
        )
        return outcome['snapshot'] if outcome['accepted'] else None

    async def record_usage_checked(
        self, expected: dict[str, Any], records: list[dict[str, Any]], *,
        native_session_id: str, collection_host: str,
        malformed: bool = False,
        source_file_identity_digest: str | None = None,
        timing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One native span: fence, identities, deltas and diagnostics commit together.

        ``timing`` is present only for a wire-v2 fenced item
        (``{'clock': <send sample>, 'receipt_now': <epoch>}``): each record is
        then classified with the fence generation's history row as its only
        candidate and only ``credited`` records are merged. ``None`` keeps the
        legacy (wire v1) admission exactly.
        """
        from store_specs import _session_row

        provider = expected.get('provider')
        if provider not in FIELDS or collection_host != expected.get('host'):
            return {'accepted': False, 'snapshot': None, 'reason': 'unauthorized_source_host',
                    'recorded': False, 'replayed': False}
        observations = []
        observed_records: list[tuple[int, Any]] = []
        reasons = {'malformed_record'} if malformed else set()
        for index, record in enumerate(records):
            observation, reason = native_usage(provider, record, native_session_id)
            if observation is not None:
                observations.append(observation)
                observed_records.append((index, observation))
            if reason:
                reasons.add(reason)

        def operation(conn):
            with conn:
                # BEGIN also makes the read/modify/write atomic against another
                # process opening this same DB, beyond Store's own queue boundary.
                conn.execute('BEGIN IMMEDIATE')
                current = _session_row(conn, conn.execute('SELECT * FROM sessions WHERE host=? AND session_name=?',
                                                        (expected.get('host'), expected.get('session_name'))).fetchone())
                if current is None or current.get('status') != 'open':
                    return {'accepted': False, 'snapshot': None, 'reason': 'session_not_open',
                            'recorded': False, 'replayed': False}
                if not expected.get('session_generation') or current.get('session_generation') != expected['session_generation']:
                    return {'accepted': False, 'snapshot': None, 'reason': 'generation_mismatch',
                            'recorded': False, 'replayed': False}
                if current.get('provider') != provider:
                    return {'accepted': False, 'snapshot': None, 'reason': 'provider_mismatch',
                            'recorded': False, 'replayed': False}
                if str(current.get('pane_pid') or '') != str(expected.get('pane_pid') or ''):
                    return {'accepted': False, 'snapshot': None, 'reason': 'pane_pid_mismatch',
                            'recorded': False, 'replayed': False}
                if any(current.get(field) != expected.get(field) for field in SOURCE_FIELDS):
                    return {'accepted': False, 'snapshot': None, 'reason': 'source_identity_mismatch',
                            'recorded': False, 'replayed': False}
                if source_file_identity_digest is not None and not observations:
                    return {'accepted': False, 'snapshot': None, 'reason': 'invalid_usage',
                            'recorded': False, 'replayed': False}

                updated_binding = current.get('observer_binding')
                if source_file_identity_digest is not None:
                    if not isinstance(updated_binding, dict):
                        return {'accepted': False, 'snapshot': None, 'reason': 'source_identity_unavailable',
                                'recorded': False, 'replayed': False}
                    transcript = updated_binding.get('transcript')
                    if transcript is not None and not isinstance(transcript, dict):
                        return {'accepted': False, 'snapshot': None, 'reason': 'source_identity_mismatch',
                                'recorded': False, 'replayed': False}
                    transcript = dict(transcript or {})
                    prior_digest = transcript.get('source_file_identity_digest')
                    if prior_digest is not None and prior_digest != source_file_identity_digest:
                        return {'accepted': False, 'snapshot': None, 'reason': 'source_identity_mismatch',
                                'recorded': False, 'replayed': False}
                    prior_native = transcript.get('native_session_id')
                    if prior_native is not None and prior_native != native_session_id:
                        return {'accepted': False, 'snapshot': None, 'reason': 'native_session_identity_mismatch',
                                'recorded': False, 'replayed': False}
                    transcript.update({
                        'source_file_identity_digest': source_file_identity_digest,
                        'native_session_id': native_session_id,
                        'provider': provider,
                    })
                    updated_binding = {**updated_binding, 'transcript': transcript}
                    if updated_binding != current.get('observer_binding'):
                        conn.execute(
                            'UPDATE sessions SET observer_binding=? WHERE host=? AND session_name=?',
                            (json.dumps(updated_binding, separators=(',', ':')),
                             current.get('host'), current.get('session_name')),
                        )
                        current = {**current, 'observer_binding': updated_binding}
                    # The identity this admission validated is copied once into
                    # the generation's history row, so a reopen that overwrites
                    # sessions.observer_binding cannot erase it.
                    history_bind_identity_conn(
                        conn, current.get('host'), current.get('session_name'), current.get('session_generation'),
                        native_session_id=native_session_id, digest=source_file_identity_digest,
                    )

                refused: list[dict[str, Any]] = []
                admitted = observations
                all_reasons_extra: set[str] = set()
                if timing is not None:
                    history = conn.execute(
                        'SELECT * FROM v2_session_generation_history WHERE host=? AND session_name=? AND generation=?',
                        (current.get('host'), current.get('session_name'), current.get('session_generation')),
                    ).fetchone()
                    fence = generation_from_row(history) if history is not None else None
                    candidates = [fence] if fence is not None else []
                    send = parse_clock(timing.get('clock'))
                    admitted = []
                    for index, observation in observed_records:
                        record = records[index]
                        outcome, _generation = classify(
                            epoch(record.get('transcript_ts')), candidates, send, float(timing['receipt_now']),
                        )
                        if outcome == 'credited':
                            admitted.append(observation)
                            continue
                        terminal = outcome in TERMINAL_REASONS
                        if terminal:
                            all_reasons_extra.add(outcome)
                        refused.append({'index': index, 'record_key': observation.record_key,
                                        'reason': outcome, 'transient': not terminal})

                before = current['usage']
                merged = merge_observations_conn(
                    conn, before=before, collection_host=collection_host, provider=provider,
                    observations=admitted, reasons=reasons | all_reasons_extra,
                )
                result = snapshot_conn(conn, current)
            if merged['changed']:
                log.info('usage recorded stream=%s revision=%s', before['stream_id'], result['revision'],
                         extra={'subsystem': 'usage_accounting', 'bug_ref': 'usage_accounting_ledger_2026_09'})
            outcome = {
                'accepted': True,
                'snapshot': result,
                'reason': None,
                'recorded': merged['recorded'],
                'replayed': bool(admitted) and not merged['recorded'],
            }
            if timing is not None:
                outcome['refused'] = refused
            return outcome

        return await self.submit(operation)

    async def record_unfenced(
        self, host: str, entries: list[dict[str, Any]], losses: list[dict[str, Any]] | None, *,
        clock: Any, receipt_now: float,
    ) -> dict[str, Any]:
        """Admit one frame's held spans and loss records in one transaction.

        Everything (credited records, refusals in ``v2_usage_unplaced``, loss
        and conflict rows) commits before the caller builds the ack.
        """
        return await self.submit(lambda conn: record_unfenced_conn(
            conn, host, entries, losses, clock=clock, receipt_now=receipt_now,
        ))

    async def prune_generation_history(self, now: float | None = None) -> int:
        return await self.submit(lambda conn: prune_generation_history_conn(
            conn, time.time() if now is None else now))


# --- shared merge core ---------------------------------------------------------

def _state_before(conn, stream_id: str, generation: str, provider: str) -> dict[str, Any]:
    """The usage projection snapshot_conn gives one (stream, generation)."""
    state = conn.execute('SELECT * FROM v2_usage_state WHERE stream_id=? AND generation=?',
                         (stream_id, generation)).fetchone()
    return {
        'stream_id': stream_id, 'session_generation': generation,
        'collection_host': state['collection_host'] if state else None,
        'collected_since': state['collected_since'] if state else None,
        'revision': state['revision'] if state else 0,
        'incomplete_reasons': json.loads(state['reasons']) if state else ['history_not_verified'],
        'tokens': json.loads(state['tokens']) if state else dict.fromkeys(FIELDS.get(provider, {}).values()),
    }


def merge_observations_conn(conn, *, before: dict[str, Any], collection_host: str, provider: str,
                            observations: list[Any], reasons: set[str]) -> dict[str, Any]:
    """Per-field max-merge into the ledger and the generation's state row.

    The one ledger writer for both the live-row and the historical writer:
    ownership_conflict, regression reasons and the revision bump are identical.
    Returns per-observation outcomes in input order.
    """
    tokens = dict(before['tokens'])
    all_reasons = set(before['incomplete_reasons']) | reasons
    sid, generation = before['stream_id'], before['session_generation']
    recorded = False
    outcomes: list[str] = []
    for observation in observations:
        key = (collection_host, provider, observation.native_session_id, observation.record_key)
        previous = conn.execute('SELECT * FROM v2_usage_records WHERE host=? AND provider=? AND native_session_id=? AND record_key=?', key).fetchone()
        if previous and (previous['stream_id'], previous['generation']) != (sid, generation):
            all_reasons.add('ownership_conflict')
            outcomes.append('ownership_conflict')
            continue
        old = json.loads(previous['tokens']) if previous else {}
        merged = {field: max(old.get(field, 0), value) for field, value in observation.tokens.items()}
        if previous and merged == old:
            outcomes.append('replayed')
            continue
        if any(value < old.get(field, 0) for field, value in observation.tokens.items()):
            all_reasons.add('counter_regression' if provider == 'codex' else 'message_usage_regression')
        recorded = True
        outcomes.append('recorded')
        for field, value in merged.items():
            tokens[field] = (tokens[field] or 0) + value - old.get(field, 0)
        conn.execute('''INSERT INTO v2_usage_records VALUES (?,?,?,?,?,?,?)
                        ON CONFLICT(host,provider,native_session_id,record_key)
                        DO UPDATE SET tokens=excluded.tokens''', (*key, sid, generation, json.dumps(merged)))
    changed = (before['collection_host'] is None or tokens != before['tokens']
               or sorted(all_reasons) != before['incomplete_reasons'])
    if changed:
        now = iso_now()
        conn.execute('''INSERT INTO v2_usage_state VALUES (?,?,?,?,?,?,?,?)
                        ON CONFLICT(stream_id,generation) DO UPDATE SET
                        updated_at=excluded.updated_at,revision=excluded.revision,
                        tokens=excluded.tokens,reasons=excluded.reasons''',
                     (sid, generation, collection_host, before['collected_since'] or now, now,
                      before['revision'] + 1, json.dumps(tokens), json.dumps(sorted(all_reasons))))
    return {'recorded': recorded, 'changed': changed, 'outcomes': outcomes}


def _min_iso(left: str | None, right: str | None) -> str | None:
    return min(value for value in (left, right) if value) if (left or right) else None


def _merge_identity(conn, host: str, provider: str, native: str,
                    identity: dict[str, Any], observed_at: str | None) -> bool:
    """Apply one transcript identity; conflict is sticky and monotonic."""
    row = conn.execute(
        'SELECT * FROM v2_usage_identity WHERE host=? AND provider=? AND native_session_id=?',
        (host, provider, native),
    ).fetchone()
    incoming_account = identity['account_id']
    incoming_conflict = int(identity['conflict'])
    if row is None:
        conn.execute(
            'INSERT INTO v2_usage_identity (host,provider,native_session_id,account_id,account_source,'
            'first_observed_at,conflict,cli_version) VALUES (?,?,?,?,?,?,?,?)',
            (host, provider, native, None if incoming_conflict else incoming_account,
             identity['account_source'], observed_at, incoming_conflict, identity['cli_version']),
        )
        return True
    account, source, conflict = row['account_id'], row['account_source'], int(row['conflict'])
    if not conflict:
        if incoming_conflict or (incoming_account and account and incoming_account != account):
            account, source, conflict = None, 'transcript', 1
        elif incoming_account and not account:
            account, source = incoming_account, 'transcript'
    first = _min_iso(row['first_observed_at'], observed_at)
    cli_version = row['cli_version'] or identity['cli_version']
    if (account, source, conflict, first, cli_version) == (
        row['account_id'], row['account_source'], int(row['conflict']), row['first_observed_at'], row['cli_version'],
    ):
        return False
    conn.execute(
        'UPDATE v2_usage_identity SET account_id=?,account_source=?,conflict=?,first_observed_at=?,cli_version=? '
        'WHERE host=? AND provider=? AND native_session_id=?',
        (account, source, conflict, first, cli_version, host, provider, native),
    )
    return True


def _put_flag(conn, host: str, native: str, flag: str, response_id: str, detail: str | None) -> bool:
    """Insert-or-ignore one adverse-evidence row; True when it is new."""
    cursor = conn.execute(
        'INSERT OR IGNORE INTO v2_usage_codex_thread_flags (host,native_session_id,flag,response_id,detail,'
        'first_seen_at) VALUES (?,?,?,?,?,?)', (host, native, flag, response_id, detail, iso_now()))
    return cursor.rowcount > 0


def _merge_proof(conn, host: str, native: str, data: dict[str, Any], *, dry_run: bool) -> str:
    """Apply one response's thread proof (no erasure). Returns recorded, replayed, upgraded,
    proof_conflict or duplicate_ordinal; the last two are kept as flags and never written."""
    response_id, seq = data['response_id'], data['transcript_seq']
    vector = tuple(data['thread_token_usage'][field] for field in THREAD_FIELDS)
    row = conn.execute(
        f'SELECT transcript_seq, {",".join(THREAD_COLUMNS)} FROM v2_usage_codex_thread_proof '
        'WHERE host=? AND native_session_id=? AND response_id=?', (host, native, response_id)).fetchone()
    if row is not None:
        stored_seq, stored = row[0], tuple(row[1:])
        if stored != vector or (seq is not None and stored_seq is not None and seq != stored_seq):
            if not dry_run:
                _put_flag(conn, host, native, 'proof_conflict', response_id,
                          f'stored seq={stored_seq} offered seq={seq}' if stored == vector else 'vector differs')
            return 'proof_conflict'
        if seq is None or stored_seq is not None:
            return 'replayed'
    if seq is not None:
        holder = conn.execute(
            'SELECT response_id FROM v2_usage_codex_thread_proof '
            'WHERE host=? AND native_session_id=? AND transcript_seq=?', (host, native, seq)).fetchone()
        if holder is not None and holder[0] != response_id:
            if not dry_run:
                _put_flag(conn, host, native, 'duplicate_ordinal', response_id,
                          f'ordinal {seq} held by another response')
            return 'duplicate_ordinal'
    if dry_run:
        return 'recorded' if row is None else 'upgraded'
    if row is None:
        conn.execute(
            f'INSERT INTO v2_usage_codex_thread_proof (host,native_session_id,response_id,transcript_seq,'
            f'{",".join(THREAD_COLUMNS)},source,observed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (host, native, response_id, seq, *vector, 'backfill' if seq is not None else 'live', data['observed_at']))
        return 'recorded'
    # A live row gains its ordinal from the whole-file backfill; the vector is identical.
    conn.execute("UPDATE v2_usage_codex_thread_proof SET transcript_seq=?, source='backfill' "
                 'WHERE host=? AND native_session_id=? AND response_id=?', (seq, host, native, response_id))
    return 'upgraded'


def record_provenance_conn(conn, host: str, items: list[dict[str, Any]], *,
                           dry_run: bool = False, version: int = 1) -> dict[str, Any]:
    """Apply validated claude_record/codex_response/codex_thread_flag items in one transaction.

    Never reads or writes v2_usage_records/v2_usage_state beyond the existence
    lookup that proves the native session belongs to ``host``. Returns per-item
    outcomes in input order: recorded, replayed, response_conflict,
    unknown_native_session or unknown_record, plus ``proof_counts`` for the
    version-2 thread proof carried by codex_response items.
    """
    outcomes: list[str] = []
    proof_counts: dict[str, int] = {}
    identities = 0
    known_sessions: dict[tuple[str, str], bool] = {}
    with conn:
        conn.execute('BEGIN' if dry_run else 'BEGIN IMMEDIATE')
        for item in items:
            provider, native, data = item['provider'], item['native_session_id'], item['data']
            session_key = (provider, native)
            if session_key not in known_sessions:
                known_sessions[session_key] = conn.execute(
                    'SELECT 1 FROM v2_usage_records WHERE host=? AND provider=? AND native_session_id=? LIMIT 1',
                    (host, provider, native),
                ).fetchone() is not None
            if not known_sessions[session_key]:
                outcomes.append('unknown_native_session')
                continue
            if item['kind'] == 'codex_thread_flag':
                new = dry_run or _put_flag(conn, host, native, data['flag'], data['response_id'], data['detail'])
                outcomes.append('recorded' if new else 'replayed')
                continue
            if item['kind'] == 'claude_record':
                ledger = conn.execute(
                    'SELECT 1 FROM v2_usage_records WHERE host=? AND provider=? AND native_session_id=? AND record_key=?',
                    (host, provider, native, data['record_key']),
                ).fetchone()
                if ledger is None:
                    outcomes.append('unknown_record')
                    continue
                key = (host, provider, native, data['record_key'])
                row = conn.execute(
                    'SELECT observed_at, model FROM v2_usage_provenance '
                    'WHERE host=? AND provider=? AND native_session_id=? AND record_key=?', key,
                ).fetchone()
                if row is None:
                    if not dry_run:
                        conn.execute(
                            'INSERT INTO v2_usage_provenance (host,provider,native_session_id,record_key,observed_at,model) '
                            'VALUES (?,?,?,?,?,?)', (*key, data['observed_at'], data['model']),
                        )
                    outcomes.append('recorded')
                else:
                    # Null-fill only: a known value is never replaced.
                    observed_at = row['observed_at'] or data['observed_at']
                    model = row['model'] or data['model']
                    if (observed_at, model) == (row['observed_at'], row['model']):
                        outcomes.append('replayed')
                    else:
                        if not dry_run:
                            conn.execute(
                                'UPDATE v2_usage_provenance SET observed_at=?, model=? '
                                'WHERE host=? AND provider=? AND native_session_id=? AND record_key=?',
                                (observed_at, model, *key),
                            )
                        outcomes.append('recorded')
            else:
                values = tuple(data[field] for field in (
                    'observed_at', 'model', 'input', 'cached_input', 'cache_write_input', 'output', 'reasoning_output',
                ))
                row = conn.execute(
                    'SELECT observed_at, model, input, cached_input, cache_write_input, output, reasoning_output '
                    'FROM v2_usage_codex_responses WHERE host=? AND native_session_id=? AND response_id=?',
                    (host, native, data['response_id']),
                ).fetchone()
                if row is None:
                    if not dry_run:
                        conn.execute(
                            'INSERT INTO v2_usage_codex_responses (host,native_session_id,response_id,observed_at,model,'
                            'input,cached_input,cache_write_input,output,reasoning_output) VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (host, native, data['response_id'], *values),
                        )
                    outcomes.append('recorded')
                else:
                    # Responses are immutable: insert-or-ignore, a differing replay is counted
                    # (and, from a version-2 producer, kept as adverse evidence).
                    outcomes.append('replayed' if tuple(row) == values else 'response_conflict')
                    if outcomes[-1] == 'response_conflict' and version >= 2 and not dry_run:
                        _put_flag(conn, host, native, 'duplicate_response_conflict', data['response_id'], None)
                if version >= 2 and 'thread_token_usage' in data and outcomes[-1] != 'response_conflict':
                    proof = _merge_proof(conn, host, native, data, dry_run=dry_run)
                    proof_counts[proof] = proof_counts.get(proof, 0) + 1
            identity = item['identity']
            if identity is not None and not dry_run:
                identities += _merge_identity(conn, host, provider, native, identity, data.get('observed_at'))
        if dry_run:
            conn.rollback()
    return {'outcomes': outcomes, 'identities_changed': identities, 'proof_counts': proof_counts,
            'known_native_sessions': sorted(f'{provider}:{native}' for (provider, native), known in known_sessions.items() if known),
            'unknown_native_sessions': sorted(f'{provider}:{native}' for (provider, native), known in known_sessions.items() if not known)}


def provenance_summary_conn(conn, stream_id: str, generation: str) -> dict[str, Any]:
    """Identity and coverage for one stream generation (read-only).

    Coverage counts this generation's ledger records that have provenance: a
    Claude record with a v2_usage_provenance row, a Codex cumulative record
    whose native session has at least one v2_usage_codex_responses row.
    """
    records = conn.execute(
        'SELECT COUNT(*) FROM v2_usage_records WHERE stream_id=? AND generation=?', (stream_id, generation),
    ).fetchone()[0]
    covered = conn.execute(
        """SELECT COUNT(*) FROM v2_usage_records r WHERE r.stream_id=? AND r.generation=? AND (
               (r.provider='codex' AND EXISTS (SELECT 1 FROM v2_usage_codex_responses c
                   WHERE c.host=r.host AND c.native_session_id=r.native_session_id))
               OR (r.provider<>'codex' AND EXISTS (SELECT 1 FROM v2_usage_provenance p
                   WHERE p.host=r.host AND p.provider=r.provider
                   AND p.native_session_id=r.native_session_id AND p.record_key=r.record_key)))""",
        (stream_id, generation),
    ).fetchone()[0]
    sessions = conn.execute(
        'SELECT DISTINCT host, provider, native_session_id FROM v2_usage_records '
        'WHERE stream_id=? AND generation=? ORDER BY host, provider, native_session_id', (stream_id, generation),
    ).fetchall()
    identities = []
    for host, provider, native in (tuple(row) for row in sessions):
        identity = conn.execute(
            'SELECT account_id, account_source, conflict, cli_version FROM v2_usage_identity '
            'WHERE host=? AND provider=? AND native_session_id=?', (host, provider, native),
        ).fetchone()
        identities.append({
            'native_session_id': native, 'provider': provider,
            'account_id': identity['account_id'] if identity else None,
            'account_source': identity['account_source'] if identity else 'unknown',
            'conflict': int(identity['conflict']) if identity else 0,
            'cli_version': identity['cli_version'] if identity else None,
        })
    accounts = {entry['account_id'] for entry in identities}
    conflict = int(any(entry['conflict'] for entry in identities))
    if conflict or len(accounts) != 1 or None in accounts:
        account_id = None
    else:
        account_id = next(iter(accounts))
    if account_id is not None and all(entry['account_source'] == 'transcript' for entry in identities):
        account_source = 'transcript'
    else:
        account_source = 'unknown'
    return {
        'account_id': account_id, 'account_source': account_source, 'conflict': conflict,
        'provenance_coverage': {'records': records, 'with_provenance': covered},
        'native_sessions': identities,
    }


# --- per-generation history (docs/usage_accounting.md § Unfenced spans) --------

HISTORY_RETENTION_S = 30 * 86400


def history_open_conn(conn, host: str, session_name: str, generation: str, *,
                      pane_pid: Any, provider: Any, now: float | None = None) -> None:
    """Insert the history row of a newly opened generation (ms precision).

    A prior row of the same name that is still open (a generation minted over
    an open row, without a close) is closed at the new row's ``created_at`` in
    the same transaction, so a name has at most one open history row.
    """
    stamp = iso_ms(time.time() if now is None else now)
    conn.execute(
        'UPDATE v2_session_generation_history SET closed_at=? '
        'WHERE host=? AND session_name=? AND generation<>? AND closed_at IS NULL',
        (stamp, host, session_name, generation),
    )
    conn.execute(
        'INSERT OR IGNORE INTO v2_session_generation_history '
        '(host,session_name,generation,pane_pid,provider,created_at,precision) VALUES (?,?,?,?,?,?,?)',
        (host, session_name, generation, str(pane_pid or '') or None, str(provider or '') or None, stamp, 'ms'),
    )


def history_fill_conn(conn, host: str, session_name: str, *, pane_pid: Any = None, provider: Any = None) -> None:
    """Bind a pane pid / provider learned after open onto the open row, once."""
    for column, value in (('pane_pid', pane_pid), ('provider', provider)):
        if value is None or str(value) == '':
            continue
        conn.execute(
            f"UPDATE v2_session_generation_history SET {column}=? WHERE host=? AND session_name=? "
            f"AND closed_at IS NULL AND COALESCE({column},'')=''",
            (str(value), host, session_name),
        )


def history_close_conn(conn, host: str, session_name: str, *, now: float | None = None) -> None:
    """Close the name's open history row with a ms stamp taken at the write."""
    conn.execute(
        'UPDATE v2_session_generation_history SET closed_at=? '
        'WHERE host=? AND session_name=? AND closed_at IS NULL',
        (iso_ms(time.time() if now is None else now), host, session_name),
    )


def history_bind_identity_conn(conn, host: Any, session_name: Any, generation: Any, *,
                               native_session_id: str, digest: str) -> None:
    """Write the transcript identity of one generation once; never overwrite."""
    conn.execute(
        'UPDATE v2_session_generation_history SET '
        'native_session_id=COALESCE(native_session_id,?), '
        'source_file_identity_digest=COALESCE(source_file_identity_digest,?) '
        'WHERE host=? AND session_name=? AND generation=?',
        (native_session_id, digest, host, session_name, generation),
    )


def history_backfill_conn(conn) -> int:
    """Daemon start: reconcile history with `sessions`, then backfill.

    A daemon without history (e.g. one rolled back to) can close or reopen a
    name without writing history, leaving a stale open row. Each open row is
    reconciled first:

    * its generation is still the open session's -> kept open;
    * its generation's session row is closed -> closed at ``sessions.closed_at``
      (whole-second, floored: treating it as the end only refuses more);
    * otherwise (reopened as another generation, or the row was archived) the
      real close time is unknown -> closed with zero width at its own
      ``created_at``, so its records are refused, never credited by analogy.

    Then one row per open session that has none for its current generation,
    keeping the second-resolution ``sessions.created_at`` flagged ``'s'`` (no
    fabricated milliseconds). Closed rows without a generation are not
    backfilled. Returns the number of rows inserted.
    """
    stale = conn.execute(
        """SELECT h.host, h.session_name, h.generation, h.created_at,
                  s.status AS status, s.closed_at AS session_closed_at, g.generation AS current
           FROM v2_session_generation_history h
           LEFT JOIN sessions s ON s.host=h.host AND s.session_name=h.session_name
           LEFT JOIN v2_session_generations g ON g.host=h.host AND g.session_name=h.session_name
           WHERE h.closed_at IS NULL"""
    ).fetchall()
    for row in stale:
        if row['current'] == row['generation'] and row['status'] == 'open':
            continue
        closed = epoch(row['session_closed_at']) if (
            row['current'] == row['generation'] and row['status'] is not None) else None
        if closed is None:
            closed = epoch(row['created_at'])
        if closed is None:
            continue
        conn.execute(
            'UPDATE v2_session_generation_history SET closed_at=? '
            'WHERE host=? AND session_name=? AND generation=? AND closed_at IS NULL',
            (iso_ms(closed), row['host'], row['session_name'], row['generation']),
        )
    cur = conn.execute(
        """INSERT OR IGNORE INTO v2_session_generation_history
               (host,session_name,generation,pane_pid,provider,created_at,precision)
           SELECT s.host, s.session_name, g.generation, NULLIF(s.pane_pid,''), NULLIF(s.provider,''),
                  s.created_at, 's'
           FROM sessions s JOIN v2_session_generations g
             ON g.host=s.host AND g.session_name=s.session_name
           WHERE s.status='open' AND COALESCE(g.generation,'')<>'' AND COALESCE(s.created_at,'')<>''"""
    )
    return cur.rowcount or 0


def prune_generation_history_conn(conn, now: float) -> int:
    """Delete rows closed more than 30 days ago; open rows are never pruned."""
    with conn:
        cur = conn.execute(
            'DELETE FROM v2_session_generation_history WHERE closed_at IS NOT NULL AND closed_at < ?',
            (iso_ms(now - HISTORY_RETENTION_S),),
        )
    return cur.rowcount or 0


# --- unfenced held spans (wire v2) -----------------------------------------------

UNFENCED_ENTRY_FIELDS = frozenset({
    'key', 'stream_id', 'provider', 'source_pane_pid', 'native_session_id',
    'source_file_identity_digest', 'records',
})
MAX_UNFENCED_ENTRIES = 64
MAX_UNFENCED_RECORDS = 256
LOSS_REASONS = frozenset({
    'held_span_expired_ttl', 'held_span_capacity_entries',
    'held_span_capacity_bytes', 'held_span_overflow_records',
})
LOSS_COUNTS = ('records_lost', 'bytes_lost', 'occurrences')
MAX_LOSSES = 64
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_LOSS_ID = re.compile(r'[0-9a-f]{8,64}:[1-9][0-9]{0,17}\Z')


def _unplaced_conn(conn, host: str, *, provider: str, native: str, record_key: str, stream_id: Any,
                   pane_pid: Any, transcript_ts: Any, reason: str, detail: dict[str, Any] | None) -> None:
    """Insert-or-ignore one refused record; its first reason stays."""
    conn.execute(
        'INSERT OR IGNORE INTO v2_usage_unplaced (host,provider,native_session_id,record_key,stream_id,'
        'source_pane_pid,transcript_ts,reason,first_seen_at,detail) VALUES (?,?,?,?,?,?,?,?,?,?)',
        (host, provider, native, record_key, stream_id, pane_pid,
         transcript_ts if isinstance(transcript_ts, str) else None, reason, iso_ms(time.time()),
         json.dumps(detail, sort_keys=True, separators=(',', ':')) if detail is not None else None),
    )


def _entry_problem(entry: Any, host: str) -> str | None:
    if not isinstance(entry, dict) or set(entry) != UNFENCED_ENTRY_FIELDS:
        return 'malformed_record'
    stream_id = entry.get('stream_id')
    records = entry.get('records')
    if (
        not isinstance(entry.get('key'), str) or not entry['key'] or len(entry['key']) > 128
        or not isinstance(stream_id, str) or not stream_id.startswith(f'{host}:')
        or stream_id == f'{host}:'
        or entry.get('provider') not in FIELDS
        or not isinstance(entry.get('source_pane_pid'), str) or not entry['source_pane_pid']
        or not isinstance(entry.get('native_session_id'), str) or not entry['native_session_id']
        or not isinstance(entry.get('source_file_identity_digest'), str)
        or _HEX64.fullmatch(entry['source_file_identity_digest']) is None
        or not isinstance(records, list) or not records or len(records) > MAX_UNFENCED_RECORDS
    ):
        return 'malformed_record'
    return None


def record_unfenced_conn(conn, host: str, entries: Any, losses: Any, *, clock: Any,
                         receipt_now: float, apply_losses: bool = True) -> dict[str, Any]:
    """One frame's held spans: classify, credit, refuse and record losses.

    Per-record outcomes: ``recorded`` / ``replayed`` (credited), a terminal
    reason (persisted in v2_usage_unplaced in this transaction), or transient
    (``clock_unavailable``; ``retry`` for store contention, set by the caller).
    """
    recorded: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    touched: set[tuple[str, str]] = set()
    counts = {'recorded': 0, 'replayed': 0}
    send = parse_clock(clock)
    entries = entries if isinstance(entries, list) else []
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        for entry in entries[:MAX_UNFENCED_ENTRIES]:
            problem = _entry_problem(entry, host)
            if problem is not None:
                key = entry.get('key') if isinstance(entry, dict) and isinstance(entry.get('key'), str) else ''
                if key:
                    rejected.append({'key': key, 'seq': None, 'reason': problem, 'transient': False})
                    _unplaced_conn(conn, host, provider=str(entry.get('provider') or ''),
                                   native=str(entry.get('native_session_id') or ''),
                                   record_key='entry:' + key, stream_id=entry.get('stream_id'),
                                   pane_pid=entry.get('source_pane_pid'), transcript_ts=None,
                                   reason=problem, detail=None)
                continue
            _admit_entry_conn(conn, host, entry, send, receipt_now, recorded, rejected, touched, counts)
        loss_ack = upsert_losses_conn(conn, host, losses) if apply_losses else {'recorded': [], 'conflict': []}
    return {'recorded': recorded, 'rejected': rejected, 'touched': sorted(touched),
            'counts': counts, 'losses_recorded': loss_ack['recorded'], 'losses_conflict': loss_ack['conflict']}


def _admit_entry_conn(conn, host: str, entry: dict[str, Any], send: Any, receipt_now: float,
                      recorded: list, rejected: list, touched: set, counts: dict) -> None:
    key, stream_id, provider = entry['key'], entry['stream_id'], entry['provider']
    session_name = stream_id.split(':', 1)[1]
    native, digest, pane_pid = entry['native_session_id'], entry['source_file_identity_digest'], entry['source_pane_pid']
    rows = conn.execute(
        'SELECT * FROM v2_session_generation_history WHERE host=? AND session_name=? AND pane_pid=? AND provider=?',
        (host, session_name, pane_pid, provider),
    ).fetchall()
    history = {str(row['generation']): row for row in rows}
    candidates = [generation for generation in (generation_from_row(row) for row in rows) if generation is not None]
    credited: dict[str, list[tuple[Any, Any, dict]]] = {}

    def refuse(record: Any, seq: Any, reason: str, observation: Any = None, *, transient: bool = False,
               detail: dict[str, Any] | None = None) -> None:
        rejected.append({'key': key, 'seq': seq, 'reason': reason, 'transient': transient})
        if transient:
            return
        record_key = observation.record_key if observation is not None else None
        if record_key is None:
            raw = json.dumps(record, sort_keys=True, default=str).encode()
            record_key = 'unparsed:' + __import__('hashlib').sha256(raw).hexdigest()[:24]
        _unplaced_conn(conn, host, provider=provider,
                       native=observation.native_session_id if observation is not None else native,
                       record_key=record_key, stream_id=stream_id, pane_pid=pane_pid,
                       transcript_ts=record.get('transcript_ts') if isinstance(record, dict) else None,
                       reason=reason, detail={'seq': seq, 'tokens': observation.tokens if observation else None,
                                              **(detail or {})})

    for record in entry['records']:
        seq = record.get('seq') if isinstance(record, dict) else None
        if not isinstance(record, dict) or type(seq) is not int or seq < 0:
            refuse(record if isinstance(record, dict) else {'value': record}, seq if type(seq) is int else None,
                   'malformed_record')
            continue
        capture = parse_clock(record.get('clock'))
        outcome, generation = classify(epoch(record.get('transcript_ts')), candidates, send, receipt_now, capture)
        observation, reason = native_usage(provider, record, native)
        if outcome != 'credited':
            refuse(record, seq, outcome, observation, transient=outcome == 'clock_unavailable',
                   detail={'generations': sorted(history)})
            continue
        if observation is None:
            refuse(record, seq, reason if reason == 'invalid_usage' else 'malformed_record')
            continue
        credited.setdefault(generation, []).append((record, seq, observation))

    for generation, items in credited.items():
        row = history[generation]
        if row['source_file_identity_digest'] is not None and row['source_file_identity_digest'] != digest:
            for record, seq, observation in items:
                refuse(record, seq, 'source_identity_mismatch', observation)
            continue
        if row['native_session_id'] is not None and row['native_session_id'] != native:
            for record, seq, observation in items:
                refuse(record, seq, 'native_session_identity_mismatch', observation)
            continue
        merged = record_historical_usage_conn(
            conn, host=host, history_row=row, stream_id=stream_id, provider=provider,
            native_session_id=native, digest=digest, observations=[item[2] for item in items],
        )
        for (record, seq, observation), outcome in zip(items, merged['outcomes']):
            if outcome == 'ownership_conflict':
                refuse(record, seq,
                       'cumulative_owned_by_prior_generation' if provider == 'codex' else 'ownership_conflict',
                       observation, detail={'generation': generation})
                continue
            counts[outcome] += 1
            recorded.append({'key': key, 'seq': seq, 'outcome': outcome})
        if merged['changed']:
            touched.add((stream_id, generation))


def record_historical_usage_conn(conn, *, host: str, history_row: Any, stream_id: str, provider: str,
                                 native_session_id: str, digest: str, observations: list[Any]) -> dict[str, Any]:
    """The historical writer: store records for a resolved history row.

    Unlike the live-row writer it never reads ``sessions``, so it can store a
    record for a closed generation A, or an A-interval record while B is live.
    The caller has checked the history identity; this binds it on first write,
    then runs the shared merge core with ``generation = history.generation``.
    """
    generation = str(history_row['generation'])
    history_bind_identity_conn(conn, host, history_row['session_name'], generation,
                               native_session_id=native_session_id, digest=digest)
    before = _state_before(conn, stream_id, generation, provider)
    return merge_observations_conn(conn, before=before, collection_host=host, provider=provider,
                                   observations=observations, reasons=set())


def _loss_problem(loss: Any) -> bool:
    if not isinstance(loss, dict):
        return True
    if not isinstance(loss.get('loss_id'), str) or _LOSS_ID.fullmatch(loss['loss_id']) is None:
        return True
    if loss.get('reason') not in LOSS_REASONS or not isinstance(loss.get('key'), str):
        return True
    if not isinstance(loss.get('provider'), str) or not isinstance(loss.get('native_session_id'), str):
        return True
    if type(loss.get('rev')) is not int or loss['rev'] < 1:
        return True
    return any(type(loss.get(field)) is not int or loss[field] < 0 for field in LOSS_COUNTS)


def upsert_losses_conn(conn, host: str, losses: Any) -> dict[str, list[dict[str, Any]]]:
    """Rev-guarded loss rows with a durable per-id conflict latch.

    A higher ``rev`` replaces the stored record; a lower/equal ``rev`` with
    identical counts is a replay; with different counts it is ``loss_conflict``:
    the stored row is untouched, the pending payload is kept in a
    ``loss_conflict:`` row (never counted), and from then on every payload for
    that id goes to the conflict row. Conflicts are never acked as recorded.
    """
    recorded: list[dict[str, Any]] = []
    conflict: list[dict[str, Any]] = []
    if not isinstance(losses, list) or len(losses) > MAX_LOSSES:
        return {'recorded': recorded, 'conflict': conflict}
    for loss in losses:
        if _loss_problem(loss):
            continue
        loss_id = loss['loss_id']
        payload = {name: loss.get(name) for name in (
            'loss_id', 'key', 'provider', 'native_session_id', 'reason', 'coalesced',
            *LOSS_COUNTS, 'first_at', 'last_at', 'rev')}
        stored = conn.execute('SELECT detail FROM v2_usage_unplaced WHERE host=? AND record_key=?',
                              (host, 'loss:' + loss_id)).fetchone()
        stored = json.loads(stored['detail']) if stored is not None else None
        latch = conn.execute('SELECT detail FROM v2_usage_unplaced WHERE host=? AND record_key=?',
                             (host, 'loss_conflict:' + loss_id)).fetchone()
        mismatch = (stored is not None and payload['rev'] <= stored['rev']
                    and any(payload[name] != stored[name] for name in LOSS_COUNTS))
        if latch is not None or mismatch:
            pending = json.loads(latch['detail'])['pending'] if latch is not None else None
            if pending is None or payload['rev'] >= pending['rev']:
                detail = json.dumps({'stored': stored, 'pending': payload}, sort_keys=True, separators=(',', ':'))
                if latch is None:
                    conn.execute(
                        'INSERT INTO v2_usage_unplaced (host,provider,native_session_id,record_key,stream_id,'
                        'source_pane_pid,transcript_ts,reason,first_seen_at,detail) VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (host, payload['provider'], payload['native_session_id'], 'loss_conflict:' + loss_id,
                         None, None, None, 'loss_conflict', iso_ms(time.time()), detail))
                else:
                    conn.execute('UPDATE v2_usage_unplaced SET detail=? WHERE host=? AND record_key=?',
                                 (detail, host, 'loss_conflict:' + loss_id))
            conflict.append({'loss_id': loss_id, 'rev': payload['rev']})
            continue
        detail = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        if stored is None:
            conn.execute(
                'INSERT INTO v2_usage_unplaced (host,provider,native_session_id,record_key,stream_id,'
                'source_pane_pid,transcript_ts,reason,first_seen_at,detail) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (host, payload['provider'], payload['native_session_id'], 'loss:' + loss_id,
                 None, None, None, payload['reason'], iso_ms(time.time()), detail))
            stored_rev = payload['rev']
        elif payload['rev'] > stored['rev']:
            conn.execute('UPDATE v2_usage_unplaced SET detail=? WHERE host=? AND record_key=?',
                         (detail, host, 'loss:' + loss_id))
            stored_rev = payload['rev']
        else:
            stored_rev = stored['rev']
        recorded.append({'loss_id': loss_id, 'rev': stored_rev})
    return {'recorded': recorded, 'conflict': conflict}


def is_store_contention(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and 'locked' in str(exc).lower()
