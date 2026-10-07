"""Codex reconciled_by_rows (spec_pentacle__usage_codex_reconciled_by_rows_2026_10).

Every journey runs the real producer (``native_provenance`` / ``file_items``), the real sink and Store merge,
and the real read-only rollup over a disposable data dir. The fixture writer (``Rollout``) builds the
synthetic transcript and derives every expected count from it (row sums, the client's max-merged ledger
counter, the thread counter, the boundary ordinal); nothing here is copied from live totals or tuned
toward the reconciliation's 1,289. Test names carry the Plan 3 journey / AC they prove.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path

import pytest

import usage_rollup as ur
from test_usage_rollup import Fx
from store import Store
from usage_accounting import native_provenance, native_usage
from usage_history import HistoryLog
from usage_provenance import PAYLOAD_VERSION, ProvenanceSink, file_items, validate_item

HOST = 'ledger-host'
SAT_HOST = 'worker-one'
SAT_SECRET = 'worker-one-host-secret'
SPEC = 'spec_demo__cx'
STREAM = 'ledger-host:cx'
ACCT = '00000000-0000-4000-8000-0000000000c1'
ACCT2 = '00000000-0000-4000-8000-0000000000c2'
MODEL = 'gpt-6-sol'
T0 = 1_790_812_800  # 2026-10-01T00:00:00Z
FIELDS5 = ('input', 'cached_input', 'cache_write_input', 'output', 'reasoning_output')
USAGE_KEYS = dict(zip(FIELDS5, ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                                'output_tokens', 'reasoning_output_tokens')))


def vec(i: int, c: int = 0, o: int = 0, r: int = 0, w: int = 0) -> dict[str, int]:
    return {'input': i, 'cached_input': c, 'cache_write_input': w, 'output': o, 'reasoning_output': r}


def usage(v: dict[str, int]) -> dict[str, int]:
    return {USAGE_KEYS[f]: v[f] for f in FIELDS5}


def plus(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    return {f: a[f] + b[f] for f in FIELDS5}


def buckets(v: dict[str, int]) -> dict[str, int]:
    return {'uncached_input': v['input'] - v['cached_input'], 'cache_read': v['cached_input'],
            'cache_write': v['cache_write_input'], 'output': v['output']}


class Rollout:
    """Fixture writer: one synthetic Codex rollout plus every count the oracles expect, derived here."""

    def __init__(self, native: str, *, accounts: tuple = (ACCT,)):
        self.native = native
        self.t = T0
        self.records: list[dict] = []
        for account in accounts:
            self.records.append({'type': 'session_meta', 'timestamp': self._ts(),
                                 'payload': {'id': native, 'cli_version': '0.160.0', 'creator_account_id': account}})
        self.thread = vec(0)   # Codex's own thread_token_usage counter
        self.client = vec(0)   # Codex's total_token_usage counter (the ledger's source)
        self.own: list[tuple[str, dict[str, int], dict[str, int]]] = []  # (response_id, usage, thread after)
        self.turn = 0

    def _ts(self) -> str:
        self.t += 60
        return ur.iso(self.t)

    def respond(self, rid: str, v: dict[str, int], *, applied: bool = True, snapshot: bool = True,
                thread_id: str | None = None, reset: bool = False, client: dict[str, int] | None = None) -> 'Rollout':
        """``applied=False``: the token_count reports zero last_token_usage and its total skips this response;
        ``snapshot=False``: a trailing response with no token_count; ``reset``: both counters restart here;
        ``client``: an explicit total_token_usage (the ledger source) for this response's token_count."""
        owner = thread_id or self.native
        foreign = owner != self.native
        if not foreign:
            self.thread = dict(v) if reset else plus(self.thread, v)
            self.own.append((rid, dict(v), dict(self.thread)))
        self.turn += 1
        turn = f'turn-{self.turn}'
        self.records.append({'type': 'turn_context', 'timestamp': self._ts(),
                             'payload': {'turn_id': turn, 'model': MODEL}})
        self.records.append({'type': 'token_usage_record', 'timestamp': self._ts(),
                             'payload': {'turn_id': turn, 'response_id': rid, 'thread_id': owner,
                                         'session_id': self.native, 'usage': usage(v),
                                         'thread_token_usage': usage(self.thread)}})
        if snapshot:
            if applied and not foreign:
                self.client = dict(v) if reset else plus(self.client, v)
            if client is not None:
                self.client = dict(client)
            self.records.append({'type': 'event_msg', 'timestamp': self._ts(), 'payload': {
                'type': 'token_count', 'info': {'total_token_usage': usage(self.client),
                                                'last_token_usage': usage(v if applied else vec(0))}}})
        return self

    # --- derived oracles -------------------------------------------------------------------------------------
    def ledger(self) -> dict[str, int]:
        """The cumulative ledger row exactly as the collector stores it: per-field max-merge of every
        token_count total, parsed by the product's own ``native_usage``."""
        merged: dict[str, int] = {}
        for record in self.records:
            obs, _reason = native_usage('codex', record, self.native)
            if obs is not None:
                merged = {k: max(merged.get(k, 0), v) for k, v in obs.tokens.items()}
        return merged

    def rows_sum(self, upto: int | None = None) -> dict[str, int]:
        total = vec(0)
        for _rid, v, _thread in self.own[:upto]:
            total = plus(total, v)
        return total

    @property
    def n(self) -> int:
        return len(self.own)

    def thread_at(self, n: int) -> dict[str, int]:
        return self.own[n - 1][2]

    def unverifiable_today(self, ledger: dict | None = None) -> bool:
        rows = buckets(self.rows_sum())
        ledger = ur.codex_ledger_buckets(self.ledger() if ledger is None else ledger)
        return any(rows[b] > ledger.get(b, 0) for b in rows)

    def text(self) -> str:
        return ''.join(json.dumps(r) + '\n' for r in self.records)


