import pytest

from agent_orch.quarantine_ledger import accumulate


TEST = 'tests/test_a.py::test_a'


def event(number):
    return {'id': f'public-checks/{number}/1', 'created': f'2026-11-{number:02}T00:00:00Z',
            'source': f'run/{number}', 'signature': 'call:abc', 'later_attempts': []}


def test_later_window_keeps_distinct_first_attempt_and_replay_is_deduplicated(tmp_path):
    path = tmp_path / 'ledger.json'
    accumulate(path, {TEST: [event(1), event(1)]})
    result = accumulate(path, {TEST: [event(2)]})
    assert [row['id'] for row in result[TEST]] == ['public-checks/1/1', 'public-checks/2/1']


def test_conflicting_first_attempt_is_not_overwritten(tmp_path):
    path = tmp_path / 'ledger.json'
    accumulate(path, {TEST: [event(1)]})
    changed = {**event(1), 'signature': 'call:different'}
    with pytest.raises(ValueError):
        accumulate(path, {TEST: [changed]})
