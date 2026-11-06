"""Rollback restoration must launch and close without any inference input."""
from pathlib import Path
import json
import subprocess
import sqlite3
import time
from types import SimpleNamespace

import pytest

from deploy import coordinated as c
from deploy.coordinated_runtime import code_identity_at
from .test_deploy_coordinated_helpers import runtime_preparation, transaction
from .test_deploy_log_guard import isolated_release
from .test_deploy_script import deploy_mod


def test_prior_prepare_never_plans_a_model_matrix(runtime_preparation, monkeypatch):
    runtime, fixture, intent, root = runtime_preparation
    actual = runtime.smoke_plan
    def plan(snapshot, *args, **kwargs):
        assert Path(snapshot).name != 'prior-source', 'rollback preparation invoked prior model/profile runner'
        return actual(snapshot, *args, **kwargs)
    monkeypatch.setattr(runtime, 'smoke_plan', plan)
    folder = root / 'no-prior-plan'; folder.mkdir(mode=0o700)
    result = runtime.prepare(intent, folder)
    assert 'test_profile' not in result['source']['prior']
    assert 'smoke_plan' not in result['source']['prior']
    assert result['source']['target']['test_profile']


def test_legacy_profile_module_passes_generic_helper_closure(runtime_preparation):
    runtime, fixture, intent, root = runtime_preparation
    # Exercise the legacy API shape without requiring Git history in public CI.
    # Actual archived prior bytes are qualified separately in local gate evidence.
    module = fixture.repo / 'services/_shared/spawn_profiles.py'
    data = module.read_bytes().replace(b'def resolve_test_profile(', b'def retired_test_profile(')
    assert b'def resolve_test_profile(' not in data
    module.write_bytes(data)
    fixture.git('commit', '-qam', 'legacy profile API shape')
    sha = fixture.git('rev-parse', 'HEAD')
    helper = {'target': {'name': 'fixture', 'local': True, 'ssh': 'localhost', 'root': str(root)},
              'python': __import__('sys').executable,
              'script': str(fixture.repo / 'services/chat-stream-v2/provider_account_host.py')}
    identity = code_identity_at(fixture.repo, sha, runtime.runner)
    proof = runtime.helper_proof(helper, {}, identity, {}, sha, verify_release=False, closure_only=True)
    assert proof['identity'] == identity
    assert 'test_profile' not in proof
    assert proof['modules'] and proof['dependencies']


def test_rollback_start_verify_never_invokes_model_smoke(runtime_preparation, monkeypatch):
    runtime, fixture, intent, root = runtime_preparation
    folder = root / 'no-input-start'; folder.mkdir(mode=0o700)
    receipt = {'intent': intent, 'transaction_directory': str(folder), 'receipt_path': str(root/'receipt'),
               'receipt_sha256': 'a'*64, **runtime.prepare(intent, folder)}
    runtime.old_pid = 10; runtime.old_start = 'old'; runtime.log_offsets = {}
    monkeypatch.setattr(runtime, 'validate_common', lambda r, **k: None)
    monkeypatch.setattr(runtime.d, 'verify_fresh_daemon_log_line', lambda *a, **k:
        deploy_mod.BootReadback(deploy_mod.BOOT_OBSERVED, 'fresh', 0, 20, 10))
    monkeypatch.setattr(runtime.d, '_verify_runtime_sha', lambda sha, pid: (True, {'sha': sha, 'pid': pid}))
    monkeypatch.setattr(runtime.d, '_process_start', lambda *a: 'fresh-birth')
    monkeypatch.setattr(runtime.d, '_scan_slow_consumer_window', lambda *a, **k: {'outcome': 'passed'})
    observed = []
    def restore(*args, **kwargs):
        observed.append('zero-input')
        return {'outcome': 'passed', 'mode': 'zero_input', 'cells': [{'outcome':'passed'}]}
    monkeypatch.setattr(runtime, 'restoration_proof', restore, raising=False)
    monkeypatch.setattr(runtime, 'dispatched_proofs', lambda *a: [{'fresh':True}])
    actual_runner = runtime.runner
    def runner(argv, cwd):
        assert argv[0] != '/usr/bin/env', 'rollback invoked paid smoke/profile runner'
        if argv[0] == 'launchctl': return subprocess.CompletedProcess(argv, 0, '', '')
        return actual_runner(argv, cwd)
    runtime.runner = runner
    result = runtime.start_verify(receipt, rollback=True)
    assert observed == ['zero-input']
    assert result['restoration']['outcome'] == 'passed'
    assert result['model_canary']['outcome'] == 'not_run_by_design'
    assert 'fleet_smoke' not in result


