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
    digest = hashlib.sha256(b'broken').hexdigest()
    ids = [file + '::' + name for name in ('["parent","case"]', '["split\\nline"]', 'splitline', '[" padded "]')]
    facts = [{'test': test, 'assertion_digest': digest} for test in ids]
    data = archive({'files': [{'file': file, 'status': 'fail', 'execution': {'exitCode': 1}, 'failures': facts}]}, {})
    assert failures(data) == [dict(fact, phase='call') for fact in facts]


def test_missing_or_unidentified_failure_cannot_be_silently_ignored():
    file = 'test/example.test.js'
    data = archive({'files': [{'file': file, 'status': 'fail', 'execution': {'exitCode': 1}}]}, {})
    with pytest.raises(ValueError):
        failures(data)


def test_exact_node_facts_are_fed_through_scorecard_classifier(monkeypatch):
    file = 'test/example.test.js'
    raw = archive({'files': [{'file': file, 'status': 'fail', 'execution': {'exitCode': 1},
                              'failures': [{'test': file + '::case', 'assertion_digest': 'a'*64}]}]}, {})
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


def test_nested_components_do_not_collide_and_restoration_is_exact():
    from agent_orch.quarantine_resolution import _passed
    file = 'test/example.test.js'
    name = hashlib.sha256(file.encode()).hexdigest() + '.xml'
    xml = ('<testsuites><testsuite name="a b"><testcase name="c"><failure type="testCodeFailure"/></testcase></testsuite>'
           '<testsuite name="a"><testcase name="b c"/></testsuite></testsuites>')
    data = archive({'files': [{'file': file, 'status': 'fail', 'execution': {'exitCode': 1}}]}, {name: xml})
    with pytest.raises(ValueError, match='no exact native'):
        failures(data)
    assert not _passed(xml.encode(), 'javascript', file + '::["a b","c"]')
    assert _passed(xml.encode(), 'javascript', file + '::["a","b c"]')
