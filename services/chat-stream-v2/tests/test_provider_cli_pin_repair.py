"""Final-QA regressions: recoverable non-splits and bounded input decisions."""
import asyncio
import copy
from pathlib import Path

import pytest
from provider_accounts import AccountError
from provider_cli_pin import set_pin
from sessions import VerbError
from test_account_repin import host_journey
from test_account_rotation import binding
from test_account_runtime import setup, AUTH
from test_provider_cli_pin import artifact_executor, release_pair


@pytest.mark.parametrize("recovery",["fresh","rollback"])
@pytest.mark.parametrize("failure",["network","home-busy"])
def test_confirmed_non_split_failure_is_recoverable(tmp_path,monkeypatch,recovery,failure):
    from types import SimpleNamespace
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch)
        observe,execute=host.observation,host.run
        if failure=="network":
            def error(_): raise AccountError("provider_transport_failed")
            host.observation=error
        else:
            def busy(argv,**kwargs):
                if argv[0]=="ps": return SimpleNamespace(returncode=0,stdout="123 codex /fixture/native")
                return execute(argv,**kwargs)
            host.run=busy
        try:
            try: await runtime.operation(request)
            except VerbError: pass
            assert host.registry.active(old.provider,old.alias,old.host).key==old.key
            journals={p.name:p.read_bytes() for p in host.journal.glob("*.json")}
            host.observation,host.run=observe,execute
            if recovery=="fresh":
                result=(await runtime.operation(dict(request,operation_id="fresh-after-failure",new_generation="fresh-successor")))["result"]
                assert result["binding"]["generation"]=="fresh-successor"
                assert len([v for v in native if "Reply exactly OK" in v])==1
            else:
                rollback=copy.deepcopy(pin);rollback["revision"]=3
                rollback["providers"][old.provider]["version"]=old.version
                for artifact in rollback["providers"][old.provider]["artifacts"].values():
                    artifact.update(native_sha256=old.executable_sha256,resource_sha256=old.executable_sha256)
                await runtime.run(lambda a:set_pin(a,{"pin":rollback,"expected_revision":2,"operation_id":"rollback-failed-forward","reason":"fixture"}))
                selected=await runtime.choose_launch(host=old.host,provider=old.provider,model="fixture",effort="high",message={})
                assert selected.key==old.key
                assert len([v for v in native if "Reply exactly OK" in v])==0
            assert all((host.journal/name).read_bytes()==data for name,data in journals.items())
            assert len(logins)==1
        finally:store.stop()
    asyncio.run(run())


def test_uncertain_host_outcome_does_not_lift_the_split_fence(tmp_path,monkeypatch):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch)
        async def offline(*args,**kwargs): raise AccountError("account_host_unreachable")
        runtime.executor.execute=offline
        try:
            with pytest.raises(VerbError): await runtime.operation(request)
            with pytest.raises(VerbError): await runtime.operation(dict(request,operation_id="fresh-offline",new_generation="unused"))
            from account_repin import fenced
            assert await runtime.run(lambda a:fenced(a,old))
        finally:store.stop()
    asyncio.run(run())


async def input_rows():
    store,runtime=await setup()
    await artifact_executor(runtime)
    rows=[]
    for alias in ("primary","spare"):
        rows.append(await store.open_session("host-a","input-"+alias,session_generation="gen-"+alias,
            provider="codex",account_binding=binding(alias).public()))
    return store,runtime,rows


def test_independent_input_and_spawn_admission_progress_during_other_input():
    async def run():
        store,runtime,rows=await input_rows();entered=asyncio.Event();release=asyncio.Event()
        async def first():
            async with runtime.input_guard(rows[0]):
                entered.set();await release.wait()
        task=None
        try:
            for row in rows: await runtime.before_input(row)
            task=asyncio.create_task(first());await entered.wait()
            async def independent():
                async with runtime.input_guard(rows[1]): pass
                await runtime.admit_launch(rows[1])
            await asyncio.wait_for(independent(),1)
        finally:
            release.set()
            if task: await task
            store.stop()
    asyncio.run(run())


def test_slow_remote_verification_does_not_hold_pin_or_other_input_and_cas_refuses():
    async def run():
        store,runtime,rows=await input_rows();started=asyncio.Event();release=asyncio.Event();physical=[]
        await runtime.before_input(rows[1])
        verify=runtime.executor.verify_artifact
        async def slow(account,pin):
            if account.alias=="primary": started.set();await release.wait()
            return await verify(account,pin)
        runtime.executor.verify_artifact=slow
        async def first():
            async with runtime.input_guard(rows[0]): physical.append("stale input")
        task=asyncio.create_task(first())
        try:
            await started.wait()
            async def move():
                return await runtime.operation({"action":"pin-set","pin":dict(release_pair(),revision=2),"expected_revision":1,
                    "operation_id":"move-during-verify","reason":"fixture","_auth_context":AUTH})
            await asyncio.wait_for(move(),1)
            async with runtime.input_guard(rows[1]): pass
            release.set()
            with pytest.raises(AccountError,match="provider_cli_pin_mismatch"): await task
            assert physical==[]
        finally:
            release.set()
            if not task.done():
                try: await task
                except AccountError: pass
            store.stop()
    asyncio.run(run())


def test_pin_move_orders_with_physical_input_and_refuses_the_next_old_input():
    async def run():
        store,runtime,rows=await input_rows();entered=asyncio.Event();release=asyncio.Event();order=[]
        await runtime.before_input(rows[0])
        async def input_action():
            async with runtime.input_guard(rows[0]):
                order.append("input-start");entered.set();await release.wait();order.append("input-end")
        async def move():
            pin=copy.deepcopy(release_pair());pin["revision"]=2;pin["providers"]["codex"]["version"]="9.9.9"
            await runtime.operation({"action":"pin-set","pin":pin,"expected_revision":1,
                "operation_id":"ordered-move","reason":"fixture","_auth_context":AUTH})
            order.append("pin-moved")
        first=asyncio.create_task(input_action());second=None
        try:
            await entered.wait();second=asyncio.create_task(move());await asyncio.sleep(0)
            assert not second.done()
            release.set();await first;await second
            assert order==["input-start","input-end","pin-moved"]
            with pytest.raises(AccountError,match="provider_cli_pin_mismatch"):
                async with runtime.input_guard(rows[0]): pytest.fail("old native input admitted")
        finally:
            release.set();await first
            if second: await second
            store.stop()
    asyncio.run(run())
