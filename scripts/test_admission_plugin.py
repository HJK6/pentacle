import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from admission_fingerprint import python_fingerprint
from admission_policy import identity

SCRIPTS = Path(__file__).resolve().parent


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def make_fixture(root, suite_name='daemon', legacy_fail=False):
    subprocess.run(['git', 'init', '--initial-branch=main', str(root)], check=True, capture_output=True)
    git(root, 'config', 'user.name', 'test')
    git(root, 'config', 'user.email', 'test@example.test')
    folders = {'daemon': 'services/chat-stream-v2', 'cli': 'services/agent-orch'}
    tests = root/folders[suite_name]/'tests'
    tests.mkdir(parents=True)
    (tests/'test_cases.py').write_text('def test_legacy():\n    assert ' + ('False' if legacy_fail else 'True') + '\n\ndef test_new():\n    assert False, "new case executed"\n')
    suite = root/folders[suite_name]
    fp = python_fingerprint(suite, 'tests/test_cases.py::test_legacy')
    platform = 'darwin' if sys.platform == 'darwin' else 'linux'
    inventory = {'version': 1, 'base_sha': 'f3a76f479e14a390cd3d61234e1fd6e8a7d228d1',
                 'python': {suite_name: {platform: {identity(suite_name, 'tests/test_cases.py::test_legacy'): fp}}},
                 'javascript': {}}
    scripts = root/'scripts'
    scripts.mkdir()
    for name in ('admission_fingerprint.py', 'admission_inputs.py', 'admission_policy.py', 'admission_plugin.py', 'admission_pytest.py'):
        shutil.copy(SCRIPTS/name, scripts/name)
    tools = root/'services/chat-stream-v2/tools'
    tools.mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPTS.parent/'services/chat-stream-v2/tools/gate_progress.py', tools/'gate_progress.py')
    (scripts/'test-admission-inventory.json').write_text(json.dumps(inventory))
    (scripts/'test-admission-records.json').write_text('{"version":1,"records":{}}')
    (scripts/'quarantine.json').write_text('{"version":1,"nonblocking_tests":[],"history":{}}')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'baseline')
    git(root, 'update-ref', 'refs/remotes/origin/main', git(root, 'rev-parse', 'HEAD'))
    return suite


def invoke(root, suite, marker):
    manifest = root/'manifest.json'
    env = {**os.environ, 'PYTHONPATH': str(SCRIPTS), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    proc = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'admission_plugin',
                           f'--admission-root={root}', '--admission-suite=daemon', f'--admission-manifest={manifest}',
                           '-m', marker, 'tests'], cwd=suite, env=env, capture_output=True, text=True)
    return proc, json.loads(manifest.read_text())


def test_legacy_blocks_and_new_case_runs_only_in_advisory(tmp_path):
    suite = make_fixture(tmp_path)
    blocking, manifest = invoke(tmp_path, suite, 'not admission_hold and not quarantined')
    assert blocking.returncode == 0, blocking.stdout + blocking.stderr
    assert [row['id'] for row in manifest['held']] == ['tests/test_cases.py::test_new']
    advisory, _ = invoke(tmp_path, suite, 'admission_hold')
    assert advisory.returncode == 1 and 'new case executed' in advisory.stdout


def test_unreadable_inventory_preserves_blocking_execution_and_fails_incomplete(tmp_path):
    suite = make_fixture(tmp_path)
    (tmp_path/'scripts/quarantine.json').write_text('{broken')
    proc, manifest = invoke(tmp_path, suite, 'not admission_hold and not quarantined')
    assert proc.returncode != 0 and 'new case executed' in proc.stdout
    assert manifest['complete'] is False


