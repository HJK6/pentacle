"""Offline rollback guard and real-Git activation regressions; no live service."""
from __future__ import annotations

import json
import plistlib
import sqlite3
import subprocess

import pytest

from store import SESSIONS_DDL
from . import test_deploy_log_guard as fixtures
from .test_deploy_log_guard import isolated_release


def seed_db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(SESSIONS_DDL)
        conn.execute("CREATE TABLE v2_schedules (state TEXT)")
        conn.execute("INSERT INTO sessions(host,session_name,role,visibility,created_at) "
                     "VALUES ('hostx','deployer','deployer','private','2026-01-01')")
    return path


@pytest.fixture
def guard_case(tmp_path, monkeypatch):
    module = fixtures._load_deploy(tmp_path / 'home', monkeypatch)
    service = module.SERVICES['chat-streamd-v2']
    db = seed_db(tmp_path / 'sessions.db')
    monkeypatch.setattr(module, '_launchd_plist', lambda _: {'ProgramArguments': ['python', 'main.py', '--db', str(db)]})
    monkeypatch.setattr(module, '_launchd_state', lambda *_: module.LaunchdState(True, None))
    monkeypatch.setattr(module, '_resolve_agent_orch', lambda *_: 'agent-orch')
    monkeypatch.setattr(module, '_read_stamp', lambda *_: {})
    monkeypatch.setattr(module, '_git', lambda *_a, **_k: 'a' * 40)
    calls = []
    def runner(command, cwd):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 67, '', 'snapshot unavailable')
    def check(**kwargs):
        return module.verify_deploy_guard(service, 'hostx:deployer', runner,
            repo=tmp_path, sha='b' * 40, legacy_schedules_db=tmp_path / 'absent.db', **kwargs)
    return module, db, calls, check


def test_rollback_guard_works_without_daemon(guard_case):
    module, db, calls, check = guard_case
    result = check(rollback=True)
    assert result['daemon_state'] == 'down'
    assert result['pre_restart_pid'] is None
    assert result['sole_deployer'] is True
    assert calls == []


@pytest.fixture
def capture_guard(monkeypatch):
    captured = {}
    load = fixtures._load_deploy
    def capture(*args):
        module = load(*args)
        captured['guard'] = module.verify_deploy_guard
        return module
    monkeypatch.setattr(fixtures, '_load_deploy', capture)
    return captured


@pytest.fixture
def rollback_release(capture_guard, isolated_release, tmp_path, monkeypatch):
    module, service, repo, prior, target, before, calls, activate = isolated_release
    monkeypatch.setattr(module, 'verify_deploy_guard', capture_guard['guard'])
    monkeypatch.setattr(module, '_resolve_agent_orch', lambda *_: 'agent-orch')
    monkeypatch.setattr(module, '_launchd_state', lambda *_: module.LaunchdState(True, None))
    db = seed_db(tmp_path / 'sessions.db')
    plist_path = module._launchd_plist_path(service.launchd_label)
    plist = plistlib.loads(plist_path.read_bytes())
    plist['ProgramArguments'] += ['--db', str(db)]
    plist_path.write_bytes(plistlib.dumps(plist))
    monkeypatch.setenv('PENTACLE_STREAM_SCHEDULES_DB', str(tmp_path / 'absent.db'))
    stamp = module._read_stamp(repo, service)
    stamp.update(sha=prior, prior_sha=target, daemon_boot_readback={'outcome': 'boot_failed'},
                 launchd_plist_digest=module._launchd_plist_managed_digest(service.launchd_label))
    module._write_stamp(repo, service, stamp)
    deploy = module.deploy
    def invoke(*args, runner, **kwargs):
        kwargs["deployer_stream_id"] = "hostx:deployer"
        def fake(command, cwd):
            if command[0] == 'agent-orch':
                calls.append(tuple(command))
                return subprocess.CompletedProcess(command, 67, '', 'snapshot unavailable')
            if command[0] == 'launchctl':
                calls.append(tuple(command))
                disk = module._read_stamp(repo, service)
                assert disk['restart_guard']['daemon_state'] == 'down'
                assert disk['restart_activation']['pre_restart_pid'] is None
                assert disk['restart_activation']['daemon_state'] == 'down'
                return subprocess.CompletedProcess(command, 0, '', '')
            return runner(command, cwd)
        return deploy(*args, runner=fake, **kwargs)
    monkeypatch.setattr(module, 'deploy', invoke)
    return module, service, repo, prior, target, calls, activate