def zero_last_rollout(native: str, *, k: int = 4, skipped: tuple = (3,)) -> Rollout:
    """The reproduced positive: Codex reports zero last_token_usage for some responses, so the cumulative
    ledger counter never applies them; the rows and the thread counter do."""
    r = Rollout(native)
    for i in range(1, k + 1):
        r.respond(f'{native}-r{i}', vec(1000 * i, 400 * i, 50 * i, 10 * i), applied=i not in skipped)
    return r


class Lab(Fx):
    """A disposable ledger-host data dir: real Store schema, the ledger-host backfill tool and the real rollup."""

    def __init__(self, tmp: Path):
        super().__init__(tmp)
        self.root = tmp / 'codex'
        self.seat(STREAM, created=ur.iso(T0), specs=(SPEC,), provider='codex')
        self.ledgers: dict[str, dict] = {}
        self.conn.commit()

    @property
    def db(self) -> Path:
        return self.data / 'sessions.db'

    def path(self, native: str) -> Path:
        return self.root / '2026' / '10' / '01' / f'rollout-2026-10-01T00-00-00-{native}.jsonl'

    def add(self, rollout: Rollout, *, host: str = HOST, ledger: dict | None = None, write: bool = True) -> Rollout:
        ledger = rollout.ledger() if ledger is None else ledger
        self.ledgers[rollout.native] = ledger
        if write:
            self.write(rollout)
        self.conn.execute('INSERT INTO v2_usage_records VALUES (?,?,?,?,?,?,?)',
                          (host, 'codex', rollout.native, 'cumulative', STREAM, 'g', json.dumps(ledger)))
        self.conn.commit()
        return rollout

    def write(self, rollout: Rollout) -> None:
        path = self.path(rollout.native)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rollout.text())

    def backfill(self, capsys=None) -> int:
        """One ledger-host backfill pass (tools/backfill_usage_provenance.py), no cursor."""
        from tools import backfill_usage_provenance as tool

        self.conn.commit()
        code = tool.main(['--db', str(self.db), '--host', HOST, '--provider', 'codex',
                          '--codex-root', str(self.root), '--no-cursor'])
        if capsys is not None:
            capsys.readouterr()
        return code

    def admit(self, items: list[dict], *, version: int = PAYLOAD_VERSION, host: str = HOST) -> dict:
        self.conn.commit()
        return admit(self.db, items, version=version, host=host)

    def codex(self) -> dict:
        return self.run('--spec', SPEC)['specs'][0]['codex']

    def session(self, native: str) -> dict | None:
        entries = self.codex().get('proof_sessions') or []
        want = ur_digest(native)
        return next((e for e in entries if e['session'] == want), None)


def admit(db: Path, items: list[dict], *, version: int = PAYLOAD_VERSION, host: str = HOST) -> dict:
    """Items through the real sink and Store (the daemon's admission path)."""
    async def run() -> dict:
        store = Store(str(db))
        store.start()
        try:
            sink = ProvenanceSink(store.record_provenance, None)
            return await sink.admit(host, {'version': version, 'items': items})
        finally:
            store.stop()

    return asyncio.run(run())


def ur_digest(native: str) -> str:
    from usage_codex_sample_reconcile import digest
    return digest(native)


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    return Lab(tmp_path)


def proof_rows(db: Path, native: str) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute('SELECT response_id, transcript_seq, source FROM v2_usage_codex_thread_proof '
                            'WHERE native_session_id=? ORDER BY transcript_seq, response_id', (native,)).fetchall()


def flags(db: Path, native: str) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute('SELECT flag, response_id FROM v2_usage_codex_thread_flags '
                            'WHERE native_session_id=? ORDER BY flag, response_id', (native,)).fetchall()


def ledger_dump(db: Path) -> list:
    with sqlite3.connect(db) as conn:
        return [tuple(r) for t in ('v2_usage_records', 'v2_usage_state')
                for r in conn.execute(f'SELECT * FROM {t} ORDER BY 1,2,3,4')]


def assert_proven(lab: Lab, r: Rollout, n: int | None = None) -> dict:
    n = r.n if n is None else n
    entry = lab.session(r.native)
    assert entry is not None, 'session with proof rows must be listed'
    assert entry['class'] == 'reconciled_by_rows', entry
    assert entry['source'] == 'rows' and entry['proof_boundary_seq'] == n
    assert entry['thread_vector'] == r.thread_at(n)
    ledger = ur.codex_ledger_buckets(lab.ledgers[r.native])
    thread = buckets(r.thread_at(n))
    assert entry['ledger_below_thread_by'] == {b: thread[b] - ledger.get(b, 0) for b in thread}
    assert 'reconciled_by_rows_blocked_by' not in entry
    return entry


def assert_blocked(lab: Lab, r: Rollout, *clauses: str) -> dict:
    entry = lab.session(r.native)
    assert entry is not None
    assert entry['class'] == 'unverifiable', entry
    for clause in clauses:
        assert clause in entry['reconciled_by_rows_blocked_by'], entry
    return entry


# --- (a) the reproduced positive ------------------------------------------------------------------------------

