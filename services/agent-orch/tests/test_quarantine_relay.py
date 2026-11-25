import hashlib
import json
import subprocess
import sys

from agent_orch.quarantine_relay import prepare_bundle


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


def test_bundle_contains_only_list_change_and_pinned_fleet_receipt(tmp_path):
    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', '--initial-branch=main', str(remote)], check=True, capture_output=True)
    initial = tmp_path / 'initial'
    subprocess.run(['git', 'clone', str(remote), str(initial)], check=True, capture_output=True)
    git(initial, 'config', 'user.name', 'Pentacle Agent')
    git(initial, 'config', 'user.email', 'agent@users.noreply.github.com')
    (initial / 'scripts').mkdir()
    (initial / 'scripts/quarantine-v2.json').write_text('{"version": 2, "nonblocking_tests": [{"id": "cli::tests/test_other.py::test_case", "owner": "other", "reason": "other flake", "expires": "2027-01-01"}]}\n')
    git(initial, 'add', 'scripts/quarantine-v2.json')
    git(initial, 'commit', '-m', 'initial')
    git(initial, 'push', 'origin', 'main')
    base = git(initial, 'rev-parse', 'HEAD')
    checker = tmp_path / 'checker.py'
    checker.write_text('''import argparse,json\np=argparse.ArgumentParser()\np.add_argument("--root"); p.add_argument("--fleet-base"); p.add_argument("--fleet-tip"); a=p.parse_args()\nprint(json.dumps({"passed":True,"unexcepted_match_count":0,"rule_hits":[],"base":a.fleet_base,"tip":a.fleet_tip}))\n''')
    digest = hashlib.sha256(checker.read_bytes()).hexdigest()
    packet = prepare_bundle(str(remote), checker, digest, tmp_path / 'out',
                            ['daemon::tests/test_example.py::test_case'], activations={'daemon::tests/test_example.py::test_case': {'id': 'daemon::tests/test_example.py::test_case', 'owner': 'team', 'reason': 'flake', 'expires': '2027-01-01'}}, python=sys.executable)
    assert packet['base_sha'] == base
    assert packet['candidate_sha'] != base
    assert packet['bundle']['sha256'] == hashlib.sha256((tmp_path / 'out/change.bundle').read_bytes()).hexdigest()
    assert packet['fleet_guard']['passed'] is True
    replay = prepare_bundle(str(remote), checker, digest, tmp_path / 'out2',
                            ['daemon::tests/test_example.py::test_case'], activations={'daemon::tests/test_example.py::test_case': {'id': 'daemon::tests/test_example.py::test_case', 'owner': 'team', 'reason': 'flake', 'expires': '2027-01-01'}}, python=sys.executable)
    assert replay['candidate_sha'] == packet['candidate_sha']
    verify = tmp_path / 'verify'
    subprocess.run(['git', 'clone', str(remote), str(verify)], check=True, capture_output=True)
    git(verify, 'fetch', str(tmp_path / 'out/change.bundle'), packet['candidate_ref'])
    assert git(verify, 'diff', '--name-only', base, 'FETCH_HEAD') == 'scripts/quarantine-v2.json'
    blob = git(verify, 'show', 'FETCH_HEAD:scripts/quarantine-v2.json')
    assert [row['id'] for row in json.loads(blob)['nonblocking_tests']] == ['cli::tests/test_other.py::test_case', 'daemon::tests/test_example.py::test_case']
    assert 'Coverage-Drop: daemon::tests/test_example.py::test_case' in git(verify, 'show', '-s', '--format=%B', 'FETCH_HEAD')
