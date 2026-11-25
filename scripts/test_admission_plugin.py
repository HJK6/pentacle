import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

SCRIPTS = Path(__file__).resolve().parent


def fixture(root, suite='cli'):
    folder = {'daemon': 'chat-stream-v2', 'cli': 'agent-orch', 'shared': '_shared'}[suite]
    tests = root / 'services' / folder / 'tests'
    tests.mkdir(parents=True)
    (tests / 'test_cases.py').write_text('def test_old(): pass\ndef test_new(): assert False, "new failure"\n')
    scripts = root / 'scripts'
    scripts.mkdir()
    for name in ('quarantine_list.py', 'quarantine_plugin.py', 'quarantine_pytest.py'):
        shutil.copy(SCRIPTS / name, scripts / name)
    tools = root / 'services/chat-stream-v2/tools'
    tools.mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPTS.parent / 'services/chat-stream-v2/tools/gate_progress.py', tools / 'gate_progress.py')
    write_list(root, [])
    return tests.parent


def write_list(root, ids):
    rows = [{'id': x, 'owner': 'team', 'reason': 'fixture', 'expires': '2099-01-01'} for x in ids]
    (root / 'scripts/quarantine-v2.json').write_text(json.dumps({'version': 2, 'nonblocking_tests': rows}))


def invoke(root, suite, selection='tests', extra=()):
    folder = {'daemon': 'chat-stream-v2', 'cli': 'agent-orch', 'shared': '_shared'}[suite]
    manifest = root / 'selection.json'
    env = {**os.environ, 'PYTHONPATH': str(root / 'scripts'), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    proc = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'quarantine_plugin',
                           f'--quarantine-root={root}', f'--quarantine-suite={suite}',
                           f'--quarantine-manifest={manifest}', *extra, selection],
                          cwd=root / 'services' / folder, env=env, capture_output=True, text=True)
    return proc, json.loads(manifest.read_text())


def test_list_removal_restores_cases_with_old_source_markers(tmp_path):
    suite = fixture(tmp_path)
    file = suite / 'tests/test_cases.py'
    file.write_text("import pytest\npytestmark = pytest.mark.quarantined\n"
                    "@pytest.mark.quarantined\ndef test_restored(): assert False, 'restored'\n"
                    "def test_listed(): pass\n")
    write_list(tmp_path, ['cli::tests/test_cases.py::test_listed'])
    proc, manifest = invoke(tmp_path, 'cli', extra=('-m', 'not quarantined'))
    assert proc.returncode == 1 and 'restored' in proc.stdout
    assert manifest['blocking'] == ['tests/test_cases.py::test_restored']
    assert manifest['quarantined'] == ['tests/test_cases.py::test_listed']


def test_new_and_changed_cases_block_by_presence(tmp_path):
    fixture(tmp_path)
    proc, manifest = invoke(tmp_path, 'cli', extra=('-m', 'not quarantined'))
    assert proc.returncode == 1 and 'new failure' in proc.stdout
    assert manifest['blocking'] == ['tests/test_cases.py::test_old', 'tests/test_cases.py::test_new']
    assert manifest['quarantined'] == []


def test_candidate_quarantine_is_exact_and_removal_restores(tmp_path):
    fixture(tmp_path)
    identity = 'cli::tests/test_cases.py::test_new'
    write_list(tmp_path, [identity])
    proc, manifest = invoke(tmp_path, 'cli', extra=('-m', 'not quarantined'))
    assert proc.returncode == 0 and manifest['quarantined'] == ['tests/test_cases.py::test_new']
    write_list(tmp_path, [])
    proc, _ = invoke(tmp_path, 'cli', extra=('-m', 'not quarantined'))
    assert proc.returncode == 1 and 'new failure' in proc.stdout


def test_unreadable_inventory_preserves_blocking_execution_and_fails_incomplete(tmp_path):
    fixture(tmp_path)
    (tmp_path / 'scripts/quarantine-v2.json').write_text('{broken')
    proc, manifest = invoke(tmp_path, 'cli')
    assert proc.returncode != 0 and 'new failure' in proc.stdout
    assert manifest['complete'] is False