def test_candidate_quarantine_cannot_remove_a_blocking_case(tmp_path):
    suite = make_fixture(tmp_path, legacy_fail=True)
    test = 'daemon::tests/test_cases.py::test_legacy'
    key = hashlib.sha256(test.encode()).hexdigest()
    file = hashlib.sha256(b'tests/test_cases.py').hexdigest()
    (tmp_path/'scripts/quarantine.json').write_text(json.dumps({'version': 1, 'nonblocking_tests': [test],
        'history': {key: {'activation': 'candidate', 'file': file, 'parents': []}}}))
    proc, manifest = invoke(tmp_path, suite, 'not admission_hold and not quarantined')
    assert proc.returncode == 1
    assert 'test_legacy' in proc.stdout
    assert [row['id'] for row in manifest['held']] == ['tests/test_cases.py::test_new']
    assert 'PENDING, NOT ON BASE' in proc.stderr


def test_real_wrapper_keeps_new_failure_advisory_and_legacy_failure_blocking(tmp_path):
    for failing in (False, True):
        root = tmp_path / ('bad' if failing else 'good')
        suite = make_fixture(root, 'cli', legacy_fail=failing)
        env = {**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
        receipt = root/'gate'/'pytest.junit.xml'
        receipt.parent.mkdir()
        command = [sys.executable, str(root/'scripts/admission_pytest.py'), '--root', str(root), '--suite', 'cli',
                   '--junit', str(receipt), '--basetemp', str(root/'tmp'), '--manifest', str(root/'gate/admission.json'), '--', 'tests']
        proc = subprocess.run(command, cwd=suite, env=env, capture_output=True, text=True)
        assert proc.returncode == (1 if failing else 0), proc.stdout + proc.stderr
        assert 'new case executed' in proc.stdout
        assert (receipt.parent/'pytest-advisory.junit.xml').is_file()
        assert json.loads((receipt.parent/'admission.json.final').read_text())['complete'] is True


def test_daemon_unreadable_quarantine_still_executes_blocking_test(tmp_path):
    suite = tmp_path / 'services/chat-stream-v2'
    tests = suite / 'tests'
    tests.mkdir(parents=True)
    shutil.copy(SCRIPTS.parent / 'services/chat-stream-v2/tests/conftest.py', tests / 'conftest.py')
    (tests / 'capabilities.py').write_text('')
    (suite / 'satellite.py').write_text('DEFAULT_CHAT_CHECKPOINT_PATH = None\n')
    (tests / 'test_live.py').write_text('def test_blocking():\n    print("BLOCKING EXECUTED")\n')
    env = {**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    proc = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-s', 'tests/test_live.py'],
                          cwd=suite, env=env, capture_output=True, text=True)
    assert proc.returncode != 0
    assert 'BLOCKING EXECUTED' in proc.stdout
    assert 'quarantine inventory unavailable' in proc.stderr


def test_unit_separates_exhaustive_but_admits_soak_cases_outside_the_soak_directory(tmp_path):
    suite = make_fixture(tmp_path)
    tests = suite / "tests"
    (tests / "test_matrix.py").write_text(
        "import pytest\npytestmark = pytest.mark.repin_exhaustive\n"
        "def test_matrix(): raise AssertionError('exhaustive ran in unit')\n")
    with (tests / "test_cases.py").open("a") as stream:
        stream.write("\nimport pytest\n@pytest.mark.soak\ndef test_slow(): pass\n")
    manifest = tmp_path / "selection.json"
    cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests", "-m",
           "quarantined or admission_hold", "-p", "admission_plugin",
           "--admission-root=" + str(tmp_path), "--admission-suite=daemon",
           "--admission-manifest=" + str(manifest),
           "--admission-separate-marker=repin_exhaustive"]
    env = {**os.environ, "PYTHONPATH": str(SCRIPTS), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    proc = subprocess.run(cmd, env=env, cwd=suite, text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(manifest.read_text())
    assert result["separate_tiers"] == {
        "tests/test_matrix.py::test_matrix": "repin_exhaustive",
    }
    assert {row["id"] for row in result["held"]} == {"tests/test_cases.py::test_new", "tests/test_cases.py::test_slow"}
    assert "tests/test_cases.py::test_slow" in proc.stdout.splitlines()
    assert "tests/test_cases.py::test_new" in proc.stdout.splitlines()
    assert "tests/test_matrix.py::test_matrix" not in proc.stdout.splitlines()
