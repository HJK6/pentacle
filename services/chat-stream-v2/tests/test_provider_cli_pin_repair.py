"""Final-QA regressions: recoverable non-splits and bounded input decisions."""
import asyncio
import copy
import json

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
        observe,execute=host._probe_observation,host.run
        if failure=="network":
            def error(_): raise AccountError("provider_transport_failed")
            host._probe_observation=error
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
            host._probe_observation,host.run=observe,execute
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
        from repin_state_matrix import outage_after_admission
        runtime.executor.execute=outage_after_admission(runtime.executor.execute)
        try:
            with pytest.raises(VerbError): await runtime.operation(request)
            with pytest.raises(VerbError): await runtime.operation(dict(request,operation_id="fresh-offline",new_generation="unused"))
            from account_repin import fenced
            assert await runtime.run(lambda a:fenced(a,old))
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("case",["binding","successor","central-health","auth","auth-race"])
def test_non_split_resolution_requires_exact_proof_origin_and_current_authority(tmp_path,monkeypatch,case):
    from account_repin import fenced
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch)
        observe=host._probe_observation;execute=runtime.executor.execute;fresh=False
        def fail(_): raise AccountError("provider_transport_failed")
        host._probe_observation=fail
        async def uncertain(account,action,*args,**kwargs):
            result=await execute(account,action,*args,**kwargs)
            if action in {"repin-inspect","repin-settle"}:
                result=copy.deepcopy(result)
                if not fresh: result.pop("seal",None)
                elif case=="binding": result["seal"]["base"]["generation"]="wrong-origin"
                elif case=="successor": result["seal"]["successor_absent"]=False
                elif case=="auth-race": runtime.server.assistant_composite.enabled=False
            return result
        runtime.executor.execute=uncertain
        try:
            with pytest.raises(VerbError): await runtime.operation(request)
            assert await runtime.run(lambda a:fenced(a,old))
            host._probe_observation=observe;fresh=True
            if case=="central-health": await runtime.record_failure(old,"identity")
            auth={} if case=="auth" else AUTH
            with pytest.raises(VerbError):
                await runtime.operation(dict(request,operation_id="unproven-recovery",new_generation="unused",_auth_context=auth))
            if case=="central-health":
                # B explicitly allows disposition-only CAS of current metadata;
                # it must preserve the independent health refusal.
                assert not await runtime.run(lambda a:fenced(a,old))
                assert (await runtime.run(lambda a:a._cell(old.host,old.provider,old.alias)))["health"]=="identity_conflict"
            else:
                assert await runtime.run(lambda a:fenced(a,old))
            assert host.registry.active(old.provider,old.alias,old.host).key==old.key
            assert not [v for v in native if "Reply exactly OK" in v] and len(logins)==1
        finally: store.stop()
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


