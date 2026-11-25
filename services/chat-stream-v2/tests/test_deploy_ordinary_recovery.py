"""Actual recovery entry with real Git/files/child loss, synthetic OS/account probes."""
import base64
import copy
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

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
    monkeypatch.setattr(a,'process',lambda *args:{'sha':fixture.prior,'pid':111,'birth':'synthetic','loaded':True,'provenance':{'launchd_answered':True,'pid':111,'processes':[{'pid':111,'start':'synthetic'}]}})
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
def crash(self,bundle,now,decision):
    stop(self,bundle,now,decision);os._exit(75)
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


def test_enrollment_allows_proven_departed_helper(operation, tmp_path):
    a, b, prior, target, git = operation
    # Remove the synthetic existing operation fence before a separate enrollment.
    (o.c.state_root(a.repo)/'ordinary-active.json').unlink()
    probes = a.probes()
    probes['process']['provenance'] = {'launchd_answered': True, 'pid': 101, 'processes': [{'pid': 101, 'start': 'synthetic-prior'}]}
    a.save_probes(probes)
    original_capture = a.capture
    def capture(*args):
        metadata = original_capture(*args)
        metadata['prior_process']['provenance']['processes'].append({'pid': 404, 'start': 'helper-birth'})
        return metadata
    a.capture = capture
    a.process_absent = lambda pid, birth: pid == 404 and birth == 'helper-birth'
    enrolled = o.Bundle.prepare(a, tmp_path/'second-bundle', prior, target, None, [])
    assert enrolled.journal['phase'] == 'forward'


def test_recovery_allows_proven_departed_helper(operation):
    a, b, prior, target, git = operation
    forward(operation)
    probes = a.probes()
    probes['process']['provenance'] = {'launchd_answered': True, 'pid': 202, 'processes': [{'pid': 202, 'start': 'synthetic-target'}]}
    a.save_probes(probes)
    process = copy.deepcopy(probes['process'])
    process['provenance']['processes'].append({'pid': 404, 'start': 'helper-birth'})
    b.persist(process=process)
    a.process_absent = lambda pid, birth: pid == 404 and birth == 'helper-birth'
    auth = authority(a, b)
    assert b.admit(auth, 'synthetic-go')['source'] == target


def process_snapshot(*rows):
    return {'loaded':True, 'pid':101, 'birth':'root', 'sha':'a'*40,
            'provenance':{'launchd_answered':True, 'pid':101,
                          'processes':[{'pid':pid,'start':birth} for pid,birth in rows]}}


def test_process_comparison_turnover_and_refusal_controls():
    from types import SimpleNamespace
    expected=process_snapshot((101,'root'),(202,'old'))
    current=process_snapshot((303,'new'),(101,'root'))
    adapter=SimpleNamespace(process_absent=lambda pid,birth: (pid,birth)==(202,'old'),
                            authenticate_new_processes=lambda primary,rows: rows in ([{'pid':303,'start':'new'}], [{'pid':202,'start':'old'}]))
    decision=o.compare_process(adapter,expected,current)
    assert decision['departed_absent']==[{'pid':202,'start':'old'}]
    assert decision['added_owned']==[{'pid':303,'start':'new'}]
    assert o.compare_process(adapter,expected,process_snapshot((202,'old'),(101,'root')))['added_owned']==[]
    adapter.process_absent=lambda *args: False
    with pytest.raises(o.c.Invalid,match='exit unproven'): o.compare_process(adapter,expected,current)
    adapter.process_absent=lambda *args: True
    adapter.authenticate_new_processes=lambda *args: False
    with pytest.raises(o.c.Invalid,match='ownership unproven'): o.compare_process(adapter,expected,current)
    for malformed in (process_snapshot((101,'root'),(101,'root')),
                      process_snapshot((101,'root'),(202,'reused')),
                      process_snapshot((202,'old'))):
        with pytest.raises(o.c.Invalid): o.compare_process(adapter,expected,malformed)
    replaced=process_snapshot((101,'root'));replaced['birth']='different'
    with pytest.raises(o.c.Invalid,match='primary identity drift'): o.compare_process(adapter,expected,replaced)


def test_replacement_controller_is_exactly_operation_bound(operation):
    a,b,prior,target,git=operation
    forward(operation)
    b.bundle['operation']='08826e95f2d347f498d8caea51d4ad13'
    auth=authority(a,b)
    # authority() reloads the sealed operation; use a synthetic sealed-operation
    # identity here solely to exercise admission, not the original-bundle rehearsal.
    b.bundle['operation']='08826e95f2d347f498d8caea51d4ad13'
    payload=o.c.read_json(auth);payload['operation']=b.bundle['operation']
    payload.update(replacement_controller_sha='e'*40,authorized_by='fixture FD')
    o.c.atomic_json(auth,payload)
    probes=a.probes();probes['controller_sha']='e'*40;a.save_probes(probes)
    before=(b.folder/'journal.json').read_bytes()
    assert b.admit(auth,'synthetic-go')['source']==target
    assert a.recovery_authorization['replacement_controller_sha']=='e'*40
    payload['operation']='f'*32;o.c.atomic_json(auth,payload)
    with pytest.raises(o.c.Invalid): b.admit(auth,'synthetic-go')
    assert (b.folder/'journal.json').read_bytes()==before


def test_authorization_accepts_owned_turnover_but_refuses_unexplained(operation):
    a,b,prior,target,git=operation;forward(operation)
    p=a.probes();p['process']['provenance']={'launchd_answered':True,'pid':202,
        'processes':[{'pid':202,'start':'synthetic-target'}]};a.save_probes(p)
    b.persist(process=copy.deepcopy(p['process']))
    auth=authority(a,b)
    p=a.probes();p['process']['provenance']['processes'].append({'pid':404,'start':'new-helper'});a.save_probes(p)
    a.authenticate_new_processes=lambda primary,rows: True
    assert b.admit(auth,'synthetic-go')['source']==target
    a.authenticate_new_processes=lambda primary,rows: False
    journal=(b.folder/'journal.json').read_bytes()
    with pytest.raises(o.c.Invalid,match='ownership unproven'): b.admit(auth,'synthetic-go')
    assert (b.folder/'journal.json').read_bytes()==journal
    assert not (a.root/'events').exists()


def test_stopped_fence_refuses_survivor_and_reappearance():
    from types import SimpleNamespace
    a=o.OrdinaryRuntime.__new__(o.OrdinaryRuntime)
    a.runtime=SimpleNamespace(stopped=True)
    a.tracked_processes={(202,'old-helper')}
    a.process_absent=lambda *args: False
    with pytest.raises(o.c.Invalid,match='exit no longer proven'): a.ensure_stopped({})
    a.process_absent=lambda *args: True
    a.d=SimpleNamespace(USAGE_COLLECTOR_LABEL='fixture')
    a.process=lambda *args: {'pid':None,'loaded':False,'provenance':{'processes':[{'pid':303,'start':'writer'}]}}
    a.job=lambda *args: {'pid':None,'loaded':False}
    with pytest.raises(o.c.Invalid,match='reappeared'): a.ensure_stopped({'prior_sha':'a','target_sha':'b'})


