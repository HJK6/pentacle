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