def test_legacy_rollback_never_resolves_test_profile(isolated_release, monkeypatch):
    module, service, repo, prior, target, before, calls, activate = isolated_release
    stamp = module._read_stamp(repo, service)
    stamp.update(prior_sha=target)
    module._write_stamp(repo, service, stamp)
    def forbidden(*a, **k):
        pytest.fail('direct rollback resolved a test profile')
    monkeypatch.setattr(module, '_release_test_profile', forbidden)
    stamp['prior_qualification'] = {'sha':target, 'stamp':{'sha':target,
        'daemon_runtime_readback':{'sha':target}, 'daemon_boot_readback':{'outcome':'observed'},
        'fleet_smoke':{'outcome':'passed','cells':[]}}}
    module._write_stamp(repo, service, stamp)
    result = activate(rollback=True)
    assert result['sha'] == target


@pytest.mark.parametrize("binding_count", [1, 5])
def test_zero_input_verifier_raw_rpc_and_owned_close(tmp_path, monkeypatch, binding_count):
    from contextlib import contextmanager
    from deploy import restoration
    from _shared.spawn_profiles import catalog
    wire = []; closed = []; owned = []; prepared = []
    bindings = [{'host':h,'provider':p,'alias':a,'generation':'login-one','enabled':True}
        for h,p,a in [('fixture','claude','primary'),('fixture','codex','primary'),
                      ('peer','claude','primary'),('peer','codex','primary'),('peer','codex','secondary')]][:binding_count]
    row = {}
    def rpc(payload, prefix):
        wire.append(dict(payload))
        assert not set(payload) & {'initial_prompt','initial_prompt_blob','brief','role','text'}
        assert payload['type'] not in {'send','resume'}
        if prefix == 'spawn_catalog_get': return catalog()
        if prefix == 'account': return {'type':'account.ok','accounts':bindings}
        if prefix == 'spawn':
            binding = next(b for b in bindings if all(b[k] == payload[k] for k in ('host','provider')) and b['alias']==payload['account'])
            row.update(stream_id=payload['host']+':'+payload['session_name'], session_generation='runtime-one',
                       account_binding=binding, host=payload['host'], session_name=payload['session_name'], bootstrap_state='ready')
            return dict(row)
        if prefix == 'list_sessions': return {'type':'list_sessions.ok','active':[dict(row)]}
        raise AssertionError(prefix)
    @contextmanager
    def connection(*args, **kwargs):
        def register(payload, result): owned.append(result['stream_id'])
        register.prepare_owned_spawn = lambda payload: prepared.append(dict(payload))
        yield rpc, lambda sid: None, lambda *a: pytest.fail('waited for model response'), register, closed.append
    monkeypatch.setattr(restoration, '_operator_connection', connection)
    result = restoration.verify_no_input(bindings, tmp_path/'owned')
    assert result['outcome'] == 'passed'
    assert len(result['cells']) == binding_count and all(c['session_generation']=='runtime-one' for c in result['cells'])
    assert prepared and closed == owned and len(closed) == binding_count
    assert [p['type'] for p in wire].count('spawn') == binding_count

def test_coordinated_recovery_rejects_missing_typed_restoration(transaction, monkeypatch):
    tx, adapter, intent, receipt = transaction
    tx.prepare(intent, receipt)
    tx.apply(intent, receipt)
    monkeypatch.setattr(adapter, 'start_verify', lambda *a, **k:{'outcome':'passed'})
    result = tx.rollback(receipt)
    assert result['phase'] == 'recovery_required'
    assert result['outcome'] == 'failed'


