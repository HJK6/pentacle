"""Usage rollup and calibration (spec_pentacle__usage_rollup_and_calibration_2026_10).

Synthetic ledgers built on the real Store schema; every oracle is stated here,
never taken from live totals. Test names carry the AC they prove.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

import usage_rollup as ur  # noqa: E402
from store import Store  # noqa: E402

FIXTURE_ITEMS = Path(__file__).resolve().parent / 'fixtures' / 'work_items'
NOW = '2026-10-10T00:00:00Z'
FLEET = '00000000-0000-4000-8000-0000000000f1'
SHARED = '00000000-0000-4000-8000-0000000000f2'
OPUS = 'claude-opus-5-5'
SONNET = 'claude-sonnet-5-5'


def out(n: int) -> dict[str, int]:
    return {'uncached_input': 0, 'cache_read': 0, 'cache_write': 0, 'output': n}


class Fx:
    """A synthetic Thoth data dir: sessions.db (real schema), archive, notifications, history, work items."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.data = tmp / 'data'
        self.data.mkdir()
        self.work = tmp / 'work'
        db = self.data / 'sessions.db'
        store = Store(str(db))
        store.start()
        store.stop()
        self.conn = sqlite3.connect(db)
        # holds is owned by the daemon's resource-hold store; same columns as the live table.
        self.conn.execute('CREATE TABLE IF NOT EXISTS holds (id TEXT PRIMARY KEY, resource TEXT NOT NULL, '
                          'owner_stream TEXT NOT NULL, reason TEXT NOT NULL, acquired_at TEXT NOT NULL, '
                          'expires_at TEXT, released_at TEXT, release_cause TEXT)')
        self.archive_rows: list[tuple] = []
        self.cards: list[tuple] = []
        self.history: list[dict] = []
        self.n = 0

    def seat(self, sid: str, *, created: str, closed: str | None = None, status: str | None = None,
             specs: tuple = (), handoff: str | None = None, provider: str = 'claude', archive: bool = False) -> None:
        host, name = sid.split(':', 1)
        status = status or ('closed' if closed else 'open')
        row = (host, name, 'hidden', created, closed, status, specs[0] if specs else None,
               json.dumps(list(specs)) if specs else None, handoff, provider)
        if archive:
            self.archive_rows.append(row)
        else:
            self.conn.execute('INSERT INTO sessions (host, session_name, visibility, created_at, closed_at, status, '
                              'spec_id, spec_ids, handoff_from_stream_id, provider) VALUES (?,?,?,?,?,?,?,?,?,?)', row)

    def ident(self, native: str, account: str | None, *, host: str = 'thoth', conflict: int = 0,
              provider: str = 'claude') -> None:
        self.conn.execute('INSERT INTO v2_usage_identity VALUES (?,?,?,?,?,?,?,?)',
                          (host, provider, native, None if conflict else account,
                           'transcript' if account or conflict else 'unknown', None, conflict, '2.1.292'))

    def rec(self, stream: str, tokens: dict, *, observed: str | None = '2026-09-01T00:00:00Z', model: str | None = OPUS,
            native: str = 'n-fleet', host: str = 'thoth', provider: str = 'claude', row: bool = True) -> None:
        self.n += 1
        key = f'msg_{self.n:06d}'
        self.conn.execute('INSERT INTO v2_usage_records VALUES (?,?,?,?,?,?,?)',
                          (host, provider, native, key, stream, 'g', json.dumps(tokens)))
        if row and provider == 'claude':
            self.conn.execute('INSERT INTO v2_usage_provenance VALUES (?,?,?,?,?,?)',
                              (host, provider, native, key, observed, model))

    def report(self, stream: str, verdict: str | None, at: str) -> None:
        self.n += 1
        self.conn.execute('INSERT INTO v2_reports (report_id, from_stream_id, status, qa_verdict, ingested_at, '
                          'created_at) VALUES (?,?,?,?,?,?)', (f'r{self.n}', stream, 'done', verdict, at, 0.0))

    def hold(self, owner: str, start: str, end: str) -> None:
        self.n += 1
        self.conn.execute('INSERT INTO holds VALUES (?,?,?,?,?,?,?,?)',
                          (f'h{self.n}', 'gate:x', owner, 'test', start, None, end, 'released'))

    def card(self, producer: str, start: str, end: str | None, *, state: str = 'answered') -> None:
        self.cards.append((f'q{len(self.cards)}', producer, start, end, end, state))

    def item(self, spec_id: str, *, status: str = 'completed', completed_at: str | None = '2026-09-30',
             epic: str | None = None, tags: tuple = ()) -> None:
        folder = self.work / status / spec_id.replace('spec_', '')
        folder.mkdir(parents=True, exist_ok=True)
        lines = ['---', f'id: {spec_id}', f'status: {status}']
        if completed_at:
            lines.append(f"completed_at: '{completed_at}'")
        if epic:
            lines.append(f'epic: {epic}')
        lines.append('tags:')
        lines += [f'- {t}' for t in tags]
        lines += ['---', '# item', '']
        (folder / 'spec.md').write_text('\n'.join(lines))

    def hist(self, observed: str, pct: int, *, account: str | None = FLEET, kind: str = 'seven_day',
             resets: str | None = '2026-10-07T16:00:00.291Z', source: str = 'cache', host: str = 'thoth') -> None:
        self.history.append({'observed_at': observed, 'probed_at': observed, 'host': host, 'provider': 'claude',
                             'account_id': account, 'window_kind': kind, 'window_minutes': 10080, 'pct': pct,
                             'resets_at': resets, 'source': source})

    def usage_state(self, stream: str, updated: str, *, host: str | None = None) -> None:
        self.conn.execute('INSERT INTO v2_usage_state VALUES (?,?,?,?,?,?,?,?)',
                          (stream, 'g', host or stream.split(':', 1)[0], updated, updated, 1, '{}', '[]'))

    def config(self, *, accounts: list | None = None, excluded: tuple = (), threshold: float | None = None,
               anchor: str = '2026-09-02T16:00:00Z', mode: int = 0o600, retired: tuple | None = None) -> Path:
        data = {'schema_version': 1, 'windows': {'anchor': anchor, 'days': 7, 'excluded': list(excluded)},
                'accounts': accounts if accounts is not None else [
                    {'label': 'fleet_only', 'account_id': FLEET, 'provider': 'claude', 'role': 'fleet_only'},
                    {'label': 'shared', 'account_id': SHARED, 'provider': 'claude', 'role': 'shared'}]}
        if threshold is not None:
            data['unplaceable_threshold'] = threshold
        if retired is not None:
            data['retired_hosts'] = list(retired)
        path = self.data / ur.CONFIG_NAME
        path.write_text(json.dumps(data))
        os.chmod(path, mode)
        return path

    def run(self, *args: str, work_root: Path | None = None) -> dict:
        self.conn.commit()
        archive = sqlite3.connect(self.data / 'sessions_archive.db')
        ddl = self.conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone()[0]
        archive.execute(ddl.replace('CREATE TABLE sessions', 'CREATE TABLE IF NOT EXISTS sessions'))
        archive.execute('DELETE FROM sessions')
        archive.executemany('INSERT INTO sessions (host, session_name, visibility, created_at, closed_at, status, '
                            'spec_id, spec_ids, handoff_from_stream_id, provider) VALUES (?,?,?,?,?,?,?,?,?,?)',
                            self.archive_rows)
        archive.commit()
        archive.close()
        notes = sqlite3.connect(self.data / 'notifications.db')
        notes.execute('CREATE TABLE IF NOT EXISTS agent_questions (question_id TEXT PRIMARY KEY, schema_version '
                      'INTEGER, created_at TEXT, updated_at TEXT, answered_at TEXT, producer_stream_id TEXT, '
                      'producer_provider TEXT, spec_id TEXT, dedup_key TEXT, notification_id TEXT, state TEXT, '
                      'envelope TEXT, answer TEXT)')
        notes.execute('DELETE FROM agent_questions')
        notes.executemany('INSERT INTO agent_questions (question_id, producer_stream_id, created_at, answered_at, '
                          'updated_at, state, schema_version, envelope) VALUES (?,?,?,?,?,?,1,"{}")', self.cards)
        notes.commit()
        notes.close()
        (self.data / 'usage_history.jsonl').write_text(''.join(json.dumps(h) + '\n' for h in self.history))
        argv = ['--data-dir', str(self.data), '--work-root', str(work_root or self.work), '--now', NOW, '--json',
                '--calibration-out', str(self.data / 'calibration.json'), *args]
        code, result = ur.run(argv)
        assert code == 0
        return result


