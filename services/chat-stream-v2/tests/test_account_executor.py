import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest
from account_executor import AccountExecutor as RealAccountExecutor
from account_runtime import AccountRuntime
from provider_accounts import AccountError, Binding
from sessions import Sessions
from store import Store
from test_account_rotation import binding, observation, policy, NOW


def AccountExecutor(runtime,hosts,*,execute):
    """Independent authenticated host counterpart for enrollment unit journeys."""
    permits={}
    async def guarded(account,action,operation,generation=None,*,reconcile=None):
        if action=="recovery-status":
            from provider_cli_artifact import code_identity
            return {"outcome":"ok","binding":account.public(),"code_identity":code_identity(),"mutation_clearance_required":True}
        if action=="mutation-reserve":
            permit={**reconcile,"provider":account.provider,"alias":account.alias}
            if operation in permits:assert permits[operation]==permit
            permits[operation]=permit
            return {"outcome":"ok","permit":permit}
        if action=="enroll":
            permit=permits[operation]
            assert reconcile=={"_clearance":permit["clearance_id"]}
            assert permit["expected_generation"]==account.generation and permit["generation"]==generation
        return await execute(account,"observe" if action=="observe-bound" else action,operation,generation)
    return RealAccountExecutor(runtime,hosts,execute=guarded)


def test_healthy_probe_never_relogs_and_auth_repairs_only_bound_cell(tmp_path):
    async def run():
        store = Store(str(tmp_path / "state.db")); store.start()
        runtime = AccountRuntime(store, Sessions(store, local_host="host-a"), policy=policy(), clock=lambda: NOW)
        account = binding(); calls = []; unhealthy = False
        async def execute(selected, action, operation_id, generation=None):
            calls.append((selected.key, action, operation_id, generation))
            if action == "observe":
                return {"outcome": "provider_auth_failed"} if unhealthy else {
                    "outcome": "ok", "binding_key": selected.key, "observation": observation(selected)}
            fresh = replace(account, generation=generation, home="/private-fixture/new-home")
            return {"state": "done", "outcome": "ok", "binding": fresh.public()}
        executor = AccountExecutor(runtime, None, execute=execute); runtime.executor = executor
        try:
            await runtime.run(lambda ledger: ledger.register(account))
            assert (await executor.recover(account, relogin=False, operation_id="one"))["outcome"] == "ok"
            assert len(calls) == 1 and calls[0][1] == "observe"
            unhealthy = True
            result = await executor.recover(account, relogin=False, operation_id="signal")
            assert result["outcome"] == "provider_auth_failed" and all(c[1] == "observe" for c in calls)
            result = await executor.recover(account, relogin=True, operation_id="two")
            assert result["outcome"] == "recovered" and result["binding"]["generation"] != account.generation
            assert all(call[0] == account.key for call in calls)
            count = len(calls)
            assert await executor.recover(account, relogin=True, operation_id="two") == result
            assert len(calls) == count
            old = await runtime.run(lambda ledger: ledger.bound(account.key))
            assert old == account
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["provider_quota_blocked", "provider_capacity", "provider_transport_failed", "provider_permission_denied"])
def test_non_auth_failures_never_relogin(tmp_path, failure):
    async def run():
        store = Store(":memory:"); store.start()
        runtime = AccountRuntime(store, Sessions(store), policy=policy(), clock=lambda: NOW)
        calls = []
        async def execute(account, action, *args):
            calls.append(action); return {"outcome": failure}
        executor = AccountExecutor(runtime, None, execute=execute)
        try:
            await runtime.run(lambda ledger: ledger.register(binding()))
            result = await executor.recover(binding(), relogin=False, operation_id="one")
            assert result["outcome"] == failure and calls == ["observe"]
        finally: store.stop()
    asyncio.run(run())


def test_restart_after_remote_timeout_inspects_same_generation_without_new_login(tmp_path):
    path = str(tmp_path / "state.db"); calls = []; state = {}
    async def execute(account, action, operation_id, generation=None):
        calls.append((action, operation_id, generation))
        if action == "observe": return {"outcome": "provider_auth_failed"}
        if action == "enroll":
            state["generation"] = generation
            raise AccountError("account_host_unreachable")
        assert action == "status" and generation == state["generation"]
        return {"state": "done", "outcome": "ok", "binding": replace(account,
            generation=generation, home="/private-fixture/new-generation").public()}
    async def first():
        store = Store(path); store.start()
        try:
            runtime = AccountRuntime(store, Sessions(store), policy=policy(), clock=lambda: NOW)
            await runtime.run(lambda ledger: ledger.register(binding()))
            executor = AccountExecutor(runtime, None, execute=execute)
            assert await executor.recover(binding(), relogin=True, operation_id="original") == {"outcome": "execution_unresolved"}
        finally: store.stop()
    async def second():
        store = Store(path); store.start()
        try:
            runtime = AccountRuntime(store, Sessions(store), policy=policy(), clock=lambda: NOW + 10)
            executor = AccountExecutor(runtime, None, execute=execute)
            assert (await executor.recover(binding(), relogin=True, operation_id="different"))["outcome"] == "recovered"
        finally: store.stop()
    asyncio.run(first()); asyncio.run(second())
    assert [call[0] for call in calls] == ["observe", "enroll", "status"]
    assert calls[2][1:] == calls[1][1:]


