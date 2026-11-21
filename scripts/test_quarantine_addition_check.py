import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from quarantine_addition_check import Refusal, Unavailable, verify_additions

TEST = 'daemon::tests/test_sample.py::test_case'


def setup(root):
    work = root/'work'
    folder = work/'backlog'/'pentacle__sample'
    folder.mkdir(parents=True)
    (folder/'summary.md').write_text('---\nid: work_pentacle__sample\nstatus: backlog\nowner: agent\n---\n')
    (folder/'spec.md').write_text(TEST + '\nhttps://github.com/HJK6/pentacle/actions/runs/101/attempts/1\n'
                                  'https://github.com/HJK6/pentacle/actions/runs/102/attempts/1\n')
    register = {'records': [{'test': TEST, 'item': 'pentacle__sample',
        'occurrences': ['public-checks/101/1', 'public-checks/102/1']}]}
    return work, register


def test_addition_requires_exact_open_decision_and_two_independent_links(tmp_path):
    work, register = setup(tmp_path)
    verify_additions({TEST}, register, work)
    register['records'][0]['occurrences'] = ['public-checks/101/1', 'public-checks/101/1']
    with pytest.raises(Refusal):
        verify_additions({TEST}, register, work)
    with pytest.raises(Refusal):
        verify_additions({'daemon::tests/test_other.py::test_case'}, register, work)
    with pytest.raises(Unavailable):
        verify_additions({TEST}, {'records': []}, tmp_path/'missing')


def test_cli_checks_added_seed_against_existing_register(tmp_path):
    repo = tmp_path/'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
    git('init', '-q')
    git('config', 'user.name', 'fixture')
    git('config', 'user.email', 'fixture@example.test')
    (repo/'scripts').mkdir()
    path = repo/'scripts/quarantine.json'
    path.write_text(json.dumps({'version': 1, 'nonblocking_tests': [], 'history': {}}))
    git('add', '.')
    git('commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    key = hashlib.sha256(TEST.encode()).hexdigest()
    file = hashlib.sha256(b'tests/test_sample.py').hexdigest()
    path.write_text(json.dumps({'version': 1, 'nonblocking_tests': [TEST],
                                'history': {key: {'file': file, 'activation': 'new', 'parents': []}}}))
    git('add', '.')
    git('commit', '-qm', 'addition')
    tip = git('rev-parse', 'HEAD')
    # A missing private register is an error, not a successful relay decision.
    proc = subprocess.run([sys.executable, str(Path(__file__).with_name('quarantine_addition_check.py')),
                           '--root', str(repo), '--base', base, '--tip', tip,
                           '--register', str(tmp_path/'missing'), '--work-root', str(tmp_path)],
                          capture_output=True, text=True)
    assert proc.returncode == 2 and json.loads(proc.stdout)['status'] == 'error'