@pytest.fixture
def fx(tmp_path: Path) -> Fx:
    return Fx(tmp_path)


def by_account(section: dict, account: str) -> dict:
    return next(a for a in section['by_account'] if a['account_id'] == account)


def entry(result: dict, account: str) -> dict:
    return next(e for e in result['calibration']['entries'] if e['account_id'] == account)


# --- AC1 attribution + folding ------------------------------------------------

def test_usage_rollup_ac1_attribution_folding_and_project(fx: Fx) -> None:
    A, B, C = 'spec_demo__a', 'spec_demo__b', 'spec_demo__c'
    fx.ident('n-fleet', FLEET)
    fx.seat('thoth:p1', created='2026-09-01T00:00:00Z', closed='2026-09-01T01:00:00Z', archive=True)
    fx.seat('thoth:s1', created='2026-09-01T01:00:00Z', specs=(A,), handoff='thoth:p1')
    fx.seat('thoth:p2', created='2026-09-01T00:00:00Z', closed='2026-09-01T01:00:00Z', specs=(B,))
    fx.seat('thoth:s2', created='2026-09-01T01:00:00Z', specs=(A,), handoff='thoth:p2')
    fx.seat('thoth:u', created='2026-09-01T00:00:00Z')
    fx.seat('thoth:x', created='2026-09-01T00:00:00Z', specs=(C,))
    fx.seat('thoth:y', created='2026-09-01T00:00:00Z', specs=(A, C))
    for stream, n in (('thoth:p1', 100), ('thoth:s1', 10), ('thoth:p2', 1000), ('thoth:s2', 1),
                      ('thoth:u', 5), ('thoth:x', 7), ('thoth:y', 50), ('thoth:ghost', 3)):
        fx.rec(stream, out(n))
    for spec in (A, C):
        fx.item(spec, status='in_progress', completed_at=None, epic='epic_demo')
    result = fx.run('--spec', A, B, '--project', 'epic_demo')
    a, b = result['specs']
    assert a['claude']['total_tokens'] == 100 + 10 + 1 + 50  # P1 folded; P2 stays with B
    assert a['folded_streams'] == ['thoth:p1']
    assert 'thoth:p2' not in a['streams']
    assert b['claude']['total_tokens'] == 1000 and b['folded_streams'] == []
    project = result['project']
    assert project['member_specs'] == [A, C]
    assert project['streams'] == 5  # p1, s1, s2, x, y: y counted once although it names A and C
    assert project['claude']['total_tokens'] == 161 + 7
    fleet = by_account(result['fleet_totals'], FLEET)
    assert fleet['unattributed']['tokens'] == 5 + 3  # U and a stream with no seat row
    assert fleet['attributed']['tokens'] == 100 + 10 + 1000 + 1 + 7 + 50


def test_usage_rollup_ac1_folding_walks_unattributed_chain(fx: Fx) -> None:
    fx.seat('thoth:p0', created='2026-09-01T00:00:00Z', closed='2026-09-01T01:00:00Z')
    fx.seat('thoth:p1', created='2026-09-01T01:00:00Z', closed='2026-09-01T02:00:00Z', handoff='thoth:p0')
    fx.seat('thoth:s', created='2026-09-01T02:00:00Z', specs=('spec_demo__a',), handoff='thoth:p1')
    for stream in ('thoth:p0', 'thoth:p1', 'thoth:s'):
        fx.rec(stream, out(1))
    spec = fx.run('--spec', 'spec_demo__a')['specs'][0]
    assert spec['folded_streams'] == ['thoth:p0', 'thoth:p1'] and spec['claude']['total_tokens'] == 3


# --- AC2 pricing ------------------------------------------------------------------

def test_usage_rollup_ac2_pricing_token_weighted_unpriced_and_codex_reconciled(fx: Fx) -> None:
    S = 'spec_demo__priced'
    fx.ident('n-fleet', FLEET)
    fx.seat('thoth:a', created='2026-09-01T00:00:00Z', specs=(S,))
    fx.seat('thoth:cx', created='2026-09-01T00:00:00Z', specs=(S,), provider='codex')
    fx.rec('thoth:a', {'uncached_input': 0, 'cache_read': 1_000_000, 'cache_write': 0, 'output': 0})  # $1.50
    fx.rec('thoth:a', out(1_000))  # $0.075
    fx.rec('thoth:a', {'uncached_input': 1_000_000, 'cache_read': 0, 'cache_write': 0, 'output': 0}, model=SONNET)  # $3
    fx.rec('thoth:a', {'uncached_input': 0, 'cache_read': 0, 'cache_write': 1_000_000, 'output': 0},
           model='claude-fable-5-1')  # $18.75 (Opus-class assumption)
    fx.rec('thoth:a', out(500), model='claude-unknown-9')  # unpriced, never zero
    fx.rec('thoth:cx', {'input_total': 900, 'cached_input': 800, 'output': 50, 'reasoning': 5},
           native='cx-1', provider='codex', row=False)
    claude = fx.run('--spec', S)['specs'][0]['claude']
    assert claude['dollars'] == pytest.approx(1.5 + 0.075 + 3.0 + 18.75)
    assert claude['unpriced_tokens'] == 500
    groups = {g['model']: g for g in claude['by_model_account']}
    assert groups[OPUS]['dollars'] == pytest.approx(1.575) and groups[OPUS]['records'] == 2
    assert groups['claude-unknown-9']['dollars'] is None and groups['claude-unknown-9']['bucket'] == 'unpriced'
    assert claude['partition_tokens']['unpriced'] == 500
    assert by_account(claude, FLEET)['unpriced_tokens'] == 500
    codex = fx.run('--spec', S)['specs'][0]['codex']
    # Codex follow-up shipped: a cumulative row with no responses is partial and wholly unreconciled, never priced.
    assert 'status' not in codex and codex['streams'] == ['thoth:cx'] and codex['dollars'] == 0
    assert codex['outside_window_tokens']['unreconciled'] == {'uncached_input': 100, 'cache_read': 800,
                                                              'cache_write': 0, 'output': 50}
    assert codex['reconciliation']['partial'] == {'sessions': 1, 'tokens': 950, 'unreconciled_tokens': 950}
    assert codex['completeness'] == 0.0