def test_concrete_stop_rejects_changed_sha_before_bootout():
    from types import SimpleNamespace
    from deploy.coordinated_runtime import Runtime
    calls=[]
    runtime=Runtime.__new__(Runtime)
    runtime.repo=Path('/private/tmp')
    runtime.service=SimpleNamespace(launchd_label='synthetic-label')
    runtime.runner=None
    runtime.d=SimpleNamespace(_launchd_state=lambda *a:SimpleNamespace(answered=True,loaded=True,pid=111),
        _process_start=lambda *a:'synthetic-birth', _capture_log_offsets=lambda *a:{},
        _daemon_log_paths=lambda *a:(), _verify_runtime_sha=lambda *a:(False,None))
    runtime.process_rows=lambda:[]
    runtime.checked=lambda *a:calls.append(a)
    with pytest.raises(o.c.Invalid,match='SHA changed before bootout'):
        runtime.stop({},expected_identity={'pid':111,'birth':'synthetic-birth','sha':'a'*40})
    assert not calls


def test_new_helper_requires_live_same_user_ancestry():
    from types import SimpleNamespace
    a=o.OrdinaryRuntime.__new__(o.OrdinaryRuntime)
    a.service=SimpleNamespace(launchd_label='fixture');a.runner=None
    a.job=lambda label:{'loaded':True,'pid':101,'birth':'root'}
    a.d=SimpleNamespace(_process_start=lambda pid,runner:{202:'helper',303:'unrelated'}.get(pid))
    a.runtime=SimpleNamespace(process_rows=lambda:[(101,1,os.getuid(),'root'),
        (202,101,os.getuid(),'helper'),(303,1,os.getuid(),'unrelated')])
    primary={'loaded':True,'pid':101,'birth':'root'}
    assert a.authenticate_new_processes(primary,[{'pid':202,'start':'helper'}])
    assert not a.authenticate_new_processes(primary,[{'pid':303,'start':'unrelated'}])
    assert not a.authenticate_new_processes(primary,[{'pid':202,'start':'reused'}])


def test_pre_stop_tracks_sealed_and_authorized_identities():
    from types import SimpleNamespace
    a=o.OrdinaryRuntime.__new__(o.OrdinaryRuntime)
    recorded=[]
    a.runtime=SimpleNamespace(stop=lambda receipt,provenance,expected_identity: recorded.extend(provenance['processes']))
    a.d=SimpleNamespace(USAGE_COLLECTOR_LABEL='fixture',RELOAD_UNLOAD_WAIT_S=0,
        _wait_launchd_unloaded=lambda *args,**kw:SimpleNamespace(answered=True,loaded=False,pid=None))
    a.runner=None
    process=process_snapshot((101,'root'))
    collector={'loaded':False,'pid':None,'birth':None}
    now={'source':'a','files':{},'process':process,'collector':collector,'journal_sha256':'seal'}
    a.observe=lambda bundle:{k:v for k,v in now.items() if k!='journal_sha256'}
    a.job=lambda label:collector
    a.before_stop=lambda decision:None
    a.process_absent=lambda pid,birth:True
    a.compare_process=lambda expected,current:{'previous':[{'pid':101,'start':'root'}],
        'observed':[{'pid':101,'start':'root'}],'departed_absent':[],'added_owned':[]}
    decisions=[{'previous':[{'pid':202,'start':'authorized'}], 'observed':[]},
               {'previous':[], 'observed':[{'pid':101,'start':'root'}]}]
    bundle={'metadata':{'prior_process':{'provenance':{'processes':[{'pid':303,'start':'sealed'}]}}}}
    a.stop(bundle,now,decisions)
    assert {(row['pid'],row['start']) for row in recorded}=={(101,'root'),(202,'authorized'),(303,'sealed')}


def test_sealed_prior_pid_reuse_refuses_before_stop():
    a=o.OrdinaryRuntime.__new__(o.OrdinaryRuntime)
    a.process_absent=lambda pid,birth:False
    bundle={'metadata':{'prior_process':{'provenance':{'processes':[{'pid':202,'start':'old'}]}}}}
    with pytest.raises(o.c.Invalid,match='PID reused'):
        a.verify_sealed_prior(bundle,process_snapshot((101,'root'),(202,'new')))
    with pytest.raises(o.c.Invalid,match='exit unproven'):
        a.verify_sealed_prior(bundle,process_snapshot((101,'root')))


def test_replacement_controller_grant_survives_restoration_revalidation(operation):
    a,b,prior,target,git=operation;forward(operation)
    synthetic_operation='08826e95f2d347f498d8caea51d4ad13'
    body={k:v for k,v in b.bundle.items() if k!='sha256'};body['operation']=synthetic_operation
    b.bundle=o.sealed_write(b.folder/'bundle.json',body)
    journal={k:v for k,v in b.journal.items() if k!='sha256'}
    journal.update(operation=synthetic_operation,bundle_sha256=b.bundle['sha256'])
    b.journal=o.sealed_write(b.folder/'journal.json',journal)
    pointer=o.c.state_root(a.repo)/'ordinary-active.json'
    o.c.atomic_json(pointer,{'operation':synthetic_operation,'bundle':str(b.folder)})
    auth=authority(a,b);payload=o.c.read_json(auth)
    payload.update(replacement_controller_sha='e'*40,authorized_by='fixture FD')
    o.c.atomic_json(auth,payload)
    probes=a.probes();probes['controller_sha']='e'*40;a.save_probes(probes)
    original_bundle=(b.folder/'bundle.json').read_bytes()
    assert invoke(a,b,auth)==d.EXIT_PRIOR_RESTORED
    journal=o.sealed_read(b.folder/'journal.json')
    assert journal['controller_used']=='e'*40
    assert journal['controller_authorization_sha256']==o.c.file_digest(auth)
    assert (b.folder/'bundle.json').read_bytes()==original_bundle


def manual_failure(operation):
    a,b,prior,target,git=operation
    forward(operation)
    historical={'loaded':True,'pid':101,'birth':'synthetic-prior','sha':prior,
                'provenance':{'launchd_answered':True,'pid':101,
                              'processes':[{'pid':101,'start':'synthetic-prior'}]}}
    b.persist(phase='restoring')
    b.persist(phase='starting',process=historical,allowed_source=[prior])
    b.persist(phase='recovery_required',result={'disposition':'target not active, restore unproven',
                                              'failure':'synthetic Store startup deadline'})
    assert b.journal['sequence']==9
    probes=a.probes()
    probes['process']={'loaded':True,'pid':32202,'birth':'synthetic-manual-birth','sha':target,
                       'provenance':{'launchd_answered':True,'pid':32202,
                                     'processes':[{'pid':32202,'start':'synthetic-manual-birth'}]}}
    a.save_probes(probes)
    return copy.deepcopy(b.journal)