@pytest.mark.parametrize('failure', ['account','readiness','binding','cleanup'])
def test_no_input_wire_failure_is_not_accepted_and_owned_close_runs(tmp_path, monkeypatch, failure):
    from contextlib import contextmanager
    from deploy import restoration
    from _shared.spawn_profiles import catalog
    binding = {'host':'fixture','provider':'claude','alias':'primary','generation':'one','enabled':True}
    closed=[]; requests=[]; row={}
    def rpc(payload, prefix):
        requests.append(payload)
        if prefix=='spawn_catalog_get': return catalog()
        if prefix=='account': return {'accounts':[dict(binding,generation='wrong') if failure=='account' else binding]}
        if prefix=='spawn':
            row.update(stream_id=payload['host']+':'+payload['session_name'],session_generation='fresh',
                bootstrap_state='ready',account_binding=binding)
            return dict(row)
        if prefix=='list_sessions':
            if failure=='binding': row['account_binding']=dict(binding,generation='wrong')
            return {'active':[row]}
        raise AssertionError(prefix)
    @contextmanager
    def connection(*a,**k):
        def register(*a): pass
        register.prepare_owned_spawn=lambda payload:None
        def ready(*a):
            if failure=='readiness': raise RuntimeError('not ready')
        def close(stream):
            closed.append(stream)
            if failure=='cleanup': raise RuntimeError('cleanup failed')
        yield rpc,ready,lambda *a:pytest.fail('inference'),register,close
    monkeypatch.setattr(restoration,'_operator_connection',connection)
    with pytest.raises((c.Invalid,RuntimeError)):
        restoration.verify_no_input([binding],tmp_path/'owned')
    assert len(closed)==(0 if failure=='account' else 1)
    assert not any(r['type'] in {'send','resume'} or 'initial_prompt' in r for r in requests)


@pytest.mark.parametrize('failure', ['missing','stale','helper','pin','binding'])
def test_restored_dispatch_proof_stays_strict(runtime_preparation, failure):
    from provider_accounts import Binding, AccountError
    runtime,fixture,intent,root=runtime_preparation
    accounts=runtime.account_snapshot(c.read_json(fixture.policy),runtime.database())
    identity=code_identity_at(fixture.repo,fixture.prior,runtime.runner)
    helper={'target':{'name':'fixture','local':True,'ssh':'localhost','root':str(root)},
            **c.read_json(fixture.policy)['executors']['fixture']}
    runtime.verification_started=time.time()
    for raw in accounts['selected_bindings']:
        binding=Binding(**raw)
        proof=runtime.remote_json(helper,[helper['python'],helper['script'],'verify-artifact',
            '--provider',binding.provider,'--account',binding.alias,'--login-generation',binding.generation,
            '--operation-id','no-input-control','--pin-json',json.dumps(accounts['pin'])])
        with sqlite3.connect(accounts['database']) as conn:
            value={'proof':proof,'verified_at':time.time()}
            if failure=='stale': value['verified_at']=runtime.verification_started-1
            if failure=='helper': value['proof']['code_identity']='0'*64
            if failure=='binding': value['proof']['binding_key']='wrong'
            if failure=='pin': value['proof']['pin_digest']='0'*64
            if failure!='missing':
                conn.execute('INSERT OR REPLACE INTO kv VALUES (?,?)',('provider_cli.verified:'+binding.key,json.dumps(value)))
    with pytest.raises((c.Invalid,AccountError)):
        runtime.dispatched_proofs({'accounts':accounts,'scope':{'included_hosts':['fixture']}},
            {'code_identity':identity},None)


@pytest.mark.parametrize('failure',['empty','missing','duplicate','cleanup','generation'])
def test_restoration_requires_complete_binding_population(runtime_preparation, monkeypatch, failure):
    from deploy import restoration
    runtime,fixture,intent,root=runtime_preparation
    accounts=runtime.account_snapshot(c.read_json(fixture.policy),runtime.database())
    cells=[{k:b[k] for k in ('host','provider','alias','generation')}|
           {'outcome':'passed','cleanup':'passed','session_generation':'fresh'}
           for b in accounts['selected_bindings']]
    if failure=='empty': cells=[]
    if failure=='missing': cells=cells[:1]
    if failure=='duplicate': cells=[cells[0],cells[0]]
    if failure=='cleanup': cells[0]['cleanup']='failed'
    if failure=='generation': cells[0]['session_generation']=''
    monkeypatch.setattr(restoration,'verify_no_input',lambda *a,**k:{'outcome':'passed','cells':cells})
    with pytest.raises(c.Invalid,match='incomplete'):
        runtime.restoration_proof({'accounts':accounts,'scope':{'included_hosts':['fixture']},
                                  'transaction_directory':str(root)}, {'sha':fixture.prior},None)