# --- AC3 time metrics --------------------------------------------------------------

def test_usage_rollup_ac3_delivery_endpoint_waits_and_activity(fx: Fx) -> None:
    T = 'spec_demo__timed'
    fx.ident('n-fleet', FLEET)
    fx.seat('thoth:t1', created='2026-09-01T00:00:00Z', closed='2026-09-01T02:00:00Z', specs=(T,))
    fx.seat('thoth:t2', created='2026-09-01T01:30:00Z', closed='2026-09-01T05:00:00Z', specs=(T,),
            handoff='thoth:t1', archive=True)
    fx.report('thoth:t1', 'accept', '2026-09-01T01:00:00Z')  # spec-QA accept
    fx.report('thoth:t2', 'accept', '2026-09-01T03:00:00Z')  # code-QA accept: never the endpoint
    fx.report('thoth:t2', None, '2026-09-01T04:00:00Z')
    for minute in (0, 5, 10):
        fx.rec('thoth:t1', out(1), observed=f'2026-09-01T00:{minute:02d}:00Z')
    for stamp in ('00:08', '00:12', '01:00'):
        fx.rec('thoth:t2', out(1), observed=f'2026-09-01T{stamp}:00Z')
    fx.card('thoth:t2', '2026-09-01T04:00:00Z', None, state='open')  # open: clipped to last seat close
    fx.card('thoth:t2', '2026-09-01T06:00:00Z', '2026-09-01T07:00:00Z')  # outside every seat
    fx.hold('thoth:t1', '2026-09-01T01:00:00Z', '2026-09-01T01:30:00Z')
    fx.item(T, completed_at='2026-09-01')
    t = fx.run('--spec', T)['specs'][0]['time']
    assert t['elapsed_delivery_h'] == {'censored': False, 'value_h': 5.0, 'endpoint': 'close',
                                       'first_open': '2026-09-01T00:00:00Z', 'last_close': '2026-09-01T05:00:00Z'}
    assert t['seats'] == 2 and t['qa_rounds'] == 2 and t['time_to_first_qa_h'] == 1.0
    assert t['known_wait_h'] == 1.5  # 04:00-05:00 card + 01:00-01:30 hold
    assert t['activity_proxy_h']['value'] == 0.2  # union 00:00-00:12, not the 0.2667 sum; the 48-min gap is out
    assert 'proxy' in t['activity_proxy_h']['label']


@pytest.mark.parametrize('case,reason', [('open_seat', 'open_seat'), ('no_close', 'no_close'),
                                         ('not_completed', 'not_completed')])
def test_usage_rollup_ac3_censored_delivery(fx: Fx, case: str, reason: str) -> None:
    T = 'spec_demo__censored'
    fx.seat('thoth:c1', created='2026-09-01T00:00:00Z', closed='2026-09-01T02:00:00Z', specs=(T,))
    if case == 'open_seat':
        fx.seat('thoth:c2', created='2026-09-01T02:00:00Z', specs=(T,), handoff='thoth:c1')
    elif case == 'no_close':
        fx.seat('thoth:c2', created='2026-09-01T02:00:00Z', status='closed', specs=(T,))
    fx.item(T, status='in_progress' if case == 'not_completed' else 'completed',
            completed_at=None if case == 'not_completed' else '2026-09-02')
    d = fx.run('--spec', T)['specs'][0]['time']['elapsed_delivery_h']
    assert d['censored'] is True and d['reason'] == reason and d['elapsed_so_far_h'] > 0
    assert 'value_h' not in d


def test_usage_rollup_ac3_missing_provenance_nulls_activity(fx: Fx) -> None:
    T = 'spec_demo__noprov'
    fx.seat('thoth:m', created='2026-09-01T00:00:00Z', specs=(T,))
    fx.rec('thoth:m', out(1), observed='2026-09-01T00:01:00Z')
    fx.rec('thoth:m', out(1), row=False)
    spec = fx.run('--spec', T)['specs'][0]
    assert spec['time']['activity_proxy_h']['value'] is None
    assert spec['claude']['provenance_row_coverage'] == 0.5


# --- AC4 comparables ---------------------------------------------------------------

def _completed_spec(fx: Fx, spec: str, hours: int, mtok_cache_read: int, *, tags: tuple = ('feature',),
                    completed_at: str = '2026-09-30') -> None:
    fx.seat(f'thoth:{spec}', created='2026-09-01T00:00:00Z', closed=f'2026-09-01T{hours:02d}:00:00Z', specs=(spec,))
    fx.rec(f'thoth:{spec}', {'uncached_input': 0, 'cache_read': mtok_cache_read * 1_000_000, 'cache_write': 0,
                             'output': 0})
    fx.item(spec, completed_at=completed_at, tags=tags)


def test_usage_rollup_ac4_comparables_sparse(fx: Fx) -> None:
    _completed_spec(fx, 'spec_demo__one', 1, 1)
    _completed_spec(fx, 'spec_demo__two', 2, 2)
    comp = fx.run('--comparables', '--repo', 'demo')['comparables']
    assert comp['status'] == 'insufficient_comparables' and comp['count'] == 2
    assert {r['spec_id'] for r in comp['rows']} == {'spec_demo__one', 'spec_demo__two'}


def test_usage_rollup_ac4_comparables_quantiles_kind_and_censoring(fx: Fx) -> None:
    for name, h, mtok in (('a', 1, 1), ('b', 2, 2), ('c', 3, 3), ('d', 4, 4), ('e', 10, 10)):
        _completed_spec(fx, f'spec_demo_repo__{name}', h, mtok)
    _completed_spec(fx, 'spec_demo_repo__legacy', 5, 1, tags=('bug',))
    fx.seat('thoth:open', created='2026-09-01T00:00:00Z', specs=('spec_demo_repo__open',))  # censored
    fx.item('spec_demo_repo__open', tags=('feature',))
    comp = fx.run('--comparables', '--repo', 'demo-repo', '--kind', 'feature')['comparables']
    assert comp['status'] == 'ok' and comp['count'] == 5
    assert comp['elapsed_delivery_h'] == {'p25': 2.0, 'median': 3.0, 'p75': 4.0}
    assert comp['dollars'] == {'p25': 3.0, 'median': 4.5, 'p75': 6.0}
    assert {'spec_id': 'spec_demo_repo__open', 'reason': 'open_seat'} in comp['excluded']
    assert 'spec_demo_repo__open' not in {r['spec_id'] for r in comp['rows']}
    defect = fx.run('--comparables', '--repo', 'demo_repo', '--kind', 'defect')['comparables']
    assert [r['spec_id'] for r in defect['rows']] == ['spec_demo_repo__legacy']  # legacy tag bug == defect
    legacy = fx.run('--comparables', '--repo', 'demo_repo', '--kind', 'bug')['comparables']
    assert legacy['kind'] == 'defect' and legacy['count'] == 1


