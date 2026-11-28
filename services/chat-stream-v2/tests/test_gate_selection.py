from pathlib import Path

from tools.gate_selection import resolve
from tools.run_gate import MERGE_TIERS


def test_resolves_import_chain_changed_test_table_and_unmapped(tmp_path):
    service = tmp_path / 'services/chat-stream-v2'
    (service / 'tests').mkdir(parents=True)
    (service / 'leaf.py').write_text('VALUE = 1\n')
    (service / 'middle.py').write_text('import leaf\n')
    (service / 'top.py').write_text('from middle import VALUE\n')
    (service / 'tests/test_chain.py').write_text('import top\n')
    (service / 'tests/test_changed.py').write_text('pass\n')
    (service / 'tests/test_link.py').write_text('pass\n')
    prefix = 'services/chat-stream-v2/'
    selected, unmapped = resolve(tmp_path, [prefix + 'leaf.py', prefix + 'tests/test_changed.py',
                                          prefix + 'data.json', 'other.txt'],
                                 {prefix + 'data.json': ['tests/test_link.py']})
    assert selected == ['tests/test_chain.py', 'tests/test_changed.py', 'tests/test_link.py']
    assert unmapped == ['other.txt']
    assert MERGE_TIERS == ('contract', 'unit', 'smoke')


def test_bare_helper_package_init_and_conftest_select_tests(tmp_path):
    service = tmp_path / 'services/chat-stream-v2'
    (service / 'tests').mkdir(parents=True)
    (service / 'pkg').mkdir()
    (service / 'tests/extra').mkdir()
    (service / 'pkg/__init__.py').write_text('VALUE = 1\n')
    (service / 'tests/helpers.py').write_text('VALUE = 1\n')
    (service / 'tests/conftest.py').write_text('')
    (service / 'tests/extra/nested.py').write_text('VALUE = 1\n')
    (service / 'tests/extra/bridge.py').write_text('from nested import VALUE\n')
    (service / 'tests/test_nested.py').write_text('from extra import bridge\n')
    (service / 'tests/test_helper.py').write_text('from helpers import VALUE\n')
    (service / 'tests/test_package.py').write_text('import pkg\n')
    prefix = 'services/chat-stream-v2/'
    assert resolve(tmp_path, [prefix + 'tests/helpers.py'])[0] == ['tests/test_helper.py']
    assert resolve(tmp_path, [prefix + 'pkg/__init__.py'])[0] == ['tests/test_package.py']
    assert resolve(tmp_path, [prefix + 'tests/extra/nested.py'])[0] == ['tests/test_nested.py']
    assert resolve(tmp_path, [prefix + 'tests/conftest.py'])[0] == [
        'tests/test_helper.py', 'tests/test_nested.py', 'tests/test_package.py']


def test_selected_empty_shard_passes_through_gate(tmp_path):
    import json
    import os
    import subprocess
    import sys

    source = Path(__file__).resolve().parents[3]
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'clone', '--quiet', '--shared', str(source), str(repo)], check=True)
    subprocess.run(['git', 'update-ref', 'refs/remotes/origin/main', 'HEAD'], cwd=repo, check=True)
    test = repo / 'services/chat-stream-v2/tests/test_shard_fixture.py'
    test.write_text('def test_only(): pass\n')
    subprocess.run(['git', 'add', str(test.relative_to(repo))], cwd=repo, check=True)
    subprocess.run(['git', '-c', 'user.name=Test Agent', '-c', 'user.email=test@example.invalid',
                    'commit', '-qm', 'Add one selected test'], cwd=repo, check=True)
    env = os.environ.copy()
    env['V2_PYTHON_BIN'] = sys.executable
    empty = 0
    for shard in range(2):
        evidence = tmp_path / f'evidence-{shard}'
        result = subprocess.run([sys.executable, str(repo / 'services/chat-stream-v2/tools/run_gate.py'),
                                 'unit', '--changed-tests', '--shard-index', str(shard), '--shard-count', '2',
                                 '--evidence-dir', str(evidence)], cwd=repo, env=env, text=True,
                                capture_output=True, timeout=45)
        assert result.returncode == 0, result.stdout + result.stderr
        empty += 'no selected tests for this shard' in result.stdout
        assert json.loads((evidence / 'v2-unit-evidence.json').read_text())['passed'] is True
    assert empty == 1


def test_unselected_empty_unit_collection_fails(tmp_path):
    import json
    import os
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[3]
    evidence = tmp_path / 'empty-evidence'
    env = os.environ.copy()
    env['V2_PYTHON_BIN'] = sys.executable
    env['PYTEST_ADDOPTS'] = '--ignore-glob=tests/test_*.py'
    result = subprocess.run([sys.executable, str(repo / 'services/chat-stream-v2/tools/run_gate.py'),
                             'unit', '--evidence-dir', str(evidence)], cwd=repo, env=env, text=True,
                            capture_output=True, timeout=45)
    assert result.returncode != 0, result.stdout + result.stderr
    report = json.loads((evidence / 'v2-unit-evidence.json').read_text())
    assert report['passed'] is False
    assert report['tiers'][0]['reason'] == 'no_tests_collected'
