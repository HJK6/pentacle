"""One installed declaration scopes ordinary/nightly smoke without hiding reds."""
import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from .test_deploy_script import deploy_mod as d, V2_SERVICE, TARGET_SHA
from .test_deploy_log_guard import isolated_release
import nightly_journey as nj

DECLARATION = {'host':'optional','reason':'CLI pin mismatch',
               'readiness_item':'pentacle__fixture_readiness_2026_11'}


def machines(tmp_path, declarations):
    path=tmp_path/'machines.json'
    path.write_text(json.dumps({'machines':[{'name':'primary','ssh_target':None},
        {'name':'optional','ssh_target':'optional.example.invalid','smoke_optional':True}],
        'release_exclusions':declarations}))
    return path


def rows(host, outcome='passed'):
    return [{'host':host,'provider':p,'prompt_mode':m,'outcome':outcome}
        for p in ('claude','codex') for m in ('prompted','promptless')]


@pytest.mark.parametrize('included_outcome', ['passed','failed'])
@pytest.mark.parametrize('declared', [True, False])
def test_smoke_scope_preserves_included_failures(tmp_path, monkeypatch, included_outcome, declared):
    declarations = [DECLARATION] if declared else []
    path=machines(tmp_path,declarations);calls=[]
    monkeypatch.setattr(d,'_launchd_environment',lambda _: {'PENTACLE_MACHINES_FILE':str(path)})
    monkeypatch.setattr(d,'_scan_slow_consumer_window',lambda *a,**k:{'outcome':'passed'})
    monkeypatch.setattr(d,'_process_start',lambda *a:'start')
    monkeypatch.setattr(d,'_notify_failed_checks',lambda *a:None)
    def runner(argv,cwd):
        calls.append(argv)
        cells = rows('primary', included_outcome) + ([] if declared else rows('optional'))
        payload={'ok':included_outcome=='passed','cells':cells} if any('spawn_fleet_smoke.py' in v for v in argv) else {'outcome':'passed'}
        return subprocess.CompletedProcess(argv,0,json.dumps(payload),'')
    stamp={'release_exclusions':declarations}
    result=d.run_post_release_checks(V2_SERVICE,tmp_path,TARGET_SHA,stamp,runner=runner,
        boot=d.BootReadback(d.BOOT_OBSERVED,'fixture',0,99,None))
    smoke=next(c for c in calls if any('spawn_fleet_smoke.py' in v for v in c))
    setting = 'PENTACLE_SMOKE_EXCLUDED_HOSTS=' + ('optional' if declared else '')
    assert setting in smoke
    assert ('-u', 'PENTACLE_SMOKE_EXCLUDED_HOSTS') == smoke[smoke.index('PENTACLE_SMOKE_EXCLUDED_HOSTS')-1:smoke.index('PENTACLE_SMOKE_EXCLUDED_HOSTS')+1]
    from tools.spawn_fleet_smoke import smoke_plan
    monkeypatch.delenv('PENTACLE_MACHINES_JSON', raising=False)
    monkeypatch.delenv('PENTACLE_SMOKE_HOSTS', raising=False)
    monkeypatch.setenv('PENTACLE_MACHINES_FILE', str(path))
    monkeypatch.setenv('PENTACLE_SMOKE_EXCLUDED_HOSTS', setting.partition('=')[2])
    assert {cell[0] for cell in smoke_plan()['cells']} == ({'primary'} if declared else {'primary', 'optional'})
    assert result['fleet_smoke']['outcome']==included_outcome
    assert len([c for c in result['fleet_smoke']['cells'] if c['outcome']=='SKIPPED_DECLARED']) == (4 if declared else 0)
    assert stamp['release_exclusions']==declarations


def test_ordinary_snapshot_is_independent_of_later_home_change(tmp_path, monkeypatch):
    path=machines(tmp_path,[DECLARATION]);monkeypatch.setattr(d,'_launchd_environment',lambda _: {'PENTACLE_MACHINES_FILE':str(path)})
    snapshot=d.release_exclusions(V2_SERVICE,tmp_path)
    machines(tmp_path,[])
    assert snapshot==[DECLARATION]
    assert d.release_exclusions(V2_SERVICE,tmp_path)==[]


def test_ordinary_stamps_scope_before_activation(isolated_release, tmp_path, monkeypatch):
    module, service, repo, _, _, _, _, activate = isolated_release
    path = machines(tmp_path, [DECLARATION])
    monkeypatch.setattr(module, '_launchd_environment', lambda _: {'PENTACLE_MACHINES_FILE': str(path)})
    kickstart = module._kickstart
    def activation(*args):
        assert module._read_stamp(repo, service)['release_exclusions'] == [DECLARATION]
        machines(tmp_path, [])
        return kickstart(*args)
    monkeypatch.setattr(module, '_kickstart', activation)
    stamp = activate()
    assert stamp['serving'] is True
    assert stamp['release_exclusions'] == [DECLARATION]
    assert module._read_stamp(repo, service)['release_exclusions'] == [DECLARATION]


def test_nightly_reads_same_home_afresh_without_mutating_runtime_stamp(tmp_path,monkeypatch):
    path=machines(tmp_path,[DECLARATION]);stamp={'sha':TARGET_SHA,'release_exclusions':[]};original=copy.deepcopy(stamp)
    monkeypatch.setattr(d,'_launchd_environment',lambda _: {'PENTACLE_MACHINES_FILE':str(path)})
    monkeypatch.setattr(d,'_read_stamp',lambda *a:stamp)
    monkeypatch.setattr(d,'_write_stamp',lambda *a:pytest.fail('nightly must not rewrite recovery-bound stamp'))
    seen=[]
    def checks(service,repo,sha,value,**kwargs):
        seen.append(copy.deepcopy(value));return {k:{'outcome':'passed'} for k in ('fleet_smoke','slow_consumer','front_desk')}
    monkeypatch.setattr(d,'run_post_release_checks',checks)
    args=SimpleNamespace(checkout=str(tmp_path),evidence=str(tmp_path/'evidence'),service='chat-streamd-v2')
    assert nj.check_release(args,deploy_module=d)==0
    assert seen[-1]['release_exclusions']==[DECLARATION]
    assert json.loads((tmp_path/'evidence/receipt.json').read_text())['release_exclusions']==[DECLARATION]
    machines(tmp_path,[])
    assert nj.check_release(args,deploy_module=d)==0
    assert seen[-1]['release_exclusions']==[] and stamp==original