def test_a_zero_last_session_reconciled_by_rows_one_source_of_mass(lab: Lab, capsys) -> None:
    r = lab.add(zero_last_rollout('n-a'))
    assert r.unverifiable_today()  # the fixture reproduces today's class
    assert lab.backfill(capsys) == 0
    codex = lab.codex()
    assert_proven(lab, r)
    rows = buckets(r.rows_sum())
    # AC4: exactly the proven prefix's row mass, ledger never added, unreconciled zero by construction
    assert codex['tokens'] == rows and codex['placed_tokens'] == sum(rows.values())
    assert codex['partition_tokens']['unverifiable'] == 0
    assert codex['partition_tokens']['unreconciled'] == 0
    assert codex['reconciliation']['reconciled_by_rows'] == {
        'sessions': 1, 'tokens': sum(rows.values()), 'blocked_sessions': 0, 'blocked_by': {}}
    assert codex['reconciliation']['unverifiable']['sessions'] == 0
    assert codex['completeness'] == 1.0


# --- (b) completeness: wrong id, missing + extra id with the same five-field sum -------------------------------

@pytest.mark.parametrize('setup', ['proof_names_wrong_response', 'missing_plus_extra_response'])
def test_b_same_sum_wrong_ids_blocked_completeness(lab: Lab, capsys, setup: str) -> None:
    r = lab.add(zero_last_rollout('n-b'))
    assert lab.backfill(capsys) == 0
    victim = r.own[1][0]
    if setup == 'proof_names_wrong_response':
        lab.conn.execute('UPDATE v2_usage_codex_thread_proof SET response_id=? WHERE response_id=?',
                         ('n-b-wrong', victim))
    else:
        row = lab.conn.execute('SELECT * FROM v2_usage_codex_responses WHERE response_id=?', (victim,)).fetchone()
        lab.conn.execute('DELETE FROM v2_usage_codex_responses WHERE response_id=?', (victim,))
        lab.conn.execute('INSERT INTO v2_usage_codex_responses VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (row[0], row[1], 'n-b-extra', *row[3:]))
    lab.conn.commit()
    with sqlite3.connect(lab.db) as conn:  # the five-field sum is unchanged
        total = conn.execute('SELECT SUM(input),SUM(cached_input),SUM(cache_write_input),SUM(output),'
                             'SUM(reasoning_output) FROM v2_usage_codex_responses').fetchone()
    assert dict(zip(FIELDS5, total)) == r.rows_sum()
    assert_blocked(lab, r, 'completeness')


# --- (b2) equal-sum duplicate ordinal --------------------------------------------------------------------------

def test_b2_equal_sum_duplicate_ordinal_not_written_and_blocked(lab: Lab) -> None:
    v = vec(900, 300, 40, 10)
    r = Rollout('n-b2').respond('n-b2-r1', v)
    lab.add(r, ledger={'input_total': 800, 'cached_input': 300, 'output': 40, 'reasoning': 10})
    assert r.unverifiable_today(lab.ledgers[r.native])
    item = next(i for i in file_items('codex', str(lab.path(r.native))) if i['kind'] == 'codex_response')
    zero = json.loads(json.dumps(item))
    zero['data'].update(response_id='n-b2-r0', **vec(0))  # a zero-usage response offered at the same ordinal
    assert item['data']['transcript_seq'] == zero['data']['transcript_seq'] == 1
    assert zero['data']['thread_token_usage'] == item['data']['thread_token_usage'] == v
    ack = lab.admit([item, zero])
    assert ack['counts'] == {'recorded': 2}  # both response rows are stored (responses are independent)
    assert proof_rows(lab.db, r.native) == [('n-b2-r1', 1, 'backfill')]  # the second ordinal-1 proof is not
    assert flags(lab.db, r.native) == [('duplicate_ordinal', 'n-b2-r0')]
    entry = assert_blocked(lab, r, 'completeness', 'flags')
    assert entry['flags'] == {'duplicate_ordinal': 1}
    # equality alone would pass (V + 0 == V): completeness and the flag are what block it
    assert 'equality' not in entry['reconciled_by_rows_blocked_by']


def test_b2_partial_unique_index_rejects_direct_duplicate_ordinal(lab: Lab) -> None:
    sql = ('INSERT INTO v2_usage_codex_thread_proof (host,native_session_id,response_id,transcript_seq,thread_input,'
           'thread_cached_input,thread_cache_write_input,thread_output,thread_reasoning_output,source,observed_at) '
           'VALUES (?,?,?,?,?,?,?,?,?,?,?)')
    lab.conn.execute(sql, (HOST, 'n', 'r1', 1, 1, 0, 0, 0, 0, 'backfill', None))
    with pytest.raises(sqlite3.IntegrityError):
        lab.conn.execute(sql, (HOST, 'n', 'r2', 1, 1, 0, 0, 0, 0, 'backfill', None))
    lab.conn.execute(sql, (HOST, 'n', 'r3', None, 1, 0, 0, 0, 0, 'live', None))  # nulls never collide
    lab.conn.execute(sql, (HOST, 'n', 'r4', None, 1, 0, 0, 0, 0, 'live', None))
    lab.conn.execute(sql, (HOST, 'other', 'r1', 1, 1, 0, 0, 0, 0, 'backfill', None))  # per native session


@pytest.mark.parametrize('column', ['thread_input', 'thread_cached_input', 'thread_cache_write_input',
                                    'thread_output', 'thread_reasoning_output'])
@pytest.mark.parametrize('value', [None, -1])
def test_ac5_proof_columns_integer_not_null_nonnegative(lab: Lab, column: str, value) -> None:
    values = {c: 0 for c in ('thread_input', 'thread_cached_input', 'thread_cache_write_input',
                             'thread_output', 'thread_reasoning_output')}
    values[column] = value
    with pytest.raises(sqlite3.IntegrityError):
        lab.conn.execute('INSERT INTO v2_usage_codex_thread_proof (host,native_session_id,response_id,transcript_seq,'
                         f'{",".join(values)},source) VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (HOST, 'n', 'r1', 1, *values.values(), 'backfill'))
    info = {row[1]: row for row in lab.conn.execute('PRAGMA table_info(v2_usage_codex_thread_proof)')}
    assert info[column][2] == 'INTEGER' and info[column][3] == 1  # declared type, NOT NULL


# --- (c) counter reset hidden by the ledger's max-merge -----------------------------------------------------------

def test_c_counter_reset_flagged_and_blocked(lab: Lab, capsys) -> None:
    r = Rollout('n-c')
    r.respond('n-c-r1', vec(5000, 1000, 100, 10)).respond('n-c-r2', vec(4000, 1000, 100, 10))
    r.respond('n-c-r3', vec(1000, 200, 20, 5), reset=True).respond('n-c-r4', vec(1000, 200, 20, 5))
    lab.add(r)
    assert r.unverifiable_today()  # the max-merged ledger hides the reset; rows exceed it
    assert lab.backfill(capsys) == 0
    assert flags(lab.db, r.native) == [('counter_reset', 'n-c-r3')]
    entry = assert_blocked(lab, r, 'counter_consistency', 'flags')
    assert entry['flags'] == {'counter_reset': 1}


def test_c_counter_decrease_alone_blocks_counter_consistency(lab: Lab, capsys) -> None:
    """Clause (d) alone: a non-monotone stored counter with no producer flag and every other clause true."""
    r = lab.add(zero_last_rollout('n-c2'))
    assert lab.backfill(capsys) == 0
    assert_proven(lab, r)
    lab.conn.execute('UPDATE v2_usage_codex_thread_proof SET thread_output=? WHERE native_session_id=? '
                     'AND transcript_seq=1', (r.thread_at(2)['output'] + 1, 'n-c2'))  # still a valid vector
    lab.conn.commit()
    entry = assert_blocked(lab, r, 'counter_consistency')
    assert entry['reconciled_by_rows_blocked_by'] == ['counter_consistency']


def test_c_thread_counter_beyond_rows_blocks_equality_alone(lab: Lab, capsys) -> None:
    """Clause (c) alone: the thread counter counts a record the producer rejects (invalid usage), so the
    accepted rows never reach it although ordinals stay contiguous."""
    r = zero_last_rollout('n-c3')
    bad = vec(500, 0, 5)
    r.thread = plus(r.thread, bad)
    r.records.append({'type': 'token_usage_record', 'timestamp': r._ts(), 'payload': {
        'turn_id': 'turn-x', 'response_id': 'n-c3-bad', 'thread_id': 'n-c3', 'session_id': 'n-c3',
        'usage': {**usage(bad), 'output_tokens': -1}, 'thread_token_usage': usage(r.thread)}})
    r.respond('n-c3-r9', vec(100, 0, 1))
    lab.add(r)
    assert r.unverifiable_today()
    assert lab.backfill(capsys) == 0
    entry = assert_blocked(lab, r, 'equality')
    assert entry['reconciled_by_rows_blocked_by'] == ['equality']


# --- (d) foreign-thread copy and conflicting duplicate replay ----------------------------------------------------

def test_d_foreign_thread_response_flagged_and_blocked(lab: Lab, capsys) -> None:
    r = zero_last_rollout('n-d')
    r.respond('parent-r9', vec(777, 0, 7), thread_id='parent-thread')  # copied parent response
    lab.add(r)
    assert r.unverifiable_today()
    assert lab.backfill(capsys) == 0
    assert flags(lab.db, r.native) == [('foreign_thread_response', 'parent-r9')]
    with sqlite3.connect(lab.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM v2_usage_codex_responses WHERE response_id='parent-r9'").fetchone() == (0,)
    entry = assert_blocked(lab, r, 'flags')
    assert entry['reconciled_by_rows_blocked_by'] == ['flags']  # every other clause holds
    assert entry['flags'] == {'foreign_thread_response': 1}


def test_d_conflicting_duplicate_replay_persisted_and_blocked(lab: Lab, capsys) -> None:
    r = lab.add(zero_last_rollout('n-d2'))
    assert lab.backfill(capsys) == 0
    assert_proven(lab, r)
    item = next(i for i in file_items('codex', str(lab.path(r.native))) if i['kind'] == 'codex_response')
    item['data']['output'] += 1  # the same response id replayed with a different vector
    assert lab.admit([item])['counts'] == {'response_conflict': 1}
    assert lab.backfill(capsys) == 0  # re-running the backfill never erases the evidence
    assert flags(lab.db, r.native) == [('duplicate_response_conflict', item['data']['response_id'])]
    entry = assert_blocked(lab, r, 'flags')
    assert entry['reconciled_by_rows_blocked_by'] == ['flags']


# --- (e) live rows beyond the backfilled prefix -------------------------------------------------------------------

def test_e_live_tail_beyond_backfill_blocks_until_backfill_covers_it(lab: Lab, capsys) -> None:
    full = zero_last_rollout('n-e', k=4, skipped=(2,))
    head = Rollout('n-e')
    for rid, v, _thread in full.own[:3]:
        head.respond(rid, v, applied=rid != 'n-e-r2')
    lab.add(head)  # the collector stopped after ordinal 3; the ledger stays a lower bound of every prefix
    assert head.unverifiable_today() and full.unverifiable_today(head.ledger())
    live = native_provenance('codex', full.records[:7], native_session_id='n-e', proof=True, state={})
    assert [i['data']['transcript_seq'] for i in live if i['kind'] == 'codex_response'] == [None, None]
    lab.admit(live)  # live first: out of order with the backfill
    assert [s for _r, _seq, s in proof_rows(lab.db, 'n-e')] == ['live', 'live']
    assert lab.backfill(capsys) == 0  # backfill of the 3-response file upgrades both live rows
    assert proof_rows(lab.db, 'n-e') == [('n-e-r1', 1, 'backfill'), ('n-e-r2', 2, 'backfill'),
                                         ('n-e-r3', 3, 'backfill')]
    assert_proven(lab, head)
    state: dict = {}
    native_provenance('codex', full.records[:-3], native_session_id='n-e', proof=True, state=state)
    tail = native_provenance('codex', full.records[-3:], native_session_id='n-e', proof=True, state=state)
    lab.admit(tail)  # the live tail now holds ordinal 4's response without an ordinal
    assert ('n-e-r4', None, 'live') in proof_rows(lab.db, 'n-e')
    assert_blocked(lab, full, 'completeness')
    lab.write(full)
    assert lab.backfill(capsys) == 0
    assert_proven(lab, full)


# --- (f) compatibility control: version-1 producer, no proof rows, byte-identical output ------------------------

GOLDEN = Path(__file__).resolve().parent / 'fixtures' / 'usage_codex_v1_compat_rollup.json'


def _strip_v2(items: list[dict]) -> list[dict]:
    out = []
    for item in items:
        if item['kind'] == 'codex_thread_flag':
            continue
        data = {k: v for k, v in item['data'].items() if k not in ('thread_token_usage', 'transcript_seq')}
        out.append({**item, 'data': data})
    return out


def test_f_compatibility_control_v1_byte_identical(lab: Lab) -> None:
    """Passing control, not a RED: the same output at ba1c61eb (where the golden was written) and here."""
    sessions = [zero_last_rollout('n-f1'), Rollout('n-f2').respond('n-f2-r1', vec(100, 50, 5)),
                Rollout('n-f3').respond('n-f3-r1', vec(100, 50, 5), applied=False)]
    sessions[2].respond('n-f3-r2', vec(30, 0, 3))
    for r in sessions:
        lab.add(r)
        ack = lab.admit(_strip_v2(file_items('codex', str(lab.path(r.native)))), version=1)
        assert not ack.get('error') and 'bad_provenance' not in ack['counts']
    with sqlite3.connect(lab.db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'v2_usage_codex_thread_proof' in tables:
            assert conn.execute('SELECT COUNT(*) FROM v2_usage_codex_thread_proof').fetchone() == (0,)
            assert conn.execute('SELECT COUNT(*) FROM v2_usage_codex_thread_flags').fetchone() == (0,)
    result = lab.run('--spec', SPEC)
    rendered = json.dumps(result, indent=2, sort_keys=True) + '\n'
    if os.environ.get('PENTACLE_WRITE_CODEX_V1_GOLDEN') == '1':  # run once at ba1c61eb only
        GOLDEN.write_text(rendered)
    assert rendered == GOLDEN.read_text()
    assert 'proof_sessions' not in result['specs'][0]['codex']
    assert 'reconciled_by_rows' not in result['specs'][0]['codex']['reconciliation']


# --- (f2) cold producer, one file in one batch, version-2 daemon ---------------------------------------------------

class _Loopback:
    """Satellite frames straight into a real EventPush (the daemon's event.push handler)."""

    def __init__(self, ep) -> None:
        self.ep = ep
        self.frames: list[dict] = []
        self._replies: list[str] = []

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        self.frames.append(frame)
        self._replies.append(json.dumps(await self.ep.handle_push(frame)))

    async def recv(self) -> str:
        return self._replies.pop(0)


class _V1Sink:
    """The pinned ba1c61eb admission contract: version 1 only (usage_provenance.py:144-145 at that SHA)."""

    def __init__(self, inner: ProvenanceSink) -> None:
        self.inner = inner

    async def admit(self, host, payload):
        if isinstance(payload, dict) and payload.get('version') != 1:
            return {'version': 1, 'error': 'unsupported_version'}
        return await self.inner.admit(host, payload)


def _satellite_backfill(lab: Lab, monkeypatch, *, v1_daemon: bool) -> tuple[dict, list[dict]]:
    import satellite
    from event_push import EventPush

    class _Alerts:
        def emit(self, *_a, **_k):
            return None

    async def secret() -> str:
        return 'secret'

    async def run() -> tuple[dict, list[dict]]:
        store = Store(str(lab.db))
        store.start()
        try:
            sink = ProvenanceSink(store.record_provenance, HistoryLog(lab.data / 'usage_history.jsonl'))
            ep = EventPush(store, lambda _f: asyncio.sleep(0), _Alerts(), recent_limit=20,
                           host_secrets={SAT_HOST: SAT_SECRET}, provenance=_V1Sink(sink) if v1_daemon else sink)
            ep._secret = secret
            ws = _Loopback(ep)

            class Connect:
                def __init__(self, *_a, **_k):
                    pass

                async def __aenter__(self):
                    return ws

                async def __aexit__(self, *_exc):
                    return False

            monkeypatch.setattr(satellite.websockets, 'connect', Connect)
            sat = satellite.Satellite(satellite.SatelliteConfig(host=SAT_HOST, checkout=str(lab.tmp),
                                                                host_secret=SAT_SECRET, push_secret='secret'))
            sat.sha = 'a' * 40
            args = satellite._parse_args(['--backfill', '--provider', 'codex', '--codex-root', str(lab.root),
                                          '--cursor', str(lab.tmp / 'cursor.json')])
            summary = await satellite._backfill(sat, args)
            return summary, ws.frames
        finally:
            store.stop()

    return asyncio.run(run())


def test_f2_cold_producer_single_batch_carries_ordinal_one(lab: Lab, monkeypatch) -> None:
    r = lab.add(zero_last_rollout('n-f2c'), host=SAT_HOST)
    summary, frames = _satellite_backfill(lab, monkeypatch, v1_daemon=False)
    assert frames[0]['usage_provenance'] == {'version': 2, 'items': []}  # the empty probe precedes data
    assert len(frames) == 2 and frames[1]['usage_provenance']['version'] == 2  # one data batch
    seqs = [i['data'].get('transcript_seq') for i in frames[1]['usage_provenance']['items']
            if i['kind'] == 'codex_response']
    assert seqs == list(range(1, r.n + 1))
    assert proof_rows(lab.db, r.native)[0] == (r.own[0][0], 1, 'backfill')
    assert summary['providers']['codex']['batches'] == 1
    cursor = json.loads((lab.tmp / 'cursor.json').read_text())
    assert [entry[2] for entry in cursor['done'].values()] == [2]
    assert_proven(lab, r)  # P can pass after the single pass


# --- (f3) version-2 producer against a version-1 daemon --------------------------------------------------------

def test_f3_downgrade_then_requeue_once_after_first_v2_ack(lab: Lab, monkeypatch) -> None:
    r = lab.add(zero_last_rollout('n-f3'), host=SAT_HOST)
    old, frames = _satellite_backfill(lab, monkeypatch, v1_daemon=True)
    assert frames[0]['usage_provenance'] == {'version': 2, 'items': []}
    assert all(f['usage_provenance']['version'] == 1 for f in frames[1:])  # downgraded, fields stripped
    assert not any('transcript_seq' in i['data'] for f in frames[1:] for i in f['usage_provenance']['items'])
    assert old['providers']['codex'].get('batches_failed', 0) == 0
    assert old['providers']['codex']['recorded'] == r.n
    assert proof_rows(lab.db, r.native) == []
    cursor = json.loads((lab.tmp / 'cursor.json').read_text())
    assert [entry[2] for entry in cursor['done'].values()] == [1]
    new, frames = _satellite_backfill(lab, monkeypatch, v1_daemon=False)  # daemon upgraded
    assert new['providers']['codex'].get('files_skipped_cursor', 0) == 0  # re-queued for a version-2 pass
    assert new['providers']['codex']['replayed'] == r.n  # responses replay, proof rows insert
    assert [seq for _r, seq, _s in proof_rows(lab.db, r.native)] == list(range(1, r.n + 1))
    again, frames = _satellite_backfill(lab, monkeypatch, v1_daemon=False)
    assert again['providers']['codex']['files_skipped_cursor'] == 1  # once only
    assert len(frames) == 1  # just the probe


def test_f3_live_tail_downgrades_and_reprobes_hourly(tmp_path: Path, monkeypatch) -> None:
    import satellite

    sat = satellite.Satellite(satellite.SatelliteConfig(host=SAT_HOST, checkout=str(tmp_path)))
    r = zero_last_rollout('n-live')
    sat._queue_provenance(native_provenance('codex', r.records, native_session_id='n-live', proof=True, state={}))
    clock = [1000.0]
    monkeypatch.setattr(satellite.time, 'monotonic', lambda: clock[0])
    probe: dict = {}
    sat._attach_provenance(probe)
    assert probe['usage_provenance'] == {'version': 2, 'items': []}
    sat._apply_ack({'type': 'event.push.ok', 'usage_provenance': {'version': 1, 'error': 'unsupported_version'}}, {})
    data: dict = {}
    sat._attach_provenance(data)
    assert data['usage_provenance']['version'] == 1
    assert all(set(i['data']) == {'response_id', 'observed_at', 'model', *FIELDS5}
               for i in data['usage_provenance']['items'] if i['kind'] == 'codex_response')
    sat._apply_ack({'type': 'event.push.ok', 'usage_provenance': {'version': 1, 'counts': {}}}, {})
    clock[0] += 3599
    again: dict = {}
    sat._queue_provenance(native_provenance('codex', r.records, native_session_id='n-live', proof=True, state={}))
    sat._attach_provenance(again)
    assert again['usage_provenance']['version'] == 1
    sat._apply_ack({'type': 'event.push.ok', 'usage_provenance': {'version': 1, 'counts': {}}}, {})
    clock[0] += 2
    sat._queue_provenance(native_provenance('codex', r.records, native_session_id='n-live', proof=True, state={}))
    reprobe: dict = {}
    sat._attach_provenance(reprobe)
    assert reprobe['usage_provenance'] == {'version': 2, 'items': []}  # hourly re-probe
    sat._apply_ack({'type': 'event.push.ok', 'usage_provenance': {'version': 2, 'counts': {}}}, {})
    upgraded: dict = {}
    sat._queue_provenance(native_provenance('codex', r.records, native_session_id='n-live', proof=True, state={}))
    sat._attach_provenance(upgraded)
    assert upgraded['usage_provenance']['version'] == 2


# --- (g) ledger above the thread vector ----------------------------------------------------------------------------

def test_g_ledger_above_thread_blocked(lab: Lab, capsys) -> None:
    r = zero_last_rollout('n-g')
    ledger = r.ledger()
    ledger['output'] = r.thread_at(r.n)['output'] + 1  # the ledger claims more output than the thread counter
    lab.add(r, ledger=ledger)
    assert r.unverifiable_today(ledger)
    assert lab.backfill(capsys) == 0
    entry = assert_blocked(lab, r, 'ledger_le_thread')
    assert entry['reconciled_by_rows_blocked_by'] == ['ledger_le_thread']


# --- (h) missing-head sessions ---------------------------------------------------------------------------------------

@pytest.mark.parametrize('missing', [1, 2])
def test_h_missing_head_blocked_completeness(lab: Lab, capsys, missing: int) -> None:
    r = lab.add(zero_last_rollout('n-h', k=5, skipped=(4,)))
    assert lab.backfill(capsys) == 0
    head = [rid for rid, _v, _t in r.own[:missing]]
    for table in ('v2_usage_codex_responses', 'v2_usage_codex_thread_proof'):
        lab.conn.executemany(f'DELETE FROM {table} WHERE response_id=?', [(rid,) for rid in head])
    lab.conn.commit()
    assert lab.codex()['reconciliation']['unverifiable']['sessions'] == 1  # rows(3..5) still exceed the ledger
    assert_blocked(lab, r, 'completeness')


# --- (i) identity conflict -------------------------------------------------------------------------------------------

def test_i_identity_conflict_blocked(lab: Lab, capsys) -> None:
    r = Rollout('n-i', accounts=(ACCT, ACCT2))
    for i in range(1, 4):
        r.respond(f'n-i-r{i}', vec(1000 * i, 100, 10), applied=i != 2)
    lab.add(r)
    assert lab.backfill(capsys) == 0
    with sqlite3.connect(lab.db) as conn:
        assert conn.execute('SELECT conflict FROM v2_usage_identity WHERE native_session_id=?', ('n-i',)).fetchone() == (1,)
    entry = assert_blocked(lab, r, 'identity')
    assert entry['reconciled_by_rows_blocked_by'] == ['identity']


# --- (j) torn read: one deferred read-only snapshot -------------------------------------------------------------

def test_j_torn_read_sees_one_consistent_prefix(lab: Lab, capsys, monkeypatch) -> None:
    full = zero_last_rollout('n-j', k=4, skipped=(2,))
    head = Rollout('n-j')
    for rid, v, _thread in full.own[:3]:
        head.respond(rid, v, applied=rid != 'n-j-r2')
    lab.add(head)
    assert lab.backfill(capsys) == 0
    old = assert_proven(lab, head)
    batch = [i for i in file_items('codex', str(_write_tmp(lab, full))) if i['kind'] == 'codex_response'][3:]
    commits: list[str] = []
    real_ro = ur._ro

    class Racing:
        """The rollup's read connection; a backfill batch commits right after the response rows are read."""

        def __init__(self, path):
            self.conn = real_ro(path)

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, sql, *args):
            cursor = self.conn.execute(sql, *args)
            if 'v2_usage_codex_responses' in sql and not commits and 'sessions.db' in str(self.conn_path):
                rows = _Rows(cursor.fetchall())
                writer = threading.Thread(target=lambda: commits.append(json.dumps(admit(lab.db, batch))))
                writer.start()
                writer.join()
                return rows
            return cursor

    def racing_ro(path):
        wrapper = Racing(path)
        wrapper.conn_path = path
        return wrapper

    monkeypatch.setattr(ur, '_ro', racing_ro)
    entry = lab.session('n-j')
    assert commits and json.loads(commits[0])['counts'] == {'recorded': 1}  # the batch did commit mid-read
    assert entry == old  # the old consistent prefix, never a mix (proof row 4 without its response row)
    monkeypatch.setattr(ur, '_ro', real_ro)
    assert_proven(lab, full)  # the next read sees the new consistent prefix


class _Rows(list):
    def fetchall(self) -> list:
        return list(self)


def _write_tmp(lab: Lab, r: Rollout) -> Path:
    path = lab.tmp / 'scratch' / f'rollout-{r.native}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(r.text())
    return path


# --- (k) out-of-band malformed stored proof (advisor ruling dc8306cc, adopted verbatim) ------------------------

def _db_digest(data: Path) -> str:
    digest = hashlib.sha256()
    for name in ('sessions.db', 'sessions.db-wal'):  # an empty WAL holds no frames: same as none
        path = data / name
        if path.exists() and path.stat().st_size:
            digest.update(name.encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()


def test_k_out_of_band_malformed_stored_proof_blocked_without_writes(lab: Lab, capsys) -> None:
    r = Rollout('n-k')
    r.respond('n-k-r1', vec(100, 0, 10)).respond('n-k-r2', vec(200, 160, 10), applied=False)
    lab.add(r)
    assert r.unverifiable_today()
    assert lab.backfill(capsys) == 0
    assert_proven(lab, r)  # a valid proven-session fixture first
    # Disposable-database setup write: ordinal 1's stored vector violates cached_input <= input while every
    # field stays non-negative (the CHECKs admit it) and the vectors stay non-decreasing, so without the
    # stored-proof validator every clause of P would still hold.
    lab.conn.execute('UPDATE v2_usage_codex_thread_proof SET thread_cached_input=150 '
                     'WHERE native_session_id=? AND transcript_seq=1', ('n-k',))
    lab.conn.commit()
    lab.conn.close()
    before = _db_digest(lab.data)
    with sqlite3.connect(lab.db) as conn:
        dump_before = list(conn.iterdump())
    lab.conn = sqlite3.connect(lab.db)
    entry = lab.session('n-k')
    assert entry['class'] == 'unverifiable'
    assert entry['reconciled_by_rows_blocked_by'] == ['malformed_proof']
    assert entry['malformed_proof_rows'] == 1 and 'flags' not in entry
    lab.conn.close()
    assert _db_digest(lab.data) == before
    with sqlite3.connect(lab.db) as conn:
        assert list(conn.iterdump()) == dump_before
    lab.conn = sqlite3.connect(lab.db)


def test_k_stored_flag_named_like_derived_clause_still_blocks(lab: Lab, capsys) -> None:
    """QA 3d385876: a stored flag blocks P whatever its name, including one spelled 'malformed_proof'."""
    r = lab.add(zero_last_rollout('n-k2'))
    assert lab.backfill(capsys) == 0
    assert_proven(lab, r)
    lab.conn.execute('INSERT INTO v2_usage_codex_thread_flags (host,native_session_id,flag,response_id,detail,'
                     'first_seen_at) VALUES (?,?,?,?,?,?)',
                     (HOST, r.native, 'malformed_proof', 'unexpected', 'stored row', '2026-10-07T00:00:00Z'))
    lab.conn.commit()
    entry = assert_blocked(lab, r, 'flags')
    assert entry['reconciled_by_rows_blocked_by'] == ['flags']
    assert entry['flags'] == {'malformed_proof': 1} and 'malformed_proof_rows' not in entry


# --- AC2 merge rules: no erasure, adverse evidence retained ----------------------------------------------------------

def test_ac2_merge_no_erasure_proof_conflict_retained(lab: Lab, capsys) -> None:
    r = lab.add(zero_last_rollout('n-m'))
    assert lab.backfill(capsys) == 0
    items = [i for i in file_items('codex', str(lab.path(r.native))) if i['kind'] == 'codex_response']
    replay = lab.admit(items)
    assert replay['counts'] == {'replayed': r.n} and replay['proof_counts'] == {'replayed': r.n}
    bad_vector = json.loads(json.dumps(items[1]))
    bad_vector['data']['thread_token_usage']['output'] += 1
    bad_seq = json.loads(json.dumps(items[2]))
    bad_seq['data']['transcript_seq'] = 9
    live_after = json.loads(json.dumps(items[0]))
    live_after['data']['transcript_seq'] = None  # a live replay never erases a backfill ordinal
    ack = lab.admit([bad_vector, bad_seq, live_after])
    assert ack['proof_counts'] == {'proof_conflict': 2, 'replayed': 1}
    assert proof_rows(lab.db, r.native) == [(rid, i, 'backfill') for i, (rid, _v, _t) in enumerate(r.own, 1)]
    for _ in range(2):
        assert lab.backfill(capsys) == 0
    assert flags(lab.db, r.native) == sorted([('proof_conflict', items[1]['data']['response_id']),
                                              ('proof_conflict', items[2]['data']['response_id'])])
    assert_blocked(lab, r, 'flags')


def test_ac4_ledger_tables_byte_identical_across_backfill(lab: Lab, capsys) -> None:
    lab.add(zero_last_rollout('n-l1'))
    lab.add(zero_last_rollout('n-l2', k=3, skipped=(1,)))
    before = ledger_dump(lab.db)
    assert lab.backfill(capsys) == 0
    assert lab.backfill(capsys) == 0
    assert ledger_dump(lab.db) == before


# --- AC5 wire validation ----------------------------------------------------------------------------------------------

def _v2_item() -> dict:
    r = zero_last_rollout('n-v', k=1, skipped=())
    return json.loads(json.dumps(next(i for i in native_provenance('codex', r.records, complete=True, proof=True)
                                      if i['kind'] == 'codex_response')))


@pytest.mark.parametrize('mutate', [
    lambda d: d['thread_token_usage'].update(input=-1),
    lambda d: d['thread_token_usage'].update(cached_input=10 ** 9),
    lambda d: d['thread_token_usage'].update(reasoning_output=10 ** 9),
    lambda d: d['thread_token_usage'].pop('output'),
    lambda d: d['thread_token_usage'].update(extra=1),
    lambda d: d['thread_token_usage'].update(input=True),
    lambda d: d.update(transcript_seq=0),
    lambda d: d.update(transcript_seq='1'),
    lambda d: d.update(thread_token_usage=None),
    lambda d: d.pop('transcript_seq'),
])
def test_ac5_malformed_optional_fields_bad_provenance(mutate) -> None:
    item = _v2_item()
    assert validate_item(item, 2) is None
    mutate(item['data'])
    assert validate_item(item, 2) == 'bad_provenance'


def test_ac5_versions_accepted_and_v1_items_unchanged(lab: Lab) -> None:
    item = _v2_item()
    assert validate_item(item, 1) == 'bad_provenance'  # version-1 items never carry the fields
    flag = {'kind': 'codex_thread_flag', 'provider': 'codex', 'native_session_id': 'n-v',
            'source_file_identity_digest': None, 'identity': None,
            'data': {'flag': 'counter_reset', 'response_id': 'r', 'detail': None}}
    assert validate_item(flag, 2) is None and validate_item(flag, 1) == 'unsupported_kind'
    assert validate_item({**flag, 'data': {**flag['data'], 'flag': 'proof_conflict'}}, 2) == 'bad_provenance'
    probe = lab.admit([], version=2)
    assert probe['version'] == 2 and not probe.get('error')
    assert lab.admit([], version=1)['version'] == 2
    assert lab.admit([], version=3) == {'version': 2, 'error': 'unsupported_version'}
