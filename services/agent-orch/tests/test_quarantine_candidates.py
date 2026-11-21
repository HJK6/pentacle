from agent_orch.quarantine_candidates import candidates, merge_seed_occurrences


TEST = 'tests/test_example.py::test_case'
INDEX = {'tests.test_example::test_case': TEST}


def observation(run, kind='non_code', **fields):
    cause = {'test': 'tests.test_example::test_case', 'kind': kind, 'reviewer': 'reviewer',
             'reason': 'fixture evidence', 'revision': 'a' * 40, 'evidence': f'run/{run}',
             'job': 'unit', 'step': 'tests', 'assertion_digest': fields.pop('signature', 'one')}
    cause.update(fields)
    return {'id': str(run), 'sha': 'a' * 40, 'created': f'2026-11-{run:02}T00:00:00Z',
            'first': 'failure', 'source': f'run/{run}', 'causes': [cause],
            'failures': [{'job': 'unit', 'step': 'tests', 'runner': ['ubuntu-24.04']}]}


def test_distinct_reviewed_occurrences_aggregate_even_with_different_signatures():
    rows = [observation(1), observation(1), observation(2, signature='other')]
    result = candidates(rows, INDEX)
    assert [r['id'] for r in result[TEST]] == ['public-checks/1/1', 'public-checks/2/1']
    assert [r['signature'] for r in result[TEST]] == ['one', 'other']
    assert result[TEST][0]['runner'] == ['ubuntu-24.04']


def test_retry_alone_unknown_identity_and_mixed_same_test_do_not_count():
    unknown = observation(1, kind='unclassified')
    unknown['latest'] = 'success'
    unmapped = observation(2)
    unmapped['causes'][0]['test'] = 'family'
    mixed = observation(3)
    mixed['causes'].append({**mixed['causes'][0], 'kind': 'code'})
    assert candidates([unknown, unmapped, mixed], INDEX) == {}


def test_restoration_boundary_excludes_old_failures():
    result = candidates([observation(1), observation(2)], INDEX,
                        boundaries={TEST: '2026-11-01T12:00:00Z'})
    assert [r['id'] for r in result[TEST]] == ['public-checks/2/1']


def test_unidentified_mixed_failure_in_same_job_cannot_authorize_quarantine():
    mixed = observation(1)
    mixed['causes'].append({'kind': 'unclassified', 'job': 'unit', 'step': 'tests'})
    assert candidates([mixed], INDEX) == {}


def test_adopted_seed_replay_never_adds_an_occurrence_twice():
    register = {'records': [{'test': TEST, 'item': 'existing',
                             'occurrences': ['public-checks/1/1', 'public-checks/2/1']}]}
    current = candidates([observation(1), observation(2)], INDEX)
    result = merge_seed_occurrences(register, current, {TEST})
    assert [row['id'] for row in result[TEST]] == ['public-checks/1/1', 'public-checks/2/1']
    assert all(row['source'].startswith('run/') for row in result[TEST])


def test_first_attempt_identity_binds_fixture_phase_to_signature():
    index = {('1', 'unit', 'tests', 'tests.test_example::test_case'):
             {'test': TEST, 'phase': 'setup'}}
    row = candidates([observation(1)], index)[TEST][0]
    assert row['fixture_phase'] == 'setup'
    assert row['signature'] == 'setup:one'


def test_incomplete_first_attempt_cannot_count():
    row = observation(1)
    row['missing'] = 'incomplete first attempt job census'
    assert candidates([row], INDEX) == {}