@pytest.fixture
def manual_operation(operation, monkeypatch):
    a,b,_,target,_=operation
    failed=manual_failure(operation)
    operator=a.root/'operator-receipt'
    o.c.atomic_bytes(operator,b'synthetic operator authority and incident recovery receipt\n')
    pointer=o.c.state_root(a.repo)/'ordinary-active.json'
    journal=b.folder/'journal.json'
    immutable=b.folder/'bundle.json'
    pins={'operation':b.bundle['operation'],'bundle_sha256':b.bundle['sha256'],
          'bundle_bytes_sha256':o.c.file_digest(immutable),'pointer_bytes_sha256':o.c.file_digest(pointer),
          'journal_sha256':failed['sha256'],'journal_bytes_sha256':o.c.file_digest(journal),
          'operator_receipt_sha256':o.c.file_digest(operator)}
    monkeypatch.setattr(o,'MANUAL_LEGACY',pins)

    replacement='e'*40
    probes=a.probes()
    probes.update(controller_sha=replacement,boot_uuid='0d38fe9d-6292-414c-8ecf-ab400a1fc404',
                  listener=o.MANUAL_LISTENER,working_directory='/')
    a.save_probes(probes)
    def controller(self,bundle,sha):
        assert bundle['sha256']==pins['bundle_sha256']
        if self.probes()['controller_sha']!=sha:
            raise o.c.Invalid('synthetic controller identity drift')

    def observe(self,bundle):
        p=self.probes()
        return {**self.observe(bundle),'boot_uuid':p['boot_uuid'],'listener':p['listener'],
                'boot_evidence':'paired',
                'working_directory':p['working_directory'],
                'launchd_label':d.V2_DAEMON_LABEL,'launch_arguments_sha256':'4'*64}

    monkeypatch.setattr(SyntheticRuntime,'validate_controller',controller)
    monkeypatch.setattr(SyntheticRuntime,'observe_manual',observe)
    auth=a.root/'manual-authority.json'
    expected={'schema':o.MANUAL_SCHEMA,**pins,'failed_sequence':9,'original_controller_sha':'f'*40,
              'prior_sha':b.bundle['prior_sha'],'target_sha':target,'deployer_stream_id':'synthetic-owner',
              'replacement_controller_sha':replacement,'expected_serving':a.observe_manual(b.bundle)}
    o.c.atomic_json(auth,expected)
    from _shared import operator_auth
    token=a.root/'operator-credential'
    envelope=operator_auth.encode_envelope('12345678-1234-4234-8234-123456789abc','pentacle',bytes(32))
    o.c.atomic_bytes(token,envelope.encode())
    go='synthetic-current-go'
    binding={'type':'assistant.binding.ok','composite_stream_id':'fixture:assistant','stream_id':'fixture:desk',
             'generation':'fd-generation','revision':5,'source':'durable'}
    tell={'tell_id':go,'ledger_row_id':10,'from_stream_id':binding['stream_id'],'sender_verified':True,'sender_kind':'peer',
          'sender_generation':binding['generation'],'to_stream_id':'synthetic-owner',
          'original_to_stream_id':'synthetic-owner','delivery_status':'delivered',
          'delivered_at':datetime.now(timezone.utc).isoformat(),
          'text':f'GO record-ordinary-manual-recovery {pins["operation"]} {o.c.file_digest(auth)} {replacement}'}
    ledger={'type':'ledger_get.ok','tell_id':go,'ledger_row_id':10,'tell':tell,'read_only':True,
            'source':'v2_tell_deliveries'}
    wire={'binding':binding,'ledger':ledger,'requests':[],'runtime_sha':target,'on_request':{}}
    class Socket:
        def __init__(self):
            self.pending=[{'type':'welcome','runtime_sha':wire['runtime_sha'],
                           'auth':{'operator':{'nonce':operator_auth.encode_b64url(bytes(32))}}}]
        def __enter__(self):
            return self
        def __exit__(self,*error):
            if 'on_disconnect' in wire:
                wire['on_disconnect']()
        def send(self,data):
            frame=json.loads(data)
            verb=frame['type']
            wire['requests'].append(verb)
            if verb=='hello':
                assert frame['auth_v2']['proof']
                response={'type':'hello'}
                if wire.get('reject_hello'):
                    response={'type':'hello.error'}
            elif verb=='assistant.binding':
                response=wire['binding']
                if wire.get('role_drift') and wire['requests'].count(verb)>1:
                    response={**response,'revision':6}
            elif verb=='inspect_stream':
                assert frame['stream_id']==binding['stream_id'] and frame['event_tail']==0
                response={'type':'inspect_stream.ok','stream_id':binding['stream_id'],
                          'session':{'status':'closed' if wire.get('closed_fd') else 'open',
                                     'session_generation':binding['generation']}}
            else:
                assert verb=='ledger_get'
                response={'type':'ledger_get.error'} if frame.get('tell_id')!=go else wire['ledger']
                if 'after_request' in wire:
                    wire['after_request']()
            hook=wire['on_request'].get((verb,wire['requests'].count(verb)))
            if hook is not None:
                hook()
            self.pending.append({**response,'request_id':frame['request_id']})
        def recv(self,*,timeout):
            assert timeout>0
            return json.dumps(self.pending.pop(0))

    def connect(url,**options):
        assert url=='ws://127.0.0.1:7791' and options['origin'] and options['proxy'] is None
        return Socket()

    monkeypatch.setattr(d,'connect',connect)
    args=['--record-ordinary-manual-recovery',str(b.folder),'--manual-recovery-authorization',str(auth),
          '--operator-recovery-receipt',str(operator),'--manual-recovery-operator-credential',str(token),
          '--recovery-go-tell-id',go]
    return a,b,failed,pointer,journal,immutable,auth,operator,wire,args


def test_manual_disposition_cli_closes_old_fence(manual_operation, capsys):
    a,b,failed,pointer,journal,immutable,_,_,wire,args=manual_operation
    protected_before=(pointer.read_bytes(),immutable.read_bytes(),a.observe(b.bundle))
    failed_bytes=journal.read_bytes()
    with pytest.raises(o.c.Invalid,match='unfinished'):
        o.fence(a.repo)
    assert invoke(a,b,authority(a,b))==d.EXIT_REFUSED
    assert journal.read_bytes()==failed_bytes
    assert cli(a.root,args)==d.EXIT_OK
    rendered=json.loads(capsys.readouterr().out)
    terminal=o.sealed_read(journal)
    audit=terminal['manual_recovery_audit']
    assert terminal['sequence']==10 and terminal['phase']=='aborted'
    assert terminal['result']==rendered
    assert rendered['outcome']==o.MANUAL_OUTCOME and rendered['full_fleet_acceptance'] is False
    assert base64.b64decode(audit['original_journal_bytes'])==failed_bytes
    assert audit['original_failure']==failed['result']
    assert terminal['process']==failed['process']
    assert audit['serving']['process']['pid']==32202
    assert audit['serving']['working_directory']=='/'
    assert audit['authenticated_readback']['ledger_row_id']==10
    assert wire['requests']==['hello','assistant.binding','ledger_get','inspect_stream','assistant.binding']
    o.fence(a.repo)  # This fence is byte-for-byte unchanged from #656.
    assert (pointer.read_bytes(),immutable.read_bytes(),a.observe(b.bundle))==protected_before
    assert not (a.root/'events').exists()

    terminal_bytes=journal.read_bytes()
    wire['requests'].clear()
    assert cli(a.root,args)==d.EXIT_OK
    assert json.loads(capsys.readouterr().out)==rendered
    assert journal.read_bytes()==terminal_bytes and not wire['requests']
    assert invoke(a,b,authority(a,b))==d.EXIT_REFUSED


