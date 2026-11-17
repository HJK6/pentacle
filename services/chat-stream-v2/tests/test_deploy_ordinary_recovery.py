"""Actual recovery entry with real Git/files/child loss, synthetic OS/account probes."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from deploy import ordinary_recovery as o
from deploy import deploy as d
from .ordinary_recovery_fixture import SyntheticRuntime, command, cli


@pytest.fixture
def operation(tmp_path,request):
    repo=tmp_path/'source';repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git',*args],cwd=repo,text=True,stderr=subprocess.DEVNULL).strip()
    git('init','-q');git('config','user.name','Synthetic');git('config','user.email','fixture@example.invalid')
    (repo/'code').write_text('prior');git('add','.');git('commit','-qm','prior');prior=git('rev-parse','HEAD')
    (repo/'code').write_text('target');git('commit','-qam','target');target=git('rev-parse','HEAD');git('checkout','--detach',prior)
    files=tmp_path/'files';files.mkdir()
    for index,name in enumerate(('stamp','plist','collector','settings','profile','policy','machines')):
        (files/name).write_bytes(b'legacy-unqualified' if name=='stamp' else ('prior '+name).encode())
        (files/name).chmod(0o600 if index%2 else 0o640)
    probes={'controller_sha':'f'*40,'accounts':{'host':'fixture','generation':'one','cli_hash':'1'*64},
            'process':{'loaded':True,'pid':101,'birth':'synthetic-prior','sha':prior,'provenance':{'processes':[]}},
            'collector':{'loaded':getattr(request,'param',False),'pid':None,'birth':None}}
    (tmp_path/'probes.json').write_text(json.dumps(probes))
    a=SyntheticRuntime(d,d.SERVICES['chat-streamd-v2'],repo,command,stream='synthetic-owner',boot_wait_seconds=0)
    with o.c.transaction_lock(repo): b=o.Bundle.prepare(a,tmp_path/'bundle',prior,target,None,[])
    return a,b,prior,target,git


def forward(operation):
    a,b,prior,target,git=operation
    git('checkout','--detach',target)
    b.publish(a.root/'files'/'stamp',b'target unqualified',0o600)
    b.publish(a.root/'files'/'plist',b'target plist',0o600)
    p=a.probes();p['process'].update(pid=202,birth='synthetic-target',sha=target);a.save_probes(p)
    b.observed_process();b.persist(phase='serving_unqualified')


def authority(a,b,go='synthetic-go'):
    b.load()
    value={'schema':o.SCHEMA,'operation':b.bundle['operation'],'bundle_sha256':b.bundle['sha256'],
       'controller_sha':b.bundle['metadata']['controller_sha'],'prior_sha':b.bundle['prior_sha'],
       'target_sha':b.bundle['target_sha'],'expected_state':b.state(),'go_tell_id':go,'fd_gate_reference':'synthetic separate FD approval'}
    path=a.root/'authorization.json';o.c.atomic_json(path,value);return path


def invoke(a,b,auth):
    return cli(a.root,['--recover-ordinary',str(b.folder),'--ordinary-recovery-authorization',str(auth),
                       '--recovery-go-tell-id','synthetic-go'])


def test_actual_entry_restores_bytes_modes_and_unqualified_legacy(operation):
    a,b,prior,target,git=operation;forward(operation);auth=authority(a,b)
    assert invoke(a,b,auth)==d.EXIT_PRIOR_RESTORED
    assert git('rev-parse','HEAD')==prior
    for name,row in b.bundle['preimages'].items():
        assert o.file_state(row['path'])=={'sha256':row['sha256'],'mode':row['mode']}
    assert (a.root/'files'/'stamp').read_bytes()==b'legacy-unqualified'
    events=(a.root/'events').read_text().splitlines()
    assert events.index('stop-proven')<events.index('source')<events.index('start')<events.index('zero-input-owned-close')
    assert o.sealed_read(b.folder/'journal.json')['result']['full_fleet_acceptance'] is False
    assert a.probes()['collector']['loaded'] is False
    # Reentry requires a fresh authorization for the exact new journal state.
    auth=authority(a,b);before=(a.root/'events').read_bytes()
    assert invoke(a,b,auth)==d.EXIT_PRIOR_RESTORED
    assert (a.root/'events').read_bytes()==before


@pytest.mark.parametrize('damage',['authorization','bundle','preimage','source_bundle','files','process','binding','journal','missing_authority'])
def test_actual_entry_refuses_without_runtime_mutation(operation,damage):
    a,b,prior,target,git=operation;forward(operation);auth=authority(a,b)
    if damage=='authorization':
        value=o.c.read_json(auth);value['target_sha']=prior;o.c.atomic_json(auth,value)
    elif damage=='bundle': (b.folder/'bundle.json').write_bytes(b'{partial')
    elif damage=='preimage': (b.folder/'stamp.before').write_bytes(b'corrupt')
    elif damage=='source_bundle': (b.folder/'source.bundle').write_bytes(b'corrupt')
    elif damage=='files': (a.root/'files'/'plist').write_bytes(b'foreign')
    elif damage=='process':
        p=a.probes();p['process']['birth']='foreign';a.save_probes(p)
    elif damage=='binding':
        p=a.probes();p['accounts']['generation']='two';a.save_probes(p)
    elif damage=='journal': (b.folder/'journal.json').write_bytes(b'{partial')
    elif damage=='missing_authority': auth.unlink()
    assert invoke(a,b,auth)==d.EXIT_REFUSED
    assert not (a.root/'events').exists()
    assert git('rev-parse','HEAD')==target


@pytest.mark.parametrize('stage',['fail_stop','drift_during_stop','fail_start','fail_proof'])
def test_failure_never_starts_partial_tree_or_claims_success(operation,stage):
    a,b,prior,target,git=operation;forward(operation)
    p=a.probes();p[stage]=True;a.save_probes(p);auth=authority(a,b)
    assert invoke(a,b,auth)!=d.EXIT_PRIOR_RESTORED
    events=(a.root/'events').read_text().splitlines()
    if stage in ('fail_stop','drift_during_stop'):
        assert 'source' not in events and 'start' not in events
    else: assert git('rev-parse','HEAD')==prior
    assert o.sealed_read(b.folder/'journal.json')['phase']=='recovery_required'
    if stage=='fail_proof': assert a.probes()['process']['pid']==303


def test_unfinished_ordinary_fences_all_other_deployment_modes(operation):
    a,b,*_=operation
    with pytest.raises(o.c.Invalid,match='unfinished'): o.fence(a.repo)
    o.fence(a.repo,recovering=b.folder)


def child(script,*args):
    return subprocess.run([sys.executable,'-c',script,*map(str,args)],
       env={**os.environ,'PYTHONPATH':os.pathsep.join(sys.path)},capture_output=True,text=True,timeout=30)


@pytest.mark.parametrize('stage',['before_replace','after_replace','partial'])
def test_real_child_loss_publication_then_actual_recovery(operation,stage):
    a,b,prior,target,git=operation;forward(operation)
    script=r'''
import os,sys
from pathlib import Path
from deploy import ordinary_recovery as o, deploy as d
from ordinary_recovery_fixture import SyntheticRuntime,command
root=Path(sys.argv[1]);stage=sys.argv[2]
a=SyntheticRuntime(d,d.SERVICES['chat-streamd-v2'],root/'source',command,stream='synthetic-owner',boot_wait_seconds=0)
b=o.Bundle(a,root/'bundle').load()
# Journal writes stay intact; interrupt only the actual destination publication.
replace=os.replace
if stage=='partial':
    real=o.c.atomic_bytes
    def partial(path,data,**kw):
        if Path(path)==root/'files'/'stamp':
            (root/'files'/'.interrupted').write_bytes(data[:3]);os._exit(73)
        return real(path,data,**kw)
    o.c.atomic_bytes=partial
else:
    def interrupted(src,dst):
        if Path(dst)==root/'files'/'stamp':
            if stage=='after_replace':replace(src,dst)
            os._exit(73)
        return replace(src,dst)
    os.replace=interrupted
b.publish(root/'files'/'stamp',b'new target failed publication',0o600)
'''
    result=child(script,a.root,stage);assert result.returncode==73,result.stderr
    auth=authority(a,b)
    assert invoke(a,b,auth)==d.EXIT_PRIOR_RESTORED
    assert git('rev-parse','HEAD')==prior
    assert (a.root/'files'/'stamp').read_bytes()==b'legacy-unqualified'


def test_real_controller_exit_after_mutation_recovered_by_actual_cli(operation):
    a,b,prior,target,git=operation
    script=r'''
import os,sys
from pathlib import Path
from deploy import ordinary_recovery as o,deploy as d
from ordinary_recovery_fixture import SyntheticRuntime,command
root=Path(sys.argv[1]);a=SyntheticRuntime(d,d.SERVICES['chat-streamd-v2'],root/'source',command,stream='synthetic-owner',boot_wait_seconds=0)
b=o.Bundle(a,root/'bundle').load();a.runtime.checked(('git','checkout','--detach',b.bundle['target_sha']))
b.publish(root/'files'/'stamp',b'target failed smoke',0o600)
p=a.probes();p['process'].update(pid=202,birth='synthetic-target',sha=b.bundle['target_sha']);a.save_probes(p)
b.observed_process();os._exit(74)
'''
    result=child(script,a.root);assert result.returncode==74,result.stderr
    auth=authority(a,b)
    result=child("import sys;from ordinary_recovery_fixture import cli;raise SystemExit(cli(sys.argv[1],sys.argv[2:]))",
                 a.root,'--recover-ordinary',b.folder,'--ordinary-recovery-authorization',auth,'--recovery-go-tell-id','synthetic-go')
    assert result.returncode==d.EXIT_PRIOR_RESTORED,result.stderr
    assert git('rev-parse','HEAD')==prior

from functools import partial
from .test_deploy_log_guard import isolated_release


@pytest.mark.parametrize('failure',['smoke','publication'])
def test_actual_forward_enrollment_then_actual_recovery_entry(isolated_release,tmp_path,monkeypatch,failure):
    module,service,repo,prior,target,before,calls,activate=isolated_release
    extra=tmp_path/'recovery-files';extra.mkdir()
    for name in ('policy','machines'): (extra/name).write_bytes(b'synthetic '+name.encode())
    daemon=module._launchd_plist_path(service.launchd_label)
    paths={'stamp':module._stamp_path(repo,service),'plist':daemon,
           'collector':extra/'collector','schedule':extra/'schedule','settings':extra/'settings',
           'profile':module._stamp_path(repo,service).with_name(service.name+'.test-profile.json'),
           'policy':extra/'policy','machines':extra/'machines'}
    probes={'controller_sha':'f'*40,'accounts':{'generation':'synthetic-one'},
      'process':{'loaded':True,'pid':111,'birth':'synthetic-prior','sha':prior,'provenance':{'processes':[]}},
      'collector':{'loaded':False,'pid':None,'birth':None}}
    (tmp_path/'probes.json').write_text(json.dumps(probes))
    class Enrolled(SyntheticRuntime):
        def paths(self, metadata): return paths
    monkeypatch.setattr(o,'OrdinaryRuntime',Enrolled)
    original_deploy=module.deploy
    monkeypatch.setattr(module,'deploy',partial(original_deploy,ordinary_recovery_bundle=tmp_path/'bundle'))
    original=module._apply_post_activation
    def runtime(sha,pid):
        p=json.loads((tmp_path/'probes.json').read_text());p['process'].update(pid=pid,birth='synthetic-target',sha=sha)
        (tmp_path/'probes.json').write_text(json.dumps(p));return True,{'sha':sha,'pid':pid}
    monkeypatch.setattr(module,'_apply_post_activation',partial(original.func,
        verify_boot=original.keywords['verify_boot'],verify_runtime=runtime))
    def checks(service,repo,sha,stamp,**kwargs):
        stamp.update(fleet_smoke={'outcome':'failed','cells':[]},disposition='active with failed checks')
    monkeypatch.setattr(module,'run_post_release_checks',checks)
    if failure=='publication':
        actual=module._write_stamp
        def write(repo,service,stamp):
            if stamp.get('serving'): raise OSError('synthetic final publication failure')
            return actual(repo,service,stamp)
        monkeypatch.setattr(module,'_write_stamp',write)
    result=activate()
    assert result['serving'] and result['disposition']=='active with failed checks'
    a=Enrolled(module,service,repo,command,stream='synthetic-owner',boot_wait_seconds=0)
    b=o.Bundle(a,tmp_path/'bundle').load();auth=authority(a,b)
    restored=original_deploy(service,release_checkout=repo,ref=target,recover_ordinary=b.folder,
        ordinary_recovery_authorization=auth,recovery_go_tell_id='synthetic-go',deployer_stream_id='synthetic-owner',runner=command)
    assert restored['outcome']=='restored_unqualified'
    assert paths['stamp'].read_bytes()==before
    assert a.git('rev-parse','HEAD')==prior

from .test_deploy_coordinated_helpers import runtime_preparation


from .test_deploy_prior_helper_reuse import legacy_prior


def test_concrete_capture_rechecks_real_synthetic_accounts_helpers_and_dependencies(legacy_prior,monkeypatch):
    runtime,fixture,intent,root=legacy_prior.runtime,legacy_prior.fixture,legacy_prior.intent,legacy_prior.root
    a=o.OrdinaryRuntime.__new__(o.OrdinaryRuntime)
    a.d,a.service,a.repo,a.runner=runtime.d,runtime.service,fixture.repo,runtime.runner
    a.runtime,a.controller,a.stream=runtime,runtime.controller,'fixture:deployer'
    stamp=runtime.d._read_stamp(fixture.repo,runtime.service)
    stamp['requirements_hash']=runtime.d._sha256(fixture.repo/runtime.service.requirements_path)
    runtime.d._write_stamp(fixture.repo,runtime.service,stamp)
    monkeypatch.setattr(a,'process',lambda *args:{'sha':fixture.prior,'pid':111,'birth':'synthetic','loaded':True,'provenance':{'processes':[]}})
    monkeypatch.setattr(a,'job',lambda label:{'loaded':False,'pid':None,'birth':None})
    monkeypatch.setattr(runtime.d,'verify_deploy_guard',lambda *args,**kw:{'sole_deployer':True})
    metadata=a.capture(fixture.prior,fixture.prior,None,[])
    assert metadata['accounts']['selected_bindings']
    assert metadata['helpers']['fixture']['proof']['artifacts']
    assert metadata['helpers']['fixture']['proof']['closure']['dependency_files']
    with o.c.transaction_lock(fixture.repo):
        bundle=o.Bundle.prepare(a,root/'ordinary-real-capture',fixture.prior,fixture.prior,None,[])
    a.validate(bundle.bundle)
    original=bundle.bundle['metadata']['daemon_proof']['python_sha256']
    bundle.bundle['metadata']['daemon_proof']['python_sha256']='0'*64
    with pytest.raises(o.c.Invalid,match='daemon interpreter/dependency drift'): a.validate(bundle.bundle)
    bundle.bundle['metadata']['daemon_proof']['python_sha256']=original
    archive=legacy_prior.release/'pentacle.tar'; saved=archive.read_bytes(); archive.write_bytes(saved+b'tampered')
    with pytest.raises(o.c.Invalid,match='manifest/archive mismatch'): a.validate(bundle.bundle)
    archive.write_bytes(saved)
    policy=o.c.read_json(fixture.policy);policy['changed']='foreign';o.c.atomic_json(fixture.policy,policy)
    with pytest.raises(o.c.Invalid,match='immutable input drift'): a.validate(bundle.bundle)


def test_replacement_during_proof_cannot_be_adopted_as_restored(operation,monkeypatch):
    a,b,prior,target,git=operation;forward(operation);auth=authority(a,b)
    def proof(self,bundle,boot):
        p=self.probes();p['process'].update(pid=404,birth='replacement');self.save_probes(p)
        return {'outcome':'passed','mode':'zero_input'}
    monkeypatch.setattr(SyntheticRuntime,'prove',proof)
    assert invoke(a,b,auth)!=d.EXIT_PRIOR_RESTORED
    assert o.sealed_read(b.folder/'journal.json')['phase']=='recovery_required'


def test_missing_bundle_read_does_not_create_directory(operation):
    a,b,*_=operation;missing=a.root/'does-not-exist'
    with pytest.raises(o.c.Invalid):o.Bundle(a,missing).load()
    assert not missing.exists()


def test_stale_preimage_cannot_return_after_new_image_committed(operation):
    a,b,prior,target,git=operation;forward(operation)
    (a.root/'files'/'stamp').write_bytes((b.folder/'stamp.before').read_bytes())
    auth=authority(a,b)
    assert invoke(a,b,auth)==d.EXIT_REFUSED
    assert not (a.root/'events').exists()


@pytest.mark.parametrize('operation',[True],indirect=True)
@pytest.mark.parametrize('lost',[False,True])
def test_prior_loaded_collector_must_be_read_back_loaded(operation,monkeypatch,lost):
    a,b,*_=operation;forward(operation);auth=authority(a,b)
    original=SyntheticRuntime.prove
    def proof(self,bundle,boot):
        result=original(self,bundle,boot)
        if lost:
            p=self.probes();p['collector']['loaded']=False;self.save_probes(p)
        return result
    monkeypatch.setattr(SyntheticRuntime,'prove',proof)
    code=invoke(a,b,auth)
    assert (code==d.EXIT_PRIOR_RESTORED) is (not lost)
    assert o.sealed_read(b.folder/'journal.json')['phase']==('recovery_required' if lost else 'restored_unqualified')


def test_process_reappearing_after_stop_blocks_source_restore(operation,monkeypatch):
    a,b,*_=operation;forward(operation);auth=authority(a,b)
    original=SyntheticRuntime.validate
    def validate(self,bundle):
        original(self,bundle)
        events=self.root/'events'
        if events.exists() and 'stop-proven' in events.read_text():
            p=self.probes();p['process'].update(pid=404,birth='unrelated',loaded=True);self.save_probes(p)
    monkeypatch.setattr(SyntheticRuntime,'validate',validate)
    assert invoke(a,b,auth)!=d.EXIT_PRIOR_RESTORED
    assert 'source' not in (a.root/'events').read_text().splitlines()


@pytest.mark.parametrize('moment',['initial','before_bootout'])
def test_concrete_stop_rejects_replacement_before_any_stop(moment):
    from types import SimpleNamespace
    from deploy.coordinated_runtime import Runtime
    queries=[];calls=[]
    def state(*args):
        queries.append(1)
        pid=222 if moment=='initial' or len(queries)>1 else 111
        return SimpleNamespace(answered=True,loaded=True,pid=pid)
    runtime=Runtime.__new__(Runtime)
    runtime.service=SimpleNamespace(launchd_label='synthetic-label')
    runtime.runner=None
    runtime.d=SimpleNamespace(_launchd_state=state,_process_start=lambda *a:'synthetic-birth',
        _capture_log_offsets=lambda *a:{},_daemon_log_paths=lambda *a:())
    runtime.process_rows=lambda:[]
    runtime.checked=lambda *a:calls.append(a)
    with pytest.raises(o.c.Invalid,match='identity changed'):
        runtime.stop({},expected_identity={'pid':111,'birth':'synthetic-birth'})
    assert not calls


def test_real_loss_after_stop_before_checkpoint_refuses_ambiguous_reentry(operation):
    a,b,prior,target,git=operation;forward(operation);auth=authority(a,b)
    script=r'''
import os,sys
from ordinary_recovery_fixture import SyntheticRuntime,cli
stop=SyntheticRuntime.stop
def crash(self,bundle,now):
    stop(self,bundle,now);os._exit(75)
SyntheticRuntime.stop=crash
cli(sys.argv[1],sys.argv[2:])
'''
    result=child(script,a.root,'--recover-ordinary',b.folder,'--ordinary-recovery-authorization',auth,'--recovery-go-tell-id','synthetic-go')
    assert result.returncode==75,result.stderr
    assert git('rev-parse','HEAD')==target
    before=(a.root/'events').read_bytes();auth=authority(a,b)
    assert invoke(a,b,auth)==d.EXIT_REFUSED
    assert (a.root/'events').read_bytes()==before


def test_failed_proof_report_is_not_a_restoration_success(operation,monkeypatch):
    a,b,*_=operation;forward(operation);auth=authority(a,b)
    monkeypatch.setattr(SyntheticRuntime,'prove',lambda *args:{'outcome':'failed','mode':'zero_input'})
    assert invoke(a,b,auth)!=d.EXIT_PRIOR_RESTORED
    assert o.sealed_read(b.folder/'journal.json')['phase']=='recovery_required'


def test_forward_terminal_publication_cannot_adopt_foreign_file_drift(operation):
    a,b,*_=operation;forward(operation)
    (a.root/'files'/'plist').write_bytes(b'foreign')
    with pytest.raises(o.c.Invalid,match='state drift'):
        b.finish({'serving':True,'disposition':'active and checks passed'})
    assert o.sealed_read(b.folder/'journal.json')['phase']!='qualified'

def test_forward_terminal_publication_cannot_adopt_prior_source(operation):
    a,b,prior,target,git=operation;forward(operation)
    git('checkout','--detach',prior)
    with pytest.raises(o.c.Invalid,match='state drift'):
        b.finish({'serving':True,'disposition':'active and checks passed'})
    assert o.sealed_read(b.folder/'journal.json')['phase']!='qualified'