def test_old_receipt_cannot_be_reinterpreted_under_new_policy(tmp_path):
    path=tmp_path/'receipt.json'
    c.atomic_json(path,c.seal_receipt({'schema':'pentacle.coordinated-deploy.receipt.v1'}))
    with pytest.raises(c.Invalid,match='invalid sealed'):
        c.load_receipt(path)


def test_full_scope_restoration_is_not_fresh_full_fleet_model_acceptance():
    receipt={'receipt_path':'/private/receipt','receipt_sha256':'a'*64,'intent':{'excluded_hosts':[]}}
    result=c.Transaction.result(receipt,{'phase':'rolled_back','rollback_result':{'restoration':{'outcome':'passed'}}},explicit_rollback=True)
    assert result['exit_code']==0 and result['outcome']=='passed'
    assert result['verification_kind']=='restoration' and result['full_fleet_acceptance'] is False


@pytest.mark.parametrize('restoration_result',[None,{'outcome':'failed'},{'outcome':'passed'}])
def test_direct_rollback_exit_requires_explicit_restoration(monkeypatch,restoration_result):
    stamp={'verification_kind':'restoration','sha':'a'*40,'model_canary':{'outcome':'not_run_by_design'}}
    if restoration_result is not None: stamp['restoration']=restoration_result
    monkeypatch.setattr(deploy_mod,'deploy',lambda *a,**k:stamp)
    code=deploy_mod.main(['chat-streamd-v2','--release-checkout','/unused','--rollback'])
    assert code==(0 if restoration_result=={'outcome':'passed'} else deploy_mod.EXIT_FLEET_SMOKE_FAILED)


def test_direct_post_activation_uses_no_input_proof_only(tmp_path,monkeypatch):
    from .test_deploy_script import _synthetic_boot_log,_boot_observed,V2_SERVICE,TARGET_SHA
    _synthetic_boot_log(tmp_path,monkeypatch)
    monkeypatch.setattr(deploy_mod,'_write_stamp',lambda *a:None)
    monkeypatch.setattr(deploy_mod,'_verify_direct_restoration',lambda *a:{'outcome':'passed','mode':'zero_input'})
    monkeypatch.setattr(deploy_mod,'_scan_slow_consumer_window',lambda *a,**k:{'outcome':'passed'})
    def runner(argv,cwd):
        assert 'spawn_fleet_smoke.py' not in ' '.join(argv)
        return subprocess.CompletedProcess(argv,0,'','')
    stamp={'sha':TARGET_SHA,'verification_kind':'restoration'}
    deploy_mod._apply_post_activation(V2_SERVICE,tmp_path,TARGET_SHA,stamp,prior_log_size=0,prior_pid=111,
        reload_launchd=False,runner=runner,verify_boot=_boot_observed,
        verify_runtime=lambda sha,pid:(True,{'sha':sha,'pid':pid}))
    assert stamp['restoration']['outcome']=='passed' and 'fleet_smoke' not in stamp
    assert stamp['model_canary']['outcome']=='not_run_by_design'


def test_direct_rollback_binds_controller_before_checkout(isolated_release, monkeypatch):
    from deploy import coordinated_runtime
    module,service,repo,prior,target,before,calls,activate=isolated_release
    created=[]
    def runtime(*a,**k):
        created.append(module._git(repo,'rev-parse','HEAD',runner=module._run))
        return SimpleNamespace()
    monkeypatch.setattr(coordinated_runtime,'Runtime',runtime)
    activate(rollback=True)
    assert created==[prior], 'rollback controller dependencies must bind before switching source'


@pytest.mark.parametrize('damage',['missing','invalid'])
def test_forward_profile_failure_cannot_block_intact_prior_recovery(runtime_preparation,damage):
    runtime,fixture,intent,root=runtime_preparation
    folder=root/'damaged-target-profile';folder.mkdir(mode=0o700)
    receipt={'intent':intent,'transaction_directory':str(folder),'receipt_path':str(root/'receipt'),
             'receipt_sha256':'a'*64,**runtime.prepare(intent,folder)}
    profile=Path(receipt['source']['target']['test_profile']['path'])
    if damage=='missing': profile.unlink()
    else: c.atomic_json(profile,{})
    with pytest.raises((c.Invalid,OSError)):
        runtime.revalidate(receipt,forward=True)
    runtime.validate_recovery(receipt,{'phase':'verifying'})