def test_usage_rollup_ac4_codex_only_rows_have_no_claude_dollars(fx: Fx) -> None:
    for name, h, mtok in (('a', 1, 1), ('b', 2, 2), ('c', 3, 3)):
        _completed_spec(fx, f'spec_demo__{name}', h, mtok)
    fx.seat('thoth:cx', created='2026-09-01T00:00:00Z', closed='2026-09-01T04:00:00Z', specs=('spec_demo__cx',),
            provider='codex')
    fx.rec('thoth:cx', {'input_total': 5, 'cached_input': 0, 'output': 1, 'reasoning': 0}, native='cx',
           provider='codex', row=False)
    fx.item('spec_demo__cx', tags=('feature',))
    comp = fx.run('--comparables', '--repo', 'demo')['comparables']
    cx = next(r for r in comp['rows'] if r['spec_id'] == 'spec_demo__cx')
    assert cx['dollars'] is None and cx['dollars_reason'] == 'codex_only' and cx['codex_dollars'] == 0
    assert comp['count'] == 4 and comp['dollars'] == {'p25': 2.25, 'median': 3.0, 'p75': 3.75}


def test_usage_rollup_ac4_checked_in_synthetic_feature_item(fx: Fx) -> None:
    spec = 'spec_synthetic__feature_completed'
    fx.seat('thoth:syn', created='2026-09-18T00:00:00Z', closed='2026-09-18T06:00:00Z', specs=(spec,))
    fx.rec('thoth:syn', out(1000))
    comp = fx.run('--comparables', '--repo', 'synthetic', '--kind', 'feature',
                  work_root=FIXTURE_ITEMS)['comparables']
    assert [r['spec_id'] for r in comp['rows']] == [spec]
    assert comp['rows'][0]['kind'] == 'feature' and comp['rows'][0]['elapsed_delivery_h'] == 6.0
    assert comp['status'] == 'insufficient_comparables'


# --- AC5 method A -------------------------------------------------------------------

def _method_a_ledger(fx: Fx) -> None:
    fx.ident('n-fleet', FLEET)
    fx.ident('n-unknown', None)
    fx.seat('thoth:w', created='2026-09-01T00:00:00Z')
    fx.rec('thoth:w', out(500_000), observed='2026-09-05T00:00:00Z')  # W0, excluded by config
    fx.rec('thoth:w', out(899_000), observed='2026-09-12T00:00:00Z')  # W1: 0.899
    fx.rec('thoth:w', out(101_000), observed='2026-09-12T01:00:00Z', native='n-unknown')
    fx.rec('thoth:w', out(900_000), observed='2026-09-19T00:00:00Z')  # W2: 0.90 -> used, $67.50
    fx.rec('thoth:w', out(100_000), observed='2026-09-19T01:00:00Z', native='n-unknown')


def test_usage_rollup_ac5_method_a_windows_and_floor(fx: Fx) -> None:
    _method_a_ledger(fx)
    fx.config(excluded=('2026-09-02T16:00:00Z',))
    e = entry(fx.run('--calibrate'), FLEET)
    points = {p['window_start']: p for p in e['methods']['full_week_100']['points']}
    w0, w1, w2 = (points[k] for k in ('2026-09-02T16:00:00Z', '2026-09-09T16:00:00Z', '2026-09-16T16:00:00Z'))
    assert w0['used'] is False and w0['reason'] == 'excluded_by_config'
    assert w1['used'] is False and w1['reason'] == 'coverage_below_0.90' and w1['measured_coverage'] == 0.899
    assert w2['used'] is True and w2['measured_coverage'] == 0.9 and w2['bias'] == 'floor'
    assert w2['dollars'] == 67.5 and w2['usd_per_pct'] == 0.675
    assert e['coefficient'] == 0.675 and e['basis'] == 'full_week_100' and e['status'] == 'fitted'
    assert {'window_start': '2026-09-02T16:00:00Z', 'reason': 'excluded_by_config'} in e['exclusions']
    assert e['quota'] == 'seven_day' and e['window_minutes'] == 10080
    assert e['provenance_row_coverage'] == 1.0 and e['unplaceable']['passes'] is True


def test_usage_rollup_ac5_token_vs_row_coverage(fx: Fx) -> None:
    fx.ident('n-fleet', FLEET)
    fx.ident('n-unknown', None)
    fx.seat('thoth:w', created='2026-09-01T00:00:00Z')
    for i in range(19):
        fx.rec('thoth:w', out(1), observed=f'2026-09-19T00:{i:02d}:00Z')
    fx.rec('thoth:w', out(1000), observed='2026-09-19T01:00:00Z', native='n-unknown')
    fx.config()
    e = entry(fx.run('--calibrate'), FLEET)
    w2 = next(p for p in e['methods']['full_week_100']['points'] if p['window_start'] == '2026-09-16T16:00:00Z')
    assert w2['measured_coverage'] == pytest.approx(19 / 1019, abs=1e-6)  # row counting would give 0.95
    assert w2['used'] is False and w2['reason'] == 'coverage_below_0.90'
    assert e['coefficient'] is None and e['status'] == 'insufficient'


def test_usage_rollup_ac5_two_accounts_no_pooling(fx: Fx) -> None:
    fx.ident('n-fleet', FLEET)
    fx.ident('n-shared', SHARED)
    fx.ident('n-unknown', None)
    fx.ident('n-conflict', None, conflict=1)
    fx.seat('thoth:w', created='2026-09-01T00:00:00Z')
    fx.rec('thoth:w', out(1000), observed='2026-09-19T00:00:00Z')
    fx.rec('thoth:w', out(500), observed='2026-09-19T00:01:00Z', native='n-shared')
    fx.rec('thoth:w', out(200), observed='2026-09-19T00:02:00Z', native='n-unknown')
    fx.rec('thoth:w', out(300), observed='2026-09-19T00:03:00Z', native='n-conflict')
    fx.config()
    result = fx.run('--calibrate')
    w2 = next(p for p in entry(result, FLEET)['methods']['full_week_100']['points']
              if p['window_start'] == '2026-09-16T16:00:00Z')
    assert w2['tokens'] == {'measured': 1000, 'unpriced': 0, 'unknown_account': 500}
    assert w2['dollars'] == pytest.approx(1000 * 75 / 1e6, abs=1e-4)  # only the fleet account's tokens
    shared = entry(result, SHARED)
    assert shared['measured_rollup']['tokens'] == {'measured': 500, 'unpriced': 0, 'unknown_account': 500}
    assert shared['coefficient'] is None and shared['conversion'] is None


