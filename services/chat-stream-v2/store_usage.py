"""Usage persistence extension of Store, executed only on its SQLite worker."""
from __future__ import annotations

import json
import logging
from typing import Any

from usage_accounting import FIELDS, native_usage
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
)
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
                                dry_run: bool = False) -> dict[str, Any]:
        """Persist validated provenance items for ``host`` on the Store worker."""
        return await self.submit(lambda conn: record_provenance_conn(conn, host, items, dry_run=dry_run))

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
    ) -> dict[str, Any]:
        """One native span: fence, identities, deltas and diagnostics commit together."""
        from store_specs import _session_row

        provider = expected.get('provider')
        if provider not in FIELDS or collection_host != expected.get('host'):
            return {'accepted': False, 'snapshot': None, 'reason': 'unauthorized_source_host',
                    'recorded': False, 'replayed': False}
        observations = []
        reasons = {'malformed_record'} if malformed else set()
        for record in records:
            observation, reason = native_usage(provider, record, native_session_id)
            if observation is not None:
                observations.append(observation)
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

                before = current['usage']
                tokens = dict(before['tokens'])
                all_reasons = set(before['incomplete_reasons']) | reasons
                sid, generation = before['stream_id'], before['session_generation']
                recorded = False
                observed = bool(observations)
                for observation in observations:
                    key = (collection_host, provider, observation.native_session_id, observation.record_key)
                    previous = conn.execute('SELECT * FROM v2_usage_records WHERE host=? AND provider=? AND native_session_id=? AND record_key=?', key).fetchone()
                    if previous and (previous['stream_id'], previous['generation']) != (sid, generation):
                        all_reasons.add('ownership_conflict')
                        continue
                    old = json.loads(previous['tokens']) if previous else {}
                    merged = {field: max(old.get(field, 0), value) for field, value in observation.tokens.items()}
                    if previous and merged == old:
                        continue
                    if any(value < old.get(field, 0) for field, value in observation.tokens.items()):
                        all_reasons.add('counter_regression' if provider == 'codex' else 'message_usage_regression')
                    recorded = True
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
                result = snapshot_conn(conn, current)
            if changed:
                log.info('usage recorded stream=%s revision=%s', sid, result['revision'],
                         extra={'subsystem': 'usage_accounting', 'bug_ref': 'usage_accounting_ledger_2026_09'})
            return {
                'accepted': True,
                'snapshot': result,
                'reason': None,
                'recorded': recorded,
                'replayed': observed and not recorded,
            }

        return await self.submit(operation)


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


def record_provenance_conn(conn, host: str, items: list[dict[str, Any]], *,
                           dry_run: bool = False) -> dict[str, Any]:
    """Apply validated claude_record/codex_response items in one transaction.

    Never reads or writes v2_usage_records/v2_usage_state beyond the existence
    lookup that proves the native session belongs to ``host``. Returns per-item
    outcomes in input order: recorded, replayed, response_conflict,
    unknown_native_session or unknown_record.
    """
    outcomes: list[str] = []
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
                    # Responses are immutable: insert-or-ignore, a differing replay is counted.
                    outcomes.append('replayed' if tuple(row) == values else 'response_conflict')
            identity = item['identity']
            if identity is not None and not dry_run:
                identities += _merge_identity(conn, host, provider, native, identity, data.get('observed_at'))
        if dry_run:
            conn.rollback()
    return {'outcomes': outcomes, 'identities_changed': identities,
            'known_native_sessions': sorted(f'{provider}:{native}' for (provider, native), known in known_sessions.items() if known),
            'unknown_native_sessions': sorted(f'{provider}:{native}' for (provider, native), known in known_sessions.items() if not known)}


def provenance_summary_conn(conn, stream_id: str, generation: str) -> dict[str, Any]:
    """Identity and coverage for one stream generation (read-only).

    Coverage counts this generation's ledger records that have provenance: a
    Claude record with a v2_usage_provenance row, a Codex cumulative record
    whose native session has at least one v2_usage_codex_responses row.
    """
    rows = conn.execute(
        'SELECT host, provider, native_session_id, record_key FROM v2_usage_records '
        'WHERE stream_id=? AND generation=?', (stream_id, generation),
    ).fetchall()
    covered = 0
    sessions: dict[tuple[str, str, str], None] = {}
    for row in rows:
        sessions.setdefault((row['host'], row['provider'], row['native_session_id']), None)
        if row['provider'] == 'codex':
            hit = conn.execute(
                'SELECT 1 FROM v2_usage_codex_responses WHERE host=? AND native_session_id=? LIMIT 1',
                (row['host'], row['native_session_id']),
            ).fetchone()
        else:
            hit = conn.execute(
                'SELECT 1 FROM v2_usage_provenance WHERE host=? AND provider=? AND native_session_id=? AND record_key=?',
                tuple(row),
            ).fetchone()
        covered += hit is not None
    identities = []
    for host, provider, native in sessions:
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
        'provenance_coverage': {'records': len(rows), 'with_provenance': covered},
        'native_sessions': identities,
    }