def test_real_send_evidence_wait_allows_other_send_and_pin_move_but_refuses_enter_retry(tmp_path):
    from comms import Comms
    from spawnctl import SpawnCtl
    from test_send_semantics import FakeTmux
    async def run():
        store,runtime,rows=await input_rows()
        started=asyncio.Event();release=asyncio.Event()
        class Tmux(FakeTmux):
            async def capture(self,name):
                text=self.pastes[0] if name=="input-primary" and self.pastes else ""
                return "OpenAI Codex\n─────────\n› "+text+"\n  gpt-6-test high"
        tmux=Tmux(submit_on_paste=False)
        comms=Comms(store,runtime.sessions,SpawnCtl(store,runtime.sessions,tmux=tmux),attachment_root=tmp_path)
        comms.account_runtime=runtime
        async def evidence(_tmux,name,*args,**kwargs):
            if name=="input-primary": started.set();await release.wait();return False
            return True
        comms._submission_evidence=evidence
        task=None
        try:
            await runtime.sessions.refresh()
            task=asyncio.create_task(comms.send({"stream_id":rows[0]["stream_id"],"text":"primary marker","request_id":"primary-send"}))
            await asyncio.wait_for(started.wait(),1)
            result=await asyncio.wait_for(comms.send({"stream_id":rows[1]["stream_id"],"text":"spare marker","request_id":"spare-send"}),1)
            assert result["submission_confirmed"]
            pin=copy.deepcopy(release_pair());pin["revision"]=2;pin["providers"]["codex"]["version"]="9.9.9"
            await asyncio.wait_for(runtime.operation({"action":"pin-set","pin":pin,"expected_revision":1,
                "operation_id":"move-during-send-evidence","reason":"fixture","_auth_context":AUTH}),1)
            release.set();result=await task
            assert result["action_committed"] and result["confirmation_pending"]
            assert result["reason"]=="enter_failed"
            assert tmux.enters==0 and tmux.pastes==["primary marker","spare marker"]
        finally:
            release.set()
            if task: await task
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("move_at",["qualification","physical"])
def test_real_queued_control_rechecks_pin_and_orders_physical_keys(move_at):
    from account_runtime import AccountRuntime
    from test_account_rotation import policy
    from test_queued_send_now import setup as queued_setup, message, finish
    from provider_cli_fixtures import seed_pin
    from dataclasses import replace
    async def run():
        h=await queued_setup();started=asyncio.Event();release=asyncio.Event();order=[]
        runtime=AccountRuntime(h.store,h.sessions,policy=policy())
        account=replace(binding(),host="fixture",version=h.sessions.get("fixture:v2-capacity")["account_binding"]["version"],
            executable_sha256=h.sessions.get("fixture:v2-capacity")["account_binding"]["executable_sha256"])
        await runtime.run(lambda a:a.register(account))
        pin=copy.deepcopy(release_pair())
        for artifact in pin["providers"]["codex"]["artifacts"].values():
            artifact["native_sha256"]=account.executable_sha256
        await seed_pin(runtime,pin)
        await artifact_executor(runtime)
        def bind_fixture(conn):
            with conn:
                conn.execute("UPDATE sessions SET account_binding=? WHERE host=? AND session_name=?",
                    (json.dumps(account.public()),"fixture","v2-capacity"))
        await h.store.submit(bind_fixture)
        h.comms.account_runtime=runtime
        source=h.controller.source_snapshot;run_keys=h.tmux.run
        async def paused_source(row):
            if move_at=="qualification": started.set();await release.wait()
            return await source(row)
        async def paused_keys(*args,**kwargs):
            if args[0]=="send-keys" and move_at=="physical":
                order.append("keys-start");started.set();await release.wait();order.append("keys-end")
            return await run_keys(*args,**kwargs)
        h.controller.source_snapshot=paused_source;h.tmux.run=paused_keys
        async def move():
            changed=copy.deepcopy(pin);changed["revision"]=2;changed["providers"]["codex"]["version"]="9.9.9"
            await runtime.operation({"action":"pin-set","pin":changed,"expected_revision":1,"operation_id":"queued-race",
                "reason":"fixture","_auth_context":{"operator_authenticated":True}})
            order.append("pin-moved")
        task=asyncio.create_task(h.controller.send(message()));writer=None
        task.add_done_callback(lambda _:started.set())
        try:
            await asyncio.wait_for(started.wait(),1)
            if task.done():
                await task
                pytest.fail("queued control returned before the race point")
            writer=asyncio.create_task(move())
            if move_at=="qualification":
                await asyncio.wait_for(writer,1);release.set()
                with pytest.raises(VerbError,match="provider_cli_pin_mismatch"): await task
                record,_=await h.controller._record(h.controller.operation_key("fixture:v2-capacity","test-generation","selected"))
                assert record["state"]=="cancelled" and h.tmux.keys==[]
            else:
                await asyncio.sleep(0);assert not writer.done()
                release.set();await task;await writer
                assert order==["keys-start","keys-end","pin-moved"] and len(h.tmux.keys)==1
        finally:
            release.set()
            if not task.done():
                try: await task
                except VerbError: pass
            if writer: await writer
            await finish(h)
    asyncio.run(run())