def test_usage_rollup_ac5_untimed_records_are_unplaceable(fx: Fx) -> None:
    fx.ident('n-fleet', FLEET)
    # Resumed/imported transcript: really timed 2026-10-05, seat created 2026-10-06, timestamp removed.
    fx.seat('thoth:resumed', created='2026-10-06T00:00:00Z')
    fx.rec('thoth:resumed', out(7), observed=None)  # provenance row with observed_at NULL
    fx.rec('thoth:resumed', out(11), row=False)  # no provenance row
    fx.rec('thoth:resumed', out(10_000), observed='2026-09-19T00:00:00Z')
    fx.config(anchor='2026-09-02T16:00:00Z')
    result = fx.run('--calibrate', '--spec', 'spec_none')
    cal = result['calibration']
    assert cal['unplaceable']['unplaceable_tokens'] == 18
    assert cal['unplaceable']['by_host_account'] == [{'host': 'thoth', 'account_id': FLEET, 'tokens': 18}]
    points = entry(result, FLEET)['methods']['full_week_100']['points']
    assert sum(p['tokens']['measured'] for p in points) == 10_000  # placed in no window (not Oct 5 or Oct 6)
    assert entry(result, FLEET)['measured_rollup']['untimed_tokens'] == 18


@pytest.mark.parametrize('untimed,passes', [(20_000, False), (5_000, True)])
@pytest.mark.parametrize('with_seat', [True, False])
def test_usage_rollup_ac5_unplaceable_mass_gate(fx: Fx, untimed: int, passes: bool, with_seat: bool) -> None:
    fx.ident('n-fleet', FLEET)
    fx.seat('thoth:w', created='2026-09-01T00:00:00Z')
    if with_seat:
        fx.seat('thoth:old', created='2026-08-01T00:00:00Z', closed='2026-08-02T00:00:00Z')
    fx.rec('thoth:w', out(1_000_000 - untimed), observed='2026-09-19T00:00:00Z')
    fx.rec('thoth:old', out(untimed), host='bart', native='n-bart', row=False)
    fx.config()
    result = fx.run('--calibrate')
    u = result['calibration']['unplaceable']
    assert u['ratio'] == untimed / 1_000_000 and u['threshold'] == 0.01 and u['passes'] is passes
    e = entry(result, FLEET)
    points = e['methods']['full_week_100']['points']
    assert {p['eligibility'] for p in points} == {'eligible' if passes else 'unknown'}
    assert e['unplaceable']['ratio'] == untimed / 1_000_000  # printed with every entry
    w2 = next(p for p in points if p['window_start'] == '2026-09-16T16:00:00Z')
    if passes:
        assert w2['used'] is True and w2['measured_coverage'] == 1.0
    else:
        assert w2['used'] is False and w2['reason'] == 'eligibility_unknown'
        assert w2['measured_coverage'] is None and w2['measured_coverage_if_bounded'] == 1.0
        assert e['coefficient'] is None and e['status'] == 'insufficient'


def test_usage_rollup_ac5_fable_quota_reported_only(fx: Fx) -> None:
    _method_a_ledger(fx)
    fx.hist('2026-09-19T00:00:00Z', 10, kind='seven_day_fable')
    fx.hist('2026-09-19T01:00:00Z', 12, kind='seven_day_fable')
    fx.config()
    cal = fx.run('--calibrate')['calibration']
    fable = [r for r in cal['reported_only'] if r['window_kind'] == 'seven_day_fable']
    assert fable and fable[0]['observations'] == 2 and 'not fitted' in fable[0]['reason']
    assert all(e['quota'] == 'seven_day' for e in cal['entries'])


# --- AC6 method B -------------------------------------------------------------------

R1 = '2026-10-07T15:59:59.548Z'   # jittered spellings of the same reset
R1b = '2026-10-07T16:00:00.291Z'
R2 = '2026-10-14T16:00:00.102Z'


class Seq:
    """Builds consecutive history observations and the tokens inside each interval."""

    def __init__(self, fx: Fx, start: str = '2026-10-01T00:00:00Z'):
        self.fx = fx
        self.t = ur.parse_ts(start)
        self.pct = 10
        fx.ident('n-fleet', FLEET)
        fx.ident('n-unknown', None)
        fx.seat('thoth:b', created='2026-09-01T00:00:00Z')
        fx.hist(ur.iso(self.t), self.pct, resets=R1)

    def step(self, delta: int, *, dollars: float | None = None, unknown: int = 0, measured: int | None = None,
             dt: float = 3600.0, resets: str = R1b, source: str = 'cache', fable: bool = False) -> None:
        mid = ur.iso(self.t + dt / 2)
        tokens = measured if measured is not None else round((120.0 * delta if dollars is None else dollars) / 75 * 1e6)
        if tokens:
            self.fx.rec('thoth:b', out(tokens), observed=mid)
        if unknown:
            self.fx.rec('thoth:b', out(unknown), observed=mid, native='n-unknown')
        self.t += dt
        self.pct += delta
        self.fx.hist(ur.iso(self.t), self.pct, resets=resets, source=source)
        if fable:
            self.fx.hist(ur.iso(self.t), self.pct + 50, resets=resets, kind='seven_day_fable')


def _method_b(fx: Fx, valid: list[int], *, noise: list[float] | None = None) -> dict:
    seq = Seq(fx)
    seq.step(valid[0], measured=round(120.0 * valid[0] / 75 * 1e6), unknown=round(120.0 * valid[0] / 75 * 1e6 / 19))
    seq.step(valid[1], dt=86400.0, fable=True)  # exactly 24 h: kept
    seq.step(2, dt=86401.0)  # 24 h + 1 s: excluded
    seq.step(1, measured=949_000, unknown=51_000)  # coverage 0.949: excluded
    seq.step(1, measured=0)  # 19 one-token rows + one 1000-token unknown row below
    for i in range(19):
        fx.rec('thoth:b', out(1), observed=ur.iso(seq.t - 1800 + i))
    fx.rec('thoth:b', out(1000), observed=ur.iso(seq.t - 1700), native='n-unknown')
    seq.step(0)  # delta <= 0
    fx.hist(ur.iso(seq.t), seq.pct, resets=R1, host='merlin')  # same observed_at from another host: stale
    seq.step(0, measured=0, source='probe')  # probe line: dropped before pairing
    fx.history[-1]['account_id'] = FLEET
    for i, delta in enumerate(valid[2:]):
        dollars = 120.0 * delta * (1 + noise[i]) if noise else None
        seq.step(delta, dollars=dollars, fable=True)
    seq.step(5, resets=R2)  # reset crossing
    fx.config()
    return entry(fx.run('--calibrate'), FLEET)['methods']['history_regression']


