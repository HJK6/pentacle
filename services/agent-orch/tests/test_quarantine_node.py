import hashlib
import io
import json
import zipfile

import pytest

from agent_orch import reliability_hosted
from agent_orch.quarantine_node import enrich, failures


def archive(summary, files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as z:
        z.writestr('npm/gate/npm-test-summary.json', json.dumps(summary))
        for name, body in files.items():
            z.writestr('npm/gate/node-junit/' + name, body)
    return stream.getvalue()


def test_node_failure_is_exact_case_and_phase():
    file = 'test/example.test.js'
    name = hashlib.sha256(file.encode()).hexdigest() + '.xml'
    xml = '<testsuites><testsuite name="parent"><testcase name="case"><failure type="testCodeFailure" message="broken"/></testcase></testsuite></testsuites>'
    data = archive({'files': [{'file': file, 'status': 'fail', 'execution': {'exitCode': 1}}]}, {name: xml})
    assert failures(data) == [{'test': file + '::parent case',
                                'assertion_digest': hashlib.sha256(b'broken').hexdigest(), 'phase': 'call', 'parents': ['parent']}]


def test_missing_or_unidentified_failure_cannot_be_silently_ignored():
    file = 'test/example.test.js'
    data = archive({'files': [{'file': file, 'status': 'fail', 'execution': {'exitCode': 1}}]}, {})
    with pytest.raises(ValueError):
        failures(data)


def test_exact_node_facts_are_fed_through_scorecard_classifier(monkeypatch):
    file = 'test/example.test.js'
    name = hashlib.sha256(file.encode()).hexdigest() + '.xml'
    xml = '<testsuites><testcase name="case"><failure type="testCodeFailure" message="broken"/></testcase></testsuites>'
    raw = archive({'files': [{'file': file, 'status': 'fail', 'execution': {'exitCode': 1}}]}, {name: xml})
    row = {'id': '42', 'sha': 'a'*40, 'first': 'failure', 'failures': [
        {'job': 'frontend', 'step': 'Supported npm tests', 'runner': ['ubuntu'], 'source': 'job/1'}], 'causes': []}
    def api(repo, route):
        return {'total_count': 1, 'artifacts': [{'name': 'gate-summaries-frontend-42-1', 'id': 7, 'size_in_bytes': len(raw)}]}
    def classify(enriched, decisions, signatures, checkout):
        assert enriched['failures'][0]['tests'][0]['test'] == file + '::case'
        return 'genuine_red', [{'kind': 'code', 'test': 'other'}, {'kind': 'non_code', 'test': file + '::case'}]
    monkeypatch.setattr(reliability_hosted, 'classifications', classify)
    rows, bound, missing = enrich([row], {'repository': 'org/repo'}, api=api, download=lambda *_: raw)
    assert rows[0]['classification'] == 'genuine_red'
    assert len(rows[0]['causes']) == 2 and not missing
    assert bound[('42', 'frontend', 'Supported npm tests', file + '::case')]['phase'] == 'call'
