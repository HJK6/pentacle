"""Regressions from the 2026-11-13 nightly soak receipt."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from .test_deploy_script import deploy_mod

SERVICE = Path(__file__).resolve().parents[1]


def test_check_release_script_imports_shared_for_test_profile(tmp_path):
    # Copy the actual entry point and deploy implementation into a disposable tree.
    services = tmp_path / 'services'
    service = services / 'chat-stream-v2'
    shutil.copytree(SERVICE / 'deploy', service / 'deploy')
    shutil.copytree(SERVICE / 'tools', service / 'tools')
    shutil.copytree(SERVICE.parent / '_shared', services / '_shared')
    shutil.copy(SERVICE / 'managed_logs.py', service / 'managed_logs.py')
    # Stub all process and daemon adapters, while retaining the real release checks.
    with (service / 'deploy/deploy.py').open('a') as out:
        out.write('''\n
def release_exclusions(*args): return []
def _launchd_state(*args): return LaunchdState(loaded=True, pid=1)
def _capture_log_offsets(*args): return {}
def _daemon_log_paths(*args): return ()
def _release_machines_file(*args): return "fixture"
def _declared_release_scope(*args): return []
def _fleet_smoke_verdict(*args, **kwargs): return {"outcome": "passed"}
def _process_start(*args): return "fixture"
def _scan_slow_consumer_window(*args, **kwargs): return {"outcome": "passed"}
def _notify_failed_checks(*args): pass
def _run(argv, cwd):
    profile = json.loads((cwd / ".pentacle-deploy/chat-streamd-v2.json").read_text())["test_profile"]
    return subprocess.CompletedProcess(argv, 0, json.dumps({"test_profile": profile, "outcome": "passed"}), "")
''')
    from _shared.spawn_profiles import MODELS, _profile_digest
    pairs = {name: None for name in MODELS}
    profile = {'schema': 'pentacle.test-profile.v1', 'providers': pairs,
               'profile_sha256': _profile_digest(pairs),
               'settings_sha256': {'spawn_defaults.json': '0' * 64, 'spawn_defaults.local.json': None},
               'resolver_sha256': '0' * 64}
    stamps = tmp_path / '.pentacle-deploy'
    stamps.mkdir()
    (stamps / 'chat-streamd-v2.json').write_text(json.dumps({'sha': 'a' * 40, 'test_profile': profile}))
    env = dict(os.environ, PYTHONPATH='')
    result = subprocess.run([sys.executable, str(service / 'tools/nightly_journey.py'), 'check-release',
                             '--checkout', str(tmp_path), '--evidence', str(tmp_path / 'evidence')],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((tmp_path / 'evidence/receipt.json').read_text())['result'] == 'PASS'


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
