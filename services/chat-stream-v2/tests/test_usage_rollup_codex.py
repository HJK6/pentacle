"""Codex rollup and calibration (spec_pentacle__usage_codex_rollup_and_calibration_2026_10).

Synthetic ledgers on the real Store schema; every oracle is stated here, never taken from live totals.
Test names carry the AC they prove.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import usage_rollup as ur
from test_usage_rollup import Fx

ACCT = '00000000-0000-4000-8000-0000000000c1'
ACCT2 = '00000000-0000-4000-8000-0000000000c2'
SOL, TERRA, LUNA = 'gpt-6-sol', 'gpt-5.6-terra', 'gpt-6-luna'
T0 = 1_790_812_800  # 2026-10-01T00:00:00Z
RESET = '2026-10-08T00:00:00Z'


def at(seconds: float) -> str:
    return ur.iso(T0 + seconds)


class Cx(Fx):
    """Fx plus Codex sessions: a cumulative ledger row (or none) and per-response rows."""

    def __init__(self, tmp: Path):
        super().__init__(tmp)
        self.rid = 0

    def session(self, native: str, rows: list[tuple], *, stream: str | None = 'thoth:cx', host: str = 'thoth',
                account: str | None = ACCT, cumulative: str | dict | None = 'match', conflict: int = 0) -> None:
        """rows: (observed|None, model, input, cached_input, output[, reasoning]); cumulative 'match' = Σ rows."""
        total = {'input_total': 0, 'cached_input': 0, 'output': 0, 'reasoning': 0}
        for observed, model, inp, cached, output, *rest in rows:
            self.rid += 1
            reasoning = rest[0] if rest else 0
            self.conn.execute('INSERT INTO v2_usage_codex_responses VALUES (?,?,?,?,?,?,?,?,?,?)',
                              (host, native, f'resp_{self.rid}', observed, model, inp, cached, 0, output, reasoning))
            for key, value in (('input_total', inp), ('cached_input', cached), ('output', output),
                               ('reasoning', reasoning)):
                total[key] += value
        if cumulative is not None:
            self.rec(stream, total if cumulative == 'match' else cumulative, native=native, host=host,
                     provider='codex', row=False)
        if account is not None or conflict:
            self.ident(native, account, host=host, provider='codex', conflict=conflict)
        else:
            self.ident(native, None, host=host, provider='codex')

    def chist(self, observed: str, pct: int, *, account: str | None = ACCT, kind: str = 'codex',
              minutes: int = 10080, resets: str = RESET, source: str = 'rollout') -> None:
        self.history.append({'observed_at': observed, 'probed_at': observed, 'host': 'thoth', 'provider': 'codex',
                             'account_id': account, 'window_kind': kind, 'window_minutes': minutes, 'pct': pct,
                             'resets_at': resets, 'source': source})


@pytest.fixture
def cx(tmp_path: Path) -> Cx:
    return Cx(tmp_path)


def u(n: int) -> dict:
    """Cumulative row of n uncached input tokens."""
    return {'input_total': n, 'cached_input': 0, 'output': 0, 'reasoning': 0}


def codex_entry(result: dict, account: str = ACCT) -> dict:
    return next(e for e in result['calibration']['codex']['entries'] if e['account_id'] == account)


# --- AC1 per-session reconciliation -------------------------------------------------------------

AC1_CASES = {
    # name: (rows, cumulative, class, placed, untimed, unreconciled, unverifiable, completeness)
    'reconciled': ([(at(60), SOL, 60, 0, 0), (at(120), SOL, 40, 0, 0)], u(100), 'reconciled', 100, 0, 0, 0, 1.0),
    'partial': ([(at(60), SOL, 60, 0, 0)], u(100), 'partial', 60, 0, 40, 0, 0.6),
    'partial_zero_responses': ([], u(100), 'partial', 0, 0, 100, 0, 0.0),
    'unverifiable_overage': ([(at(60), SOL, 60, 0, 0), (at(120), SOL, 60, 0, 0)], u(100), 'unverifiable',
                             0, 0, 0, 120, 0.0),
    'no_cumulative_row': ([(at(60), SOL, 50, 0, 0)], None, 'unverifiable', 0, 0, 0, 50, 0.0),
    'reconciled_with_null_time': ([(at(60), SOL, 70, 0, 0), (None, SOL, 30, 0, 0)], u(100), 'reconciled',
                                  70, 30, 0, 0, 0.7),
    'no_cumulative_with_null_time': ([(at(60), SOL, 40, 0, 0), (None, SOL, 10, 0, 0)], None, 'unverifiable',
                                     0, 0, 0, 50, 0.0),
    'null_time_pushes_over': ([(at(60), SOL, 80, 0, 0), (None, SOL, 30, 0, 0)], u(100), 'unverifiable',
                              0, 0, 0, 110, 0.0),
    'null_time_exact': ([(at(60), SOL, 80, 0, 0), (None, SOL, 20, 0, 0)], u(100), 'reconciled', 80, 20, 0, 0, 0.8),
}


@pytest.mark.parametrize('name', sorted(AC1_CASES))
def test_usage_codex_ac1_reconciliation_classes_exact(cx: Cx, name: str) -> None:
    rows, cumulative, cls, placed, untimed, unreconciled, unverifiable, completeness = AC1_CASES[name]
    cx.seat('thoth:cx', created=at(0), specs=('spec_demo__cx',), provider='codex')
    cx.session('n1', rows, cumulative=cumulative)
    result = cx.run('--calibrate', '--spec', 'spec_demo__cx')
    codex = result['calibration']['codex']
    assert {c: codex['reconciliation'][c]['sessions'] for c in ur.RECON_CLASSES} == {
        c: int(c == cls) for c in ur.RECON_CLASSES}  # each session in exactly one class
    e = codex_entry(result)
    assert e['reconciliation_masses'] == {'placed': placed, 'untimed': untimed, 'unreconciled': unreconciled,
                                          'unverifiable': unverifiable}
    assert e['completeness'] == completeness
    gate = codex['unplaceable']
    assert gate['unplaceable_tokens'] == untimed + unreconciled + unverifiable
    assert gate['provider_total_tokens'] == placed + untimed + unreconciled + unverifiable
    assert gate['by_class'] == {'unverifiable': unverifiable, 'unreconciled': unreconciled, 'untimed': untimed}
    if cls != 'unverifiable':  # placed + untimed + unreconciled == C_n exactly
        assert placed + untimed + unreconciled == sum(ur.codex_ledger_buckets(cumulative).values())
    else:  # unverifiable mass appears nowhere else
        assert placed == untimed == unreconciled == 0
    spec = result['specs'][0]['codex']
    if cumulative is not None:  # a session without a cumulative row has no stream to attribute it to
        assert spec['placed_tokens'] == placed and spec['completeness'] == completeness
        assert spec['partition_tokens']['unverifiable'] == unverifiable
        assert spec['partition_tokens']['untimed'] == untimed
        assert spec['partition_tokens']['unreconciled'] == unreconciled


def test_usage_codex_ac1_per_bucket_and_double_count_invariant(cx: Cx) -> None:
    cx.seat('thoth:cx', created=at(0), specs=('spec_demo__cx',), provider='codex')
    # C: uncached 200, cache_read 800, output 50; responses: uncached 100, cache_read 500, output 30 (reasoning 10)
    cx.session('n1', [(at(60), SOL, 600, 500, 30, 10)],
               cumulative={'input_total': 1000, 'cached_input': 800, 'output': 50, 'reasoning': 20})
    spec = cx.run('--spec', 'spec_demo__cx')['specs'][0]['codex']
    assert spec['tokens'] == {'uncached_input': 100, 'cache_read': 500, 'cache_write': 0, 'output': 30}
    assert spec['outside_window_tokens']['unreconciled'] == {'uncached_input': 100, 'cache_read': 300,
                                                             'cache_write': 0, 'output': 20}
    assert spec['reasoning_output'] == 10
    placed_plus_residual = spec['placed_tokens'] + spec['partition_tokens']['unreconciled']
    assert placed_plus_residual == 1050  # == C_n; placing the responses AND the full row would give 1680


def test_usage_codex_ac1_mixed_sessions_each_counted_once(cx: Cx) -> None:
    cx.seat('thoth:cx', created=at(0), specs=('spec_demo__cx',), provider='codex')
    for i, name in enumerate(sorted(AC1_CASES)):
        rows, cumulative = AC1_CASES[name][:2]
        cx.session(f'n{i}', rows, cumulative=cumulative)
    codex = cx.run('--calibrate')['calibration']['codex']
    counts = {c: codex['reconciliation'][c]['sessions'] for c in ur.RECON_CLASSES}
    assert counts == {'reconciled': 3, 'partial': 2, 'unverifiable': 4}
    expected = {k: sum(case[3 + i] for case in AC1_CASES.values())
                for i, k in enumerate(('placed', 'untimed', 'unreconciled', 'unverifiable'))}
    assert codex_entry({'calibration': {'codex': codex}})['reconciliation_masses'] == expected
    assert codex['unplaceable']['provider_total_tokens'] == sum(expected.values())


# --- AC2 account per session ---------------------------------------------------------------------

def test_usage_codex_ac2_accounts_never_pooled(cx: Cx) -> None:
    cx.seat('thoth:cx', created=at(0), specs=('spec_demo__cx',), provider='codex')
    cx.session('a', [(at(60), SOL, 100, 0, 0)])
    cx.session('b', [(at(60), SOL, 200, 0, 0)], account=ACCT2)
    cx.session('u', [(at(60), SOL, 400, 0, 0)], account=None)
    cx.session('c', [(at(60), SOL, 800, 0, 0)], account=None, conflict=1)
    result = cx.run('--calibrate', '--spec', 'spec_demo__cx')
    spec = result['specs'][0]['codex']
    assert {a['account_id']: a['tokens'] for a in spec['by_account']} == {
        ACCT: 100, ACCT2: 200, 'unknown': 400, 'conflict': 800}
    assert spec['partition_tokens']['measured'] == 300 and spec['partition_tokens']['unknown_account'] == 1200
    entries = result['calibration']['codex']['entries']
    assert [e['account_id'] for e in entries] == [ACCT, ACCT2]
    assert all(e['basis'] == 'creator_account' for e in entries)
    # unknown/conflict mass sits in every account's denominator; the other account's mass in neither
    assert codex_entry(result)['measured_rollup']['tokens'] == {'measured': 100, 'unpriced': 0,
                                                                'unknown_account': 1200}
    host = codex_entry(result)['identity_mass_by_host'][0]['window']
    assert host == {'measured': 100, 'unpriced': 0, 'unknown_account': 1200, 'conflict': 800}


def _alias_config(cx: Cx, justification: str | None) -> None:
    path = cx.config(accounts=[])
    data = json.loads(path.read_text())
    data['account_aliases'] = [{'provider': 'codex', 'account_id': ACCT2, 'alias_of': ACCT,
                                'justification': justification}]
    path.write_text(json.dumps(data))


def test_usage_codex_ac2_alias_without_justification_rejected(cx: Cx, capsys: pytest.CaptureFixture) -> None:
    cx.seat('thoth:cx', created=at(0), specs=('spec_demo__cx',), provider='codex')
    cx.session('a', [(at(60), SOL, 100, 0, 0)])
    cx.session('b', [(at(60), SOL, 200, 0, 0)], account=ACCT2)
    _alias_config(cx, None)
    codex = cx.run('--calibrate')['calibration']['codex']
    assert 'account_aliases entry rejected: alias requires a justification' in capsys.readouterr().err
    assert codex['account_aliases'][0]['status'] == 'rejected'
    assert [e['account_id'] for e in codex['entries']] == [ACCT, ACCT2]


def test_usage_codex_ac2_alias_with_justification_applied(cx: Cx) -> None:
    cx.seat('thoth:cx', created=at(0), specs=('spec_demo__cx',), provider='codex')
    cx.session('a', [(at(60), SOL, 100, 0, 0)])
    cx.session('b', [(at(60), SOL, 200, 0, 0)], account=ACCT2)
    _alias_config(cx, 'same login re-provisioned (synthetic)')
    result = cx.run('--calibrate', '--redact')
    codex = result['calibration']['codex']
    assert codex['account_aliases'][0]['status'] == 'applied'
    assert len(codex['entries']) == 1 and codex['entries'][0]['basis'] == 'aliased'
    assert codex['entries'][0]['measured_rollup']['tokens']['measured'] == 300
    assert ACCT not in json.dumps(result) and ACCT2 not in json.dumps(result)  # --redact covers alias ids


# --- AC3 pricing ---------------------------------------------------------------------------------

LISTED = {'gpt-6-sol': 40.0, 'gpt-6-luna': 10.0, 'gpt-6-astra': 40.0, 'gpt-6.1-sol': 40.0, 'gpt-5.6-luna': 10.0,
          'gpt-5.6-terra': 2.0, 'codex-auto-review': 10.0, 'gpt-reserve': 40.0}  # $ per 1M output tokens


def test_usage_codex_ac3_pricing_per_bucket_reasoning_subset_unlisted_unpriced(cx: Cx) -> None:
    cx.seat('thoth:cx', created=at(0), specs=('spec_demo__cx',), provider='codex')
    rows = [(at(60 + i), model, 0, 0, 1_000_000, 400_000) for i, model in enumerate(sorted(LISTED))]
    rows.append((at(10), SOL, 2_000_000, 1_000_000, 0))  # 1M uncached $5 + 1M cache_read $0.50
    rows.append((at(11), 'gpt-9-unlisted', 0, 0, 777))
    cx.session('n1', rows)
    spec = cx.run('--spec', 'spec_demo__cx')['specs'][0]['codex']
    groups = {g['model']: g for g in spec['by_model_account']}
    for model, usd in LISTED.items():
        expected = usd + (5.5 if model == SOL else 0.0)
        assert groups[model]['dollars'] == pytest.approx(expected), model  # reasoning never priced twice
        assert groups[model]['reasoning_output'] <= groups[model]['tokens']['output']
    assert groups['gpt-9-unlisted']['dollars'] is None and groups['gpt-9-unlisted']['bucket'] == 'unpriced'
    assert spec['unpriced_tokens'] == 777 and spec['partition_tokens']['unpriced'] == 777
    assert spec['dollars'] == pytest.approx(sum(LISTED.values()) + 5.5)


def test_usage_codex_ac3_unreconciled_and_unverifiable_never_priced(cx: Cx) -> None:
    cx.seat('thoth:cx', created=at(0), specs=('spec_demo__cx',), provider='codex')
    cx.session('p', [(at(60), SOL, 0, 0, 1_000_000)],
               cumulative={'input_total': 0, 'cached_input': 0, 'output': 3_000_000, 'reasoning': 0})
    cx.session('v', [(at(60), SOL, 0, 0, 2_000_000)],
               cumulative={'input_total': 0, 'cached_input': 0, 'output': 1_000_000, 'reasoning': 0})
    spec = cx.run('--spec', 'spec_demo__cx')['specs'][0]['codex']
    assert spec['dollars'] == pytest.approx(40.0)  # only the placed 1M of session p


# --- AC4 rollup rows --------------------------------------------------------------------------------

def test_usage_codex_ac4_mobile_spec_rows_and_no_deferred(cx: Cx) -> None:
    S = 'spec_pentacle_mobile__bart_first_home_2026_10'
    cx.seat('thoth:m1', created=at(0), specs=(S,), provider='codex')
    cx.seat('amaterasu:m2', created=at(0), specs=(S,), provider='codex')
    cx.session('m1', [(at(60), SOL, 1_000_000, 0, 0), (at(300), LUNA, 0, 0, 1_000_000)], stream='thoth:m1')
    cx.session('m2', [(at(60), SOL, 1_000_000, 0, 0)], stream='amaterasu:m2', host='amaterasu', cumulative=u(3_000_000))
    cx.item(S, status='in_progress', completed_at=None, epic='epic_demo')
    result = cx.run('--spec', S, '--project', 'epic_demo')
    codex = result['specs'][0]['codex']
    assert codex['dollars'] == pytest.approx(5.0 + 10.0 + 5.0)
    assert codex['tokens']['uncached_input'] == 2_000_000 and codex['tokens']['output'] == 1_000_000
    assert codex['reconciliation']['reconciled']['sessions'] == 1 and codex['reconciliation']['partial']['sessions'] == 1
    assert codex['completeness'] == pytest.approx(3 / 5)
    assert {r['stream_id']: r['completeness'] for r in codex['by_stream']} == {'amaterasu:m2': pytest.approx(1 / 3),
                                                                               'thoth:m1': 1.0}
    assert result['project']['codex']['placed_tokens'] == 3_000_000
    assert 'deferred' not in json.dumps(result)
    assert result['specs'][0]['time']['codex_activity_proxy_h']['reason'] == 'Codex mass outside placed responses'


# --- AC5 partition + gate ----------------------------------------------------------------------------

def test_usage_codex_ac5_two_host_partition_and_retired_gate(cx: Cx) -> None:
    cx.seat('thoth:cx', created=at(0), specs=('spec_demo__cx',), provider='codex')
    cx.session('t1', [(at(60), SOL, 1000, 0, 0), (at(61), 'gpt-9-unlisted', 300, 0, 0)])  # measured + unpriced
    cx.session('t2', [(at(62), SOL, 500, 0, 0)], account=None)  # unknown_account
    cx.session('a1', [(at(63), SOL, 2000, 0, 0), (None, SOL, 40, 0, 0)], host='amaterasu')  # + untimed 40
    cx.session('a2', [(at(64), SOL, 10, 0, 0)], host='amaterasu', cumulative=u(70))  # unreconciled 60
    cx.session('a3', [(at(65), SOL, 90, 0, 0)], host='amaterasu', cumulative=u(80))  # unverifiable 90
    cx.session('m1', [(at(66), SOL, 5000, 0, 0)], host='merlin', cumulative=u(6000))  # retired candidate
    cx.config(accounts=[])
    e = codex_entry(cx.run('--calibrate'))
    window = e['measured_rollup']['tokens']
    rows = {r['host']: r for r in e['identity_mass_by_host']}
    for bucket in ur.WINDOW_BUCKETS:  # Σ hosts == the window denominator
        assert sum(r['window'][bucket] for r in rows.values()) == window[bucket]
    assert window == {'measured': 1000 + 2000 + 10 + 5000, 'unpriced': 300, 'unknown_account': 500}
    assert rows['amaterasu']['outside_window_sum'] == {'retired': 0, 'unverifiable': 90, 'unreconciled': 60,
                                                       'untimed': 40}
    assert rows['merlin']['outside_window_sum']['unreconciled'] == 1000
    gate = cx.run('--calibrate')['calibration']['codex']['unplaceable']
    total = 1300 + 500 + 2040 + 70 + 90 + 6000
    assert gate['provider_total_tokens'] == total and gate['unplaceable_tokens'] == 40 + 60 + 90 + 1000
    cx.config(accounts=[], retired=('merlin',))
    result = cx.run('--calibrate')
    gate = result['calibration']['codex']['unplaceable']
    assert gate['provider_total_tokens'] == total - 6000 and gate['unplaceable_tokens'] == 190
    assert gate['retired_mass'] == [{'provider': 'codex', 'host': 'merlin', 'tokens': 6000}]
    e = codex_entry(result)
    assert e['measured_rollup']['tokens']['measured'] == 3010  # retired records enter no window
    assert {r['host']: r for r in e['identity_mass_by_host']}['merlin']['outside_window_sum']['retired'] == 6000


@pytest.mark.parametrize('unreconciled, eligible', [(110, False), (90, True)])
def test_usage_codex_ac5_gate_threshold(cx: Cx, unreconciled: int, eligible: bool) -> None:
    cx.seat('thoth:cx', created=at(0), specs=('spec_demo__cx',), provider='codex')
    measured = 10_000 - unreconciled
    cx.session('n1', [(at(60), SOL, measured, 0, 0)], cumulative=u(10_000))  # ratio 1.1 % or 0.9 %
    cx.chist(at(0), 0)
    cx.chist(at(120), 1)
    e = codex_entry(cx.run('--calibrate'))
    assert e['unplaceable']['passes'] is eligible
    sample = (e['methods']['history_regression']['samples'] + e['methods']['history_regression']['exclusions'])[0]
    assert sample.get('reason') == (None if eligible else 'eligibility_unknown')
    assert (e['measured_coverage'] is None) is (not eligible)


# --- AC6 one quota -------------------------------------------------------------------------------------

def test_usage_codex_ac6_weekly_codex_only(cx: Cx) -> None:
    cx.seat('thoth:cx', created=at(0), specs=('spec_demo__cx',), provider='codex')
    cx.session('n1', [(at(3600 * i - 60), SOL, 1000, 0, 0) for i in range(1, 7)])
    for i in range(7):  # the one quota: weekly codex rollout lines with an account
        cx.chist(at(3600 * i), 2 * i)
    for i in range(7):  # interleaved auxiliary windows with their own pct changes
        cx.chist(at(3600 * i + 1800), 5 * i, kind='codex_bengalfox')
        cx.chist(at(3600 * i + 900), 7 * i, minutes=300)
        cx.chist(at(3600 * i + 1200), 3 * i, kind='base_model_inference')
        cx.chist(at(3600 * i + 1500), 9 * i, account=None)
    e = codex_entry(cx.run('--calibrate'))
    c = e['methods']['history_regression']
    assert c['valid_samples'] == 6 and c['span_pct'] == 12
    assert all(s['from'] in {at(3600 * i) for i in range(7)} for s in c['samples'])
    only = {(r['window_kind'], r['window_minutes'], r['account_id']): r['observations']
            for r in cx.run('--calibrate')['calibration']['codex']['reported_only']}
    assert only == {('codex_bengalfox', 10080, ACCT): 7, ('codex', 300, ACCT): 7,
                    ('base_model_inference', 10080, ACCT): 7, ('codex', 10080, 'null'): 7}
    assert len(e['reported_only']) == 3  # per-account view excludes the null-account lines


# --- AC7 Method C ----------------------------------------------------------------------------------------

def _slope_fixture(cx: Cx, deltas: list[int]) -> dict:
    """True $ per 1 % = 60 and tokens per 1 % = 50M: per point 10M Sol uncached ($50) + 40M Terra uncached ($10)."""
    cx.seat('thoth:cx', created=at(0), specs=('spec_demo__cx',), provider='codex')
    rows, pct = [], 0
    cx.chist(at(0), 0)
    for i, d in enumerate(deltas, 1):
        rows += [(at(3600 * i - 600), SOL, 10_000_000 * d, 0, 0), (at(3600 * i - 300), TERRA, 40_000_000 * d, 0, 0)]
        pct += d
        cx.chist(at(3600 * i), pct)
    cx.session('n1', rows)
    return codex_entry(cx.run('--calibrate'))


@pytest.mark.parametrize('deltas, fitted', [([3] * 9, False), ([3] * 9 + [2], False), ([3] * 10, True)])
def test_usage_codex_ac7_known_slope_activation(cx: Cx, deltas: list[int], fitted: bool) -> None:
    e = _slope_fixture(cx, deltas)
    assert e['methods']['history_regression']['valid_samples'] == len(deltas)
    if not fitted:
        assert e['status'] == 'insufficient' and e['coefficient'] is None and e['tokens_per_pct'] is None
        assert 'needs >= 10 spanning >= 30' in e['reason']
        return
    assert e['status'] == 'fitted' and e['coefficient'] == pytest.approx(60.0, abs=0.5)
    assert e['tokens_per_pct'] == pytest.approx(50_000_000)
    assert e['residual_mape_pct'] == 0 and e['tokens_residual_mape_pct'] == 0
    assert set(e['methods']) == {'history_regression'}  # no Method A for Codex


@pytest.mark.parametrize('unknown, kept', [(51, False), (50, True)])
def test_usage_codex_ac7_sample_coverage_boundary(cx: Cx, unknown: int, kept: bool) -> None:
    cx.seat('thoth:cx', created=at(0), specs=('spec_demo__cx',), provider='codex')
    cx.session('k', [(at(60), SOL, 1000 - unknown, 0, 0)])
    cx.session('x', [(at(61), SOL, unknown, 0, 0)], account=None)  # coverage 0.949 or 0.95
    cx.chist(at(0), 0)
    cx.chist(at(120), 1)
    c = codex_entry(cx.run('--calibrate'))['methods']['history_regression']
    assert c['valid_samples'] == int(kept)
    if not kept:
        assert c['exclusions'][0]['reason'] == 'coverage_below_0.95'


@pytest.mark.parametrize('extra, kept', [(0, True), (1, False)])
def test_usage_codex_ac7_interval_cap(cx: Cx, extra: int, kept: bool) -> None:
    cx.seat('thoth:cx', created=at(0), specs=('spec_demo__cx',), provider='codex')
    cx.session('k', [(at(60), SOL, 1000, 0, 0)])
    cx.chist(at(0), 0)
    cx.chist(at(86400 + extra), 1)
    c = codex_entry(cx.run('--calibrate'))['methods']['history_regression']
    assert c['valid_samples'] == int(kept)
    if not kept:
        assert c['exclusions'][0]['reason'] == 'interval_over_24h'


def test_usage_codex_ac7_zero_delta_run_extends_interval(cx: Cx) -> None:
    cx.seat('thoth:cx', created=at(0), specs=('spec_demo__cx',), provider='codex')
    cx.session('k', [(at(60), SOL, 1000, 0, 0), (at(4000), SOL, 500, 0, 0)])
    for i, pct in enumerate((10, 10, 10, 12)):
        cx.chist(at(3000 * i), pct)
    c = codex_entry(cx.run('--calibrate'))['methods']['history_regression']
    assert c['valid_samples'] == 1
    s = c['samples'][0]
    # repeated rollout lines dedupe by (account, kind, minutes, resets_at, pct); the run still extends the
    # interval to the base's first observation, so the row after the second 10 stays in this sample
    assert s['delta_pct'] == 2 and s['from'] == at(0) and s['tokens']['measured'] == 1500


def test_usage_codex_ac5_outside_mass_is_per_account(cx: Cx) -> None:
    cx.seat('thoth:cx', created=at(0), specs=('spec_demo__cx',), provider='codex')
    cx.session('a', [(at(60), SOL, 10, 0, 0)], cumulative=u(50))  # ACCT: unreconciled 40
    cx.session('b', [(at(60), SOL, 10, 0, 0)], account=ACCT2, cumulative=u(90))  # ACCT2: unreconciled 80
    cx.session('u', [(at(60), SOL, 10, 0, 0)], account=None, cumulative=u(15))  # unknown: unreconciled 5, ownable by both
    result = cx.run('--calibrate')
    outside = {e['account_id']: e['identity_mass_by_host'][0]['outside_window_sum']['unreconciled']
               for e in result['calibration']['codex']['entries']}
    assert outside == {ACCT: 45, ACCT2: 85}
    fleet = result['calibration']['codex']['identity_mass_by_host'][0]['outside_window_sum']['unreconciled']
    assert fleet == 125


@pytest.mark.parametrize('order', [0, 1])
def test_usage_codex_ac2_alias_chain_rejected_in_either_order(cx: Cx, order: int) -> None:
    third = '00000000-0000-4000-8000-0000000000c3'
    cx.seat('thoth:cx', created=at(0), specs=('spec_demo__cx',), provider='codex')
    cx.session('a', [(at(60), SOL, 100, 0, 0)])
    cx.session('b', [(at(60), SOL, 200, 0, 0)], account=ACCT2)
    cx.session('c', [(at(60), SOL, 400, 0, 0)], account=third)
    path = cx.config(accounts=[])
    data = json.loads(path.read_text())
    chain = [{'account_id': ACCT, 'alias_of': ACCT2, 'justification': 'synthetic'},
             {'account_id': ACCT2, 'alias_of': third, 'justification': 'synthetic'}]
    data['account_aliases'] = chain if order == 0 else chain[::-1]
    path.write_text(json.dumps(data))
    codex = cx.run('--calibrate')['calibration']['codex']
    assert {a['status'] for a in codex['account_aliases']} == {'rejected'}
    assert sorted(e['account_id'] for e in codex['entries']) == sorted([ACCT, ACCT2, third])
