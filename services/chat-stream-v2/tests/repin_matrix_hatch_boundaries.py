"""Hatch crash, refusal and retained-new-home continuation boundaries."""
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from provider_accounts import AccountError
from sessions import VerbError
from test_account_runtime import AUTH

VARIANTS={"B-hatch-races":("status-offline","before-seal","after-seal","before-signin","after-signin","host-success-offline",
    "pin-after-enroll","hold-after-enroll","policy-after-enroll","before-commit","after-commit","delayed-old-dispatch","foreign-permit","foreign-seal","started-uncertain","damaged-new-proof","lost-new-target"),
    "B-hatch-refusals":("unauthorized","stale-digest","missing-hold","expired-hold","replaced-hold","wrong-provider","wrong-pool",
        "reused-generation","reused-home","missing-artifact","malformed-proof","wrong-generation-receipt","reused-home-receipt",
        "cap","cooldown","human-stop","cancelled","locked-keychain")}


async def prepare(case,j,mp,result,tmp_path):
    from test_account_repin_state_machine import recovery,move_pin
    import account_repin_hatch as hatch
    from account_executor import AccountExecutor
    from provider_account_mutation import read,write
    from account_repin import _host_id
    store,runtime,host,old,request,calls,native,logins,cell,pin=j
    current=result["current"];execute=runtime.executor.execute;login=host.login;observe=host.observation
    commit=hatch._commit;uuid=hatch.uuid.uuid4;save=host._save_operation;variant=case.variant;hit={"once":False}
    baseline_logins=len(logins);status=await recovery(runtime,old)
    selected=await runtime.run(lambda a:a._cell(old.host,old.provider,old.alias)["generation"])
    payload={"action":"relogin","host":old.host,"provider":old.provider,"account":old.alias,
        "login_generation":selected,"operation_id":"hatch-boundary-attempt","supersede_repin":status["snapshot_digest"],"_auth_context":dict(AUTH)}
    corrupted=None;collision_home=None;retained=False;committed=False
    async def dispatch(*args,**kwargs):
        nonlocal current,retained
        action=args[1]
        if case.row=="B-hatch-races":
            if variant=="status-offline" and action=="recovery-status" and not hit["once"]:
                hit["once"]=True;raise AccountError("account_host_unreachable")
            if variant=="before-seal" and action=="repin-supersede" and not hit["once"]:
                hit["once"]=True;raise AccountError("account_host_unreachable")
            if variant in {"foreign-permit","foreign-seal"} and action=="repin-supersede" and not hit["once"]:
                hit["once"]=True
                write(host,"permits" if variant=="foreign-permit" else "seals","delayed-foreign-intent",
                    {"provider":old.provider,"alias":old.alias,"action":"repin","state":"pending"})
            if variant=="before-signin" and action=="enroll" and not hit["once"]:
                hit["once"]=True;raise AccountError("account_host_unreachable")
        value=await execute(*args,**kwargs)
        if case.row=="B-hatch-races":
            if variant in {"after-seal","delayed-old-dispatch"} and action=="repin-supersede" and not hit["once"]:
                hit["once"]=True;raise AccountError("account_host_unreachable")
            if action=="enroll" and variant in {"host-success-offline","pin-after-enroll","hold-after-enroll","policy-after-enroll","damaged-new-proof","lost-new-target"}:
                hit["once"]=True;retained=True
                if variant=="pin-after-enroll":current=await move_pin(runtime,current,current["revision"]+1)
                if variant=="hold-after-enroll":await store.set_spawn_admission_hold(old.host,held_until=runtime.clock()+1200,reason="changed after host enrollment")
                if variant=="policy-after-enroll":runtime.policy["hatch-race-policy"]=1
                if variant in {"damaged-new-proof","lost-new-target"}:
                    current=await move_pin(runtime,current,current["revision"]+1)
                    if variant=="damaged-new-proof":
                        operation=hashlib.sha256(args[2].encode()).hexdigest();broken=host._read_operation(operation)
                        broken["proof"]["code_identity"]="f"*64;host._save_operation(operation,broken);retained=False
                if variant in {"host-success-offline","damaged-new-proof","lost-new-target"}:raise AccountError("account_host_unreachable")
        elif action=="enroll" and variant in {"malformed-proof","wrong-generation-receipt","reused-home-receipt"}:
            value=copy.deepcopy(value);hit["once"]=True;retained=True
            if variant=="malformed-proof":value["proof"]["code_identity"]="f"*64
            elif variant=="wrong-generation-receipt":value["generation"]="wrong"
            else:value["binding"]["home"]=old.home
        return value
    def commit_boundary(*args,**kwargs):
        nonlocal retained,committed
        hit["once"]=True;retained=True
        if variant=="after-commit":
            commit(*args,**kwargs);committed=True
        raise AccountError("fixture_hatch_commit_crash")
    def login_boundary(*args,**kwargs):
        value=login(*args,**kwargs);hit["once"]=True
        if variant=="after-signin":raise AccountError("account_host_unreachable")
        return {"human-stop":"human_reseed_required","cancelled":"cancelled","locked-keychain":"locked_keychain"}.get(variant,value)
    def started_unknown(operation,data):
        save(operation,data)
        if data.get("state")=="started" and data.get("generation")!=request["new_generation"]:
            hit["once"]=True;raise AccountError("account_host_unreachable")
    async def limits(value):
        key="cell:"+":".join((old.host,old.provider,old.alias))
        await runtime.run(lambda a:AccountExecutor._save_recovery(a,key,value))
    try:
        runtime.executor.execute=dispatch
        if case.row=="B-hatch-races":
            if variant in {"before-commit","after-commit"}:mp.setattr(hatch,"_commit",commit_boundary)
            if variant=="after-signin":host.login=login_boundary
            if variant=="started-uncertain":host._save_operation=started_unknown
        else:
            if variant=="unauthorized":payload["_auth_context"]={"token_verified":False}
            elif variant=="stale-digest":payload["supersede_repin"]="f"*64
            elif variant=="missing-hold":await store.clear_spawn_admission_hold(old.host)
            elif variant=="expired-hold":await store.set_spawn_admission_hold(old.host,held_until=runtime.clock(),reason="expired")
            elif variant=="replaced-hold":await store.set_spawn_admission_hold(old.host,held_until=runtime.clock()+1100,reason="replaced after FD snapshot")
            elif variant in {"wrong-provider","wrong-pool"}:
                def foreign(binding):
                    row=observe(binding);row["provider_id" if variant=="wrong-provider" else "pool_id"]="foreign";return row
                host.observation=foreign
            elif variant in {"reused-generation","reused-home"}:
                generation=selected if variant=="reused-generation" else "preexisting-unused-home"
                mp.setattr(hatch.uuid,"uuid4",lambda:SimpleNamespace(hex=generation))
                if variant=="reused-home":
                    collision_home=host.registry.root/old.host/old.provider/old.alias/generation
                    collision_home.mkdir(mode=0o700)
            elif variant=="missing-artifact":
                path=Path(cell["executable"]);corrupted=(path,path.read_bytes());path.write_bytes(b"tampered fixture native")
            elif variant=="cap":await limits({"attempts":[runtime.clock()-100]*3,"last_attempt":runtime.clock()-100})
            elif variant=="cooldown":await limits({"attempts":[runtime.clock()-100],"last_attempt":runtime.clock()-100})
            elif variant in {"human-stop","cancelled","locked-keychain"}:host.login=login_boundary
        with pytest.raises(VerbError):await runtime.operation(payload)
        if case.row=="B-hatch-races":assert hit["once"],"Named hatch fault must actually occur"
        if not committed:
            assert (await recovery(runtime,old))["fence_causes"]
            assert await runtime.run(lambda a:a._cell(old.host,old.provider,old.alias)["generation"])==selected
        if variant=="delayed-old-dispatch":
            before=list(native)
            with pytest.raises(AccountError):
                host.handle("repin",provider=old.provider,alias=old.alias,expected_generation=old.generation,
                    operation_id=_host_id(request),generation=request["new_generation"],pin=pin)
            assert native==before and read(host,"seals",_host_id(request))
        if variant in {"human-stop","cancelled","locked-keychain","after-signin","before-signin","started-uncertain"}:
            count=len(logins)
            # Exact retry inspects the old operation. It never repeats sign-in.
            with pytest.raises(VerbError):await runtime.operation(payload)
            assert len(logins)==count
    finally:
        runtime.executor.execute=execute;host.login=login;host.observation=observe;host._save_operation=save
        mp.setattr(hatch,"_commit",commit);mp.setattr(hatch.uuid,"uuid4",uuid)
        if corrupted:corrupted[0].write_bytes(corrupted[1])
    if variant=="lost-new-target":
        from repin_state_matrix import target_version
        current=await target_version(j,current,"8.8.2",tmp_path,"new-home-continuation")
        again=dict(payload,operation_id="hatch-lost-new-target",supersede_repin=(await recovery(runtime,old))["snapshot_digest"])
        async def lost_target(*args,**kwargs):
            value=await execute(*args,**kwargs)
            if args[1]=="repin-reconcile":raise AccountError("account_host_unreachable")
            return value
        runtime.executor.execute=lost_target
        try:
            with pytest.raises(VerbError):await runtime.operation(again)
        finally:runtime.executor.execute=execute
        assert len(logins)==baseline_logins+1
    if committed:
        historical=await runtime.operation(payload)
        result.update(hatch=False,parent=None,historical=(payload,historical),expected_logins=len(logins))
    else:
        # Only the fake elapsed prerequisite is repaired. No production limit
        # is reset; the final operation has a new explicit FD snapshot/ID.
        if not retained and (len(logins)>baseline_logins or variant in {"before-signin","started-uncertain","reused-generation","reused-home","missing-artifact","cap","cooldown"}):
            now=runtime.clock()+3700;runtime.clock=lambda:now
        await store.set_spawn_admission_hold(old.host,held_until=runtime.clock()+1600,reason="current authorized hatch hold")
        result.update(current=current,hatch=True,hatch_signins=len(logins) if retained else len(logins)+1)
    return result
