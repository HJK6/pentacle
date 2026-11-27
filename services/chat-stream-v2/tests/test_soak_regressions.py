"""Regressions from the 2026-11-13 nightly soak receipt."""
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys

import pytest

from .test_deploy_script import deploy_mod

SERVICE = Path(__file__).resolve().parents[1]


def test_check_release_script_imports_shared_in_fresh_process(tmp_path):
    services = tmp_path / 'services'
    service = services / 'chat-stream-v2'
    shutil.copytree(SERVICE / 'deploy', service / 'deploy')
    shutil.copytree(SERVICE / 'tools', service / 'tools')
    shutil.copytree(SERVICE.parent / '_shared', services / '_shared')
    shutil.copy(SERVICE / 'managed_logs.py', service / 'managed_logs.py')
    with (service / 'deploy/deploy.py').open('a') as out:
        out.write("""\n
class FixtureSocket:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def recv(self, **kwargs): return json.dumps({'type': 'welcome', 'runtime_sha': 'a' * 40})
def connect(*args, **kwargs): return FixtureSocket()
def _launchd_state(*args): return LaunchdState(loaded=True, pid=1)
def _process_start(*args): return 'fixture'
def _capture_log_offsets(*args, **kwargs): return {}
def _daemon_log_paths(*args): return ()
def _managed_log_config(*args): return None
def _scan_slow_consumer_window(*args, **kwargs): return {'outcome': 'passed'}
def _check_client_delivery(*args): return {'outcome': 'passed'}
def _client_delivery_cause(*args): return None
""")
    env = dict(os.environ, PYTHONPATH='')
    result = subprocess.run([sys.executable, str(service / 'tools/nightly_journey.py'), 'check-release',
                             '--checkout', str(tmp_path), '--evidence', str(tmp_path / 'evidence')],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((tmp_path / 'evidence/receipt.json').read_text())['source_sha'] == 'a' * 40


@pytest.mark.parametrize(('change', 'expected'), [
    ('local-settings', 'passed'),
    ('test-prompt', 'failed'),
])
def test_post_release_profile_drift_tracks_only_test_prompt_content(tmp_path, monkeypatch, change, expected):
    from _shared.spawn_profiles import MODELS, _profile_digest

    providers = {name: None for name in MODELS}
    stamped = {
        'schema': 'pentacle.test-profile.v1',
        'providers': providers,
        'profile_sha256': _profile_digest(providers),
        'settings_sha256': {'spawn_defaults.json': '0' * 64, 'spawn_defaults.local.json': '1' * 64},
        'resolver_sha256': '2' * 64,
    }
    live = json.loads(json.dumps(stamped))
    if change == 'local-settings':
        live['settings_sha256']['spawn_defaults.local.json'] = '3' * 64
    else:
        live['providers']['codex'] = {'model': 'gpt-6-luna', 'effort': 'low'}
        live['profile_sha256'] = _profile_digest(live['providers'])

    d = deploy_mod
    monkeypatch.setattr(d, '_launchd_state', lambda *_: d.LaunchdState(loaded=True, pid=222))
    monkeypatch.setattr(d, '_capture_log_offsets', lambda *_, **__: {})
    monkeypatch.setattr(d, '_daemon_log_paths', lambda *_: ())
    monkeypatch.setattr(d, '_release_machines_file', lambda *_: str(tmp_path / 'machines.json'))
    monkeypatch.setattr(d, '_declared_release_scope', lambda *_: [])
    monkeypatch.setattr(d, '_release_test_profile', lambda *_, **__: live)
    monkeypatch.setattr(d, '_fleet_smoke_verdict', lambda *_, **__: {'outcome': 'passed'})
    monkeypatch.setattr(d, '_process_start', lambda *_: 'fixture-start')
    monkeypatch.setattr(d, '_scan_slow_consumer_window', lambda *_, **__: {'outcome': 'passed'})
    monkeypatch.setattr(d, '_smoke_payload', lambda *_: {'outcome': 'passed'})
    monkeypatch.setattr(d, '_notify_failed_checks', lambda *_: None)
    runner = lambda argv, cwd: subprocess.CompletedProcess(argv, 0, '{}', '')

    checks = d.run_post_release_checks(
        d.SERVICES['chat-streamd-v2'], tmp_path, 'a' * 40,
        {'test_profile': stamped}, runner=runner,
    )

    assert checks['fleet_smoke']['outcome'] == expected
    if change == 'test-prompt':
        assert 'test_prompt profile drift before canary' in checks['fleet_smoke']['detail']


def test_receipt_keepalive_lapses_are_excluded():
    lines = (SERVICE / 'tests/fixtures/soak_keepalive_closes.log').read_text().splitlines()
    scan = deploy_mod.classify_slow_consumer_lines(lines)
    assert scan['outcome'] == 'passed'
    assert scan['excluded_unauthenticated_keepalive_count'] == 2
    assert scan['excluded_unauthenticated_keepalive_conn_ids'] == [
        json.loads(line.split('conn_diag ', 1)[1])['conn_id'] for line in lines]


@pytest.mark.parametrize('patch', [
    {'auth_state': 'accepted'}, {'queue_depth': 1}, {'queue_peak': 1},
    {'close_code': 1000}, {'close_reason': 'other'}, {'queue_depth': None},
])
def test_keepalive_exclusion_requires_all_fields(patch):
    line = (SERVICE / 'tests/fixtures/soak_keepalive_closes.log').read_text().splitlines()[0]
    record = json.loads(line.split('conn_diag ', 1)[1])
    record.update(patch)
    scan = deploy_mod.classify_slow_consumer_lines(['conn_diag ' + json.dumps(record)])
    assert scan['failure_count'] == 1
    assert scan['excluded_unauthenticated_keepalive_count'] == 0


def test_keepalive_exclusions_require_consistent_connection_evidence():
    lines = (SERVICE / 'tests/fixtures/soak_keepalive_closes.log').read_text().splitlines()
    first = json.loads(lines[0].split('conn_diag ', 1)[1])
    conflict = {**first, 'queue_peak': 1}
    auth_ok = {'schema': 1, 'event': 'auth_ok', 'conn_id': first['conn_id']}
    for extra in (conflict, auth_ok):
        scan = deploy_mod.classify_slow_consumer_lines([lines[0], 'conn_diag ' + json.dumps(extra)])
        assert scan['failure_count'] == 1
        assert scan['excluded_unauthenticated_keepalive_count'] == 0
    scan = deploy_mod.classify_slow_consumer_lines(lines, max_examples=1)
    assert scan['excluded_unauthenticated_keepalive_count'] == 2
    assert len(scan['excluded_unauthenticated_keepalive_conn_ids']) == 2
    assert len(scan['excluded_unauthenticated_keepalives']) == 1
