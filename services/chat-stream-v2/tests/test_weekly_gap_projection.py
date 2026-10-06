"""Retained source inventories distinguish current gaps from sampled counts."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from tools import daily_retro as retro


def snapshot(settings, day, gaps, **extra):
    value = {'run_id': day, 'timezone': 'America/Chicago',
             'collected_at': day + 'T12:00:00+00:00', 'sources': [],
             'gaps': gaps, 'coverage': {'gaps': len(gaps), 'deferred': 0,
                                     'baseline_not_reviewed': 0},
             'primary': {'gaps': ['primary-only'], 'coverage': {}}}
    value.update(extra)
    path = settings.state_root / 'runs' / day / 'collection.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def gap(path='work/completed/old/spec.md', reason='missing Retro'):
    return {'path': path, 'reason': reason}


@pytest.fixture
def settings(tmp_path):
    return SimpleNamespace(state_root=tmp_path / 'state', memory_root=tmp_path / 'memory')


def hashes(root):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def test_current_distinct_baseline_does_not_hide_active_or_operational(settings):
    inventory = [gap(), gap('work/deprecated/old/spec.md'),
                 gap('work/in_progress/live/spec.md'),
                 gap('work/completed', 'directory unavailable')]
    for day in ('2026-09-28', '2026-09-29', '2026-09-30'):
        snapshot(settings, day, inventory)
    before = hashes(settings.state_root)
    result = retro.weekly_summary(settings, '2026-09-30')
    assert result['retro_coverage']['gaps'] == 4  # old sum is 12
    a = result['gap_accounting']
    assert a['baseline_current'] == 2 and a['actionable_current'] == 2
    assert a['new_this_week'] == 0 and a['comparison_mode'] == 'first_observed'
    assert result['primary_coverage']['gaps'] == 3  # separate daily denominator
    assert before == hashes(settings.state_root)


def test_new_is_union_minus_prior_not_net_growth(settings):
    old = gap('work/in_progress/old/spec.md')
    transient = gap('work/in_progress/transient/spec.md')
    changed = gap('work/in_progress/old/spec.md', 'empty Retro')
    snapshot(settings, '2026-09-27', [old])
    snapshot(settings, '2026-09-28', [old, transient, transient])
    snapshot(settings, '2026-09-29', [changed])
    result = retro.weekly_summary(settings, '2026-10-04')
    a = result['gap_accounting']
    assert result['retro_coverage']['gaps'] == 1
    assert a['new_this_week'] == 2 and a['comparison_mode'] == 'prior_snapshot'
    assert a['status'] == 'stale' and a['current']['run_id'] == '2026-09-29'
    assert '2026-10-04' in a['missing_dates']
    assert a['actionable_current'] == 1


@pytest.mark.parametrize('damage', ['absent', 'malformed', 'wrong_id', 'wrong_zone', 'wrong_capture', 'invalid_date'])
def test_invalid_inventory_is_unknown_not_empty(settings, damage):
    p = snapshot(settings, '2026-09-28', [gap()])
    data = json.loads(p.read_text())
    if damage == 'absent':
        data.pop('gaps')
    elif damage == 'malformed':
        data['gaps'] = [{}]
    elif damage == 'wrong_id':
        data['run_id'] = '2026-09-27'
    elif damage == 'wrong_zone':
        data['timezone'] = 'UTC'
    elif damage == 'wrong_capture':
        data['collected_at'] = '2026-09-29T12:00:00Z'
    else:
        p.rename(p.parent / 'unused.json')
        p = settings.state_root / 'runs/2026-09-31/collection.json'
        p.parent.mkdir(parents=True); data['run_id'] = '2026-09-31'
    p.write_text(json.dumps(data))
    if damage != 'invalid_date':
        # Weekly collection counters are unchanged; this test exercises the
        # inventory projection without invalidating unrelated packet fields.
        result = retro.weekly_summary(settings, '2026-09-28')
    else:
        result = retro.weekly_summary(settings, '2026-10-04')
    a = result['gap_accounting']
    assert a['status'] == 'unavailable'
    assert a['distinct_current'] is None and a['new_this_week'] is None
    assert result['retro_coverage']['gaps'] == 0
    assert a['excluded_snapshots']


def test_observed_empty_and_future_exclusion(settings):
    snapshot(settings, '2026-09-28', [])
    snapshot(settings, '2026-10-05', [gap()])
    result = retro.weekly_summary(settings, '2026-09-28')
    a = result['gap_accounting']
    assert a['status'] == 'current' and a['distinct_current'] == 0
    assert a['baseline_current'] == 0 and a['new_this_week'] == 0
    assert a['current']['run_id'] == '2026-09-28'
    assert a['baseline']['run_id'] == '2026-09-28'


def test_pre_week_inventory_is_stale_with_no_week_additions_measure(settings):
    snapshot(settings, '2026-09-20', [gap()])
    result = retro.weekly_summary(settings, '2026-10-04')
    assert result['gap_accounting']['status'] == 'stale'
    assert result['gap_accounting']['new_this_week'] is None
    assert result['retro_coverage']['gaps'] == 1
