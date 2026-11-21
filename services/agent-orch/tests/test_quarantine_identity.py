import io
import json
import zipfile

from agent_orch import reliability_hosted
from agent_orch.quarantine_identity import bind_first_attempts, enrich_other_python, identities, pytest_facts, reviewed_bindings


def archive(records):
    target = io.BytesIO()
    with zipfile.ZipFile(target, 'w') as z:
        z.writestr('daemon/gate/unit.progress.jsonl', '\n'.join(json.dumps(r) for r in records))
    return target.getvalue()


def test_failed_fixture_phase_maps_only_exact_node_identity():
    raw = archive([{'kind': 'phase', 'node': 'tests/test_a.py::TestThing::test_a[case]',
                    'phase': 'setup', 'outcome': 'failed'}])
    got = identities(raw, {'tests.test_a.TestThing::test_a[case]'})
    assert got == {'tests.test_a.TestThing::test_a[case]':
                   {'test': 'tests/test_a.py::TestThing::test_a[case]', 'phase': 'setup'}}
    assert identities(raw, {'tests/test_a.py::TestThing::test_a[case]'}) == {
        'tests/test_a.py::TestThing::test_a[case]': {'test': 'tests/test_a.py::TestThing::test_a[case]', 'phase': 'setup'}}


def test_truncated_or_ambiguous_identity_is_never_guessed():
    raw = archive([{'kind': 'phase', 'node': 'tests/test_a.py::test_a[long...#123456789abc]',
                    'phase': 'call', 'outcome': 'failed'}])
    assert identities(raw, {'tests.test_a::test_a[long...#123456789abc]'}) == {}


def test_binding_uses_first_attempt_artifact_and_reports_missing_case():
    raw = archive([{'kind': 'phase', 'node': 'tests/test_a.py::test_one', 'phase': 'call', 'outcome': 'failed'}])
    rows = [{'id': '42', 'first': 'failure', 'causes': [
        {'kind': 'non_code', 'test': 'tests.test_a::test_one', 'job': 'daemon-unit (0)', 'step': 'tests'},
        {'kind': 'non_code', 'test': 'tests.test_a::missing', 'job': 'daemon-unit (0)', 'step': 'tests'}]}]
    def api(repo, route):
        assert route == 'actions/runs/42/artifacts?per_page=100'
        return {'total_count': 1, 'artifacts': [{'name': 'daemon-unit-0-42-1', 'id': 7, 'size_in_bytes': len(raw)}]}
    def download(repo, route):
        assert route == 'actions/artifacts/7/zip'
        return raw
    bound, missing = bind_first_attempts(rows, 'org/repo', api=api, download=download)
    assert bound[('42', 'daemon-unit (0)', 'tests', 'tests.test_a::test_one')]['test'] == 'tests/test_a.py::test_one'
    assert len(missing) == 1 and 'missing' in missing[0]


def test_known_signature_must_be_reviewed_for_actual_fixture_phase():
    cause = {'kind': 'non_code', 'test': 'tests.test_a::test_one', 'job': 'unit', 'step': 'tests',
             'assertion_digest': 'digest', 'revision': 'a' * 40}
    rows = [{'id': '1', 'causes': [cause], 'failures': [{'job': 'unit', 'step': 'tests', 'runner': ['ubuntu-24.04']}]}]
    key = ('1', 'unit', 'tests', cause['test'])
    bound = {key: {'test': 'tests/test_a.py::test_one', 'phase': 'setup'}}
    rule = {'kind': 'non_code', 'test': cause['test'], 'step': 'tests', 'runner': 'ubuntu-',
            'assertion_digest': 'digest', 'revision': 'a' * 40, 'fixture_phase': 'call'}
    assert reviewed_bindings(rows, bound, [rule]) == {}
    rule['fixture_phase'] = 'setup'
    assert reviewed_bindings(rows, bound, [rule]) == bound