@pytest.mark.parametrize("answered", [True, False])
def test_boot_failed_candidate_can_activate_stamped_prior(rollback_release, monkeypatch, answered):
    module, service, repo, prior, target, calls, activate = rollback_release
    monkeypatch.setattr(module, '_launchd_state', lambda *_: module.LaunchdState(True, None, answered))
    result = activate(rollback=True)
    assert result['sha'] == target
    assert module._git(repo, 'rev-parse', 'HEAD', runner=module._run) == target
    assert result['restart_guard']['daemon_state'] == 'down'
    assert result['restart_activation']['daemon_state'] == 'down'
    assert result['restart_activation']['pre_restart_pid'] is None
    assert result['restart_activation']['pre_restart_launchd_answered'] is answered
    assert result['restart_guard']['launchd_answered'] is answered
    assert result['restart_guard']['down_basis'] == ('launchd_answered_no_pid' if answered else 'boot_failed_stamp')
    assert any(c[:2] == ('launchctl', 'kickstart') for c in calls)
    assert not any(c[0] == 'agent-orch' for c in calls)


@pytest.mark.parametrize("rollback,state", [(False, (True, None)), (True, (True, 111)), (True, (False, None, False))])
def test_no_offline_bypass_without_down_rollback(guard_case, monkeypatch, rollback, state):
    module, db, calls, check = guard_case
    monkeypatch.setattr(module, '_launchd_state', lambda *_: module.LaunchdState(*state))
    with pytest.raises(module.DeployError, match="command failed"):
        check(rollback=rollback)
    assert calls == [('agent-orch', 'list')]


def test_other_open_deployer_refuses(guard_case):
    module, db, calls, check = guard_case
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO sessions(host,session_name,role,visibility,created_at) "
                     "VALUES ('hosty','deployer','deployer','private','2026-01-01')")
    with pytest.raises(module.DeployError, match="other open deployer"):
        check(rollback=True)
    assert calls == []


@pytest.mark.parametrize("kind", ['missing', 'corrupt'])
def test_unreadable_sessions_refuses_without_creating_db(guard_case, kind):
    module, db, calls, check = guard_case
    db.unlink()
    if kind == 'corrupt':
        db.write_text('not sqlite')
    with pytest.raises(module.DeployError, match="sessions DB readback failed"):
        check(rollback=True)
    assert db.exists() == (kind == 'corrupt')
    assert calls == []


def test_nonterminal_schedule_refuses(guard_case):
    module, db, calls, check = guard_case
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO v2_schedules VALUES ('pending')")
    with pytest.raises(module.DeployError, match="nonterminal v2 schedules"):
        check(rollback=True)
    assert calls == []


def test_missing_prior_sha_refuses(rollback_release, monkeypatch):
    module, service, repo, prior, target, calls, activate = rollback_release
    stamp = module._read_stamp(repo, service)
    stamp['prior_sha'] = ''
    module._write_stamp(repo, service, stamp)
    with pytest.raises(module.DeployError, match="prior_sha is not stamped"):
        activate(rollback=True)
    assert module._git(repo, 'rev-parse', 'HEAD', runner=module._run) == prior
    assert not any(c[0] == 'launchctl' for c in calls)