@pytest.mark.parametrize('damage',[
    'operation','pointer','sequence','journal','bundle','receipt','no_auth','auth_state',
    'wrong_go','go_text','unverified_fd','fd_generation','role_drift','closed_fd','stale_go','hello_error',
    'request_error','release','pid','birth','boot','listener','stamp','controller','file_drift',
    'cas_pointer','cas_auth','cas_process','proof_expired',
])
def test_manual_disposition_mismatch_keeps_fence(manual_operation, damage, monkeypatch):
    a,b,_,pointer,journal,immutable,auth,operator,wire,args=manual_operation
    if damage in ('operation','auth_state'):
        value=o.c.read_json(auth)
        value['operation' if damage=='operation' else 'expected_serving']='foreign'
        o.c.atomic_json(auth,value)
    elif damage in ('pointer','journal','bundle'):
        path={'pointer':pointer,'journal':journal,'bundle':immutable}[damage]
        with path.open('ab') as out:
            out.write(b' ')
    elif damage=='sequence':
        value=o.sealed_read(journal)
        value.pop('sha256')
        o.sealed_write(journal,{**value,'sequence':8})
    elif damage=='receipt':
        o.c.atomic_bytes(operator,b'foreign')
    elif damage=='no_auth':
        auth.unlink()
    elif damage=='wrong_go':
        args[-1]='other-go'
    elif damage=='go_text':
        wire['ledger']['tell']['text']='old authorization'
    elif damage=='unverified_fd':
        wire['ledger']['tell']['sender_verified']=False
    elif damage=='fd_generation':
        wire['ledger']['tell']['sender_generation']='retired'
    elif damage in ('role_drift','closed_fd'):
        wire[damage]=True
    elif damage=='stale_go':
        wire['ledger']['tell']['delivered_at']=(datetime.now(timezone.utc)-timedelta(minutes=11)).isoformat()
    elif damage=='hello_error':
        wire['reject_hello']=True
    elif damage=='request_error':
        wire['ledger']['type']='ledger_get.error'
    elif damage=='release':
        wire['runtime_sha']='1'*40
    elif damage in ('pid','birth','boot','listener','controller'):
        p=a.probes()
        if damage in ('pid','birth'):
            p['process'][damage]=17 if damage=='pid' else 'foreign'
        else:
            p[{'boot':'boot_uuid','controller':'controller_sha'}.get(damage,damage)]='foreign'
        a.save_probes(p)
    elif damage in ('stamp','file_drift'):
        (a.root/'files'/('stamp' if damage=='stamp' else 'plist')).write_bytes(b'foreign')
    elif damage in ('cas_pointer','cas_auth'):
        def during_request():
            path=pointer if damage=='cas_pointer' else auth
            with path.open('ab') as out:
                out.write(b' ')
        wire['after_request']=during_request
    elif damage=='cas_process':
        def during_request():
            p=a.probes()
            p['process']['pid']=70
            a.save_probes(p)
        wire['after_request']=during_request
    elif damage=='proof_expired':
        monkeypatch.setattr(o,'MANUAL_PROOF_SECONDS',0.0)
    before=(pointer.read_bytes(),journal.read_bytes(),immutable.read_bytes(),a.observe(b.bundle))
    assert cli(a.root,args)==d.EXIT_REFUSED
    after=(pointer.read_bytes(),journal.read_bytes(),immutable.read_bytes(),a.observe(b.bundle))
    assert after[1:3]==before[1:3]
    if damage!='cas_pointer':
        assert after[0]==before[0]
    if damage!='cas_process':
        assert after[3]==before[3]
    assert not (a.root/'events').exists()
    with pytest.raises(o.c.Invalid,match='unfinished'):
        o.fence(a.repo)


@pytest.mark.parametrize('edge,damage',[
    pytest.param(('inspect_stream',1),'process',id='inspect-process'),
    pytest.param(('assistant.binding',2),'stamp',id='final-binding-stamp'),
    pytest.param(None,'process',id='post-rpc-disconnect-process'),
    pytest.param(None,'cwd',id='post-rpc-disconnect-cwd'),
])
def test_manual_disposition_rejects_late_serving_drift(manual_operation, monkeypatch, edge, damage):
    a,b,_,pointer,journal,immutable,auth,operator,wire,args=manual_operation
    protected=(pointer,journal,immutable,auth,operator)
    before=[path.read_bytes() for path in protected]
    injected=[]
    journal_writes=[]
    def change_serving():
        if damage=='process':
            probes=a.probes()
            probes['process'].update(pid=33303,birth='synthetic-replacement-birth')
            probes['boot_uuid']=str(uuid4())
            a.save_probes(probes)
        elif damage=='cwd':
            probes=a.probes()
            probes['working_directory']='/different'
            a.save_probes(probes)
        else:
            (a.root/'files'/'stamp').write_bytes(b'synthetic external stamp drift')
        injected.append(a.observe_manual(b.bundle))

    if edge is None:
        wire['on_disconnect']=change_serving
    else:
        wire['on_request'][edge]=change_serving

    atomic_bytes=o.c.atomic_bytes
    def record_writes(path,data,**options):
        if path==journal:
            journal_writes.append(path)
        return atomic_bytes(path,data,**options)
    monkeypatch.setattr(o.c,'atomic_bytes',record_writes)

    status=cli(a.root,args)
    assert len(injected)==1
    assert status==d.EXIT_REFUSED and not journal_writes, 'late serving drift reached terminal publication'
    assert [path.read_bytes() for path in protected]==before
    assert a.observe_manual(b.bundle)==injected[0]
    assert not (a.root/'events').exists()
    with pytest.raises(o.c.Invalid,match='unfinished'):
        o.fence(a.repo)


