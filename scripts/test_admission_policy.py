import hashlib

from admission_policy import admission, identity, manifest_digest


def h(value):
    return hashlib.sha256(value.encode()).hexdigest()


def record(test, suite='daemon'):
    return {'test_id': test, 'suite': suite, 'fingerprint': 'a'*64, 'platforms': ['linux'],
            'journey': 'delivery', 'boundary': 'client', 'failing_receipt': 'receipt/fail',
            'failing_sha256': 'b'*64, 'passing_receipt': 'receipt/pass', 'passing_sha256': 'c'*64,
            'qa_report_id': 'report-1', 'qa_target_sha': 'd'*40,
            'evidence_manifest': 'scripts/test-admission-evidence/' + 'e'*64 + '.json',
            'evidence_manifest_sha256': 'e'*64}


def test_grandfather_changed_new_and_platform_are_separate():
    test = 'tests/test_a.py::test_a'
    key = identity('daemon', test)
    legacy = {'python': {'daemon': {'linux': {key: 'a'*64}}}}
    quarantine = {'nonblocking_tests': [], 'history': {}}
    assert admission('daemon', test, 'a'*64, 'linux', legacy, {}, quarantine) == ('blocking', 'grandfathered')
    assert admission('daemon', test, 'b'*64, 'linux', legacy, {}, quarantine)[0] == 'advisory'
    assert admission('daemon', test, 'a'*64, 'darwin', legacy, {}, quarantine)[0] == 'advisory'
    assert admission('daemon', 'tests/test_a.py::new', 'a'*64, 'linux', legacy, {}, quarantine)[0] == 'advisory'


def test_only_complete_base_attestation_promotes_and_restoration_requires_current_activation():
    test = 'tests/test_a.py::test_a'
    key = identity('daemon', test)
    legacy = {'python': {'daemon': {'linux': {key: 'a'*64}}}}
    quarantine = {'nonblocking_tests': [], 'history': {h(test): {'activation': 'old-series', 'file': h('tests/test_a.py')}}}
    assert admission('daemon', test, 'a'*64, 'linux', legacy, {}, quarantine)[0] == 'advisory'
    attestation = record(test)
    attestation['restoration_of'] = ['old-series']
    assert admission('daemon', test, 'a'*64, 'linux', legacy, {key: attestation}, quarantine)[0] == 'blocking'
    del attestation['qa_report_id']
    assert admission('daemon', test, 'a'*64, 'linux', legacy, {key: attestation}, quarantine)[0] == 'advisory'
    quarantine['nonblocking_tests'] = [test]
    assert admission('daemon', test, 'a'*64, 'linux', legacy, {key: attestation}, quarantine)[0] == 'quarantine'


def test_receipt_digest_binds_both_controls_and_test_identity():
    a = record('tests/test_a.py::test_a')
    b = {**a, 'passing_sha256': 'e'*64}
    assert manifest_digest(a) != manifest_digest(b)


def test_quarantine_identity_includes_suite():
    test = 'tests/test_same.py::test_case'
    legacy = {'python': {'daemon': {'linux': {identity('daemon', test): 'a'*64}},
                         'cli': {'linux': {identity('cli', test): 'a'*64}}}}
    canonical = 'cli::' + test
    quarantine = {'nonblocking_tests': [canonical], 'history': {
        h(canonical): {'activation': 'series', 'file': h('tests/test_same.py')}}}
    assert admission('cli', test, 'a'*64, 'linux', legacy, {}, quarantine)[0] == 'quarantine'
    assert admission('daemon', test, 'a'*64, 'linux', legacy, {}, quarantine)[0] == 'blocking'