@pytest.mark.parametrize("stamp_sha,outcome,accepted", [
    ('a' * 40, 'boot_failed', True),
    ('b' * 40, 'boot_failed', False),
    ('a' * 40, 'boot_not_observed', False),
])
def test_failed_stamp_is_bound_to_current_checkout(guard_case, monkeypatch, stamp_sha, outcome, accepted):
    module, db, calls, check = guard_case
    monkeypatch.setattr(module, '_launchd_state', lambda *_: module.LaunchdState(False, None, False))
    monkeypatch.setattr(module, '_read_stamp', lambda *_: {
        'sha': stamp_sha, 'daemon_boot_readback': {'outcome': outcome}})
    if accepted:
        assert check(rollback=True)['daemon_state'] == 'down'
        assert calls == []
    else:
        with pytest.raises(module.DeployError, match="command failed"):
            check(rollback=True)


def test_plist_db_precedes_environment_and_closed_rows_are_allowed(guard_case, monkeypatch):
    module, db, calls, check = guard_case
    monkeypatch.setenv('PENTACLE_STREAM_SESSIONS_DB', str(db.with_name('missing.db')))
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO sessions(host,session_name,role,visibility,created_at,status) "
                     "VALUES ('hosty','deployer','deployer','private','2026-01-01','closed')")
    assert check(rollback=True)['sole_deployer'] is True


def test_sessions_environment_fallback(guard_case, monkeypatch):
    module, db, calls, check = guard_case
    monkeypatch.setattr(module, '_launchd_plist', lambda _: {})
    monkeypatch.setenv('PENTACLE_STREAM_SESSIONS_DB', str(db))
    assert check(rollback=True)['daemon_state'] == 'down'
    monkeypatch.delenv('PENTACLE_STREAM_SESSIONS_DB')
    with pytest.raises(module.DeployError, match="DB path is missing"):
        check(rollback=True)


@pytest.mark.parametrize("pid", [0, -1, True])
def test_down_stamp_does_not_allow_invalid_nonnull_pid(guard_case, monkeypatch, pid):
    module, db, calls, check = guard_case
    monkeypatch.setattr(module, '_launchd_state', lambda *_: module.LaunchdState(True, pid))
    monkeypatch.setattr(module, '_read_stamp', lambda *_: {
        'sha': 'a' * 40, 'daemon_boot_readback': {'outcome': 'boot_failed'}})
    with pytest.raises(module.DeployError, match="positive launchd PID"):
        check(rollback=True)


@pytest.mark.parametrize('case,expected', [
    ('answered', 'launchd_answered_no_pid'),
    ('unanswered', 'boot_failed_stamp'),
    ('live', 'boot_failed_stamp'),
])
def test_down_basis_records_admission_without_changing_it(guard_case, monkeypatch, case, expected):
    module, db, calls, check = guard_case
    first = module.LaunchdState(True, 111 if case == 'live' else None, case != 'unanswered')
    samples = iter([first, module.LaunchdState(False, None, False)])
    monkeypatch.setattr(module, '_launchd_state', lambda *_: next(samples))
    monkeypatch.setattr(module, '_read_stamp', lambda *_: {
        'sha': 'a' * 40, 'daemon_boot_readback': {'outcome': 'boot_failed'}})
    result = check(rollback=True)
    assert result['daemon_state'] == 'down'
    assert result['down_basis'] == expected
    assert result['launchd_answered'] is (case != 'unanswered')
    assert result['pre_restart_launchd_answered'] is False
    assert result['pre_restart_pid'] is None


