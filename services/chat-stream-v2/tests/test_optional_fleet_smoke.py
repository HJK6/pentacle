"""Optional portable hosts skip only proven offline cells; required cells gate."""
import json
import subprocess

import pytest
from tools import spawn_fleet_smoke as smoke
from .test_deploy_fleet_verdict import _rows
from .test_deploy_script import deploy_mod


def config(tmp_path):
    path = tmp_path / 'machines.json'
    path.write_text(json.dumps([
        {'name': 'primary', 'ssh_target': None},
        {'name': 'required', 'ssh_target': 'required.example.com'},
        {'name': 'portable', 'ssh_target': 'portable.example.com', 'smoke_optional': True},
    ]))
    return path


def test_scheduled_smoke_optional_offline_is_pass(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('PENTACLE_MACHINES_FILE', str(config(tmp_path)))
    monkeypatch.delenv('PENTACLE_MACHINES_JSON', raising=False)
    monkeypatch.delenv('PENTACLE_SMOKE_HOSTS', raising=False)
    monkeypatch.delenv('PENTACLE_SMOKE_EXCLUDED_HOSTS', raising=False)
    offline = _rows('portable', 'UNTESTED', 'host_unavailable')
    def complete_matrix(*args, evidence, **kwargs):
        evidence.extend(_rows('primary') + _rows('required') + offline)
        return [{**row, 'class': 'untested'} for row in offline]
    monkeypatch.setattr(smoke, 'run_matrix', complete_matrix)
    assert smoke.main([]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['status'] == 'PASS' and payload['ok']
    assert payload['skipped'][0]['outcome'] == 'SKIPPED_OFFLINE'
    assert not payload['untested']


def test_deploy_optional_offline_is_pass_with_all_required_cells(tmp_path):
    offline = _rows('portable', 'UNTESTED', 'host_unavailable')
    payload = dict(ok=True, status='PASS', failures=[], untested=[],
                   cells=_rows('primary') + _rows('required') + offline)
    result = deploy_mod._fleet_smoke_verdict(subprocess.CompletedProcess([], 0, json.dumps(payload), ''), str(config(tmp_path)))
    assert result['outcome'] == 'passed'
    assert len(result['skipped']) == 4
    assert not result['untested'] and not result['unreachable_hosts']


@pytest.mark.parametrize('host,reason,outcome', [
    ('required', 'host_unavailable', 'partial'),
    ('portable', 'quota_exhausted', 'untested'),
    ('portable', 'event', 'failed'),
])
def test_optional_policy_keeps_other_failures(tmp_path, host, reason, outcome):
    cells = _rows('primary') + _rows('required') + _rows('portable')
    bad = _rows(host, 'failed' if reason == 'event' else 'UNTESTED', reason)
    cells = [row for row in cells if row['host'] != host] + bad
    payload = dict(ok=outcome != 'failed', status='PASS', cells=cells, failures=[])
    result = deploy_mod._fleet_smoke_verdict(subprocess.CompletedProcess([], 0, json.dumps(payload), ''), str(config(tmp_path)))
    assert result['outcome'] == outcome


@pytest.mark.parametrize('value', ['true', 1, None])
def test_optional_flag_requires_boolean(tmp_path, value):
    from machines import load_machines
    path = config(tmp_path)
    rows = json.loads(path.read_text())
    rows[-1]['smoke_optional'] = value
    path.write_text(json.dumps(rows))
    with pytest.raises(ValueError, match='smoke_optional'):
        load_machines({'PENTACLE_MACHINES_FILE': str(path)})
    payload = dict(ok=True, status='PASS', cells=_rows('primary') + _rows('required') + _rows('portable'))
    result = deploy_mod._fleet_smoke_verdict(subprocess.CompletedProcess([], 0, json.dumps(payload), ''), str(path))
    assert result['outcome'] == 'untested'


def test_optional_offline_cannot_cover_missing_required_evidence(tmp_path):
    payload = dict(ok=True, status='PASS', cells=_rows('primary') + _rows('portable', 'UNTESTED', 'host_unavailable'))
    result = deploy_mod._fleet_smoke_verdict(subprocess.CompletedProcess([], 0, json.dumps(payload), ''), str(config(tmp_path)))
    assert result['outcome'] == 'untested'
    assert len(result['skipped']) == 4
    assert any(row['reason'] == 'missing_evidence' for row in result['untested'])


def test_optional_offline_cannot_cover_conflicting_failure(tmp_path):
    payload = dict(ok=True, status='PASS', cells=_rows('primary') + _rows('required') + _rows('portable', 'UNTESTED', 'host_unavailable'), failures=_rows('portable', 'failed', 'event'))
    result = deploy_mod._fleet_smoke_verdict(subprocess.CompletedProcess([], 0, json.dumps(payload), ''), str(config(tmp_path)))
    assert result['outcome'] == 'failed'
    assert not result['skipped']


@pytest.mark.parametrize('missing_host', [None, 'required', 'portable'])
def test_scheduled_missing_cell_evidence_stays_untested(tmp_path, monkeypatch, capsys, missing_host):
    monkeypatch.setenv('PENTACLE_MACHINES_FILE', str(config(tmp_path)))
    for key in ('PENTACLE_MACHINES_JSON', 'PENTACLE_SMOKE_HOSTS', 'PENTACLE_SMOKE_EXCLUDED_HOSTS'):
        monkeypatch.delenv(key, raising=False)
    rows = _rows('primary') + _rows('required') + _rows('portable')
    def incomplete_matrix(*args, evidence, **kwargs):
        if missing_host is None:
            return [dict(host='portable', provider='codex', prompt_mode='promptless',
                         **{'class': 'untested', 'reason': 'host_unavailable'})]
        evidence.extend(row for row in rows if not (row['host'] == missing_host
                        and row['provider'] == 'codex' and row['prompt_mode'] == 'promptless'))
        return []
    monkeypatch.setattr(smoke, 'run_matrix', incomplete_matrix)
    assert smoke.main([]) == smoke.EXIT_UNTESTED
    payload = json.loads(capsys.readouterr().out)
    assert payload['status'] == 'UNTESTED' and not payload['ok']
    assert any(row['reason'] == 'missing_evidence' for row in payload['untested'])