def test_usage_rollup_ac6_method_b_fit_and_exclusions(fx: Fx) -> None:
    b = _method_b(fx, [19, 3] + [1] * 8)  # 10 valid spanning 30
    reasons = sorted(x['reason'] for x in b['exclusions'])
    # The delta-0 step and the same-time merlin line extend the next interval (amendments § 1): no exclusion.
    assert reasons == sorted(['interval_over_24h', 'coverage_below_0.95', 'coverage_below_0.95',
                              'probe_source', 'reset_crossing'])
    tiny = [x for x in b['exclusions'] if x['reason'] == 'coverage_below_0.95' and x['delta_pct'] == 1]
    assert sorted(x['measured_coverage'] for x in tiny) == pytest.approx([19 / 1019, 0.949], abs=1e-6)
    assert b['valid_samples'] == 10 and b['span_pct'] == 30
    assert b['status'] == 'fitted' and b['coefficient'] == pytest.approx(120.0, abs=0.5)
    assert b['residual_mape_pct'] == 0
    kept = [s for s in b['samples'] if s['interval_s'] == 86400.0]
    assert len(kept) == 1
    assert any(s['measured_coverage'] == 0.95 for s in b['samples'])


@pytest.mark.parametrize('valid', [[19, 3] + [1] * 7, [19, 2] + [1] * 8, [19, 4] + [1] * 7])
def test_usage_rollup_ac6_method_b_activation_threshold(fx: Fx, valid: list[int]) -> None:
    b = _method_b(fx, valid)  # 9 valid / 10 valid spanning 29 / 9 valid spanning 30
    assert b['status'] == 'insufficient' and b['coefficient'] is None


def test_usage_rollup_ac6_method_b_noisy_residual(fx: Fx) -> None:
    # Valid samples (19, 3, then eight 3-pt steps at +/-10 %): k = 120 exactly, median abs pct error = 10 %.
    b = _method_b(fx, [19, 3] + [3] * 8, noise=[0.1, -0.1] * 4)
    assert b['valid_samples'] == 10
    assert b['coefficient'] == pytest.approx(120.0, abs=1e-6)
    assert b['residual_mape_pct'] == pytest.approx(10.0, abs=1e-6)


def test_usage_rollup_ac6_regression_preferred_over_method_a(fx: Fx) -> None:
    _method_b(fx, [19, 3] + [1] * 8)
    e = entry(fx.run('--calibrate'), FLEET)
    assert e['basis'] == 'history_regression' and e['coefficient'] == pytest.approx(120.0, abs=0.5)


# --- amendments AC1: Method B pairs against the previous pct-change observation ---------------
# spec_pentacle__usage_calibration_amendments_2026_10

def _pairs(fx: Fx, pcts: list[int], gaps: list[int], *, dt: float = 3600.0,
           unknown_gaps: dict[int, int] | None = None) -> dict:
    """History pcts[0..n] one dt apart; gaps[i] measured tokens (unknown_gaps[i] unknown) between obs i and i+1."""
    fx.ident('n-fleet', FLEET)
    fx.ident('n-unknown', None)
    fx.seat('thoth:b', created='2026-09-01T00:00:00Z')
    t = ur.parse_ts('2026-10-01T00:00:00Z')
    fx.hist(ur.iso(t), pcts[0], resets=R1)
    for i, pct in enumerate(pcts[1:]):
        if gaps[i]:
            fx.rec('thoth:b', out(gaps[i]), observed=ur.iso(t + dt / 2))
        if (unknown_gaps or {}).get(i):
            fx.rec('thoth:b', out(unknown_gaps[i]), observed=ur.iso(t + dt / 2), native='n-unknown')
        t += dt
        fx.hist(ur.iso(t), pct, resets=R1b)
    fx.config()
    return entry(fx.run('--calibrate'), FLEET)['methods']['history_regression']


def _formed(b: dict) -> list[dict]:
    return sorted([*b['samples'], *[x for x in b['exclusions'] if 'interval_s' in x]], key=lambda x: x['from'])


def test_usage_calibration_amend_ac1_zero_run_extends_interval(fx: Fx) -> None:
    b = _pairs(fx, [10, 10, 10, 12], [100, 200, 300])
    assert len(b['samples']) == 1 and b['exclusions'] == []
    s = b['samples'][0]
    # Consecutive pairing would give one sample of 300 tokens (and drop t1 + t2).
    assert s['delta_pct'] == 2 and s['tokens']['measured'] == 600 and s['observations'] == 4
    assert s['interval_s'] == 3 * 3600.0 and s['dollars'] == pytest.approx(600 * 75 / 1e6)
    assert b['interval_union']['intervals'] == 1 and b['interval_union']['tokens']['measured'] == 600


def test_usage_calibration_amend_ac1_two_samples_token_split(fx: Fx) -> None:
    b = _pairs(fx, [10, 12, 12, 15], [100, 200, 300])
    assert [(s['delta_pct'], s['tokens']['measured']) for s in b['samples']] == [(2, 100), (3, 500)]
    assert b['exclusions'] == []


def test_usage_calibration_amend_ac1_decrease_starts_new_base(fx: Fx) -> None:
    b = _pairs(fx, [10, 12, 11, 13], [100, 200, 300])
    assert [(s['delta_pct'], s['tokens']['measured']) for s in b['samples']] == [(2, 100), (2, 300)]
    assert [(x['delta_pct'], x['reason']) for x in b['exclusions']] == [(-1, 'pct_decrease_new_base')]
    assert b['interval_union']['tokens']['measured'] == 400  # the 12 -> 11 gap is in no sample


def test_usage_calibration_amend_ac1_zero_run_over_24h_excluded(fx: Fx) -> None:
    b = _pairs(fx, [10, 10, 10, 11], [100, 100, 100], dt=10 * 3600.0)  # each gap 10 h, merged 30 h
    assert b['samples'] == []
    assert [(x['delta_pct'], x['interval_s'], x['reason']) for x in _formed(b)] == [(1, 108000.0, 'interval_over_24h')]


def test_usage_calibration_amend_ac1_coverage_judged_on_merged_interval(fx: Fx) -> None:
    # The delta-0 gap holds 51,000 unknown-account tokens; the closing gap alone would have coverage 1.0.
    b = _pairs(fx, [10, 10, 11], [0, 949_000], unknown_gaps={0: 51_000})
    assert b['samples'] == []
    (x,) = _formed(b)
    assert x['reason'] == 'coverage_below_0.95' and x['measured_coverage'] == 0.949
    assert x['tokens'] == {'measured': 949_000, 'unpriced': 0, 'unknown_account': 51_000}


# --- amendments AC2: retired hosts ---------------------------------------------------------------

def _retired_ledger(fx: Fx, *, bart_open: bool = False) -> None:
    fx.ident('n-fleet', FLEET)
    fx.seat('thoth:w', created='2026-09-01T00:00:00Z', specs=('spec_demo__w',))
    fx.seat('bart:old', created='2026-08-01T00:00:00Z', specs=('spec_demo__old',),
            closed=None if bart_open else '2026-08-02T00:00:00Z')
    fx.rec('thoth:w', out(990_000), observed='2026-09-19T00:00:00Z')
    fx.rec('bart:old', out(30_000), host='bart', native='n-bart', row=False)  # untimed forever


