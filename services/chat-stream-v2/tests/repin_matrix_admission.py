"""Real spawn selection, scheduled firing and physical-admission fence rows."""
import copy
from types import SimpleNamespace

import pytest
import launch
from provider_accounts import AccountError
from sessions import VerbError
from spawnctl import SpawnCtl
from test_account_runtime import AUTH
from test_spawn_cancel_fence_holes import ReconTmux

CONSUMERS=("ordinary","explicit","scheduled","deferred","retry","resume","restore")
ROWS=("B-admission","B-status-locks")
VARIANTS={"B-admission":tuple(state+"/"+consumer for state in ("U","S","X") for consumer in CONSUMERS),
    "B-status-locks":("U","S","X")}


class AdmissionTmux(ReconTmux):
    async def pretrust_codex_cwd(self,_cwd):pass


async def prepare(case,j,mp,current,parent,tmp_path,attempt):
    from repin_state_matrix import outage_after_admission
    from test_account_repin_state_machine import fresh,recovery,move_pin
    from account_repin import _host_id
    store,runtime,host,old,request,calls,native,logins,cell,pin=j
    execute=runtime.executor.execute;state,_,consumer=case.variant.partition("/")
    expected_logins=len(logins);damaged=None
    if state in {"S","X"} and parent is None:
        async def split(*args,**kwargs):
            nonlocal current
            result=await execute(*args,**kwargs)
            if args[1]=="repin":current=await move_pin(runtime,current,current["revision"]+1)
            return result
        runtime.executor.execute=split
        with pytest.raises(VerbError):await runtime.operation(request)
        runtime.executor.execute=execute;parent=request["operation_id"]
    if state=="U":
        runtime.executor.execute=outage_after_admission(execute)
        node=fresh(request,current,"admission-uncertain",**({"reconcile_of":parent} if parent else {}))
        with pytest.raises(VerbError):await runtime.operation(node)
        runtime.executor.execute=execute;parent=node["operation_id"]
    elif state=="X":
        path=host.journal/(_host_id(request)+".json");damaged=(path,path.read_bytes())
        receipt=host._read_operation(_host_id(request));receipt["proof"]["code_identity"]="f"*64
        host._save_operation(_host_id(request),receipt)
        with pytest.raises(VerbError):await runtime.operation(fresh(request,current,"admission-conflict",reconcile_of=parent))
        parent=(await recovery(runtime,old))["canonical_tail"]
    status=await recovery(runtime,old)
    assert status["fence_causes"] and status["canonical_tail"]==parent and status["current_pin"]==current
    assert status["next_action"] and "missing_prerequisite" in status
    from account_repin import fenced
    assert await runtime.run(lambda a:fenced(a,old))
    if case.row=="B-status-locks":
        # A real status request paused in authenticated remote work leaves the
        # global pin decision lock available to independent work.
        import asyncio
        entered=asyncio.Event();release=asyncio.Event()
        async def slow(*args,**kwargs):
            if args[1]=="recovery-status":entered.set();await release.wait()
            return await execute(*args,**kwargs)
        runtime.executor.execute=slow
        task=asyncio.create_task(recovery(runtime,old))
        try:
            await asyncio.wait_for(entered.wait(),1)
            await asyncio.wait_for(runtime.pin_lock.acquire(),0.5)
            runtime.pin_lock.release()
        finally:release.set();await task;runtime.executor.execute=execute
        if damaged:damaged[0].write_bytes(damaged[1]);damaged[0].chmod(0o600)
        return {"current":current,"parent":parent,"expected_logins":expected_logins}
    tmux=AdmissionTmux()
    ctl=SpawnCtl(store,runtime.sessions,tmux=tmux,machine=launch.local_machine(old.host,cwd=str(tmp_path),codex_bin="/bin/echo",claude_bin="/bin/echo"))
    ctl.account_runtime=runtime
    base={"objective":"Exercise repin admission","host":old.host,"provider":old.provider,"_auth_context":AUTH,
        "session_name":"matrix-refused","request_id":"matrix-refused-request","ready_marker":"READY","_defer_launch_token_stage":True}
    if consumer=="explicit":base["account"]=old.alias
    if consumer in {"resume","restore"}:
        base.update(resume_session_id="531c40b0-706d-47d7-b2f9-24e61b50b216",_account_resume_binding=old.public(),
            _resume_jsonl_path=old.transcript_root+"/matrix.jsonl",_resume_cwd=str(tmp_path))
        if consumer=="restore":base.update(_auth_context={"service_authenticated":True,"service_actor":"daemon:scheduler"},_session_generation="f"*32)
    async def physical(name,binding,message,resolution=None,command="fake managed native"):
        nonce=name+"-nonce";request_id=name+"-request"
        fields={"provider":old.provider,"session_generation":name+"-generation","account_binding":binding}
        await store.reserve_stream_id(old.host,name,ttl_s=600,request_id=request_id,nonce=nonce)
        await store.record_spawn_intent(old.host,name,{"open_fields":fields,"brief":"","delivery_receipt":{"state":"not_requested"}},request_id=request_id,nonce=nonce)
        try:
            return await ctl._spawn_fenced(old.host,name,request_id,command,"","READY",message,[False],[False],fields,
                resolution or {"resolved_launch_tuple":{"provider":old.provider}},tmux,{"state":"not_requested"},nonce)
        finally:
            await store.release_stream_id_fenced(old.host,name,request_id)
    native_before=list(native)
    await store.clear_spawn_admission_hold(old.host)
    try:
        if consumer in {"ordinary","explicit"}:
            with pytest.raises(VerbError,match="account_repin_reconciliation_required"):await ctl.spawn(dict(base),old.host)
        elif consumer in {"resume","restore"}:
            with pytest.raises(VerbError,match="account_repin_reconciliation_required"):await ctl._resolve_launch(dict(base),old.host,"matrix-refused")
        elif consumer in {"deferred","retry"}:
            for number in range(2 if consumer=="retry" else 1):
                with pytest.raises(VerbError,match="account_repin_reconciliation_required"):
                    await physical("matrix-refused-"+str(number),old.public(),base)
        elif consumer=="scheduled":
            from window_schedule import WindowSchedule
            from test_window_schedule_contract import FakeSessions,FakeComms,RouteProbeSpecs,SPEC,link_specs,future_time,message
            fake_sessions=FakeSessions()
            fake_sessions.rows={key.replace("hosta",old.host):dict(value,stream_id=key.replace("hosta",old.host)) for key,value in fake_sessions.rows.items()}
            ctl.specs=RouteProbeSpecs()
            surface=WindowSchedule(store,fake_sessions,FakeComms(),ctl,local_host=old.host);surface.mark_store_ready()
            await link_specs(store,SPEC)
            inserted=await surface.schedule_insert(message("schedule.insert",old.host+":requester",objective="Exercise scheduled repin admission",
                fires_at_utc=future_time(),target_host=old.host,provider=old.provider,spec_ids=[SPEC]))
            with pytest.raises(VerbError) as refused:
                await surface._fire_schedule(inserted["schedule"]["schedule_id"])
            assert refused.value.extra["spawn"]["error_code"]=="account_repin_reconciliation_required"
            assert refused.value.extra["schedule"]["last_error_code"]=="account_repin_reconciliation_required"
        assert tmux.created==0 and native==native_before
        assert await store.fetch_session(old.host,"matrix-refused") is None
        count=await runtime.run(lambda a:a.conn.execute("SELECT count(*) FROM kv WHERE k GLOB 'provider_cli.admission:host-a:matrix-refused*'").fetchone()[0])
        assert count==0
    finally:
        await store.set_spawn_admission_hold(old.host,held_until=runtime.clock()+1000,reason="finish admission matrix recovery")
        if damaged:damaged[0].write_bytes(damaged[1]);damaged[0].chmod(0o600)
    async def after():
        # The same real resolver now chooses the reconciled binding, and its
        # final physical action must pass admit_launch before new_session.
        message=dict(base,session_name="matrix-consumer-green",request_id="matrix-consumer-green-request")
        command,resolution,overrides=await ctl._resolve_launch(message,old.host,"matrix-consumer-green")
        binding=overrides["account_binding"]
        green=await physical("matrix-consumer-green",binding,message,resolution,command)
        assert green["type"]=="spawn.ok" and tmux.created==1
        row=await store.fetch_session(old.host,"matrix-consumer-green")
        assert row["account_binding"]["generation"]==binding["generation"]
        await tmux.kill_session("matrix-consumer-green")
    return {"current":current,"parent":parent,"expected_logins":expected_logins,"after":after}