def test_recovery_cooldown_attempt_cap_and_human_stop(tmp_path):
    async def run():
        store = Store(":memory:"); store.start(); clock = [NOW]; outcome = ["account_enrollment_failed"]; calls = []
        runtime = AccountRuntime(store, Sessions(store), policy=policy(), clock=lambda: clock[0])
        async def execute(account, action, *args):
            calls.append(action)
            return {"outcome": "provider_auth_failed"} if action == "observe" else {"state": "done", "outcome": outcome[0]}
        executor = AccountExecutor(runtime, None, execute=execute)
        try:
            await runtime.run(lambda ledger: ledger.register(binding()))
            assert (await executor.recover(binding(), relogin=True, operation_id="one"))["outcome"] == outcome[0]
            clock[0] += 1
            assert (await executor.recover(binding(), relogin=True, operation_id="two"))["outcome"] == "recovery_cooldown"
            for name in ["three", "four"]:
                clock[0] += 900
                await executor.recover(binding(), relogin=True, operation_id=name)
            clock[0] += 900
            assert (await executor.recover(binding(), relogin=True, operation_id="five"))["outcome"] == "recovery_capped"
            assert calls.count("enroll") == 3
            clock[0] += 3600; outcome[0] = "human_reseed_required"
            assert (await executor.recover(binding(), relogin=True, operation_id="six"))["outcome"] == outcome[0]
            count = len(calls)
            assert (await executor.recover(binding(), relogin=False, operation_id="seven"))["outcome"] == "provider_auth_failed"
            assert calls[count:] == ["observe"]
        finally: store.stop()
    asyncio.run(run())


def test_successful_generations_keep_cell_cooldown_and_hourly_cap(tmp_path):
    async def run():
        path=str(tmp_path/"state.db"); clock=[NOW]; calls=[]
        store=Store(path); store.start()
        runtime=AccountRuntime(store,Sessions(store),policy=policy(),clock=lambda:clock[0])
        async def execute(account,action,operation_id,generation=None):
            calls.append((account.alias,action))
            if action=="observe": return {"outcome":"provider_auth_failed"}
            return {"state":"done","outcome":"ok","binding":replace(account,generation=generation,
                home="/private-fixture/"+generation).public()}
        executor=AccountExecutor(runtime,None,execute=execute)
        current=binding()
        try:
            await runtime.run(lambda ledger:ledger.register(current))
            first=await executor.recover(current,relogin=True,operation_id="one")
            current=Binding.from_public(first["binding"])
            # Success and daemon restart must not reset the stable cell's budget.
            store.stop(); store=Store(path); store.start(); runtime.store=store
            executor=AccountExecutor(runtime,None,execute=execute)
            assert (await executor.recover(current,relogin=True,operation_id="too-soon"))["outcome"]=="recovery_cooldown"
            for name in ("two","three"):
                clock[0]+=900
                result=await executor.recover(current,relogin=True,operation_id=name)
                assert result["outcome"]=="recovered"
                current=Binding.from_public(result["binding"])
            clock[0]+=900
            assert (await executor.recover(current,relogin=True,operation_id="four"))["outcome"]=="recovery_capped"
            assert calls.count(("primary","enroll"))==3
            peer=binding("spare")
            await runtime.run(lambda ledger:ledger.register(peer))
            assert (await executor.recover(peer,relogin=True,operation_id="peer"))["outcome"]=="recovered"
            clock[0]+=901
            assert (await executor.recover(current,relogin=True,operation_id="later"))["outcome"]=="recovered"
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('consumer,helper', [('current','current'),('old','old'),('current','old'),('old','current')])
def test_launcher_code_identity_requires_matched_release(tmp_path,monkeypatch,consumer,helper):
    import provider_cli_artifact
    import provider_cli_occupancy as occupancy
    from test_provider_cli_occupancy import occupancy_journey
    async def run():
        s=await occupancy_journey(tmp_path,monkeypatch)
        try:
            # Frozen public release identity; the current side uses the actual
            # artifact hasher and actual occupancy/proof construction.
            identities={'current':provider_cli_artifact.code_identity(),
                'old':'bcf52a169f45c6ffcd5b02c131c3fbbc0a34b72f5d728b08444ef8515c88ff7d'}
            assert identities['current']!=identities['old']
            payload=occupancy.status(s.host,s.selected,await occupancy.descriptors(s.runtime,s.selected.host,s.selected.provider),pin=s.pin)
            payload['code_identity']=identities[helper]
            async def execute(*args,**kwargs):return payload
            s.runtime.executor.execute=execute
            monkeypatch.setattr(provider_cli_artifact,'code_identity',lambda:identities[consumer])
            if consumer==helper:assert (await s.runtime.executor.verify_launchers(s.selected))['missing']==[]
            else:
                with pytest.raises(AccountError) as refused:await s.runtime.executor.verify_launchers(s.selected)
                assert refused.value.context['reason']=='launcher_unverified'
        finally:s.store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('field', ['manifest_digest','pending_mutations','process-version','root-start','root-uid',
    'process-boot','process-command','process-start','process-pid','root-pid','session-generation','duplicate','missing-row'])
