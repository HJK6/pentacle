import json
import base64

import pytest

from agent_orch import reliability_hosted
from agent_orch.reliability_selection import effective_tests


def test_effective_list_is_authoritative(tmp_path):
    path = tmp_path / 'scripts' / 'quarantine-v2.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'version': 2, 'nonblocking_tests': [{'id': 'daemon::tests/test_a.py::test_one', 'owner': 'team', 'reason': 'flake', 'expires': '2027-01-01'}]}))
    assert effective_tests(tmp_path) == {'daemon::tests/test_a.py::test_one'}


def test_invalid_list_fails_closed_instead_of_legacy_fallback(tmp_path):
    path = tmp_path / 'scripts' / 'quarantine-v2.json'
    path.parent.mkdir()
    path.write_text('{broken')
    with pytest.raises(ValueError):
        effective_tests(tmp_path)


def test_git_checkout_reads_public_main_not_stale_local_list(tmp_path, monkeypatch):
    legacy = tmp_path / 'scripts' / 'quarantine.json'
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({'version': 1, 'nonblocking_tests': []}))
    (tmp_path / '.git').mkdir()
    remote = {'version': 2, 'nonblocking_tests': [{'id': 'daemon::tests/test_a.py::test_one', 'owner': 'team', 'reason': 'flake', 'expires': '2027-01-01'}]}
    def get(repo, route):
        assert (repo, route) == ('HJK6/pentacle', 'contents/scripts/quarantine-v2.json?ref=main')
        return json.dumps({'encoding': 'base64', 'content': base64.b64encode(json.dumps(remote).encode()).decode()}).encode()
    monkeypatch.setattr(reliability_hosted, 'download', get)
    assert effective_tests(tmp_path) == {'daemon::tests/test_a.py::test_one'}

    def unavailable(*_):
        raise ValueError('GitHub GET unavailable')
    monkeypatch.setattr(reliability_hosted, 'download', unavailable)
    with pytest.raises(ValueError, match='unavailable'):
        effective_tests(tmp_path)


def test_v2_rows_and_invalid_metadata(tmp_path):
    path = tmp_path / 'scripts' / 'quarantine-v2.json'
    path.parent.mkdir()
    row = {'id': 'daemon::tests/test_a.py::test_one', 'owner': 'team',
           'reason': 'intermittent fixture', 'expires': '2027-01-01'}
    path.write_text(json.dumps({'version': 2, 'nonblocking_tests': [row]}))
    assert effective_tests(tmp_path) == {row['id']}
    path.write_text(json.dumps({'version': 2, 'nonblocking_tests': [row, row]}))
    with pytest.raises(ValueError):
        effective_tests(tmp_path)
    path.write_text(json.dumps({'version': 2, 'nonblocking_tests': [{**row, 'expires': 'invalid'}]}))
    with pytest.raises(ValueError):
        effective_tests(tmp_path)


@pytest.mark.parametrize("identity", ["test/web.test.js::case", "daemon::oops", "cli::../x.py::case", "daemon::/x.py::case"])
def test_v2_rejects_non_python_or_malformed_identity(tmp_path, identity):
    path = tmp_path / 'scripts' / 'quarantine-v2.json'
    path.parent.mkdir()
    row = {'id': identity, 'owner': 'team', 'reason': 'flake', 'expires': '2020-01-01'}
    path.write_text(json.dumps({'version': 2, 'nonblocking_tests': [row]}))
    with pytest.raises(ValueError):
        effective_tests(tmp_path)


def test_expired_row_remains_visible_as_repair_signal(tmp_path):
    path = tmp_path / 'scripts' / 'quarantine-v2.json'
    path.parent.mkdir()
    row = {'id': 'daemon::tests/test_a.py::test_one', 'owner': 'team', 'reason': 'flake', 'expires': '2020-01-01'}
    path.write_text(json.dumps({'version': 2, 'nonblocking_tests': [row]}))
    assert effective_tests(tmp_path) == {row['id']}
