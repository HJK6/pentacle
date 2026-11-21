import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from admission_fingerprint import javascript_fingerprint, python_fingerprint


def test_fixture_change_only_invalidates_its_dependents_and_selector_change_does_not(tmp_path):
    root = tmp_path / 'service'
    tests = root / 'tests'
    tests.mkdir(parents=True)
    fixture = tests / 'conftest.py'
    fixture.write_text('import pytest\n@pytest.fixture\ndef alpha():\n    return 1\n@pytest.fixture\ndef beta():\n    return 2\n')
    (tests / 'test_a.py').write_text('def test_one(alpha):\n    assert alpha\n\ndef test_two(beta):\n    assert beta\n')
    one = python_fingerprint(root, 'tests/test_a.py::test_one')
    two = python_fingerprint(root, 'tests/test_a.py::test_two')
    fixture.write_text(fixture.read_text().replace('return 1', 'return 3'))
    assert python_fingerprint(root, 'tests/test_a.py::test_one') != one
    assert python_fingerprint(root, 'tests/test_a.py::test_two') == two
    selector = tmp_path / 'selector.py'
    selector.write_text('changed selector')
    assert python_fingerprint(root, 'tests/test_a.py::test_two') == two


def test_daemon_selector_session_hook_does_not_change_fingerprint(tmp_path):
    root = tmp_path/'chat-stream-v2'
    tests = root/'tests'
    tests.mkdir(parents=True)
    conf = tests/'conftest.py'
    conf.write_text('def pytest_configure(config):\n    pass\n')
    (tests/'test_a.py').write_text('def test_one():\n    assert True\n')
    before = python_fingerprint(root, 'tests/test_a.py::test_one')
    conf.write_text(conf.read_text() + '\ndef pytest_sessionfinish(session, exitstatus):\n    pass\n')
    assert python_fingerprint(root, 'tests/test_a.py::test_one') == before
    conf.write_text(conf.read_text() + '\ndef pytest_runtest_setup(item):\n    pass\n')
    assert python_fingerprint(root, 'tests/test_a.py::test_one') != before


def test_param_case_identity_is_bound_and_missing_test_is_not_grandfathered(tmp_path):
    tests = tmp_path / 'tests'
    tests.mkdir()
    (tests / 'test_a.py').write_text('def test_one(value):\n    assert value\n')
    assert python_fingerprint(tmp_path, 'tests/test_a.py::test_one[a]') != python_fingerprint(tmp_path, 'tests/test_a.py::test_one[b]')
    with pytest.raises(ValueError):
        python_fingerprint(tmp_path, 'tests/test_a.py::missing')


def test_fixture_helper_is_resolved_in_its_own_module(tmp_path):
    tests = tmp_path / 'tests'
    tests.mkdir()
    conf = tests / 'conftest.py'
    conf.write_text('import pytest\ndef helper():\n    return 1\n@pytest.fixture\ndef fixture():\n    return helper()\n')
    (tests / 'test_a.py').write_text('def helper():\n    return 9\ndef test_one(fixture):\n    assert fixture\n')
    original = python_fingerprint(tmp_path, 'tests/test_a.py::test_one')
    conf.write_text(conf.read_text().replace('return 1', 'return 2'))
    assert python_fingerprint(tmp_path, 'tests/test_a.py::test_one') != original


def test_javascript_file_fingerprint_tracks_declared_fixture(tmp_path):
    tests = tmp_path / 'test'
    tests.mkdir()
    fixture = tests / 'helper.js'
    fixture.write_text('module.exports = 1;')
    (tests / 'test_a.js').write_text("require('./helper');")
    original = javascript_fingerprint(tmp_path, 'test/test_a.js')
    fixture.write_text('module.exports = 2;')
    assert javascript_fingerprint(tmp_path, 'test/test_a.js') != original


def test_imported_fixture_change_invalidates_only_its_users(tmp_path):
    tests = tmp_path / 'tests'
    tests.mkdir()
    helper = tests / 'fixture_helper.py'
    helper.write_text('import pytest\n@pytest.fixture\ndef fixture():\n    return 1\n')
    (tests / 'test_a.py').write_text('from fixture_helper import fixture\ndef test_one(fixture):\n    assert fixture\ndef test_two():\n    assert True\n')
    one = python_fingerprint(tmp_path, 'tests/test_a.py::test_one')
    two = python_fingerprint(tmp_path, 'tests/test_a.py::test_two')
    helper.write_text(helper.read_text().replace('return 1', 'return 2'))
    assert python_fingerprint(tmp_path, 'tests/test_a.py::test_one') != one
    assert python_fingerprint(tmp_path, 'tests/test_a.py::test_two') == two


def test_class_and_module_declared_fixtures_are_fingerprinted(tmp_path):
    tests = tmp_path/'tests'
    tests.mkdir()
    conf = tests/'conftest.py'
    conf.write_text('import pytest\n@pytest.fixture\ndef service():\n    return 1\n')
    (tests/'test_a.py').write_text('import pytest\n@pytest.mark.usefixtures("service")\nclass TestSuite:\n    def test_case(self):\n        assert True\n')
    original = python_fingerprint(tmp_path, 'tests/test_a.py::TestSuite::test_case')
    conf.write_text(conf.read_text().replace('return 1', 'return 2'))
    assert python_fingerprint(tmp_path, 'tests/test_a.py::TestSuite::test_case') != original


def test_javascript_declared_fixture_paths_and_directories_are_inputs(tmp_path):
    tests = tmp_path/'test'
    fixture_dir = tests/'fixtures/data'
    fixture_dir.mkdir(parents=True)
    item = fixture_dir/'one.json'
    item.write_text('{}')
    (tests/'case.test.js').write_text("const p = require('path'); p.join(__dirname, 'fixtures', 'data');")
    original = javascript_fingerprint(tmp_path, 'test/case.test.js')
    item.write_text('{\"changed\":true}')
    assert javascript_fingerprint(tmp_path, 'test/case.test.js') != original
