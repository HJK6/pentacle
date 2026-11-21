import json
import base64

import pytest

from agent_orch import reliability_hosted
from agent_orch.reliability_selection import effective_tests


def test_effective_list_is_authoritative(tmp_path):
    path = tmp_path / 'scripts' / 'quarantine.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'version': 1, 'nonblocking_tests': ['tests/test_a.py::test_one', 'test/web.test.js::a test']}))
    assert effective_tests(tmp_path) == {'tests/test_a.py::test_one', 'test/web.test.js::a test'}


def test_invalid_list_fails_closed_instead_of_legacy_fallback(tmp_path):
    path = tmp_path / 'scripts' / 'quarantine.json'
    path.parent.mkdir()
    path.write_text('{broken')
    with pytest.raises(ValueError):
        effective_tests(tmp_path)


def test_git_checkout_reads_public_main_not_stale_local_list(tmp_path, monkeypatch):
    inventory = tmp_path / 'scripts' / 'quarantine.json'
    inventory.parent.mkdir()
    inventory.write_text(json.dumps({'version': 1, 'nonblocking_tests': []}))
    (tmp_path / '.git').mkdir()
    remote = {'version': 1, 'nonblocking_tests': ['tests/test_a.py::test_one']}
    def get(repo, route):
        assert (repo, route) == ('HJK6/pentacle', 'contents/scripts/quarantine.json?ref=main')
        return json.dumps({'encoding': 'base64', 'content': base64.b64encode(json.dumps(remote).encode()).decode()}).encode()
    monkeypatch.setattr(reliability_hosted, 'download', get)
    assert effective_tests(tmp_path) == {'tests/test_a.py::test_one'}

    def unavailable(*_):
        raise ValueError('GitHub GET unavailable')
    monkeypatch.setattr(reliability_hosted, 'download', unavailable)
    with pytest.raises(ValueError, match='unavailable'):
        effective_tests(tmp_path)
