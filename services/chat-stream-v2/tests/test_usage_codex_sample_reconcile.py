"""Codex ledger-vs-rows sample reconciliation tool (spec_pentacle__usage_codex_ledger_vs_rows_sample_reconciliation_2026_10).

Two fixture rollouts: one complete (a dropped token_count and a trailing response), one with a counter reset.
Every oracle is written out here; the ledger is a real SQLite file opened read-only.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import usage_codex_sample_reconcile as rec

NATIVE = '019aaaaa-0000-7000-8000-000000000001'
RESET_NATIVE = '019aaaaa-0000-7000-8000-000000000002'


def usage(inp, cached, out, reasoning):
    return {'input_tokens': inp, 'cached_input_tokens': cached, 'cache_write_input_tokens': 0,
            'output_tokens': out, 'reasoning_output_tokens': reasoning, 'total_tokens': inp + out}


def meta(native):
    return {'type': 'session_meta', 'payload': {'id': native, 'creator_account_id': 'acct-1', 'cli_version': '0.159.0'}}


def response(native, rid, use, thread):
    return {'type': 'token_usage_record', 'payload': {
        'thread_id': native, 'response_id': rid, 'usage': use, 'thread_token_usage': thread}}


def count(total, last, ts='2026-10-07T10:00:00.000Z'):
    return {'timestamp': ts, 'type': 'event_msg',
            'payload': {'type': 'token_count', 'info': {'total_token_usage': total, 'last_token_usage': last}}}


def write(path: Path, records, *, tail: str = '') -> Path:
    path.write_text('\n'.join(json.dumps(r) for r in records) + '\n' + tail)
    return path


R1, R2, R3, R4 = usage(100, 40, 10, 4), usage(200, 160, 20, 8), usage(300, 250, 30, 12), usage(400, 360, 40, 16)
Z = usage(0, 0, 0, 0)


def total(*parts):
    keys = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens')
    out = {k: sum(p[k] for p in parts) for k in keys}
    out['cache_write_input_tokens'] = 0
    out['total_tokens'] = out['input_tokens'] + out['output_tokens']
    return out


@pytest.fixture
def complete(tmp_path):
    """r1 and r2 are accounted; r3's token_count is dropped (total unchanged, last zero); r4 has no token_count."""
    records = [
        meta(NATIVE),
        response(NATIVE, 'resp-1', R1, total(R1)), count(total(R1), R1),
        response(NATIVE, 'resp-2', R2, total(R1, R2)), count(total(R1, R2), R2),
        response(NATIVE, 'resp-3', R3, total(R1, R2, R3)), count(total(R1, R2), Z),
        response(NATIVE, 'resp-4', R4, total(R1, R2, R3, R4)),
    ]
    return write(tmp_path / f'rollout-2026-10-07T10-00-00-{NATIVE}.jsonl', records)


@pytest.fixture
def reset(tmp_path):
    records = [
        meta(RESET_NATIVE),
        response(RESET_NATIVE, 'resp-1', R1, total(R1)), count(total(R1), R1),
        response(RESET_NATIVE, 'resp-2', R2, total(R1, R2)), count(total(R1, R2), R2),
        response(RESET_NATIVE, 'resp-3', R3, total(R1, R2, R3)), count(total(R1), R3),
    ]
    return write(tmp_path / f'rollout-2026-10-07T11-00-00-{RESET_NATIVE}.jsonl', records)


def make_ledger(path: Path, native: str, cumulative: dict, rows: dict) -> str:
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE v2_usage_records (host TEXT, provider TEXT, native_session_id TEXT, record_key TEXT, '
                 'stream_id TEXT, generation TEXT, tokens TEXT)')
    conn.execute('CREATE TABLE v2_usage_codex_responses (host TEXT, native_session_id TEXT, response_id TEXT, '
                 'observed_at TEXT, model TEXT, input INT, cached_input INT, cache_write_input INT, output INT, '
                 'reasoning_output INT)')
    conn.execute('CREATE TABLE v2_usage_identity (host TEXT, provider TEXT, native_session_id TEXT, account_id TEXT, '
                 'account_source TEXT, first_observed_at TEXT, conflict INT, cli_version TEXT)')
    conn.execute('CREATE TABLE v2_usage_state (stream_id TEXT, generation TEXT, reasons TEXT)')
    conn.execute("INSERT INTO v2_usage_records VALUES ('thoth','codex',?,'cumulative','thoth:s1','g1',?)",
                 (native, json.dumps({'input_total': cumulative['input_tokens'], 'cached_input': cumulative['cached_input_tokens'],
                                      'output': cumulative['output_tokens'], 'reasoning': cumulative['reasoning_output_tokens']})))
    for rid, use in rows.items():
        conn.execute("INSERT INTO v2_usage_codex_responses VALUES ('thoth',?,?,NULL,'m',?,?,0,?,?)",
                     (native, rid, use['input_tokens'], use['cached_input_tokens'], use['output_tokens'],
                      use['reasoning_output_tokens']))
    conn.execute("INSERT INTO v2_usage_identity VALUES ('thoth','codex',?,'acct-1','transcript',NULL,0,'0.159.0')", (native,))
    conn.execute("INSERT INTO v2_usage_state VALUES ('thoth:s1','g1','[\"history_not_verified\"]')")
    conn.commit()
    conn.close()
    return str(path)