@pytest.mark.parametrize('stage',['before_commit','after_commit'])
def test_manual_disposition_atomic_audit_and_replay(manual_operation, monkeypatch,stage):
    a,b,_,pointer,journal,immutable,auth,_,wire,args=manual_operation
    before=(pointer.read_bytes(),journal.read_bytes(),immutable.read_bytes(),a.observe(b.bundle))
    real=o.c.atomic_bytes
    def interrupted(path,data,**kwargs):
        assert path==journal
        if stage=='after_commit':
            real(path,data,**kwargs)
        raise OSError('synthetic commit interrupted')
    with monkeypatch.context() as patch:
        patch.setattr(o.c,'atomic_bytes',interrupted)
        assert cli(a.root,args)==d.EXIT_REFUSED
    assert (pointer.read_bytes(),immutable.read_bytes(),a.observe(b.bundle))==(before[0],before[2],before[3])
    if stage=='before_commit':
        assert journal.read_bytes()==before[1]
        with pytest.raises(o.c.Invalid,match='unfinished'):
            o.fence(a.repo)
    else:
        assert base64.b64decode(o.sealed_read(journal)['manual_recovery_audit']['original_journal_bytes'])==before[1]
    assert cli(a.root,args)==d.EXIT_OK
    after=journal.read_bytes()
    different_go=[*args[:-1],'other-fresh-go']
    assert cli(a.root,different_go)==d.EXIT_REFUSED
    value=o.c.read_json(auth)
    o.c.atomic_json(auth,{**value,'replacement_controller_sha':'a'*40})
    assert cli(a.root,args)==d.EXIT_REFUSED
    assert journal.read_bytes()==after
    assert not (a.root/'events').exists()


def test_manual_disposition_cli_uses_common_lock(manual_operation):
    a,_,_,_,journal,_,_,_,wire,args=manual_operation
    before=journal.read_bytes()
    with o.c.transaction_lock(a.repo):
        assert cli(a.root,args)==d.EXIT_REFUSED
    assert journal.read_bytes()==before
    assert not wire['requests']


@pytest.mark.parametrize(('has_workdir', 'drift'), [
    pytest.param(True, None, id='None'),
    pytest.param(False, None, id='default-cwd'),
    pytest.param(True, 'launch_arguments', id='launch_arguments'),
    pytest.param(True, 'listener_pid', id='listener_pid'),
    pytest.param(True, 'boot', id='boot'),
    pytest.param(True, 'stamp_release', id='stamp_release'),
    pytest.param(True, 'managed_input', id='managed_input'),
    (True, 'plist_cwd'),
    (True, 'cwd'),
    (False, 'cwd'),
    (False, 'cwd_pid'),
    (False, 'cwd_missing'),
    pytest.param(False, 'rotated_stderr', id='rotated-stderr'),
    pytest.param(False, 'preboot_stderr', id='preboot-stderr'),
    pytest.param(False, 'later_stderr_boot', id='later-stderr-boot'),
    pytest.param(False, 'malformed_stderr', id='malformed-stderr'),
])
def test_concrete_manual_observation_binds_runtime_and_boot(manual_operation, monkeypatch, has_workdir, drift):
    a,b,_,_,_,_,_,_,_,_=manual_operation
    label=d.V2_DAEMON_LABEL
    workdir=a.repo/'services/chat-stream-v2'
    python=d._venv_python(a.repo,a.service)
    argv=[str(python),str(workdir/'main.py'),'--host','127.0.0.1','--port','7791']
    launchd=f'\n program = {argv[0]}\n arguments = {{\n'+'\n'.join(argv)+'\n }\n'
    paths=a.paths(b.bundle['metadata'])
    plist = {'Label': label, 'ProgramArguments': argv}
    if has_workdir:
        plist['WorkingDirectory'] = '/different' if drift == 'plist_cwd' else str(workdir)
    paths['plist'].write_bytes(plistlib.dumps(plist))
    expected_cwd = str(workdir) if has_workdir else '/'
    source=a.git('rev-parse','HEAD')
    paths['stamp'].write_bytes(o.c.canonical({'sha':source if drift!='stamp_release' else '1'*40}))
    if drift=='managed_input':
        paths['settings'].write_bytes(b'configuration changed')
    process={**a.probes()['process'], 'birth':'Thu Jan 1 00:00:10 1970'}
    generation={'pid':process['pid'],'start':process['birth'],'boot_uuid':a.probes()['boot_uuid']}
    logs=[a.root/'stdout.log',a.root/'stderr.log']
    for index,path in enumerate(logs):
        ident={**generation,'boot_uuid':str(uuid4())} if drift=='boot' and index else generation
        path.write_text(f'generation={json.dumps(ident)} chat_streamd_v2 listening on {o.MANUAL_LISTENER}\n')
    monkeypatch.setattr(d,'_daemon_log_paths',lambda service: logs)
    monkeypatch.setattr(d,'_managed_log_config',lambda service: None)
    if drift in {'rotated_stderr', 'preboot_stderr', 'later_stderr_boot', 'malformed_stderr'}:
        stderr = logs[1]
        older_stderr = stderr.with_suffix('.log.1')
        stderr.write_text('current diagnostic\n')
        older_stderr.write_text('earlier diagnostic\n')
        if drift == 'later_stderr_boot':
            ident = {**generation, 'pid': process['pid'] + 1, 'boot_uuid': str(uuid4())}
            older_stderr.write_text(f'generation={json.dumps(ident)} chat_streamd_v2 listening on {o.MANUAL_LISTENER}\n')
        if drift == 'malformed_stderr':
            older_stderr.write_text(f'generation=invalid chat_streamd_v2 listening on {o.MANUAL_LISTENER}\n')
        monkeypatch.setattr(d, '_managed_log_config', lambda service: SimpleNamespace(
            paths=lambda path: [stderr, older_stderr] if path == stderr else [path]))
        real_stat = Path.stat

        def stat(path, *args, **kwargs):
            observed = real_stat(path, *args, **kwargs)
            if path not in (stderr, older_stderr):
                return observed
            birth = 10.5 if path == older_stderr and drift == 'preboot_stderr' else 20
            return SimpleNamespace(st_mode=observed.st_mode, st_size=observed.st_size,
                                   st_dev=observed.st_dev, st_ino=observed.st_ino, st_birthtime=birth)

        monkeypatch.setattr(Path, 'stat', stat)
    monkeypatch.setattr(a,'job',lambda target: {key:process[key] for key in ('loaded','pid','birth')}
                        if target==label else a.probes()['collector'])

    def checked(cmd):
        if cmd[0]=='launchctl':
            return SimpleNamespace(stdout=launchd.replace('--host','--changed') if drift=='launch_arguments' else launchd)
        if cmd[0]=='ps':
            return SimpleNamespace(stdout=' '.join(argv))
        assert cmd[0]=='/usr/sbin/lsof'
        if '-d' in cmd:
            assert cmd == ('/usr/sbin/lsof', '-nP', '-a', '-p', str(process['pid']), '-d', 'cwd', '-Fpn')
            pid = 1 if drift == 'cwd_pid' else process['pid']
            cwd = '/different' if drift == 'cwd' else expected_cwd
            stdout = '' if drift == 'cwd_missing' else f'p{pid}\nfcwd\nn{cwd}\n'
            return SimpleNamespace(stdout=stdout)
        pid=1 if drift=='listener_pid' else process['pid']
        return SimpleNamespace(stdout=f'p{pid}\nn{o.MANUAL_LISTENER}\n')

    monkeypatch.setattr(a.runtime,'checked',checked)
    if drift and drift != 'rotated_stderr':
        with pytest.raises((o.c.Invalid, ValueError),match='manual recovery|generation identities|boot marker'):
            o.OrdinaryRuntime.observe_manual(a,b.bundle)
    else:
        evidence=o.OrdinaryRuntime.observe_manual(a,b.bundle)
        assert evidence['source']==source and evidence['boot_uuid']==generation['boot_uuid']
        assert evidence['boot_evidence'] == ('stdout_stderr_rotated' if drift else 'paired')
        assert evidence['process']=={'sha':source,'loaded':True,'pid':process['pid'],'birth':process['birth']}
        assert evidence['launch_arguments_sha256']==o.c.digest(argv)
        assert evidence['working_directory'] == expected_cwd
        assert evidence['files']['stamp']==o.file_state(paths['stamp'])


