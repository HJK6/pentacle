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


def snapshot_conn(conn, row: dict[str, Any]) -> dict[str, Any]:
    """Project current associations over immutable-generation accounting state."""
    from store_specs import normalize_spec_ids

    stream_id = str(row.get('stream_id') or f"{row['host']}:{row['session_name']}")
    generation = str(row.get('session_generation') or '')
    provider = row.get('provider')
    state = conn.execute('SELECT * FROM v2_usage_state WHERE stream_id=? AND generation=?', (stream_id, generation)).fetchone()
    specs = normalize_spec_ids(row.get('spec_ids'), row.get('spec_id'))
    return {
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


class UsageStoreMixin:
    async def record_usage(self, expected: dict[str, Any], records: list[dict[str, Any]], *,
                           native_session_id: str, collection_host: str,
                           malformed: bool = False) -> dict[str, Any] | None:
        """One native span: fence, identities, deltas and diagnostics commit together."""
        from store_specs import _session_row

        provider = expected.get('provider')
        if provider not in FIELDS or collection_host != expected.get('host'):
            return None
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
                if (current is None or current.get('status') != 'open'
                        or not expected.get('session_generation')
                        or current.get('session_generation') != expected['session_generation']
                        or current.get('provider') != provider
                        or any(current.get(field) != expected.get(field) for field in SOURCE_FIELDS)):
                    return None
                before = current['usage']
                tokens = dict(before['tokens'])
                all_reasons = set(before['incomplete_reasons']) | reasons
                sid, generation = before['stream_id'], before['session_generation']
                for observation in observations:
                    key = (collection_host, provider, observation.native_session_id, observation.record_key)
                    previous = conn.execute('SELECT * FROM v2_usage_records WHERE host=? AND provider=? AND native_session_id=? AND record_key=?', key).fetchone()
                    if previous and (previous['stream_id'], previous['generation']) != (sid, generation):
                        all_reasons.add('ownership_conflict')
                        continue
                    old = json.loads(previous['tokens']) if previous else {}
                    merged = {field: max(old.get(field, 0), value) for field, value in observation.tokens.items()}
                    if any(value < old.get(field, 0) for field, value in observation.tokens.items()):
                        all_reasons.add('counter_regression' if provider == 'codex' else 'message_usage_regression')
                    if previous and merged == old:
                        continue
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
            return result

        return await self.submit(operation)