def test_complete_transcript_facts(complete):
    facts = rec.analyze_transcript(complete, with_hash=True)
    assert facts['native'] == NATIVE and facts['response_unique'] == 4 and facts['malformed_lines'] == 0
    assert facts['sums']['input'] == 1000 and facts['sums']['cached_input'] == 810
    assert facts['final']['input'] == 300                       # last token_count: r3 and r4 never applied
    assert facts['last_thread_usage']['input'] == 1000          # thread counter in the last response record
    assert facts['resets'] == [] and facts['thread_inconsistent'] == 0
    assert facts['tail_responses'] == 1                         # r4 follows the last token_count
    assert facts['gap_events'] == 1 and facts['gap_zero_last_events'] == 1   # r3: total unchanged, last == 0
    assert facts['synced_snapshots'] == 2                       # r1, r2 snapshots equal the response sums
    assert rec.semantics(facts)['input_includes_cached'] is True
    assert rec.semantics(facts)['output_includes_reasoning'] is True
    assert len(facts['file_sha256']) == 64


def test_complete_session_predicate_hits_when_rows_are_complete(complete, tmp_path):
    facts = rec.analyze_transcript(complete)
    cumulative = total(R1, R2)                                   # what the daemon max-merged from token_count
    ledger = make_ledger(tmp_path / 'ledger.sqlite', NATIVE, cumulative,
                         {'resp-1': R1, 'resp-2': R2, 'resp-3': R3, 'resp-4': R4})
    conn = rec.open_ro(ledger)
    row = rec.reconcile('thoth', facts, rec.ledger_view(conn, 'thoth', NATIVE))
    assert row['rollup_class'] == 'unverifiable'                 # rows 1000 > ledger 300 in the input bucket
    assert row['ledger_vs_final'] == 'equal' and row['ids_equal'] is True
    assert row['rows_equal_thread_final'] is True and row['ledger_le_thread_final'] is True
    assert row['predicate_rows_authoritative'] is True
    assert row['native_hash'] != NATIVE and NATIVE not in json.dumps(row)     # pseudonymised
    assert 'acct-1' not in json.dumps(row)


def test_incomplete_rows_do_not_hit_the_predicate(complete, tmp_path):
    facts = rec.analyze_transcript(complete)
    ledger = make_ledger(tmp_path / 'ledger.sqlite', NATIVE, total(R1, R2),
                         {'resp-2': R2, 'resp-3': R3, 'resp-4': R4})   # head row missing
    row = rec.reconcile('thoth', facts, rec.ledger_view(rec.open_ro(ledger), 'thoth', NATIVE))
    assert row['ids_only_in_transcript'] == 1 and row['ids_equal'] is False
    assert row['predicate_rows_authoritative'] is False


def test_counter_reset_is_flagged_and_refused(reset, tmp_path):
    facts = rec.analyze_transcript(reset)
    assert facts['resets'] == [2]                                # the third token_count total fell below the second
    ledger = make_ledger(tmp_path / 'ledger.sqlite', RESET_NATIVE, total(R1, R2),
                         {'resp-1': R1, 'resp-2': R2, 'resp-3': R3})
    row = rec.reconcile('thoth', facts, rec.ledger_view(rec.open_ro(ledger), 'thoth', RESET_NATIVE))
    assert row['counter_resets'] == 1
    assert row['predicate_rows_authoritative'] is False          # a reset session is never classed by this predicate


def test_malformed_line_is_counted(complete):
    bad = complete.with_name(complete.name.replace('10-00-00', '10-00-01'))
    bad.write_text(complete.read_text() + '{not json\n')
    assert rec.analyze_transcript(bad)['malformed_lines'] == 1


def test_copied_parent_response_is_foreign_not_own(tmp_path):
    other = '019aaaaa-0000-7000-8000-0000000000ff'
    records = [meta(NATIVE), response(NATIVE, 'resp-1', R1, total(R1)),
               response(other, 'resp-parent', R2, total(R2))]
    facts = rec.analyze_transcript(write(tmp_path / f'rollout-2026-10-07T12-00-00-{NATIVE}.jsonl', records))
    assert facts['response_unique'] == 1 and facts['foreign_unique'] == 1 and facts['foreign_ids'] == ['resp-parent']


def test_ledger_has_no_write_path(complete, tmp_path):
    ledger = make_ledger(tmp_path / 'ledger.sqlite', NATIVE, total(R1), {'resp-1': R1})
    conn = rec.open_ro(ledger)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM v2_usage_records")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO v2_usage_codex_responses VALUES ('thoth','x','y',NULL,'m',1,1,0,1,1)")
    source = Path(rec.__file__).read_text()
    assert not any(word in source.upper() for word in ('INSERT INTO', 'UPDATE ', 'DELETE FROM', 'DROP TABLE', 'ALTER TABLE'))


def test_sample_is_seeded_and_stratified(tmp_path):
    pop = [{'host': h, 'native': f'{h}-{i}', 'account': a, 'ratio_bucket': b, 'size': s}
           for h in ('thoth', 'merlin') for a in ('known', 'unknown') for b in ('1-1.2', '>2') for s in ('big', 'small')
           for i in range(3)]
    first = rec.select_sample(pop, n=8, seed=7)
    assert first == rec.select_sample(pop, n=8, seed=7)
    assert first != rec.select_sample(pop, n=8, seed=8)
    assert len({(x['host'], x['account'], x['ratio_bucket'], x['size']) for x in first}) == 8   # one per cell first