@pytest.fixture
def supersession(manual_operation, monkeypatch, request):
    a, bundle, failed, pointer, journal, manifest, legacy_auth, switch, wire, legacy_args = manual_operation
    case = getattr(request, 'param', 1)
    target = bundle.bundle['target_sha']
    a.git('commit', '--allow-empty', '-qm', f'successor {case}')
    successor = a.git('rev-parse', 'HEAD')
    a.git('switch', '-qc', 'synthetic-launch')
    a.git('commit', '--allow-empty', '-qm', 'later checkout')
    paths = a.paths(bundle.bundle['metadata'])
    stamp = {'sha': target, 'serving': False}
    paths['stamp'].write_bytes(o.c.canonical(stamp))
    failed['allowed_files']['stamp'] = [o.file_state(paths['stamp'])]
    failed['allowed_source'] = [target]
    failed.pop('sha256')
    failed = o.sealed_write(journal, failed)
    pid = 400 + case
    birth = f'Tue Nov {10 + case} 01:02:03 2026'
    boot = str(uuid4())
    marker = {'pid': pid, 'start': birth, 'boot_uuid': boot}
    generation = {'pid': pid, 'birth': birth, 'sha': successor}
    wire['runtime_sha'] = successor
    process = {'loaded': True, **generation}
    label = a.service.launchd_label
    python = d._venv_python(a.repo, a.service)
    argv = [str(python), str(a.repo/'services/chat-stream-v2/main.py'), '--port', '7791']
    paths['plist'].write_bytes(plistlib.dumps({'Label': label, 'ProgramArguments': argv}))
    logs = [a.root/'stdout.log', a.root/'stderr.log']
    for path in logs:
        path.write_text(f'generation={json.dumps(marker)} chat_streamd_v2 listening on {o.MANUAL_LISTENER}\n')
    monkeypatch.setattr(d, '_daemon_log_paths', lambda service: logs)
    monkeypatch.setattr(d, '_managed_log_config', lambda service: None)
    monkeypatch.setattr(SyntheticRuntime, 'job', lambda self, name: (
        {k: process[k] for k in ('loaded', 'pid', 'birth')} if name == label else self.probes()['collector']))
    commands = []
    original_init = SyntheticRuntime.__init__

    def install_reader(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        run = self.runtime.checked

        def checked(cmd, *args, **kwargs):
            commands.append(tuple(cmd))
            if cmd[0] == 'git':
                assert cmd[1:3] in (('bundle', 'verify'), ('bundle', 'list-heads'), ('clone', '--bare'))
                return run(cmd, *args, **kwargs)
            if tuple(cmd[:2]) == ('launchctl', 'print'):
                out = f'\n program = {argv[0]}\n arguments = {{\n' + '\n'.join(argv) + '\n }\n'
            elif cmd[0] == 'ps':
                out = ' '.join(argv)
            elif cmd[0] == '/usr/sbin/lsof':
                out = f'p{process["pid"]}\n' + ('fcwd\nn/\n' if '-d' in cmd else f'n{o.MANUAL_LISTENER}\n')
            else:
                pytest.fail(f'unexpected process command {cmd!r}')
            return SimpleNamespace(stdout=out)
        self.runtime.checked = checked

    monkeypatch.setattr(SyntheticRuntime, '__init__', install_reader)
    monkeypatch.setattr(SyntheticRuntime, 'observe_manual', o.OrdinaryRuntime.observe_manual)
    for name in ('stop', 'start', 'restore_source', 'ensure_stopped'):
        monkeypatch.setattr(SyntheticRuntime, name, lambda *args: pytest.fail('process control or restore'))
    monkeypatch.setattr(os, 'kill', lambda *args: pytest.fail('signal'))
    monkeypatch.setattr(os, 'killpg', lambda *args: pytest.fail('signal'))
    o.c.atomic_json(switch, {'outcome': 'serving', 'prior': target, 'target': successor,
                             'old_pid': 202, 'live': generation})
    authority = a.root/'supersession-authority.json'
    owner = 'synthetic-owner'
    controller = o.c.read_json(legacy_auth)['replacement_controller_sha']
    pins = {'operation': failed['operation'], 'sequence': failed['sequence'],
            'journal_bytes_sha256': o.c.file_digest(journal), 'bundle_bytes_sha256': o.c.file_digest(manifest),
            'pointer_bytes_sha256': o.c.file_digest(pointer)}
    serving = {'source': a.git('rev-parse', 'HEAD'), 'checkout': str(a.repo),
               'branch': 'refs/heads/synthetic-launch', 'process': dict(process), 'boot_uuid': boot,
               'boot_evidence': 'paired', 'listener': o.MANUAL_LISTENER, 'launchd_label': label,
               'launch_arguments_sha256': o.c.digest(argv), 'working_directory': '/',
               'files': {key: o.file_state(path) for key, path in paths.items()}, 'collector': a.probes()['collector']}
    auth = {'schema': 'pentacle.ordinary-superseded.v1', 'failed': pins, 'expected_serving': serving,
            'successor_receipt_sha256': o.c.file_digest(switch), 'replacement_controller_sha': controller,
            'deployer_stream_id': owner}
    o.c.atomic_json(authority, auth)
    go = legacy_args[-1]
    wire['ledger']['tell']['text'] = f'GO supersede-ordinary {failed["operation"]} {o.c.file_digest(authority)} {controller}'
    args = ['--release-checkout', str(a.repo), '--bundle', str(bundle.folder), '--authorization', str(authority),
            '--successor-receipt', str(switch), '--operator-credential', str(a.root/'operator-credential'),
            '--deployer-stream-id', owner, '--go-tell-id', go]
    monkeypatch.setattr(o, 'OrdinaryRuntime', SyntheticRuntime)
    # The real controller may launch only these read-only Git commands (plus a
    # bare validation clone in a temporary directory). OS probes above are synthetic.
    real_popen = subprocess.Popen
    def call():
        def checked_popen(cmd, *args, **kwargs):
            assert not kwargs.get('shell') and cmd[0] == 'git', f'unexpected process control: {cmd!r}'
            assert cmd[1] in ('rev-parse', 'bundle') or tuple(cmd[1:3]) == ('clone', '--bare'), cmd
            return real_popen(cmd, *args, **kwargs)
        with monkeypatch.context() as guard:
            guard.setattr(subprocess, 'Popen', checked_popen)
            return o.supersede_main(args)

    monkeypatch.setattr(o, 'MANUAL_LEGACY', {})
    return SimpleNamespace(a=a, b=bundle, args=args, call=call, switch=switch, auth=authority, process=process,
                           pointer=pointer, journal=journal, manifest=manifest, serving=serving, commands=commands,
                           wire=wire, go=go)


@pytest.mark.parametrize('supersession', [1, 2], indirect=True)
def test_superseded_successor_and_idempotent_readback(supersession, capsys):
    s = supersession
    protected = supersession_files(s)
    with pytest.raises(o.c.Invalid, match='unfinished'):
        o.fence(s.a.repo)
    assert s.call() == 0
    result = json.loads(capsys.readouterr().out)
    path = Path(result['disposition_path'])
    receipt = o.sealed_read(path)
    assert result['outcome'] == 'superseded' and result['full_fleet_acceptance'] is False
    assert result['disposition_sha256'] == receipt['sha256']
    assert receipt['before'] == receipt['after'] == {'failed': o.c.read_json(s.auth)['failed'], 'serving': s.serving}
    assert receipt['switch']['live'] == {key: s.process[key] for key in ('sha', 'pid', 'birth')}
    assert receipt['authenticated_readback']['runtime_sha'] == s.serving['process']['sha']
    assert receipt['authority']['successor_receipt_sha256'] == o.c.file_digest(s.switch)
    assert receipt['authority']['authorization_sha256'] == o.c.file_digest(s.auth)
    assert s.serving['process']['sha'] != s.serving['source'] != s.b.bundle['target_sha']
    o.fence(s.a.repo)
    assert cli(s.a.root, ['--ordinary-recovery-status', str(s.b.folder)]) == d.EXIT_OK
    status = json.loads(capsys.readouterr().out)['ordinary_recovery_status']
    assert status['phase'] == 'superseded' and status['recorded_serving'] == s.serving
    assert status['disposition_sha256'] == receipt['sha256']
    recorded = path.read_bytes(), path.stat().st_mtime_ns
    assert s.call() == 0
    assert json.loads(capsys.readouterr().out) == result
    assert (path.read_bytes(), path.stat().st_mtime_ns) == recorded
    protected[0][path] = recorded[0]
    assert supersession_files(s) == protected
    assert not (s.a.root/'events').exists()


def supersession_files(s):
    """Include the absence of a disposition, receipt bytes, live files and process state."""
    paths = {s.switch, s.auth, s.pointer, *s.b.folder.iterdir(), *o.c.state_root(s.a.repo).iterdir(),
             *(s.a.root/'files').iterdir()}
    return ({p: p.read_bytes() if p.is_file() else None for p in paths},
            copy.deepcopy(s.process), s.wire['runtime_sha'], s.a.probes(), s.a.git('rev-parse', 'HEAD'),
            s.a.git('rev-parse', '--symbolic-full-name', 'HEAD'))


def approve_supersession(s, auth):
    o.c.atomic_json(s.auth, auth)
    s.wire['ledger']['tell']['text'] = (f'GO supersede-ordinary {auth["failed"]["operation"]} '
                                       f'{o.c.file_digest(s.auth)} {auth["replacement_controller_sha"]}')


def assert_supersession_refused(s, capsys, reason):
    before = supersession_files(s)
    assert s.call() == d.EXIT_REFUSED
    assert reason in capsys.readouterr().err
    assert supersession_files(s) == before
    with pytest.raises(o.c.Invalid, match='unfinished'):
        o.fence(s.a.repo)


@pytest.mark.parametrize('key,value,reason', [
    ('failed.operation', '1'*32, 'authority'), ('failed.sequence', 3, 'authority'),
    ('failed.bundle_bytes_sha256', '0'*64, 'authority'), ('failed.journal_bytes_sha256', '0'*64, 'authority'),
    ('failed.pointer_bytes_sha256', '0'*64, 'authority'), ('replacement_controller_sha', '1'*40, 'controller'),
    ('successor_receipt_sha256', '0'*64, 'authority'), ('expected_serving.source', '0'*40, 'checkout'),
    ('expected_serving.checkout', '/other-checkout', 'checkout'),
    ('expected_serving.branch', 'refs/heads/another-branch', 'checkout'),
    ('expected_serving.boot_uuid', 'wrong-boot', 'generation drift'),
])
def test_superseded_mismatched_authority(supersession, capsys, key, value, reason):
    s = supersession
    auth = o.c.read_json(s.auth)
    section, _, field = key.rpartition('.')
    (auth[section] if section else auth)[field] = value
    approve_supersession(s, auth)
    assert_supersession_refused(s, capsys, reason)


@pytest.mark.parametrize('damage,reason', [
    ('prior', 'distinct serving'), ('receipt_pid', 'generation drift'), ('receipt_birth', 'generation drift'),
    ('receipt_sha', 'distinct serving'), ('same_release', 'distinct serving'), ('same_pid', 'distinct serving'),
    ('same_birth', 'distinct serving'), ('missing_identity', 'birth'), ('wrong_outcome', 'distinct serving'),
])
def test_superseded_inconsistent_switch(supersession, capsys, damage, reason):
    s = supersession
    switch = o.c.read_json(s.switch)
    live = switch['live']
    changes = {
        'prior': lambda: switch.update(prior=s.b.bundle['prior_sha']),
        'receipt_pid': lambda: live.update(pid=live['pid'] + 1),
        'receipt_birth': lambda: live.update(birth=live['birth'] + ' changed'),
        'receipt_sha': lambda: live.update(sha=s.serving['source']),
        'same_pid': lambda: switch.update(old_pid=live['pid']),
        'same_birth': lambda: live.update(birth=s.b.journal['process']['birth']),
        'missing_identity': lambda: live.pop('birth'),
        'wrong_outcome': lambda: switch.update(outcome='failed'),
    }
    if damage == 'same_release':
        switch['target'] = live['sha'] = s.b.bundle['target_sha']
    else:
        changes[damage]()
    o.c.atomic_json(s.switch, switch)
    auth = o.c.read_json(s.auth)
    auth['successor_receipt_sha256'] = o.c.file_digest(s.switch)
    approve_supersession(s, auth)
    assert_supersession_refused(s, capsys, reason)


@pytest.mark.parametrize('damage,reason', [
    ('receipt_absent', 'No such file'), ('actual_source', 'release mismatch'), ('stamp_bytes', 'checkout'),
    ('stamp_sha', 'stamp unavailable'), ('malformed_stamp', 'stamp unavailable'),
    ('pid', 'boot marker'), ('birth', 'boot marker'),
    ('stale_journal', 'authority'), ('original_bundle', 'authority'), ('original_pointer', 'authority'),
    ('archive', 'source bundle corrupt'), ('wrong_go', 'FD GO'), ('expired_go', 'FD GO'),
])
def test_superseded_readback_drift(supersession, capsys, damage, reason):
    s = supersession
    stamp = s.a.root/'files/stamp'
    tell = s.wire['ledger']['tell']
    def change_bytes(path):
        path.write_bytes(path.read_bytes() + b'\n')
    changes = {
        'receipt_absent': s.switch.unlink,
        'actual_source': lambda: s.wire.update(runtime_sha=s.serving['source']),
        'stamp_bytes': lambda: change_bytes(stamp),
        'malformed_stamp': lambda: stamp.write_bytes(b'[]'),
        'stamp_sha': lambda: stamp.write_bytes(o.c.canonical({'sha': s.serving['source']})),
        'pid': lambda: s.process.update(pid=s.process['pid'] + 1),
        'birth': lambda: s.process.update(birth=s.process['birth'] + ' changed'),
        'stale_journal': lambda: change_bytes(s.journal),
        'original_bundle': lambda: change_bytes(s.manifest),
        'original_pointer': lambda: change_bytes(s.pointer),
        'archive': lambda: (s.b.folder/'source.bundle').write_bytes(b'damaged'),
        'wrong_go': lambda: tell.update(text=tell['text'].replace('supersede-ordinary', 'record-ordinary-manual-recovery')),
        'expired_go': lambda: tell.update(delivered_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()),
    }
    changes[damage]()
    assert_supersession_refused(s, capsys, reason)


def test_superseded_wrong_checkout_creates_no_state(supersession, capsys):
    s = supersession
    other = s.a.root/'other'
    other.mkdir()
    s.args[s.args.index('--release-checkout') + 1] = str(other)
    assert_supersession_refused(s, capsys, 'No such file')
    assert not o.c.state_root(other).exists()


@pytest.mark.parametrize('edge', ['under_lock', 'before_link', 'after_link'])
def test_superseded_refuses_lock_and_late_journal_mutation(supersession, monkeypatch, edge):
    s = supersession
    before = supersession_files(s)
    if edge == 'under_lock':
        with o.c.transaction_lock(s.a.repo):
            assert s.call() == d.EXIT_REFUSED
        assert supersession_files(s) == before
        return
    updated = o.sealed_read(s.journal)
    updated.pop('sha256')
    updated['sequence'] += 1
    damaged = o.c.canonical({**updated, 'sha256': o.c.digest(updated)}) + b'\n'
    if edge == 'before_link':
        stage = o.sealed_write
        def race(path, value):
            result = stage(path, value)
            if path.name.startswith('.supersession-'):
                s.journal.write_bytes(damaged)
            return result
        monkeypatch.setattr(o, 'sealed_write', race)
    else:
        publish = os.link
        def race(source, destination):
            publish(source, destination)
            s.journal.write_bytes(damaged)
        monkeypatch.setattr(os, 'link', race)
    assert s.call() == d.EXIT_REFUSED
    before[0][s.journal] = damaged  # Only the competing writer's change survives.
    if edge == 'after_link':
        path = o._disposition_path(s.a.repo, s.b.folder)
        before[0][path] = path.read_bytes()  # Durable PENDING remains fenced after interruption/drift.
    assert supersession_files(s) == before
    with pytest.raises(o.c.Invalid, match='pending' if edge == 'after_link' else 'unfinished'):
        o.fence(s.a.repo)


def _crash_after_durable_supersession_link(call, disposition_path):
    original_fsync = os.fsync
    def fsync_then_crash_on_directory(fd):
        result = original_fsync(fd)
        if stat.S_ISDIR(os.fstat(fd).st_mode) and disposition_path.exists():
            os._exit(73)
        return result
    os.fsync = fsync_then_crash_on_directory
    call()


def test_interrupted_after_durable_supersession_link_remains_fenced(supersession):
    s = supersession
    disposition_path = o._disposition_path(s.a.repo, s.b.folder)
    child = multiprocessing.get_context('fork').Process(target=_crash_after_durable_supersession_link,
                                                       args=(s.call, disposition_path))
    child.start()
    child.join(timeout=15)
    if child.is_alive():
        child.terminate()
        child.join(timeout=5)
        pytest.fail('synthetic supersession child did not reach the durable-link crash point')
    assert child.exitcode == 73
    assert disposition_path.is_file()
    with pytest.raises(o.c.Invalid, match='pending'):
        o.fence(s.a.repo)


def test_superseded_refuses_disposition_name_collision(supersession, monkeypatch):
    s = supersession
    path = o._disposition_path(s.a.repo, s.b.folder)
    original_link = os.link
    foreign = b'foreign collision bytes\n'
    def collide_once(source, destination):
        Path(destination).write_bytes(foreign)
        original_link(source, destination)
    monkeypatch.setattr(os, 'link', collide_once)
    before = supersession_files(s)
    assert s.call() == d.EXIT_REFUSED
    before[0][path] = foreign
    assert supersession_files(s) == before
    assert path.read_bytes() == foreign


def test_superseded_refuses_generation_change_during_publication(supersession, monkeypatch, capsys):
    s = supersession
    publish = os.link
    def switch_live(source, destination):
        publish(source, destination)
        s.wire['runtime_sha'] = s.serving['source']
    monkeypatch.setattr(os, 'link', switch_live)
    files_before, process, _, probes, head, branch = supersession_files(s)
    assert s.call() == d.EXIT_REFUSED
    assert 'release mismatch' in capsys.readouterr().err
    pending_path = o._disposition_path(s.a.repo, s.b.folder)
    files_before[pending_path] = pending_path.read_bytes()
    assert supersession_files(s) == (files_before, process, s.serving['source'], probes, head, branch)
    with pytest.raises(o.c.Invalid, match='pending'):
        o.fence(s.a.repo)


def test_superseded_refuses_recovery_and_stale_replay(supersession, capsys):
    s = supersession
    assert s.call() == d.EXIT_OK
    path = Path(json.loads(capsys.readouterr().out)['disposition_path'])
    before = supersession_files(s)
    assert cli(s.a.root, ['--recover-ordinary', str(s.b.folder), '--ordinary-recovery-authorization',
                          str(s.auth), '--recovery-go-tell-id', s.go]) == d.EXIT_REFUSED
    assert 'superseded operation is terminal' in capsys.readouterr().err
    assert supersession_files(s) == before
    s.args[-1] = 'another-go'
    assert s.call() == d.EXIT_REFUSED
    assert 'replay differs' in capsys.readouterr().err
    assert supersession_files(s) == before
    s.args[-1] = s.go
    s.journal.write_bytes(s.journal.read_bytes() + b'\n')
    stale = supersession_files(s)
    with pytest.raises(o.c.Invalid, match='no longer matches'):
        o.fence(s.a.repo)
    assert s.call() == d.EXIT_REFUSED
    assert supersession_files(s) == stale
    assert path.is_file()