def _spec_totals(result: dict) -> list:
    return [(s['spec_id'], s['claude']['total_tokens'], s['claude']['dollars'], s['claude']['unpriced_tokens'],
             [(a['account_id'], a['tokens'], a['dollars']) for a in s['claude']['by_account']])
            for s in result['specs']]


def test_usage_calibration_amend_ac2_retired_host_honoured(fx: Fx) -> None:
    _retired_ledger(fx)
    fx.usage_state('bart:old', '2026-10-02T23:59:00Z')  # last push 7 days + 1 min before NOW
    fx.seat('bart:assistant', created='2026-09-19T00:00:00Z', provider='composite')  # open routing alias
    fx.config()
    before = fx.run('--calibrate', '--spec', 'spec_demo__w', 'spec_demo__old')
    assert before['calibration']['unplaceable']['ratio'] == round(30_000 / 1_020_000, 6)
    assert before['calibration']['unplaceable']['passes'] is False
    fx.config(retired=('bart',))
    after = fx.run('--calibrate', '--spec', 'spec_demo__w', 'spec_demo__old')
    u = after['calibration']['unplaceable']
    assert after['calibration']['retired_hosts'] == [{'host': 'bart', 'status': 'honoured',
                                                      'open_composite_rows_not_counted': 1}]
    assert u['ratio'] == 0.0 and u['provider_total_tokens'] == 990_000 and u['passes'] is True
    assert u['retired_mass'] == [{'provider': 'claude', 'host': 'bart', 'tokens': 30_000}]
    assert u['by_host_account'] == []
    assert _spec_totals(after) == _spec_totals(before)  # per-spec rollups unchanged


def test_usage_calibration_amend_ac2_ignored_with_open_seat(fx: Fx, capsys) -> None:
    _retired_ledger(fx, bart_open=True)
    fx.config(retired=('bart',))
    cal = fx.run('--calibrate')['calibration']
    assert cal['retired_hosts'] == [{'host': 'bart', 'status': 'ignored', 'reason': 'open seat in sessions (1)'}]
    assert cal['unplaceable']['ratio'] == round(30_000 / 1_020_000, 6) and cal['unplaceable']['retired_mass'] == []
    assert "retired_hosts entry 'bart' ignored: open seat" in capsys.readouterr().err


def test_usage_calibration_amend_ac2_ignored_with_recent_push(fx: Fx, capsys) -> None:
    _retired_ledger(fx)
    fx.usage_state('bart:old', '2026-10-03T00:01:00Z')  # 7 days - 1 min before NOW
    fx.config(retired=('bart',))
    cal = fx.run('--calibrate')['calibration']
    assert cal['retired_hosts'] == [{'host': 'bart', 'status': 'ignored',
                                     'reason': 'v2_usage_state row updated within 7 days'}]
    assert cal['unplaceable']['passes'] is False and cal['unplaceable']['retired_mass'] == []
    assert 'within 7 days' in capsys.readouterr().err


# --- amendments AC3: identity mass by host is an exclusive partition -----------------------------

def test_usage_calibration_amend_ac3_identity_mass_partition(fx: Fx) -> None:
    w = '2026-09-19T00:%02d:00Z'
    fx.ident('n-fleet', FLEET)
    fx.ident('n-unknown', None)
    fx.ident('n-m-fleet', FLEET, host='merlin')
    fx.ident('n-m-conflict', None, host='merlin', conflict=1)
    fx.ident('n-bart', FLEET, host='bart')
    fx.seat('thoth:w', created='2026-09-01T00:00:00Z')
    fx.rec('thoth:w', out(1000), observed=w % 1)                                   # measured
    fx.rec('merlin:m', out(200), observed=w % 2, model='claude-mystery-1', host='merlin', native='n-m-fleet')  # unpriced
    fx.rec('thoth:w', out(300), observed=w % 3, native='n-unknown')                # unknown_account
    fx.rec('merlin:m', out(400), observed=w % 4, host='merlin', native='n-m-conflict')  # unknown_account.conflict
    fx.rec('thoth:w', out(50), observed=None)                                      # untimed
    fx.rec('merlin:m', out(60), host='merlin', native='n-m-fleet', row=False)      # untimed
    fx.rec('bart:old', out(700), observed=w % 5, host='bart', native='n-bart')     # retired, timed in window
    fx.rec('bart:old', out(800), host='bart', native='n-bart', row=False)          # retired, untimed
    fx.config(retired=('bart',))
    cal = fx.run('--calibrate')['calibration']
    e = entry({'calibration': cal}, FLEET)
    w2 = next(p for p in e['methods']['full_week_100']['points'] if p['window_start'] == '2026-09-16T16:00:00Z')
    assert w2['tokens'] == {'measured': 1000, 'unpriced': 200, 'unknown_account': 700}
    expected = [
        {'provider': 'claude', 'host': 'bart',
         'window': {'measured': 0, 'unpriced': 0, 'unknown_account': 0, 'conflict': 0},
         'outside_window_sum': {'untimed': 0, 'retired': 1500}},
        {'provider': 'claude', 'host': 'merlin',
         'window': {'measured': 0, 'unpriced': 200, 'unknown_account': 400, 'conflict': 400},
         'outside_window_sum': {'untimed': 60, 'retired': 0}},
        {'provider': 'claude', 'host': 'thoth',
         'window': {'measured': 1000, 'unpriced': 0, 'unknown_account': 300, 'conflict': 0},
         'outside_window_sum': {'untimed': 50, 'retired': 0}},
    ]
    assert w2['identity_mass_by_host'] == expected
    for block, parent in ((w2['identity_mass_by_host'], w2['tokens']),
                          (e['identity_mass_by_host'], e['measured_rollup']['tokens'])):
        for bucket in ('measured', 'unpriced', 'unknown_account'):  # window sum == parent denominator, exactly
            assert sum(h['window'][bucket] for h in block) == parent[bucket]
        assert all(h['window']['conflict'] <= h['window']['unknown_account'] for h in block)
    # Every record lands in exactly one bucket: window sum + untimed + retired == all Claude tokens.
    top = cal['identity_mass_by_host']
    placed = sum(h['window'][b] for h in top for b in ('measured', 'unpriced', 'unknown_account'))
    assert placed + sum(sum(h['outside_window_sum'].values()) for h in top) == 3510
    assert placed == 1900
    assert all('identity_mass_by_host' in x for x in cal['entries'])
    assert e['methods']['history_regression']['interval_union']['intervals'] == 0


# --- AC7 Codex mass stays out of the Claude calibration ----------------------------------