def test_cli_failure_is_bound_to_retained_case_and_phase(monkeypatch):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as z:
        z.writestr('cli/gate/pytest.junit.xml', '<testsuite tests="1" errors="0" failures="1"><testcase classname="tests.test_a" name="test_one"><failure message="broken"/></testcase></testsuite>')
        z.writestr('cli/gate/pytest.progress.jsonl', json.dumps({'kind': 'phase', 'node': 'tests/test_a.py::test_one', 'phase': 'setup', 'outcome': 'failed'}))
    raw = stream.getvalue()
    row = {'id': '42', 'sha': 'a'*40, 'first': 'failure', 'causes': [],
           'failures': [{'job': 'contracts', 'step': 'CLI checks', 'runner': ['ubuntu'], 'source': 'job'}]}
    def api(*_):
        return {'total_count': 1, 'artifacts': [{'name': 'gate-summaries-contracts-42-1', 'id': 7, 'size_in_bytes': len(raw)}]}
    def classify(enriched, *_, **kwargs):
        assert enriched['failures'][0]['tests'][0]['test'] == 'tests.test_a::test_one'
        return 'false_red', [{'kind': 'non_code', 'test': 'tests.test_a::test_one'}]
    monkeypatch.setattr(reliability_hosted, 'classifications', classify)
    rows, bound, missing = enrich_other_python([row], {'repository': 'org/repo'}, api=api, download=lambda *_: raw)
    assert rows[0]['classification'] == 'false_red' and not missing
    assert bound[('42', 'contracts', 'CLI checks', 'tests.test_a::test_one')] == {
        'test': 'tests/test_a.py::test_one', 'phase': 'setup', 'suite': 'cli'}


def test_parameter_identity_is_not_reduced_to_family():
    raw = b'<testsuite tests="1" errors="0" failures="1"><testcase classname="tests.test_a" name="test_one[case value]"><failure message="broken"/></testcase></testsuite>'
    assert pytest_facts(raw)[0]['test'] == 'tests.test_a::test_one[case value]'


def incomplete_contract_artifact(*, include_cli_progress):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as artifact:
        for suite, module, name in [('cli', 'test_a', 'test_one'), ('shared', 'test_b', 'test_two')]:
            if suite == 'shared' and not include_cli_progress:
                continue
            artifact.writestr(f'{suite}/gate/pytest.junit.xml',
                             f'<testsuite errors="0" failures="1"><testcase classname="tests.{module}" '
                             f'name="{name}"><failure message="broken"/></testcase></testsuite>')
        if include_cli_progress:
            artifact.writestr('cli/gate/pytest.progress.jsonl', json.dumps(
                {'kind': 'phase', 'node': 'tests/test_a.py::test_one', 'phase': 'setup', 'outcome': 'failed'}))
    return stream.getvalue()


def classify_contract_artifact(raw, targets):
    sha = 'a' * 40
    failures, decisions = [], []
    for step, test in targets:
        failures.append({'job': 'contracts', 'step': step, 'runner': ['ubuntu'], 'source': f'job/{step}'})
        decisions.append({'run': '42', 'sha': sha, 'job': 'contracts', 'step': step, 'reviewer': 'independent',
                          'reason': 'retained review', 'revision': sha, 'source': 'review',
                          'causes': [{'kind': 'non_code', 'test': test, 'cause': 'race', 'cause_class': 'fixture'}]})
    row = {'id': '42', 'sha': sha, 'created': '2026-11-21T10:00:00Z', 'first': 'failure', 'failures': failures}

    def api(*_):
        return {'total_count': 1, 'artifacts': [
            {'name': 'gate-summaries-contracts-42-1', 'id': 7, 'size_in_bytes': len(raw)}]}

    rows, bound, missing = enrich_other_python(
        [row], {'repository': 'org/repo', 'decisions': decisions}, api=api, download=lambda *_: raw)
    return rows[0], bound, missing


def test_junit_without_required_progress_stays_unclassified():
    from agent_orch.quarantine_candidates import candidates

    # The installed contracts artifact held a valid failed CLI JUnit case
    # without its companion progress member.
    row, bound, missing = classify_contract_artifact(incomplete_contract_artifact(include_cli_progress=False),
                                                     [('CLI checks', 'tests.test_a::test_one')])
    assert bound == {}
    assert len(missing) == 1
    assert row['classification'] == 'incomplete'
    assert [(cause['step'], cause['kind']) for cause in row['causes']] == [('CLI checks', 'unclassified')]
    assert candidates([row], {'tests.test_a::test_one': 'tests/test_a.py::test_one'}) == {}


def test_failed_second_target_cannot_reuse_first_targets_exact_identity():
    from agent_orch.quarantine_candidates import candidates

    row, bound, missing = classify_contract_artifact(
        incomplete_contract_artifact(include_cli_progress=True),
        [('CLI checks', 'tests.test_a::test_one'), ('Shared checks', 'tests.test_b::test_two')])
    assert bound == {('42', 'contracts', 'CLI checks', 'tests.test_a::test_one'):
                     {'test': 'tests/test_a.py::test_one', 'phase': 'setup', 'suite': 'cli'}}
    assert len(missing) == 1
    assert [(cause['step'], cause['kind']) for cause in row['causes']] == [
        ('CLI checks', 'non_code'), ('Shared checks', 'unclassified')]
    assert set(candidates([row], {**bound, 'tests.test_b::test_two': 'tests/test_b.py::test_two'})) == {
        'tests/test_a.py::test_one'}
