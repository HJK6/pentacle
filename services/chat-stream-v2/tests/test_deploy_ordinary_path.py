"""Ordinary serving proof and restoration with owned files and native fakes."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from .test_deploy_script import deploy_mod as d, V2_SERVICE, TARGET_SHA, _boot_observed


def test_serving_failure_does_not_run_late_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(d, '_process_start', lambda *a: 'old')
    monkeypatch.setattr(d, '_write_stamp', lambda *a: None)
    monkeypatch.setattr(d, '_kickstart', lambda *a: None)
    monkeypatch.setattr(d, 'run_post_release_checks', lambda *a, **k: pytest.fail('late check'), raising=False)
    stamp = {}
    d._apply_post_activation(V2_SERVICE, tmp_path, TARGET_SHA, stamp, prior_log_size=0,
        prior_pid=11, reload_launchd=False, runner=lambda *a: None,
        verify_boot=lambda *a, **k: d.BootReadback(d.BOOT_FAILED, 'no boot', 0, None, 11))
    assert stamp['serving'] is False


@pytest.mark.parametrize('raises', [False, True])
def test_late_check_failure_keeps_serving_candidate(tmp_path, monkeypatch, raises):
    monkeypatch.setattr(d, '_process_start', lambda *a: 'old')
    monkeypatch.setattr(d, '_write_stamp', lambda *a: None)
    monkeypatch.setattr(d, '_kickstart', lambda *a: None)
    def check(*a, **k):
        if raises: raise RuntimeError('late checker')
        a[3]['disposition'] = 'active with failed checks'
        return {'fleet_smoke': {'outcome': 'failed'}}
    monkeypatch.setattr(d, 'run_post_release_checks', check, raising=False)
    stamp = {}
    d._apply_post_activation(V2_SERVICE, tmp_path, TARGET_SHA, stamp, prior_log_size=0,
        prior_pid=11, reload_launchd=False, runner=lambda *a: None,
        verify_boot=_boot_observed, verify_runtime=lambda *a: (True, {'sha':TARGET_SHA}))
    assert stamp['serving'] is True
    assert stamp['disposition'] == 'active with failed checks'


@pytest.fixture
def restore_case(tmp_path, monkeypatch):
    repo=tmp_path/'repo'; repo.mkdir()
    def run(argv,cwd):return subprocess.run(argv,cwd=cwd,text=True,capture_output=True)
    def git(*args):return d._git(repo,*args,runner=run)
    git('init','-q');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid')
    (repo/'code').write_text('prior');git('add','.');git('commit','-qm','prior'); prior=git('rev-parse','HEAD')
    (repo/'code').write_text('candidate');git('commit','-qam','candidate')
    stamp_path=tmp_path/'stamp';stamp_path.write_bytes(b'prior stamp');stamp_path.chmod(0o640)
    plist=tmp_path/'plist';plist.write_bytes(b'prior plist');plist.chmod(0o600)
    policy=tmp_path/'policy'
    tx=d._PreActivationTransaction(repo,prior,stamp_path,stamp_path.read_bytes(),run)
    tx.remember_file(plist);tx.remember_file(policy)
    stamp_path.write_bytes(b'candidate stamp');plist.write_bytes(b'candidate plist');policy.write_bytes(b'new policy')
    events=[]
    runtime=SimpleNamespace(stop=lambda *a:events.append('stop'))
    monkeypatch.setattr(d,'_daemon_log_paths',lambda *a:())
    monkeypatch.setattr(d,'_launchd_plist_path',lambda *a:plist)
    monkeypatch.setattr(d,'_checked',lambda argv,cwd,runner: (events.append(tuple(argv)) or run(argv,cwd)) if argv[0]=='git' else (events.append(tuple(argv)) or subprocess.CompletedProcess(argv,0,'','')))
    return repo,prior,stamp_path,plist,policy,tx,runtime,events,run


@pytest.mark.parametrize('prior_serves',[True,False])
def test_restore_preserves_source_file_bytes_modes_and_absence(restore_case,prior_serves):
    repo,prior,stamp_path,plist,policy,tx,runtime,events,run=restore_case
    stamp={}
    d._restore_prior(V2_SERVICE,repo,stamp,tx,runtime,runner=run,
        verify_boot=lambda *a,**k:d.BootReadback(d.BOOT_OBSERVED if prior_serves else d.BOOT_NOT_OBSERVED,'prior',0,33,None),
        verify_runtime=lambda *a:(prior_serves,{'sha':prior}),boot_wait_seconds=0)
    assert (repo/'code').read_text()=='prior'
    assert stamp_path.read_bytes()==b'prior stamp' and stamp_path.stat().st_mode&0o777==0o640
    assert plist.read_bytes()==b'prior plist' and plist.stat().st_mode&0o777==0o600
    assert not policy.exists()
    assert events[0]=='stop'
    assert sum(isinstance(e,tuple) and e[:2]==('launchctl','bootstrap') for e in events)==1
    assert stamp['disposition']==('target not active, prior restored' if prior_serves else 'target not active, restore unproven')


def test_unknown_process_exit_never_restores_or_starts(restore_case):
    repo,prior,stamp_path,plist,policy,tx,runtime,events,run=restore_case
    def fail(*a):raise RuntimeError('exit unknown')
    runtime.stop=fail
    stamp={}
    d._restore_prior(V2_SERVICE,repo,stamp,tx,runtime,runner=run,boot_wait_seconds=0)
    assert (repo/'code').read_text()=='candidate'
    assert not events
    assert stamp['disposition']=='target not active, restore unproven'


def test_partial_restoration_never_bootstraps(restore_case,monkeypatch):
    repo,prior,stamp_path,plist,policy,tx,runtime,events,run=restore_case
    monkeypatch.setattr(tx,'_restore',lambda:['file restore failed'])
    stamp={}
    d._restore_prior(V2_SERVICE,repo,stamp,tx,runtime,runner=run,boot_wait_seconds=0)
    assert not any(isinstance(e,tuple) and e[:2]==('launchctl','bootstrap') for e in events)
    assert stamp['disposition']=='target not active, restore unproven'


@pytest.mark.parametrize('connected', [True, False])
def test_report_only_smoke_suppresses_cell_notifications(monkeypatch, connected):
    from contextlib import contextmanager
    from tools import spawn_fleet_smoke as smoke
    @contextmanager
    def connection(*args):
        if not connected:
            raise RuntimeError('connection failure')
        yield (None,) * 5
    def failed_cell(*args, **kwargs):
        raise RuntimeError('cell failure')
    monkeypatch.setattr(smoke, '_operator_connection', connection)
    monkeypatch.setattr(smoke, 'run_cell', failed_cell)
    monkeypatch.setattr(smoke, 'publish_smoke_card', lambda *a: pytest.fail('per-cell card'))
    monkeypatch.setattr(smoke, 'notify_operator_failure', lambda *a: pytest.fail('per-cell notification'))
    rows = smoke.run_matrix('ws://fixture.invalid', Path('unused'), 1,
        (('fixture','codex','promptless'),), report_only=True)
    assert len(rows) == 1 and rows[0]['class'] == 'failure'


def test_one_aggregate_blocker_for_multiple_failed_reports(tmp_path, monkeypatch):
    calls=[]
    monkeypatch.setattr(d, '_resolve_agent_orch', lambda *a: 'fixture-agent-orch')
    def runner(command, cwd):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, '{}', '')
    checks={name:{'outcome':state} for name,state in (
        ('fleet_smoke','failed'),('slow_consumer','passed'),('front_desk','untested'))}
    d._notify_failed_checks(tmp_path,V2_SERVICE,TARGET_SHA,checks,runner)
    assert len(calls)==1
    assert 'BLOCKER' in calls[0][calls[0].index('--title')+1]
    body=json.loads(calls[0][calls[0].index('--message')+1])
    assert set(body['checks'])=={'fleet_smoke','front_desk'}
