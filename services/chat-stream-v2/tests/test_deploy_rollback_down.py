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
