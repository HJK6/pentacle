import json
import subprocess

import pytest

from admission_inputs import load_inputs
from admission_query import query, verify_junit
from admission_fingerprint import javascript_fingerprint
from admission_policy import identity


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def make_repo(root):
    subprocess.run(['git', 'init', '--initial-branch=main', str(root)], check=True, capture_output=True)
    git(root, 'config', 'user.name', 'test')
    git(root, 'config', 'user.email', 'test@example.test')
    scripts = root / 'scripts'
    scripts.mkdir()
    (scripts/'test-admission-inventory.json').write_text(json.dumps({'version': 1, 'base_sha': 'f3a76f479e14a390cd3d61234e1fd6e8a7d228d1', 'python': {}, 'javascript': {}}))
    (scripts/'test-admission-records.json').write_text(json.dumps({'version': 1, 'records': {}}))
    (scripts/'quarantine.json').write_text(json.dumps({'version': 1, 'nonblocking_tests': [], 'history': {}}))
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'base')
    git(root, 'update-ref', 'refs/remotes/origin/main', git(root, 'rev-parse', 'HEAD'))
    git(root, 'switch', '-c', 'candidate')
    return scripts


def test_candidate_cannot_self_admit_and_base_records_are_used(tmp_path):
    scripts = make_repo(tmp_path)
    (scripts/'test-admission-records.json').write_text(json.dumps({'version': 1, 'records': {'new': {'test_id': 'forged'}}}))
    _, records, _ = load_inputs(tmp_path)
    assert records == {}
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-m', 'candidate')
    _, records, _ = load_inputs(tmp_path)
    assert records == {}
    git(tmp_path, 'update-ref', 'refs/remotes/origin/main', git(tmp_path, 'rev-parse', 'HEAD'))
    _, records, _ = load_inputs(tmp_path)
    assert records == {'new': {'test_id': 'forged'}}


def test_missing_or_malformed_inventory_is_incomplete(tmp_path):
    scripts = make_repo(tmp_path)
    (scripts/'quarantine.json').write_text('{broken')
    with pytest.raises(ValueError):
        load_inputs(tmp_path)


def test_javascript_file_is_held_after_fixture_change(tmp_path):
    scripts = make_repo(tmp_path)
    tests = tmp_path/'test'
    tests.mkdir()
    (tests/'helper.js').write_text('module.exports=1;')
    (tests/'case.test.js').write_text("require('./helper');")
    legacy = json.loads((scripts/'test-admission-inventory.json').read_text())
    legacy['javascript'][identity('javascript', 'test/case.test.js')] = javascript_fingerprint(tmp_path, 'test/case.test.js')
    (scripts/'test-admission-inventory.json').write_text(json.dumps(legacy))
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-m', 'rollout')
    git(tmp_path, 'update-ref', 'refs/remotes/origin/main', git(tmp_path, 'rev-parse', 'HEAD'))
    assert query(tmp_path, ['test/case.test.js'], 'linux')['files']['test/case.test.js']['state'] == 'blocking'
    (tests/'helper.js').write_text('module.exports=2;')
    assert query(tmp_path, ['test/case.test.js'], 'linux')['files']['test/case.test.js']['state'] == 'advisory'


def test_javascript_advisory_receipt_proves_exact_case(tmp_path):
    receipt = tmp_path/'node.xml'
    receipt.write_text('<testsuites><testsuite name="parent"><testcase name="child"/></testsuite></testsuites>')
    assert verify_junit(receipt, ['parent child'])['executed']
    assert not verify_junit(receipt, ['parent missing'])['executed']
    receipt.write_text('<testsuites><testcase name="case"><skipped/></testcase></testsuites>')
    assert not verify_junit(receipt, ['case'])['executed']