def test_usage_rollup_ac7_codex_never_enters_claude_windows(fx: Fx) -> None:
    _method_a_ledger(fx)
    fx.seat('thoth:cx', created='2026-09-01T00:00:00Z', specs=('spec_demo__cx',), provider='codex')
    fx.rec('thoth:cx', {'input_total': 10**9, 'cached_input': 0, 'output': 0, 'reasoning': 0},
           native='cx', provider='codex', row=False)
    fx.history.append({'observed_at': '2026-09-19T00:00:00Z', 'probed_at': '2026-09-19T00:00:00Z', 'host': 'thoth',
                       'provider': 'codex', 'account_id': 'codex-acct', 'window_kind': 'codex',
                       'window_minutes': 10080, 'pct': 50, 'resets_at': R1, 'source': 'rollout'})
    fx.config()
    result = fx.run('--calibrate', '--spec', 'spec_demo__cx')
    cal = result['calibration']
    assert {e['provider'] for e in cal['entries']} == {'claude'}
    assert [(e['provider'], e['account_id'], e['status']) for e in cal['codex']['entries']] == [
        ('codex', 'codex-acct', 'insufficient')]
    assert cal['unplaceable']['provider_total_tokens'] == 2_500_000  # Codex mass never enters Claude windows
    spec = result['specs'][0]
    assert spec['claude']['total_tokens'] == 0 and spec['codex']['records'] == 1
    stored = json.loads((fx.data / 'calibration.json').read_text())
    assert all(e['provider'] == 'claude' for e in stored['entries'])
    assert oct(os.stat(fx.data / 'calibration.json').st_mode & 0o777) == '0o600'


# --- AC8 shared account -------------------------------------------------------------

def _shared_ledger(fx: Fx) -> None:
    _method_a_ledger(fx)
    fx.ident('n-shared', SHARED)
    fx.rec('thoth:w', out(450_000), observed='2026-09-19T02:00:00Z', native='n-shared')  # $33.75
    fx.hist('2026-09-19T03:00:00Z', 62, account=SHARED)


def test_usage_rollup_ac8_shared_account_not_fitted(fx: Fx) -> None:
    _shared_ledger(fx)
    fx.config()
    shared = entry(fx.run('--calibrate'), SHARED)
    assert shared['conversion'] is None and shared['coefficient'] is None
    assert shared['reason'] == 'shared account; not fitted'


def test_usage_rollup_ac8_transfer_requires_justification(fx: Fx) -> None:
    _shared_ledger(fx)
    accounts = [{'label': 'fleet_only', 'account_id': FLEET, 'role': 'fleet_only'},
                {'label': 'shared', 'account_id': SHARED, 'role': 'shared', 'transfer_from': 'fleet_only'}]
    fx.config(accounts=accounts, excluded=('2026-09-02T16:00:00Z',))
    shared = entry(fx.run('--calibrate'), SHARED)
    assert shared['conversion'] is None and 'justification' in shared['reason']
    accounts[1]['justification'] = 'operator decision: same plan tier'
    fx.config(accounts=accounts, excluded=('2026-09-02T16:00:00Z',))
    result = fx.run('--calibrate', '--spec', 'spec_none')
    shared = entry(result, SHARED)
    assert shared['basis'] == 'transferred' and shared['conversion'] == 0.675
    w2 = next(r for r in shared['external_residual'] if r['window_start'] == '2026-09-16T16:00:00Z')
    assert w2['predicted_pct'] == 50.0 and w2['observed_pct'] == 62 and w2['external_residual'] == 12.0
    assert w2['label'] == 'estimate of non-fleet use'


def test_usage_rollup_ac8_missing_role_is_never_fitted(fx: Fx) -> None:
    _method_a_ledger(fx)
    fx.config(accounts=[{'label': 'fleet_only', 'account_id': FLEET, 'role': 'fleet_only'},
                        {'label': 'shared', 'account_id': SHARED}], excluded=('2026-09-02T16:00:00Z',))
    result = fx.run('--calibrate')
    assert entry(result, FLEET)['coefficient'] == 0.675
    shared = entry(result, SHARED)
    assert shared['status'] == 'not_fitted' and shared['coefficient'] is None and 'role' in shared['reason']


def test_usage_rollup_ac6_invalid_history_lines_are_listed(fx: Fx) -> None:
    _method_a_ledger(fx)
    fx.hist('not-a-time', 10)
    fx.hist('2026-09-19T00:00:00Z', None)
    fx.config()
    b = entry(fx.run('--calibrate'), FLEET)['methods']['history_regression']
    assert [x['reason'] for x in b['exclusions']] == ['invalid_line', 'invalid_line']


# --- AC9 private config, redaction, weekly percent ---------------------------------

def test_usage_rollup_ac9_private_config_rules(fx: Fx, tmp_path: Path) -> None:
    fx.seat('thoth:w', created='2026-09-01T00:00:00Z')
    fx.config(mode=0o644)
    with pytest.raises(ur.RollupError, match='0600'):
        fx.run('--calibrate')
    example = ur.PRICING_PATH.with_name('calibration_config.example.json')
    with pytest.raises(ur.RollupError, match='outside the repository'):
        fx.run('--calibrate', '--config', str(example))
    os.chmod(fx.data / ur.CONFIG_NAME, 0o600)
    cal = fx.run('--calibrate')['calibration']
    assert cal['config_path'] == str(fx.data / ur.CONFIG_NAME) and cal['config_loaded'] is True
    (fx.data / ur.CONFIG_NAME).unlink()
    cal = fx.run('--calibrate')['calibration']
    assert cal['config_loaded'] is False  # never falls back to the public example
    blocker = tmp_path / 'a-file'
    blocker.write_text('')
    with pytest.raises(ur.RollupError, match='cannot write'):
        fx.run('--calibrate', '--calibration-out', str(blocker / 'calibration.json'))


def test_usage_rollup_ac9_example_config_is_synthetic() -> None:
    example = json.loads(ur.PRICING_PATH.with_name('calibration_config.example.json').read_text())
    assert all(a['account_id'].startswith('00000000-0000-4000-8000-') for a in example['accounts'])


def test_usage_rollup_ac9_weekly_pct_and_redaction(fx: Fx) -> None:
    _method_a_ledger(fx)
    fx.seat('thoth:s', created='2026-09-01T00:00:00Z', specs=('spec_demo__pct',))
    fx.rec('thoth:s', out(90_000), observed='2026-09-06T00:00:00Z')  # excluded W0; $6.75 -> 10 % at $0.675 per 1 %
    fx.config(excluded=('2026-09-02T16:00:00Z',))
    result = fx.run('--calibrate', '--spec', 'spec_demo__pct', '--redact')
    acct = by_account(result['specs'][0]['claude'], 'fleet_only')
    assert acct['weekly_pct']['value'] == 10.0 and acct['weekly_pct']['basis'] == 'full_week_100'
    rendered = json.dumps(result)
    assert FLEET not in rendered
    stored = (fx.data / 'calibration.json').read_text()
    assert FLEET in stored  # the private output keeps real ids for later reads
    later = fx.run('--spec', 'spec_demo__pct')
    assert by_account(later['specs'][0]['claude'], FLEET)['weekly_pct']['value'] == 10.0


def test_usage_rollup_text_render_smoke(fx: Fx, capsys) -> None:
    _method_a_ledger(fx)
    fx.config()
    fx.run()  # materialize the data dir
    assert ur.main(['--data-dir', str(fx.data), '--work-root', str(fx.work), '--now', NOW, '--calibrate',
                    '--calibration-out', str(fx.data / 'c.json'), '--spec', 'spec_none']) == 0
    assert 'calibration:' in capsys.readouterr().out
