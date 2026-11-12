"""Regression journeys from the first lane B final-candidate review."""
import json
import os
import subprocess

import pytest

from _shared import operator_auth
from tools.live_window import core
from .test_deploy_ordinary_path import restore_case  # noqa: F401
from .test_deploy_script import deploy_mod as d, V2_SERVICE, TARGET_SHA, _boot_observed
from .test_deploy_coordinated_helpers import transaction  # noqa: F401


def test_authenticated_operator_hello_negotiates_composite_history(tmp_path):
    token = tmp_path / 'synthetic-token'
    token.write_text(operator_auth.encode_envelope('00000000-0000-4000-8000-000000000001',
        'pentacle', b'x' * operator_auth.AUTH_PROOF_BYTES))
    nonce = operator_auth.encode_b64url(b'n' * operator_auth.AUTH_NONCE_BYTES)
    hello = core.operator_hello(token, {'auth': {'operator': {'nonce': nonce}}})
    assert hello.get('capabilities', {}).get('assistant_composite_v1') is True


def test_fd_history_wait_is_small_and_spaced(monkeypatch):
    from tools import release_fd_check as check
    from .test_release_fd_check import install
    calls = install(monkeypatch, [])
    sleeps = []
    monkeypatch.setattr(check.time, 'sleep', sleeps.append)
    assert check.run_check('ws://owned.invalid', token_path=None, target_sha='a' * 40)['outcome'] == 'failed'
    history = next(call for call in calls if call['type'] == 'request_stream_events')
    assert history['limit'] <= 40 and sleeps[0] >= 2


def test_deploy_stamp_is_created_owner_only(tmp_path):
    previous = os.umask(0o022)
    try:
        d._write_stamp(tmp_path, V2_SERVICE, {'sha': TARGET_SHA})
    finally:
        os.umask(previous)
    assert d._stamp_path(tmp_path, V2_SERVICE).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('install_fails', [False, True])
def test_restore_reinstalls_prior_requirements_before_start(restore_case, monkeypatch, install_fails):
    repo, prior, stamp_path, plist, policy, tx, runtime, events, run = restore_case
    stamp = {'requirements_hash': 'target-packages'}
    def verify(repo_arg, service, installed_stamp, runner):
        assert d._git(repo, 'rev-parse', 'HEAD', runner=run) == prior
        assert stamp_path.read_bytes() == b'{}'
        assert installed_stamp['requirements_hash'] == 'target-packages'
        events.append('install-prior')
        if install_fails:
            raise d.DeployError('controlled prior package installation failure')
    monkeypatch.setattr(d, 'verify_venv', verify)
    d._restore_prior(V2_SERVICE, repo, stamp, tx, runtime, runner=run,
        verify_boot=lambda *a, **k: d.BootReadback(d.BOOT_OBSERVED, 'prior', 0, 33, None),
        verify_runtime=lambda *a: (True, {'sha': prior}))
    assert 'install-prior' in events
    boots = [i for i, event in enumerate(events) if isinstance(event, tuple)
             and event[:2] == ('launchctl', 'bootstrap')]
    if install_fails:
        assert not boots and not stamp['automatic_restore'].get('restoration_complete')
        assert stamp['disposition'] == 'target not active, restore unproven'
    else:
        assert events.index('install-prior') < boots[0]
        assert stamp['disposition'] == 'target not active, prior restored'


def test_restore_retries_launchd_rc5(restore_case):
    repo, prior, stamp_path, plist, policy, tx, runtime, events, run = restore_case
    attempts = []
    def runner(argv, cwd):
        if argv[:2] == ('launchctl', 'bootstrap'):
            attempts.append(argv)
            return subprocess.CompletedProcess(argv, 5 if len(attempts) < 3 else 0, '', '')
        return run(argv, cwd)
    stamp = {}
    d._restore_prior(V2_SERVICE, repo, stamp, tx, runtime, runner=runner,
        verify_boot=lambda *a, **k: d.BootReadback(d.BOOT_OBSERVED, 'prior', 0, 33, None),
        verify_runtime=lambda *a: (True, {'sha': prior}))
    assert len(attempts) == 3
    assert stamp['disposition'] == 'target not active, prior restored'


@pytest.mark.parametrize('applied', [False, True])
def test_explicit_coordinated_discard_and_rollback_exit_zero(transaction, monkeypatch, applied):
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    if applied:
        tx.apply(intent, path)
    result = tx.rollback(path)
    monkeypatch.setattr(d, 'deploy', lambda *a, **k: result)
    assert d.main(['chat-streamd-v2', '--rollback', '--coordinated-receipt', str(path)]) == 0
    assert 'exit_code' not in result


@pytest.mark.parametrize('raises', [False, True])
def test_ordinary_rollback_failed_restoration_report_is_failed_checks(tmp_path, monkeypatch, raises):
    monkeypatch.setattr(d, '_process_start', lambda *a: 'old')
    monkeypatch.setattr(d, '_write_stamp', lambda *a: None)
    monkeypatch.setattr(d, '_kickstart', lambda *a: None)
    notices = []
    monkeypatch.setattr(d, '_notify_failed_checks', lambda *a: notices.append(a))
    def report(*a, **k):
        if raises:
            raise RuntimeError('restoration report unavailable')
        return {'outcome': 'failed'}
    monkeypatch.setattr(d, '_verify_direct_restoration', report)
    stamp = {'verification_kind': 'restoration'}
    d._apply_post_activation(V2_SERVICE, tmp_path, TARGET_SHA, stamp, prior_log_size=0,
        prior_pid=11, reload_launchd=False, runner=lambda *a: None,
        verify_boot=_boot_observed, verify_runtime=lambda *a: (True, {'sha': TARGET_SHA}))
    assert stamp['serving'] is True
    assert stamp['disposition'] == 'active with failed checks'
    assert len(notices) == 1