def test_real_wrapper_keeps_advisory_failure_out_of_blocking_verdict(tmp_path):
    suite = fixture(tmp_path)
    write_list(tmp_path, ['cli::tests/test_cases.py::test_new'])
    gate = tmp_path / 'gate'
    gate.mkdir()
    env = {**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    proc = subprocess.run([sys.executable, str(tmp_path / 'scripts/quarantine_pytest.py'), '--root', str(tmp_path),
                           '--suite', 'cli', '--junit', str(gate / 'junit.xml'), '--basetemp', str(tmp_path / 'temp'),
                           '--manifest', str(gate / 'selection.json'), '--', 'tests'], cwd=suite, env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert 'new failure' in proc.stdout and 'ADVISORY returncode: 1' in proc.stdout
    assert json.loads((gate / 'selection.json.final').read_text())['complete'] is True


def test_unit_separates_exhaustive_and_retains_out_of_directory_soak(tmp_path):
    suite = fixture(tmp_path, 'daemon')
    (suite / 'tests/test_matrix.py').write_text('import pytest\n@pytest.mark.repin_exhaustive\ndef test_matrix(): pass\n@pytest.mark.soak\ndef test_slow(): pass\n')
    proc, manifest = invoke(tmp_path, 'daemon', extra=('--collect-only', '--quarantine-separate-marker=repin_exhaustive'))
    assert proc.returncode == 0
    assert manifest['separate_tiers'] == {'tests/test_matrix.py::test_matrix': 'repin_exhaustive'}
    assert 'tests/test_matrix.py::test_slow' in manifest['blocking']


def test_expired_base_reports_membership_but_fails_candidate(tmp_path):
    fixture(tmp_path)
    write_list(tmp_path, ['cli::tests/test_cases.py::test_new'])
    path = tmp_path / 'scripts/quarantine-v2.json'
    data = json.loads(path.read_text())
    data['nonblocking_tests'][0]['expires'] = '2000-01-01'
    path.write_text(json.dumps(data))
    proc, manifest = invoke(tmp_path, 'cli', extra=('--collect-only',))
    assert proc.returncode != 0
    assert manifest['complete'] is False and manifest['error_kind'] == 'expired'
    assert manifest['quarantined'] == ['tests/test_cases.py::test_new']


def test_xdist_preserves_exact_quarantine_collection(tmp_path):
    import importlib.util
    import pytest
    if importlib.util.find_spec('xdist') is None:
        pytest.skip('xdist unavailable')
    fixture(tmp_path)
    write_list(tmp_path, ['cli::tests/test_cases.py::test_new'])
    proc, manifest = invoke(tmp_path, 'cli', extra=('-p', 'xdist.plugin', '-n', '2', '-m', 'not quarantined'))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert manifest['blocking'] == ['tests/test_cases.py::test_old']
    assert manifest['quarantined'] == ['tests/test_cases.py::test_new']


def test_blocking_failure_stays_red_with_quarantined_sibling(tmp_path):
    suite = fixture(tmp_path)
    (suite / 'tests/test_cases.py').write_text('def test_old(): assert False, "blocking failure"\ndef test_new(): assert False, "advisory failure"\n')
    write_list(tmp_path, ['cli::tests/test_cases.py::test_new'])
    proc, manifest = invoke(tmp_path, 'cli', extra=('-m', 'not quarantined'))
    assert proc.returncode == 1 and 'blocking failure' in proc.stdout
    assert manifest['quarantined'] == ['tests/test_cases.py::test_new']


def test_duplicate_quarantined_id_stays_blocking(tmp_path):
    fixture(tmp_path)
    write_list(tmp_path, ['cli::tests/test_cases.py::test_new'])
    proc, manifest = invoke(tmp_path, 'cli', selection='tests/test_cases.py', extra=('--keep-duplicates', 'tests/test_cases.py', '-m', 'not quarantined'))
    assert proc.returncode == 1 and 'QUARANTINE AMBIGUOUS, BLOCKING' in proc.stdout
    assert manifest['blocking'].count('tests/test_cases.py::test_new') == 2
    assert manifest['quarantined'] == []


def test_manifest_obeys_marker_and_keyword_scope(tmp_path):
    suite = fixture(tmp_path, 'daemon')
    with (suite / 'tests/test_cases.py').open('a') as stream:
        stream.write('\nimport pytest\n@pytest.mark.soak\ndef test_slow(): pass\n')
    proc, manifest = invoke(tmp_path, 'daemon', extra=('--collect-only', '-m', 'not soak and not quarantined', '-k', 'slow'))
    assert proc.returncode == 5
    assert manifest['blocking'] == []


def test_wrapper_preserves_blocking_result_on_advisory_timeout(tmp_path, monkeypatch, capsys):
    import importlib.util
    suite = fixture(tmp_path)
    write_list(tmp_path, ['cli::tests/test_cases.py::test_new'])
    gate = tmp_path / 'gate'
    gate.mkdir()
    spec = importlib.util.spec_from_file_location('wrapper_under_test', SCRIPTS / 'quarantine_pytest.py')
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    real_run = subprocess.run
    def run(command, **kwargs):
        if any(str(arg).endswith('.advisory') for arg in command):
            raise subprocess.TimeoutExpired(command, 60)
        return real_run(command, **kwargs)
    monkeypatch.setattr(wrapper.subprocess, 'run', run)
    monkeypatch.setenv('PYTEST_DISABLE_PLUGIN_AUTOLOAD', '1')
    monkeypatch.chdir(suite)
    rc = wrapper.main(['--root', str(tmp_path), '--suite', 'cli', '--junit', str(gate / 'junit.xml'),
                       '--basetemp', str(tmp_path / 'temp'), '--manifest', str(gate / 'selection.json'), '--', 'tests'])
    assert rc == 0 and 'ADVISORY timed out or incomplete' in capsys.readouterr().out
    assert json.loads((gate / 'selection.json.final').read_text())['complete'] is True