def test_native_launcher_proof_keeps_daemon_rejections(tmp_path,monkeypatch,field):
    import provider_cli_occupancy as occupancy
    from test_provider_cli_occupancy import occupancy_journey
    async def run():
        s=await occupancy_journey(tmp_path,monkeypatch)
        try:
            payload=occupancy.status(s.host,s.selected,await occupancy.descriptors(s.runtime,s.selected.host,s.selected.provider))
            row=payload['managed'][0]
            if field=='manifest_digest':payload[field]='0'*64
            elif field=='pending_mutations':payload[field]=['unfinished']
            elif field=='duplicate':payload['managed'].append(row)
            elif field=='missing-row':payload['managed'].pop()
            elif field=='session-generation':row['session_generation']='stale'
            elif field=='process-version':row['process']['version']=1
            elif field=='root-start':row['pane_root']['start_id']='stale'
            elif field=='root-uid':row['pane_root']['uid']+=1
            elif field=='root-pid':row['pane_root']['pid']+=1
            else:
                key={'process-boot':'boot_id','process-command':'command_fingerprint','process-start':'start_id','process-pid':'pid'}[field]
                row['process'][key]=0 if key=='pid' else ''
            async def execute(*args,**kwargs):return payload
            s.runtime.executor.execute=execute
            with pytest.raises(AccountError) as refused:await s.runtime.executor.verify_launchers(s.selected)
            assert refused.value.context['reason']=='launcher_unverified'
        finally:s.store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('case',['valid','changed-authoritative-pin','wrong-pin-echo','missing-transport-pin'])
def test_wrapper_pin_uses_authenticated_transport_and_daemon_fence(tmp_path,monkeypatch,case):
    import provider_cli_occupancy as occupancy
    from provider_cli_fixtures import seed_pin
    from test_provider_cli_occupancy import occupancy_journey
    async def run():
        s=await occupancy_journey(tmp_path,monkeypatch,platform='wsl',launch_type='js')
        try:
            calls=[]
            class Transport:
                async def run_command(self,host,*argv,timeout):
                    assert host==s.selected.host and argv[:3]==('/fixture/python','/fixture/helper.py','launcher-status')
                    flags=dict(zip(argv[3::2],argv[4::2]));calls.append(flags)
                    pin=json.loads(flags['--pin-json'])
                    assert pin==s.pin  # Maintained account's old version is deliberately different.
                    payload=s.host.handle('launcher-status',provider=s.selected.provider,alias=s.selected.alias,
                        expected_generation=s.selected.generation,operation_id='fixture-transport',
                        pin=None if case=='missing-transport-pin' else pin,
                        reconcile=json.loads(flags['--reconcile-json']))
                    if case=='wrong-pin-echo':payload['pin_digest']='0'*64
                    if case=='changed-authoritative-pin':
                        await seed_pin(s.runtime,dict(s.pin,revision=s.pin['revision']+1))
                    return 0,json.dumps(payload)
            s.runtime.policy['executors']={s.selected.host:{'python':'/fixture/python','script':'/fixture/helper.py'}}
            s.runtime.executor=RealAccountExecutor(s.runtime,Transport())
            if case=='valid':
                value=await s.runtime.executor.verify_launchers(s.selected)
                assert value['missing']==[] and value['unmanaged']==[]
            else:
                with pytest.raises(AccountError) as refused:await s.runtime.executor.verify_launchers(s.selected)
                assert refused.value.context['reason']=='launcher_unverified'
            assert len(calls)==1 and len(json.loads(calls[0]['--reconcile-json'])['managed_launchers'])==2
        finally:s.store.stop()
    asyncio.run(run())


def test_direct_native_does_not_gain_a_release_pin_dependency(tmp_path,monkeypatch):
    import provider_cli_pin
    from test_provider_cli_occupancy import occupancy_journey
    async def run():
        s=await occupancy_journey(tmp_path,monkeypatch)
        try:
            def unavailable(ledger):raise AccountError('provider_cli_pin_unavailable')
            monkeypatch.setattr(provider_cli_pin,'current',unavailable)
            value=await s.runtime.executor.verify_launchers(s.selected)
            assert value['missing']==[] and value['pin_digest']=='absent'
        finally:s.store.stop()
    asyncio.run(run())
