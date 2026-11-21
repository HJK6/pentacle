import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from admission_attestation_check import Refusal, Unavailable, check, main, verify_changed
from admission_fingerprint import python_fingerprint
from admission_policy import identity, manifest_entry


def record():
    return {'test_id': 'tests/test_a.py::test_a', 'suite': 'daemon', 'fingerprint': 'a'*64,
            'platforms': ['linux'], 'journey': 'journey', 'boundary': 'client',
            'failing_receipt': 'receipt/fail', 'failing_sha256': 'b'*64,
            'passing_receipt': 'receipt/pass', 'passing_sha256': 'c'*64,
            'qa_report_id': 'report-1', 'qa_target_sha': 'd'*40}


def evidence(row):
    key = identity(row['suite'], row['test_id'])
    raw = (json.dumps({'version': 1, 'records': {key: manifest_entry(row)}}, sort_keys=True) + '\n').encode()
    digest = hashlib.sha256(raw).hexdigest()
    row['evidence_manifest'] = 'scripts/test-admission-evidence/' + digest + '.json'
    row['evidence_manifest_sha256'] = digest
    return raw


def test_independent_accepted_report_binds_target_and_both_controls():
    row = record()
    raw = evidence(row)
    key = identity(row['suite'], row['test_id'])
    report = {'type': 'work.report.get.ok', 'status': 'done', 'qa_verdict': 'accept',
              'report_id': row['qa_report_id'], 'target_sha': row['qa_target_sha'],
              'qa_review': {'gate_evidence_digest': row['evidence_manifest_sha256'], 'candidate_identity': row['qa_target_sha'],
                            'reviewed_scope': 'test admission', 'commission_msg_id': 0}}
    verify_changed({}, {key: row}, lambda _: report, lambda _: row['fingerprint'], lambda _: raw)
    changed = {**row, 'passing_sha256': 'e'*64}
    with pytest.raises(Refusal):
        verify_changed({}, {key: changed}, lambda _: report, lambda _: row['fingerprint'], lambda _: raw)
    with pytest.raises(Refusal):
        verify_changed({}, {key: row}, lambda _: {**report, 'qa_verdict': 'reject'}, lambda _: row['fingerprint'], lambda _: raw)
    with pytest.raises(Refusal):
        verify_changed({}, {key: row}, lambda _: report, lambda _: 'f'*64, lambda _: raw)


def test_unavailable_report_is_error_and_unchanged_record_is_not_requeried():
    row = record()
    raw = evidence(row)
    key = identity(row['suite'], row['test_id'])
    def unavailable(_):
        raise Unavailable('service unavailable')
    with pytest.raises(Unavailable):
        verify_changed({}, {key: row}, unavailable, lambda _: row['fingerprint'], lambda _: raw)
    verify_changed({key: copy.deepcopy(row)}, {key: row}, unavailable, lambda _: row['fingerprint'], lambda _: raw)


def test_relay_checker_reads_exact_target_source_and_report(tmp_path):
    repo = tmp_path/'repo'
    subprocess.run(['git', 'init', '--initial-branch=main', str(repo)], check=True, capture_output=True)
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
    git('config', 'user.name', 'test')
    git('config', 'user.email', 'test@example.test')
    testfile = repo/'services/chat-stream-v2/tests/test_a.py'
    testfile.parent.mkdir(parents=True)
    testfile.write_text('def test_a():\n    assert True\n')
    records = repo/'scripts/test-admission-records.json'
    records.parent.mkdir()
    records.write_text('{"version":1,"records":{}}')
    git('add', '.')
    git('commit', '-m', 'target')
    base = git('rev-parse', 'HEAD')
    row = record()
    row['qa_target_sha'] = base
    row['fingerprint'] = python_fingerprint(repo/'services/chat-stream-v2', row['test_id'])
    raw = evidence(row)
    evidence_path = repo/row['evidence_manifest']
    evidence_path.parent.mkdir()
    evidence_path.write_bytes(raw)
    report = {'type': 'work.report.get.ok', 'status': 'done', 'qa_verdict': 'accept',
              'report_id': row['qa_report_id'], 'target_sha': base,
              'qa_review': {'gate_evidence_digest': row['evidence_manifest_sha256'], 'candidate_identity': base,
                            'reviewed_scope': 'admission', 'commission_msg_id': 0}}
    fake = tmp_path/'agent-orch'
    fake.write_text('#!' + sys.executable + '\nprint(' + repr(json.dumps(report)) + ')\n')
    fake.chmod(0o755)
    records.write_text(json.dumps({'version': 1, 'records': {identity(row['suite'], row['test_id']): row}}))
    git('add', '.')
    git('commit', '-m', 'admission')
    tip = git('rev-parse', 'HEAD')
    check(repo, base, tip, str(fake))
    row['passing_sha256'] = 'e'*64
    records.write_text(json.dumps({'version': 1, 'records': {identity(row['suite'], row['test_id']): row}}))
    git('add', '.')
    git('commit', '-m', 'alter receipt')
    with pytest.raises(Refusal):
        check(repo, base, git('rev-parse', 'HEAD'), str(fake))


def test_exit_contract_distinguishes_refusal_and_unavailable(monkeypatch, tmp_path):
    args = ['--root', str(tmp_path), '--base', 'a'*40, '--tip', 'b'*40]
    def refuse(*_args):
        raise Refusal('invalid')
    def unavailable(*_args):
        raise Unavailable('missing service')
    monkeypatch.setattr('admission_attestation_check.check', refuse)
    assert main(args) == 1
    monkeypatch.setattr('admission_attestation_check.check', unavailable)
    assert main(args) == 2
