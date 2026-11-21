import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from quarantine_scheduler import run_child
from tools.run_gate import _advisory_observed
from tools import run_gate


def test_worker_exit_and_timeout_are_bounded():
    async def exercise():
        code = await run_child([sys.executable, '-c', 'raise SystemExit(3)'], dict(os.environ), timeout=3)
        assert code == 3
        with pytest.raises(asyncio.TimeoutError):
            await run_child([sys.executable, '-c', 'import time; time.sleep(30)'], dict(os.environ), timeout=0.1)
    asyncio.run(exercise())


def test_advisory_receipt_requires_each_exact_case_to_execute(tmp_path):
    receipt = tmp_path / 'junit.xml'
    receipt.write_text('<testsuite><testcase classname="tests.test_a.TestGroup" name="test_one[x]"/></testsuite>')
    expected = ['tests/test_a.py::TestGroup::test_one[x]']
    assert _advisory_observed(receipt, expected)
    assert not _advisory_observed(receipt, expected + ['tests/test_a.py::test_missing'])
    receipt.write_text('<testsuite><testcase classname="tests.test_a.TestGroup" name="test_one[x]"><skipped/></testcase></testsuite>')
    assert not _advisory_observed(receipt, expected)


def test_gate_executes_quarantined_and_unadmitted_cases_as_advisory(tmp_path, monkeypatch):
    monkeypatch.delenv('TMUX', raising=False)
    scripts = tmp_path/'scripts'
    scripts.mkdir()
    case = 'tests/test_a.py::test_flaky'
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
    (scripts/'quarantine.json').write_text(json.dumps({
        'version': 1, 'nonblocking_tests': [case],
        'history': {digest(case): {
            'activation': 'fixture', 'file': digest('tests/test_a.py'), 'parents': [],
        }},
    }))
    git = ['git', '-C', str(tmp_path)]
    subprocess.run([*git, 'init', '-q'], check=True)
    subprocess.run([*git, 'add', 'scripts/quarantine.json'], check=True)
    subprocess.run([*git, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                    'commit', '-qm', 'Accepted quarantine'], check=True)
    subprocess.run([*git, 'update-ref', 'refs/remotes/origin/main', 'HEAD'], check=True)
    monkeypatch.setattr(run_gate, 'REPO_ROOT', tmp_path)
    monkeypatch.setattr(run_gate, '_classify', lambda **_: (None, []))
    monkeypatch.setattr(run_gate, 'summarize_progress', lambda _: {})
    monkeypatch.setattr(run_gate, '_TERMINATION_REQUESTED', False)
    def fake(command, *, env, log_path, timeout, advisory=False):
        junit = next(x.split('=', 1)[1] for x in command if x.startswith('--junitxml='))
        if advisory:
            Path(junit).write_text('<testsuite tests="2" failures="1" errors="0" skipped="0">'
                                   '<testcase classname="tests.test_a" name="test_flaky"><failure/></testcase>'
                                   '<testcase classname="tests.test_b" name="test_new"/></testsuite>')
            return 1, False
        Path(junit).write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"/>')
        manifest = next(x.split('=', 1)[1] for x in command if x.startswith('--admission-manifest='))
        Path(manifest).write_text(json.dumps({'complete': True, 'held': [{'id': 'tests/test_b.py::test_new'}]}))
        return 0, False
    monkeypatch.setattr(run_gate, '_run_process', fake)
    result = run_gate._run_tier('unit', tmp_path, 10, basetemp=tmp_path/'temp', manifest=tmp_path/'owner')
    assert result['passed'] is True
    assert result['advisory']['returncode'] == 1
    assert result['advisory']['counts']['tests'] == 2
