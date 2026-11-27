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
