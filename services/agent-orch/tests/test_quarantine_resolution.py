import hashlib
import json
from pathlib import Path

from agent_orch.quarantine_resolution import verify_restoration


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def test_restoration_requires_installed_and_hosted_blocking_case_and_accepted_review(tmp_path):
    test = 'daemon::tests/test_a.py::test_case'
    sha = 'a'*40
    checkout = tmp_path/'runtime'
    checkout.mkdir()
    junit = b'<testsuite><testcase classname="tests.test_a" name="test_case"/></testsuite>'
    junit_path = tmp_path/'installed.xml'
    junit_path.write_bytes(junit)
    gate = {'sha': sha, 'repo_root': str(checkout), 'finished': True, 'passed': True, 'generated_at': '2026-11-20T23:00:00Z',
            'source': {'clean_before': True, 'clean_after': True, 'sha_before': sha, 'sha_after': sha},
            'tiers': [{'tier': 'unit', 'passed': True, 'junit': str(junit_path),
                       'command': ['pytest', '-m', 'not quarantined and not admission_hold']}]}
    gate_path = tmp_path/'gate.json'
    gate_path.write_text(json.dumps(gate))
    manifest = {'version': 1, 'disposition': 'repair', 'test': test, 'target_sha': sha,
                'restored_at': '2026-11-21T00:00:00Z',
                'installed': {'gate': str(gate_path), 'gate_sha256': digest(gate_path.read_bytes()),
                              'junit': str(junit_path), 'junit_sha256': digest(junit)},
                'hosted': {'run': 42, 'attempt': 1, 'artifact': 7,
                           'junit_member': 'daemon/gate/unit.junit.xml', 'junit_sha256': digest(junit)}}
    path = tmp_path/'proof.json'
    path.write_text(json.dumps(manifest))
    descriptor = {'manifest': str(path), 'sha256': digest(path.read_bytes()), 'report_id': 'qa-1',
                  'at': manifest['restored_at']}
    report = {'type': 'work.report.get.ok', 'report_id': 'qa-1', 'status': 'done',
              'qa_verdict': 'accept', 'target_sha': sha,
              'qa_review': {'candidate_identity': sha, 'gate_evidence_digest': descriptor['sha256'],
                            'reviewed_scope': 'restoration ' + test, 'commission_msg_id': 0}}
    def api(repo, route):
        if route == 'commits/main': return {'sha': sha}
        if route == 'actions/runs/42/attempts/1': return {'head_sha': sha, 'status': 'completed', 'conclusion': 'success', 'updated_at': '2026-11-20T23:30:00Z'}
        if route == 'actions/runs/42/artifacts?per_page=100': return {'total_count': 1, 'artifacts': [{'id': 7, 'name': 'daemon-unit-0-42-1', 'size_in_bytes': 100}]}
        raise AssertionError(route)
    def hosted(*_): return junit
    assert verify_restoration(test, descriptor, checkout, get_report=lambda _: report,
                              api=api, hosted_junit=hosted, main_contains=lambda *_: True, selected=set())
    assert not verify_restoration(test, descriptor, checkout, get_report=lambda _: report,
                                  api=api, hosted_junit=hosted, main_contains=lambda *_: True,
                                  selected={'tests/test_a.py::test_case'})
    assert not verify_restoration(test, descriptor, checkout, get_report=lambda _: {**report, 'qa_verdict': 'reject'},
                                  api=api, hosted_junit=hosted, main_contains=lambda *_: True, selected=set())
    junit_path.write_bytes(b'<testsuite><testcase classname="tests.test_a" name="test_case"><skipped/></testcase></testsuite>')
    assert not verify_restoration(test, descriptor, checkout, get_report=lambda _: report,
                                  api=api, hosted_junit=hosted, main_contains=lambda *_: True, selected=set())