@pytest.mark.parametrize('case,expected_exit', [
    ('answered', 0), ('smoke_partial', 10), ('smoke_untested', 9), ('smoke_failed', 6),
    ('stamp_only', 8), ('guard_unanswered', 8), ('guard_final_unanswered', 8),
    ('activation_unanswered', 8), ('forward', 8), ('serving_unproven', 8),
    ('identity_changed', 8), ('identity_changed_during_scan', 8),
    ('missing_marker', 8), ('failure', 8), ('old_failure', 0),
])
def test_down_rollback_post_boot_attribution(tmp_path, monkeypatch, capsys, case, expected_exit):
    from . import test_deploy_script as fixtures
    module = fixtures.deploy_mod
    stdout, stderr = fixtures._u2_log_paths(tmp_path, monkeypatch)
    marker = '' if case == 'missing_marker' else fixtures._u2_boot()
    failure = fixtures._u2_line('force_close') + '\n'
    stdout.write_text((failure if case == 'old_failure' else '') + marker + 'ordinary log line\n')
    stderr.write_text(marker + (failure if case == 'failure' else ''))
    guard = dict(daemon_state='down', down_basis='launchd_answered_no_pid',
                 launchd_answered=True, pre_restart_launchd_answered=True, pre_restart_pid=None)
    activation = dict(daemon_state='down', pre_restart_pid=None, pre_restart_launchd_answered=True)
    prior_pid = None
    if case == 'stamp_only':
        guard['down_basis'] = 'boot_failed_stamp'
    elif case == 'guard_unanswered':
        guard['launchd_answered'] = False
    elif case == 'guard_final_unanswered':
        guard['pre_restart_launchd_answered'] = False
    elif case == 'activation_unanswered':
        activation['pre_restart_launchd_answered'] = False
    elif case == 'forward':
        guard = activation = {}
    elif case == 'serving_unproven':
        prior_pid = 111
        guard = dict(pre_restart_pid=111)
        activation = dict(pre_restart_pid=111)
    smoke = case.removeprefix('smoke_') if case.startswith('smoke_') else 'passed'
    monkeypatch.setattr(module, '_fleet_smoke_verdict', lambda *_: {'outcome': smoke})
    monkeypatch.setattr(module, '_install_fleet_smoke_schedule', lambda *_: None)
    calls, active_samples = [], []
    def runner(argv, cwd):
        calls.append(tuple(argv))
        if '-p' in argv:
            pid = argv[argv.index('-p') + 1]
            if pid == '111':
                return subprocess.CompletedProcess(argv, 0, 'old-start', '')
            assert pid == '999'
            active_samples.append(pid)
            changed = ((case == 'identity_changed' and len(active_samples) >= 2)
                       or (case == 'identity_changed_during_scan' and len(active_samples) >= 3))
            return subprocess.CompletedProcess(argv, 0, 'changed-start' if changed else fixtures._U2_START, '')
        assert argv[0] == 'launchctl' or any('spawn_fleet_smoke.py' in part for part in argv)
        return subprocess.CompletedProcess(argv, 0, '', '')
    stamp = dict(sha=fixtures.TARGET_SHA, restart_guard=guard, restart_activation=activation)
    module._apply_post_activation(
        module.SERVICES['chat-streamd-v2'], tmp_path, fixtures.TARGET_SHA, stamp,
        prior_log_size=0, prior_pid=prior_pid, reload_launchd=False, runner=runner,
        verify_boot=fixtures._boot_observed,
        verify_runtime=lambda *_: (True, {'sha': fixtures.TARGET_SHA, 'pid': 999}))
    assert 'post_activation_error' not in stamp
    scan = stamp['slow_consumer']
    ambiguous = case in {'stamp_only', 'guard_unanswered', 'guard_final_unanswered',
                        'activation_unanswered', 'forward', 'serving_unproven',
                        'identity_changed', 'identity_changed_during_scan', 'missing_marker'}
    assert scan['attribution']['outcome'] == ('attribution_ambiguous' if ambiguous else 'no_outgoing_writer')
    assert scan['outcome'] == ('failed' if ambiguous or case == 'failure' else 'passed')
    assert scan['failure_count'] == int(case == 'failure')
    if not ambiguous:
        assert scan['attribution']['prior_writer_exited'] is False
        assert scan['selected_inputs']
    if case == 'old_failure':
        assert scan['old_generation']['classifier']['failure_count'] == 1
    code, output, error = fixtures._run_main(monkeypatch, capsys, stamp=stamp)
    assert code == expected_exit
    if code == 0:
        assert 'do NOT retry' not in error
    assert json.loads(output) == stamp
