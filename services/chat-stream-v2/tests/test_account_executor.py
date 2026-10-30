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