@pytest.mark.parametrize('damage',['sha','health','runtime','binding','version','missing_cell'])
def test_prior_requires_matching_production_qualification(damage):
    from deploy.restoration import qualified_prior
    binding={'host':'fixture','provider':'claude','alias':'primary','generation':'one',
             'executable_sha256':'1'*64,'version':'1.0'}
    stamp={'sha':'a'*40,'daemon_runtime_readback':{'sha':'a'*40},
           'daemon_boot_readback':{'outcome':'observed'},
           'fleet_smoke':{'outcome':'passed','cells':[
               {'host':'fixture','provider':'claude','prompt_mode':m,'outcome':'passed','account_binding':dict(binding)}
               for m in ('prompted','promptless')]}}
    assert qualified_prior(stamp,'a'*40,[binding])['sha']=='a'*40
    if damage=='sha': stamp['sha']='b'*40
    if damage=='health': stamp['fleet_smoke']['outcome']='failed'
    if damage=='runtime': stamp['daemon_runtime_readback']['sha']='b'*40
    if damage=='binding': stamp['fleet_smoke']['cells'][0]['account_binding']['generation']='other'
    if damage=='version': stamp['fleet_smoke']['cells'][0]['account_binding']['version']='other'
    if damage=='missing_cell': stamp['fleet_smoke']['cells'].pop()
    with pytest.raises(c.Invalid): qualified_prior(stamp,'a'*40,[binding])


def test_direct_forward_binds_qualification_before_checkout(isolated_release, monkeypatch):
    import builtins
    module,service,repo,prior,target,before,calls,activate=isolated_release
    actual_import=builtins.__import__
    observed=[]
    def guarded(name,globals=None,locals=None,fromlist=(),level=0):
        if name=='deploy.restoration' and 'qualification_stamp' in fromlist:
            observed.append(module._git(repo,'rev-parse','HEAD',runner=module._run))
            assert observed[-1]==prior, 'qualification dependency loaded from switched target source'
        return actual_import(name,globals,locals,fromlist,level)
    monkeypatch.setattr(builtins,'__import__',guarded)
    activate()
    assert observed==[prior]


@pytest.mark.parametrize('replaced_generation', [False, True])
def test_no_input_retry_cleans_persisted_owned_probe_before_new_spawn(tmp_path,monkeypatch,replaced_generation):
    from contextlib import contextmanager
    from deploy import restoration
    from _shared.spawn_profiles import catalog
    binding={'host':'fixture','provider':'claude','alias':'primary','generation':'one','enabled':True}
    rows={};events=[];fail_close=[True]
    @contextmanager
    def connection(url,token,timeout,registry):
        registry.configure_runtime({'capabilities':{'close_expected_generation':True}})
        def rpc(payload,prefix):
            if prefix=='spawn_catalog_get':return catalog()
            if prefix=='account':return {'accounts':[binding]}
            if prefix=='list_sessions':return {'active':list(rows.values())}
            if prefix=='spawn':
                sid='fixture:'+payload['session_name'];events.append(('spawn',sid))
                rows[sid]={'stream_id':sid,'session_generation':sid+'-generation','bootstrap_state':'ready',
                    'account_binding':binding,'host':'fixture','session_name':payload['session_name']}
                return rows[sid]
            raise AssertionError(prefix)
        def register(payload,result):registry.register_spawn(payload,result,rows[result['stream_id']])
        register.prepare_owned_spawn=registry.prepare_spawn
        def close(sid):
            events.append(('close',sid))
            if fail_close[0]:fail_close[0]=False;raise RuntimeError('temporary close failure')
            def send(payload):
                rows.pop('fixture:'+payload['session_name'],None)
                return {'type':'close.ok'}
            registry.close(sid,inventory=lambda deadline:list(rows.values()),send_close=send,
                wait_gone=lambda owned,deadline:owned.stream_id not in rows,timeout=1)
        yield rpc,lambda sid:None,lambda *a:pytest.fail('inference'),register,close
    monkeypatch.setattr(restoration,'_operator_connection',connection)
    with pytest.raises(RuntimeError,match='temporary close'):
        restoration.verify_no_input([binding],tmp_path/'owned')
    old=next(iter(rows))
    assert json.loads((tmp_path/'owned.0.json').read_text())['owned']
    if replaced_generation:
        from tools.live_window import OwnershipError
        rows[old]['session_generation'] = 'replacement'
        with pytest.raises(OwnershipError, match='generation changed'):
            restoration.verify_no_input([binding],tmp_path/'owned')
        assert list(rows) == [old] and [e for e in events if e[0]=='spawn'] == [('spawn',old)]
        assert json.loads((tmp_path/'owned.0.json').read_text())['owned'][0]['session_generation'] != 'replacement'
        return
    result=restoration.verify_no_input([binding],tmp_path/'owned')
    assert result['outcome']=='passed' and rows=={}, 'retry must not orphan its earlier owned probe'
    assert events[2]==('close',old), 'recover ownership and close before another spawn'


@pytest.mark.parametrize('failure', ['reply_lost', 'missing_stream', 'registration'])
def test_no_input_recovers_committed_spawn_without_second_probe(tmp_path, monkeypatch, failure):
    # QA's lost-reply repro: mocked wire, real persisted registry and close fence.
    from contextlib import contextmanager
    from deploy import restoration
    from _shared.spawn_profiles import catalog
    binding = {'host':'fixture', 'provider':'claude', 'alias':'primary', 'generation':'one', 'enabled':True}
    rows = {}; events = []; fail_once = [True]; unavailable = [False]
    @contextmanager
    def connection(url, token, timeout, registry):
        registry.configure_runtime({'capabilities':{'close_expected_generation':True}})
        def rpc(payload, prefix):
            assert not set(payload) & {'initial_prompt', 'initial_prompt_blob', 'brief', 'role', 'text'}
            if prefix == 'spawn_catalog_get': return catalog()
            if prefix == 'account': return {'accounts':[binding]}
            if prefix == 'list_sessions':
                if unavailable[0]: raise ConnectionError('inventory unavailable')
                return {'active':list(rows.values())}
            if prefix == 'spawn':
                sid = 'fixture:' + payload['session_name']
                events.append(('spawn', sid))
                rows[sid] = {'stream_id':sid, 'session_generation':sid+'-generation',
                    'bootstrap_state':'ready', 'account_binding':binding,
                    'host':'fixture', 'session_name':payload['session_name']}
                if fail_once[0] and failure != 'registration':
                    fail_once[0] = False
                    if failure == 'reply_lost': raise TimeoutError('spawn committed, response lost')
                    return {}  # The committed stream must also survive an invalid reply.
                return dict(rows[sid])
            raise AssertionError(prefix)
        def register(payload, result):
            if unavailable[0]: raise ConnectionError('inventory unavailable')
            if fail_once[0] and failure == 'registration':
                fail_once[0] = False
                raise RuntimeError('registration failed after commit')
            registry.register_spawn(payload, result, rows[result['stream_id']])
        register.prepare_owned_spawn = registry.prepare_spawn
        def close(sid):
            events.append(('close', sid))
            def send(payload):
                assert payload['expected_generation'] == rows[sid]['session_generation']
                rows.pop(sid)
                return {'type':'close.ok'}
            registry.close(sid, inventory=lambda deadline:list(rows.values()), send_close=send,
                wait_gone=lambda owned, deadline:owned.stream_id not in rows, timeout=1)
        yield rpc, lambda sid:None, lambda *a:pytest.fail('inference'), register, close
    monkeypatch.setattr(restoration, '_operator_connection', connection)
    state = tmp_path/'owned'
    with pytest.raises((TimeoutError, c.Invalid, RuntimeError)):
        restoration.verify_no_input([binding], state)
    original = next(iter(rows))
    # A failed recovery must retain the original request, never launch again.
    unavailable[0] = True
    with pytest.raises(ConnectionError, match='inventory unavailable'):
        restoration.verify_no_input([binding], state)
    assert events == [('spawn', original)]
    unavailable[0] = False
    result = restoration.verify_no_input([binding], state)
    assert result['outcome'] == 'passed' and result['cells'][0]['cleanup'] == 'passed'
    assert result['cells'][0]['stream_id'] == original
    assert rows == {} and events == [('spawn', original), ('close', original)]
    assert json.loads((tmp_path/'owned.0.json').read_text())['owned'] == []
