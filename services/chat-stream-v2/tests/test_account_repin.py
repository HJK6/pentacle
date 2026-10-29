"""Same-home version changes preserve the existing healthy authentication."""
import asyncio
from dataclasses import replace

import pytest
from account_executor import AccountExecutor
from provider_accounts import AccountError
from provider_cli_pin import digest
from provider_cli_artifact import code_identity
from sessions import VerbError
from test_account_rotation import binding, observation, NOW
from test_account_runtime import setup, AUTH
from test_provider_cli_pin import release_pair, seed_pin


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_healthy_same_home_repin_is_a_distinct_receiver_journey(provider,tmp_path,monkeypatch):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch,provider)
        try:
            result=await runtime.operation(request)
            assert result["result"]["binding"]["home"]==old.home
            assert await runtime.run(lambda a:a.bound(old.key))==old
            assert "enroll" not in calls and len(logins)==1
            assert len([argv for argv in native if "Reply exactly OK" in argv])==1
        finally:store.stop()
    asyncio.run(run())


async def host_journey(tmp_path,monkeypatch,provider="codex",platform="macos",*,adopted=False,alias="repin",central_public=False,transport=False):
    import hashlib
    from pathlib import Path
    from types import SimpleNamespace
    from provider_account_host import NativeHost
    import provider_cli_artifact
    private=tmp_path/"private";private.mkdir(mode=0o700)
    old_bin=tmp_path/"old-native";old_bin.write_text("old fixture")
    new_bin=tmp_path/"new-native";new_bin.write_text("new fixture")
    cell={"enabled":True,"expected_provider_id":"native-repin","profile_ref":"fixture-profile",
          "executable":str(old_bin),"executable_sha256":hashlib.sha256(old_bin.read_bytes()).hexdigest(),
          "version":"0.1.0","platform":platform,"model":"fixture-model","effort":"high","cwd":str(tmp_path),
          "resource_root":str(old_bin)}
    if adopted:
        dedicated=tmp_path/"dedicated-home";dedicated.mkdir(mode=0o700)
        auth=dedicated/"auth.json";auth.write_text("fake fixture only");auth.chmod(0o600)
        cell["adopt_home"]=str(dedicated)
    calls=[];native=[];logins=[]
    def login(account,*_):
        logins.append(account.key)
        if provider=="codex" or platform=="wsl":
            p=Path(account.home)/("auth.json" if provider=="codex" else ".credentials.json")
            p.write_text("fake fixture only");p.chmod(0o600)
        return "ok"
    def run(argv,**kwargs):
        native.append(argv)
        return SimpleNamespace(returncode=0,stdout="" if argv[0]=="ps" else cell.get("native_versions",{}).get(argv[0],cell["version"]) if argv[-1]=="--version" else "OK")
    host=NativeHost({"host":"host-a","registry":str(private/"registry.json"),"root":str(private/"homes"),
        "cells":{provider+":"+alias:cell}},run=run,login=login,observation=lambda b:dict(observation(b),provider_id="native-repin",pool_id="pool-repin"))
    enrolled=host.handle("adopt" if adopted else "enroll",provider=provider,alias=alias,expected_generation="unenrolled",operation_id="initial",generation="initial")
    assert enrolled["outcome"]=="ok"
    from provider_accounts import Binding
    old=host.registry.active(provider,alias,"host-a")
    host.config["mutation_clearance"]="required"
    store,runtime=await setup()
    await runtime.run(lambda a:a.register(replace(old,profile_ref="") if central_public else old))
    await runtime.run(lambda a:a.set_default(host="host-a",provider=provider,alias=alias))
    await store.set_spawn_admission_hold("host-a",held_until=NOW+1000,reason="repin fixture")
    pin=release_pair();pin["revision"]=2
    new_hash=hashlib.sha256(new_bin.read_bytes()).hexdigest()
    for artifact in pin["providers"][provider]["artifacts"].values():
        artifact.update(native_sha256=new_hash,resource_sha256=new_hash)
    from provider_cli_pin import set_pin
    await runtime.run(lambda a:set_pin(a,{"pin":pin,"expected_revision":1,"operation_id":"native-pair","reason":"fixture"}))
    cell.update(executable=str(new_bin),executable_sha256=new_hash,resource_root=str(new_bin),version=pin["providers"][provider]["version"])
    monkeypatch.setattr(provider_cli_artifact,"platform_key",lambda:"darwin-arm64" if platform=="macos" else "linux-x64")
    async def execute(account,action,op,generation=None,*,pin=None,reconcile=None):
        calls.append(action)
        return host.handle(action,provider=account.provider,alias=account.alias,expected_generation=account.generation,
            operation_id=hashlib.sha256(op.encode()).hexdigest(),generation=generation,pin=pin,**({"reconcile":reconcile} if reconcile is not None else {}))
    runtime.executor=AccountExecutor(runtime,None,execute=execute)
    if transport:
        cell["native_versions"]={old.executable:old.version,cell["executable"]:cell["version"]}
        from provider_account_native import verify_binary
        def identity_probe(binding):
            verify_binary(binding,run=host.run)
            return dict(observation(binding),provider_id="native-repin",pool_id="pool-repin")
        host._probe_observation=identity_probe
        import json
        class FixtureTransport:
            async def run_command(self,hostname,*argv,timeout):
                assert hostname==old.host and argv[:2]==("/fixture/python","/fixture/provider_account_host.py")
                action=argv[2];flags=dict(zip(argv[3::2],argv[4::2]));calls.append(action)
                target=host
                if getattr(host,"separate_dispatch_registries",False):
                    # SSH starts a separate helper process with its own local
                    # RLock; only the durable registry flock is shared.
                    import copy
                    from provider_accounts import Registry
                    target=copy.copy(host)
                    target.registry=Registry(host.registry.path,host.registry.root,host.registry.adopted)
                try:
                    result=target.handle(action,provider=flags["--provider"],alias=flags["--account"],
                        expected_generation=flags["--login-generation"],operation_id=flags["--operation-id"],generation=flags.get("--new-generation"),
                        pin=json.loads(flags["--pin-json"]) if "--pin-json" in flags else None,
                        reconcile=json.loads(flags["--reconcile-json"]) if "--reconcile-json" in flags else None)
                    return 0,json.dumps(result)
                except AccountError as exc:return 1,json.dumps({"outcome":str(exc)})
        runtime.policy["executors"]={old.host:{"python":"/fixture/python","script":"/fixture/provider_account_host.py"}}
        runtime.executor=AccountExecutor(runtime,FixtureTransport())
    request={"action":"repin","host":"host-a","provider":provider,"account":alias,"login_generation":old.generation,
        "new_generation":"successor","operation_id":"native-repin","expected_pin_revision":2,"expected_pin_digest":digest(pin),
        "reason":"reviewed pair","_auth_context":AUTH}
    native.clear()
    return store,runtime,host,old,request,calls,native,logins,cell,pin


@pytest.mark.parametrize("provider,platform",[("codex","macos"),("codex","wsl"),("claude","macos"),("claude","wsl")])
def test_real_registry_same_home_repin_and_lineage_resume(tmp_path,monkeypatch,provider,platform):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch,provider,platform)
        try:
            before=await runtime.run(lambda a:a.bound(old.key))
            default=await runtime.run(lambda a:a.defaults())
            result=await runtime.operation(request)
            new=host.registry.active(provider,"repin","host-a")
            assert result["result"]["outcome"]=="repinned" and new.home==old.home and new.key!=old.key
            assert len(logins)==1
            turns=[v for v in native if "Reply exactly OK" in v]
            assert len(turns)==1
            assert await runtime.run(lambda a:a.bound(old.key))==before
            assert await runtime.run(lambda a:a.defaults())==default
            first_calls=list(calls)
            assert (await runtime.operation(request))["result"]==result["result"]
            assert calls==first_calls
            resumed=await runtime.choose_launch(host="host-a",provider=provider,model="fixture",effort="high",
                message={"resume_session_id":"native-conversation","_account_resume_binding":old.public()})
            assert resumed.key==new.key and resumed.home==old.home
            # The successor binds through the existing immutable lifecycle attribution table.
            row=await store.open_session("host-a","resumed",session_generation="two",provider=provider,account_binding=resumed.public())
            await runtime.bind_launch(row)
            assert await runtime.run(lambda a:a.bound(old.key))==old
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("case",["no-hold","expired-hold","health","stale-generation","new-existing","stale-pin","bad-auth"])
def test_repin_refuses_before_host_effects(tmp_path,monkeypatch,case):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch)
        try:
            if case=="no-hold": await store.clear_spawn_admission_hold(old.host)
            if case=="expired-hold": await store.set_spawn_admission_hold(old.host,held_until=NOW,reason="expired")
            if case=="health": await runtime.record_failure(old,"auth")
            if case=="stale-generation": request["login_generation"]="stale"
            if case=="stale-pin": request["expected_pin_revision"]=1
            if case=="bad-auth": request["_auth_context"]={"role":"front-desk"}
            if case=="new-existing": await runtime.run(lambda a:a.register(replace(old,generation="successor")))
            with pytest.raises(VerbError): await runtime.operation(request)
            assert not calls and len(logins)==1
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("case",["hold","default","health","pin"])
def test_host_success_central_race_parks_then_exact_hold_replay_reconciles(tmp_path,monkeypatch,case):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch)
        execute=runtime.executor.execute
        async def race(*args,**kwargs):
            result=await execute(*args,**kwargs)
            if args[1]=="repin":
                if case=="hold": await store.set_spawn_admission_hold(old.host,held_until=NOW+2000,reason="renewed")
                if case=="default": await runtime.run(lambda a:a.set_default(host=old.host,provider=old.provider,alias=old.alias))
                if case=="health": await runtime.record_failure(old,"identity")
                if case=="pin":
                    from provider_cli_pin import set_pin
                    await runtime.run(lambda a:set_pin(a,{"pin":dict(pin,revision=3),"expected_revision":2,"operation_id":"move-during-proof","reason":"fixture"}))
            return result
        runtime.executor.execute=race
        try:
            with pytest.raises(VerbError): await runtime.operation(request)
            assert host.registry.active(old.provider,old.alias,old.host).generation=="successor"
            assert (await runtime.run(lambda a:a._cell(old.host,old.provider,old.alias)))["generation"]==old.generation
            assert len([v for v in native if "Reply exactly OK" in v])==1
            if case=="hold":
                runtime.executor.execute=execute
                result=await runtime.operation(request)
                assert result["result"]["outcome"]=="repinned"
                assert calls.count("repin")==1 and "repin-inspect" in calls and calls.count("repin-reconcile")==0
                assert len([v for v in native if "Reply exactly OK" in v])==1
            else:
                with pytest.raises(VerbError): await runtime.operation(request)
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("failure",["auth","identity","network","home-busy","resource","symlink"])
def test_native_repin_failure_never_logs_in_or_imports_successor(tmp_path,monkeypatch,failure):
    from pathlib import Path
    from types import SimpleNamespace
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch)
        original_run=host.run
        if failure in {"auth","network"}:
            def observation_error(_): raise AccountError("provider_auth_failed" if failure=="auth" else "provider_transport_failed")
            host.observation=observation_error
        if failure=="identity": host.observation=lambda b:dict(observation(b),provider_id="foreign")
        if failure=="resource": Path(cell["resource_root"]).write_text("tampered")
        if failure=="symlink":
            linked=tmp_path/"linked";linked.symlink_to(cell["executable"]);cell["executable"]=str(linked)
        if failure=="home-busy":
            def busy(argv,**kwargs):
                if argv[0]=="ps": return SimpleNamespace(returncode=0,stdout="123 codex /fixture/native")
                return original_run(argv,**kwargs)
            host.run=busy
        try:
            with pytest.raises((VerbError,AccountError)): await runtime.operation(request)
            assert (await runtime.run(lambda a:a._cell(old.host,old.provider,old.alias)))["generation"]==old.generation
            assert host.registry.active(old.provider,old.alias,old.host).key==old.key
            assert len(logins)==1
        finally: store.stop()
    asyncio.run(run())


def test_rollback_uses_fresh_successor_and_chain_rejects_forged_lineage(tmp_path,monkeypatch):
    import hashlib
    from pathlib import Path
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch)
        try:
            await runtime.operation(request)
            current=host.registry.active(old.provider,old.alias,old.host)
            old_hash=hashlib.sha256(Path(old.executable).read_bytes()).hexdigest()
            rollback=dict(release_pair(),revision=3)
            rollback["providers"][old.provider]["version"]=old.version
            for artifact in rollback["providers"][old.provider]["artifacts"].values():
                artifact.update(native_sha256=old_hash,resource_sha256=old_hash)
            from provider_cli_pin import set_pin
            await runtime.run(lambda a:set_pin(a,{"pin":rollback,"expected_revision":2,"operation_id":"rollback-pair","reason":"fixture"}))
            cell.update(executable=old.executable,executable_sha256=old_hash,version=old.version,resource_root=old.executable)
            backward=dict(request,login_generation=current.generation,new_generation="rollback-successor",
                operation_id="rollback-native",expected_pin_revision=3,expected_pin_digest=digest(rollback))
            result=await runtime.operation(backward)
            assert result["result"]["binding"]["generation"]=="rollback-successor"
            resumed=await runtime.choose_launch(host=old.host,provider=old.provider,model="fixture",effort="high",
                message={"resume_session_id":"conversation","_account_resume_binding":old.public()})
            assert resumed.generation=="rollback-successor" and resumed.executable==old.executable
            assert await runtime.run(lambda a:a.bound(old.key))==old and len(logins)==1
            def forge(a):
                a.conn.execute("UPDATE v2_account_recovery SET data=? WHERE binding_key=?",('{"operation_key":"unknown","new_key":"forged"}',"repin-lineage:"+old.key))
                a.conn.commit()
            await runtime.run(forge)
            with pytest.raises(AccountError,match="account_resume_lineage_invalid"):
                await runtime.choose_launch(host=old.host,provider=old.provider,model="fixture",effort="high",
                    message={"resume_session_id":"conversation","_account_resume_binding":old.public()})
        finally: store.stop()
    asyncio.run(run())


def test_compliant_repin_is_noop_without_another_native_turn(tmp_path,monkeypatch):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch)
        try:
            await runtime.operation(request)
            current=host.registry.active(old.provider,old.alias,old.host)
            native.clear()
            result=await runtime.operation(dict(request,login_generation=current.generation,new_generation="unused",
                operation_id="no-op"))
            assert result["result"]["outcome"]=="no_op"
            assert result["result"]["binding"]["generation"]==current.generation
            assert not any("Reply exactly OK" in argv for argv in native)
            assert len(logins)==1
        finally: store.stop()
    asyncio.run(run())


def test_adopted_codex_repin_keeps_original_home_and_credential_file(tmp_path,monkeypatch):
    from pathlib import Path
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch,adopted=True)
        auth=Path(old.home)/"auth.json";before=auth.stat()
        try:
            result=await runtime.operation(request)
            after=auth.stat()
            assert result["result"]["binding"]["home"]==old.home
            assert (before.st_ino,before.st_mtime_ns)==(after.st_ino,after.st_mtime_ns)
            assert not logins
        finally: store.stop()
    asyncio.run(run())



async def split_journey(tmp_path,monkeypatch,provider="codex",platform="macos",*,alias="repin",changed=False,other=False,adopted=False,central_public=False):
    import copy,hashlib
    from pathlib import Path
    from provider_cli_pin import set_pin
    j=await host_journey(tmp_path,monkeypatch,provider,platform,alias=alias,adopted=adopted,central_public=central_public)
    store,runtime,host,old,request,calls,native,logins,cell,pin=j
    latest=copy.deepcopy(pin);latest["revision"]=3
    if changed:
        target=tmp_path/"latest-native";target.write_text("latest fixture")
        sha=hashlib.sha256(target.read_bytes()).hexdigest()
        latest["providers"][provider]["version"]="9.9.9"
        for a in latest["providers"][provider]["artifacts"].values(): a.update(native_sha256=sha,resource_sha256=sha)
    if other: latest["providers"]["claude" if provider=="codex" else "codex"]["version"]="8.8.8"
    execute=runtime.executor.execute
    async def advance(*args,**kwargs):
        result=await execute(*args,**kwargs)
        if args[1]=="repin":
            await runtime.run(lambda a:set_pin(a,{"pin":latest,"expected_revision":2,
                "operation_id":"benign-revision-advance","reason":"reviewed pair"}))
        return result
    runtime.executor.execute=advance
    with pytest.raises(VerbError,match="provider_cli_pin_stale"): await runtime.operation(request)
    runtime.executor.execute=execute
    if changed: cell.update(executable=str(target),resource_root=str(target),executable_sha256=sha,version="9.9.9")
    catchup=dict(request,operation_id="catch-up-current",expected_pin_revision=3,
        expected_pin_digest=digest(latest),new_generation="current-successor",reconcile_of=request["operation_id"])
    return j,catchup,latest


@pytest.mark.parametrize("provider,platform,alias",[(p,s,a) for p in ("claude","codex") for s in ("macos","wsl") for a in ("repin","sai")])
def test_required_catchup_after_host_success_then_pin_revision_changes(tmp_path,monkeypatch,provider,platform,alias):
    async def run():
        j,latest,pin=await split_journey(tmp_path,monkeypatch,provider,platform,alias=alias)
        store,runtime,host,old,request,calls,native,logins,cell,_=j
        try:
            import copy
            saved=copy.deepcopy(list(host.journal.glob("*.json")))
            original={p.name:p.read_bytes() for p in saved}
            # Ordinary import remains strict; no caller-selected successor may bridge the split.
            with pytest.raises(VerbError): await runtime.operation({k:v for k,v in latest.items() if k!="reconcile_of"})
            result=(await runtime.operation(latest))["result"]
            new=host.registry.active(provider,alias,old.host)
            assert result["outcome"]=="repinned" and result["binding"]["generation"]=="successor"
            assert await runtime.run(lambda a:a.bound(old.key))==old
            assert await runtime.run(lambda a:a.bound(old.host+":"+provider+":"+alias+":current-successor")) is None
            assert old.host+":"+provider+":"+alias+":current-successor" not in host.registry._load()["generations"]
            assert len([v for v in native if "Reply exactly OK" in v])==1 and len(logins)==1
            assert all((host.journal/name).read_bytes()==body for name,body in original.items())
            assert (await runtime.operation(latest))["result"]==result
            resumed=await runtime.choose_launch(host=old.host,provider=provider,model="fixture",effort="high",
                message={"resume_session_id":"old-conversation","_account_resume_binding":old.public()})
            assert resumed.key==new.key
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("provider,platform",[(p,s) for p in ("claude","codex") for s in ("macos","wsl")])
def test_reconcile_catchup_changed_current_target(tmp_path,monkeypatch,provider,platform):
    async def run():
        j,latest,pin=await split_journey(tmp_path,monkeypatch,provider,platform,changed=True)
        store,runtime,host,old,request,calls,native,logins,cell,_=j
        try:
            defaults=await runtime.run(lambda a:a.defaults())
            result=(await runtime.operation(latest))["result"]
            assert result["binding"]["generation"]=="current-successor"
            assert len([v for v in native if "Reply exactly OK" in v])==2 and len(logins)==1
            assert result["binding"]["home"]==old.home
            assert await runtime.run(lambda a:a.defaults())==defaults
            resumed=await runtime.choose_launch(host=old.host,provider=provider,model="fixture",effort="high",
                message={"resume_session_id":"conversation","_account_resume_binding":old.public()})
            assert resumed.generation=="current-successor"
            assert await runtime.run(lambda a:a.bound(old.key))==old
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("provider,platform",[(p,s) for p in ("claude","codex") for s in ("macos","wsl")])
def test_reconcile_other_provider_only_pin_change(tmp_path,monkeypatch,provider,platform):
    async def run():
        j,latest,pin=await split_journey(tmp_path,monkeypatch,provider,platform,other=True)
        store,runtime,host,old,request,calls,native,logins,cell,_=j
        try:
            result=(await runtime.operation(latest))["result"]
            assert result["binding"]["generation"]=="successor"
            assert len([v for v in native if "Reply exactly OK" in v])==1 and len(logins)==1
        finally:store.stop()
    asyncio.run(run())


async def assert_split(runtime,old):
    assert (await runtime.run(lambda a:a._cell(old.host,old.provider,old.alias)))["generation"]==old.generation
    assert await runtime.run(lambda a:a.bound(old.key))==old
    from account_repin import fenced
    assert await runtime.run(lambda a:fenced(a,old))
    assert not (await runtime.pin_status())["fleet_complete"]


@pytest.mark.parametrize("case",["missing","expired","replaced","mid-proof-expired"])
def test_reconcile_requires_unchanged_real_hold(tmp_path,monkeypatch,case):
    async def run():
        j,request,pin=await split_journey(tmp_path,monkeypatch)
        store,runtime,host,old,_,calls,native,logins,cell,_=j
        execute=runtime.executor.execute
        try:
            if case=="missing": await store.clear_spawn_admission_hold(old.host)
            if case=="expired": await store.set_spawn_admission_hold(old.host,held_until=NOW,reason="expired")
            async def race(*args,**kwargs):
                result=await execute(*args,**kwargs)
                if args[1]=="repin-reconcile":
                    if case=="replaced": await store.set_spawn_admission_hold(old.host,held_until=NOW+2000,reason="replaced")
                    if case=="mid-proof-expired": runtime.clock=lambda:NOW+1001
                return result
            runtime.executor.execute=race
            with pytest.raises(VerbError): await runtime.operation(request)
            await assert_split(runtime,old)
            runtime.clock=lambda:NOW
            await store.set_spawn_admission_hold(old.host,held_until=NOW+3000,reason="real renewed hold")
            runtime.executor.execute=execute
            result=(await runtime.operation(request))["result"]
            assert result["binding"]["generation"]=="successor"
            assert len([v for v in native if "Reply exactly OK" in v])==1 and len(logins)==1
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("case",["missing-journal","missing-proof","tampered-proof","original-pin","artifact","consumer",
    "active-successor","unsafe-anchor","symlink-anchor","provider-id","pool","home","profile","platform","cycle","fork","login","enroll","adopt"])
def test_reconcile_rejects_invalid_lineage_and_anchor(tmp_path,monkeypatch,case):
    import json,hashlib
    from pathlib import Path
    async def run():
        j,request,pin=await split_journey(tmp_path,monkeypatch)
        store,runtime,host,old,original,calls,native,logins,cell,_=j
        path=host.journal/(hashlib.sha256(("repin:"+original["operation_id"]).encode()).hexdigest()+".json")
        try:
            data=json.loads(path.read_text())
            if case=="missing-journal": path.unlink()
            elif case in {"missing-proof","tampered-proof","original-pin","artifact","consumer","login","enroll","adopt"}:
                if case=="missing-proof": data.pop("proof")
                if case=="tampered-proof": data["proof"]["binding_key"]="foreign"
                if case=="original-pin": data["pin_revision"]=99
                if case=="artifact": data["proof"]["artifact"]["native_sha256"]="f"*64
                if case=="consumer": data["proof"]["code_identity"]="f"*64
                if case in {"login","enroll","adopt"}: data["kind"]=case
                path.write_text(json.dumps(data))
            elif case in {"unsafe-anchor","symlink-anchor"}:
                home=Path(old.home)
                if case=="unsafe-anchor": home.chmod(0o777)
                else:
                    moved=home.with_name("moved");home.rename(moved);home.symlink_to(moved)
            elif case in {"profile","platform"}:
                cell["profile_ref" if case=="profile" else "platform"]="drift"
            elif case=="cycle":
                def cycle(a):
                    from account_repin import _read,_write,_payload
                    args=_payload(original);s=_read(a,args);s["arguments"]["reconcile_of"]=original["operation_id"]
                    _write(a,s["arguments"],s);a.conn.commit()
                await runtime.run(cycle)
            else:
                registry=host.registry._load();key=registry["active"][old.host+":"+old.provider+":"+old.alias]
                if case=="active-successor": registry["active"][old.host+":"+old.provider+":"+old.alias]=old.key
                if case=="provider-id": registry["generations"][key]["provider_id"]="foreign"
                if case=="pool": registry["generations"][key]["pool_id"]="foreign"
                if case=="home": registry["generations"][key]["home"]=str(tmp_path)
                if case=="fork":
                    registry["generations"][key]["generation"]="unproven"
                host.registry._save(registry)
            before=await runtime.run(lambda a:[tuple(r) for r in a.conn.execute("SELECT * FROM v2_account_bindings")])
            with pytest.raises((VerbError,AccountError)): await runtime.operation(request)
            assert await runtime.run(lambda a:[tuple(r) for r in a.conn.execute("SELECT * FROM v2_account_bindings")])==before
            assert len([v for v in native if "Reply exactly OK" in v])==1 and len(logins)==1
        finally:store.stop()
    asyncio.run(run())


def test_reconcile_adopted_codex_anchor_and_retained_proof(tmp_path,monkeypatch):
    from pathlib import Path
    async def run():
        j,request,pin=await split_journey(tmp_path,monkeypatch,adopted=True)
        store,runtime,host,old,original,calls,native,logins,cell,_=j
        auth=Path(old.home)/"auth.json";before=auth.stat()
        try:
            # Retained observation can age; it is not relabelled current by byte verification.
            runtime.clock=lambda:NOW+500
            result=(await runtime.operation(request))["result"]
            after=auth.stat()
            assert result["binding"]["home"]==old.home
            assert (before.st_ino,before.st_mtime_ns)==(after.st_ino,after.st_mtime_ns)
            assert not logins and len([v for v in native if "Reply exactly OK" in v])==1
            readback=await runtime.run(lambda a:a.readback(now=NOW+500))
            assert next(r for r in readback["accounts"] if r["active"] and r["alias"]==old.alias)["observation"]["observed_at"]==NOW
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("case",["generation","health","default","policy","host-selection"])
def test_reconcile_rejects_central_or_host_race(tmp_path,monkeypatch,case):
    async def run():
        j,request,pin=await split_journey(tmp_path,monkeypatch,changed=True)
        store,runtime,host,old,original,calls,native,logins,cell,_=j
        execute=runtime.executor.execute
        async def race(*args,**kwargs):
            result=await execute(*args,**kwargs)
            if args[1]=="repin-reconcile":
                if case=="health": await runtime.record_failure(old,"auth")
                if case=="default": await runtime.run(lambda a:a.set_default(host=old.host,provider=old.provider,alias=old.alias))
                if case=="generation": await runtime.run(lambda a:a.register(replace(old,generation="other-central")))
                if case=="policy": runtime.policy["enabled"]=False
                if case=="host-selection":
                    state=host.registry._load();state["active"][old.host+":"+old.provider+":"+old.alias]=old.key;host.registry._save(state)
            return result
        runtime.executor.execute=race
        try:
            with pytest.raises(VerbError): await runtime.operation(request)
            assert await runtime.run(lambda a:a.bound(old.host+":"+old.provider+":"+old.alias+":successor")) is None
            assert await runtime.run(lambda a:a.bound(old.host+":"+old.provider+":"+old.alias+":current-successor")) is None
            assert not (await runtime.pin_status())["fleet_complete"]
            assert len(logins)==1
        finally:store.stop()
    asyncio.run(run())


def test_reconcile_second_pin_race_and_restart(tmp_path,monkeypatch):
    from provider_cli_pin import set_pin
    from account_runtime import AccountRuntime
    async def run():
        j,request,pin=await split_journey(tmp_path,monkeypatch,changed=True)
        store,runtime,host,old,original,calls,native,logins,cell,_=j
        execute=runtime.executor.execute
        async def race(*args,**kwargs):
            result=await execute(*args,**kwargs)
            if args[1]=="repin-reconcile":
                await runtime.run(lambda a:set_pin(a,{"pin":dict(pin,revision=4),"expected_revision":3,"operation_id":"second-race","reason":"fixture"}))
            return result
        runtime.executor.execute=race
        try:
            with pytest.raises(VerbError,match="provider_cli_pin_stale"): await runtime.operation(request)
            await assert_split(runtime,old)
            assert host.registry.active(old.provider,old.alias,old.host).generation=="current-successor"
            # Reconstruct the coordinator using the same Store and persisted operation chain.
            restarted=AccountRuntime(store,runtime.sessions,policy=runtime.policy,clock=lambda:NOW)
            restarted.attach(runtime.server,runtime.spawnctl,runtime.comms)
            restarted.executor=AccountExecutor(restarted,None,execute=execute)
            await store.set_spawn_admission_hold(old.host,held_until=NOW+3000,reason="restart renewed")
            newest=dict(request,operation_id="newest-catchup",reconcile_of=request["operation_id"],
                new_generation="unused-newest",expected_pin_revision=4,expected_pin_digest=digest(dict(pin,revision=4)))
            result=(await restarted.operation(newest))["result"]
            assert result["binding"]["generation"]=="current-successor"
            assert len([v for v in native if "Reply exactly OK" in v])==2 and len(logins)==1
            resumed=await restarted.choose_launch(host=old.host,provider=old.provider,model="fixture",effort="high",
                message={"resume_session_id":"conversation","_account_resume_binding":old.public()})
            assert resumed.generation=="current-successor"
        finally:store.stop()
    asyncio.run(run())


def test_reconcile_split_fence_survives_hold_expiry_and_pin_rollback(tmp_path,monkeypatch):
    import copy
    from provider_cli_pin import set_pin
    async def run():
        j,request,pin=await split_journey(tmp_path,monkeypatch)
        store,runtime,host,old,original,calls,native,logins,cell,_=j
        try:
            rollback=copy.deepcopy(pin);rollback["revision"]=4
            rollback["providers"][old.provider]["version"]=old.version
            for a in rollback["providers"][old.provider]["artifacts"].values():
                a.update(native_sha256=old.executable_sha256,resource_sha256=old.executable_sha256)
            await runtime.run(lambda a:set_pin(a,{"pin":rollback,"expected_revision":3,"operation_id":"rollback-to-old","reason":"fixture"}))
            await store.clear_spawn_admission_hold(old.host)
            for message in ({},{"account":old.alias},{"resume_session_id":"old","_account_resume_binding":old.public()}):
                with pytest.raises(AccountError,match="account_repin_reconciliation_required"):
                    await runtime.choose_launch(host=old.host,provider=old.provider,model="fixture",effort="high",message=dict(message,_auth_context=AUTH))
            historical=await store.open_session(old.host,"held-work",session_generation="held-work",provider=old.provider,account_binding=old.public())
            with pytest.raises(AccountError,match="account_repin_reconciliation_required"): await runtime.before_input(historical)
            await assert_split(runtime,old)
            status=await runtime.pin_status()
            assert next(c for c in status["cells"] if c["account"]==old.alias)["reason"]=="repin_reconciliation_pending"
            with pytest.raises(VerbError): await runtime.operation(original)
            assert len([v for v in native if "Reply exactly OK" in v])==1
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("point",["before-host","after-host","before-central","after-central"])
def test_reconcile_idempotency_crash_and_sibling_attempts(tmp_path,monkeypatch,point):
    import account_repin_state
    async def run():
        j,request,pin=await split_journey(tmp_path,monkeypatch,changed=True)
        store,runtime,host,old,original,calls,native,logins,cell,_=j
        execute=runtime.executor.execute;commit=account_repin_state._commit
        crashed=False
        async def crash(*args,**kwargs):
            nonlocal crashed
            if args[1]=="repin-reconcile" and not crashed:
                crashed=True
                if point=="before-host": raise AccountError("account_host_unreachable")
                result=await execute(*args,**kwargs)
                if point=="after-host": raise AccountError("account_host_unreachable")
                return result
            return await execute(*args,**kwargs)
        def crash_commit(*args):
            if point=="after-central": commit(*args)
            raise AccountError("fixture_lost_commit_reply")
        runtime.executor.execute=crash
        try:
            if point in {"before-central","after-central"}: monkeypatch.setattr(account_repin_state,"_commit",crash_commit)
            with pytest.raises(VerbError): await runtime.operation(request)
            # Unchanged payload is immutable even during an unresolved attempt.
            with pytest.raises(VerbError,match="account_repin_id_conflict"):
                await runtime.operation(dict(request,reason="changed"))
            if point!="after-central":
                await assert_split(runtime,old)
                with pytest.raises(VerbError):
                    await runtime.operation(dict(request,operation_id="sibling",new_generation="sibling"))
            runtime.executor.execute=execute;monkeypatch.setattr(account_repin_state,"_commit",commit)
            result=(await runtime.operation(request))["result"]
            if point=="before-host":
                assert result=={"outcome":"settled","operation_id":request["operation_id"],"historical":True}
                request=dict(request,operation_id="fresh-after-absent",reconcile_of=request["operation_id"])
                result=(await runtime.operation(request))["result"]
            count=len(calls)
            assert (await runtime.operation(request))["result"]==result and len(calls)==count
            assert result["binding"]["generation"]=="current-successor"
            assert len([v for v in native if "Reply exactly OK" in v])==2 and len(logins)==1
            with pytest.raises(VerbError):
                await runtime.operation(dict(request,operation_id="consume-twice",new_generation="unused"))
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("case",["offline","partial","failed-proof","tamper","auth","incompatible-home"])
def test_reconcile_failed_or_unreachable_host_is_parked(tmp_path,monkeypatch,case):
    from pathlib import Path
    async def run():
        j,request,pin=await split_journey(tmp_path,monkeypatch,changed=True)
        store,runtime,host,old,original,calls,native,logins,cell,_=j
        execute=runtime.executor.execute
        if case=="tamper": Path(cell["resource_root"]).write_text("tampered")
        if case=="auth": host.observation=lambda b:(_ for _ in ()).throw(AccountError("provider_auth_failed"))
        if case=="incompatible-home": Path(old.home).chmod(0o777)
        async def fail(*args,**kwargs):
            if args[1]=="repin-reconcile":
                if case=="offline": raise AccountError("account_host_unreachable")
                if case=="partial": return {"state":"started","outcome":"execution_unresolved"}
                if case=="failed-proof": return {"state":"done","outcome":"provider_probe_failed"}
            return await execute(*args,**kwargs)
        runtime.executor.execute=fail
        try:
            try: result=(await runtime.operation(request))["result"];assert result["outcome"]=="execution_unresolved"
            except VerbError: pass
            await assert_split(runtime,old)
            assert len(logins)==1
            assert host.registry._load()["active"][old.host+":"+old.provider+":"+old.alias].endswith(":successor")
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("case",["resume","restore","missing-link","forged-link","stale-import","arbitrary-successor","unauthorized","no-hold","old-generation","normal-spawn"])
def test_reconcile_resume_and_refusal_regressions(tmp_path,monkeypatch,case):
    import json
    async def run():
        j,request,pin=await split_journey(tmp_path,monkeypatch,provider="claude",changed=True)
        store,runtime,host,old,original,calls,native,logins,cell,_=j
        try:
            if case in {"unauthorized","no-hold","stale-import","arbitrary-successor"}:
                if case=="unauthorized": request["_auth_context"]={"role":"front-desk"}
                if case=="no-hold": await store.clear_spawn_admission_hold(old.host)
                if case=="stale-import": request=original
                if case=="arbitrary-successor": request["login_generation"]="successor"
                with pytest.raises(VerbError): await runtime.operation(request)
                await assert_split(runtime,old)
                return
            historical=await store.open_session(old.host,"historical-seat",session_generation="original-seat",provider=old.provider,account_binding=old.public())
            await runtime.operation(request)
            if case=="old-generation":
                with pytest.raises(VerbError): await runtime.operation(dict(request,operation_id="old",reconcile_of=None,new_generation="historical"))
                return
            if case in {"missing-link","forged-link"}:
                def corrupt(a):
                    key="repin-lineage:"+old.host+":"+old.provider+":"+old.alias+":successor"
                    if case=="missing-link": a.conn.execute("DELETE FROM v2_account_recovery WHERE binding_key=?",(key,))
                    else:
                        row=a.conn.execute("SELECT data FROM v2_account_recovery WHERE binding_key=?",(key,)).fetchone()
                        link=json.loads(row[0]);link["evidence"]["old"]["home"]="/forged"
                        a.conn.execute("UPDATE v2_account_recovery SET data=? WHERE binding_key=?",(json.dumps(link),key))
                    a.conn.commit()
                await runtime.run(corrupt)
                with pytest.raises(AccountError):
                    await runtime.choose_launch(host=old.host,provider=old.provider,model="fixture",effort="high",
                        message={"resume_session_id":"old","_account_resume_binding":old.public()})
                return
            resumed=await runtime.choose_launch(host=old.host,provider=old.provider,model="fixture",effort="high",
                message={"resume_session_id":"old","_account_resume_binding":old.public()})
            assert resumed.generation=="current-successor"
            if case in {"resume","restore"}:
                import launch
                from spawnctl import SpawnCtl
                ctl=SpawnCtl(store,runtime.sessions,tmux=object(),machine=launch.local_machine(old.host,cwd=str(tmp_path),codex_bin="/bin/echo",claude_bin="/bin/echo"))
                ctl.account_runtime=runtime
                launch_request={"provider":old.provider,"resume_session_id":"531c40b0-706d-47d7-b2f9-24e61b50b216",
                    "_account_resume_binding":old.public(),"_resume_jsonl_path":old.transcript_root+"/fixture.jsonl","_resume_cwd":str(tmp_path),"_defer_launch_token_stage":True}
                if case=="restore": launch_request.update(_auth_context={"service_authenticated":True,"service_actor":"daemon:scheduler"},_session_generation="f"*32)
                command,resolution,overrides=await ctl._resolve_launch(launch_request,old.host,"resumed-seat")
                assert overrides["account_binding"]["generation"]=="current-successor"
                assert resolution["account_binding"]["home"]==old.home and "--resume" in command
                assert await store.fetch_session(old.host,"historical-seat")==historical
            if case=="normal-spawn":
                with pytest.raises(AccountError,match="provider_cli_pin_unavailable"):
                    await runtime.admit_launch({"host":old.host,"account_binding":resumed.public()})
            assert await runtime.run(lambda a:a.bound(old.key))==old
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("changed",[False,True])
def test_reconcile_keeps_private_host_profile_out_of_central_bindings(tmp_path,monkeypatch,changed):
    async def run():
        j,request,pin=await split_journey(tmp_path,monkeypatch,changed=changed,central_public=True)
        store,runtime,host,old,original,calls,native,logins,cell,_=j
        try:
            result=(await runtime.operation(request))["result"]
            from provider_accounts import Binding
            central=await runtime.run(lambda a:a.bound(":".join((old.host,old.provider,old.alias,result["binding"]["generation"]))))
            assert central.profile_ref=="" and host.registry.active(old.provider,old.alias,old.host).profile_ref==old.profile_ref
            resumed=await runtime.choose_launch(host=old.host,provider=old.provider,model="fixture",effort="high",
                message={"resume_session_id":"old","_account_resume_binding":old.public()})
            assert resumed.key==central.key
        finally:store.stop()
    asyncio.run(run())


def test_reconcile_stale_same_id_status_never_starts_an_absent_target(tmp_path,monkeypatch):
    from provider_cli_pin import set_pin
    async def run():
        j,request,pin=await split_journey(tmp_path,monkeypatch,changed=True)
        store,runtime,host,old,original,calls,native,logins,cell,_=j
        repin=host._repin
        def crash(action,*args,**kwargs):
            if action=="repin": raise AccountError("fixture_before_target")
            return repin(action,*args,**kwargs)
        monkeypatch.setattr(host,"_repin",crash)
        try:
            with pytest.raises(VerbError): await runtime.operation(request)
            monkeypatch.setattr(host,"_repin",repin)
            await runtime.run(lambda a:set_pin(a,{"pin":dict(pin,revision=4),"expected_revision":3,"operation_id":"advance-after-start","reason":"fixture"}))
            replay=(await runtime.operation(request))["result"]
            assert replay=={"outcome":"settled","operation_id":request["operation_id"],"historical":True}
            assert len([v for v in native if "Reply exactly OK" in v])==1
            assert host.registry.active(old.provider,old.alias,old.host).generation=="successor"
            await assert_split(runtime,old)
        finally:store.stop()
    asyncio.run(run())


def test_reconcile_recovers_lost_original_host_reply_by_status_only(tmp_path,monkeypatch):
    from provider_cli_pin import set_pin
    async def run():
        j=await host_journey(tmp_path,monkeypatch)
        store,runtime,host,old,original,calls,native,logins,cell,pin=j
        execute=runtime.executor.execute
        async def lose(*args,**kwargs):
            result=await execute(*args,**kwargs)
            if args[1]=="repin":
                await runtime.run(lambda a:set_pin(a,{"pin":dict(pin,revision=3),"expected_revision":2,"operation_id":"lost-reply-pin","reason":"fixture"}))
                raise AccountError("account_host_unreachable")
            return result
        runtime.executor.execute=lose
        try:
            with pytest.raises(VerbError): await runtime.operation(original)
            runtime.executor.execute=execute
            request=dict(original,operation_id="recover-lost",new_generation="unused",reconcile_of=original["operation_id"],
                expected_pin_revision=3,expected_pin_digest=digest(dict(pin,revision=3)))
            result=(await runtime.operation(request))["result"]
            assert result["binding"]["generation"]=="successor"
            assert calls.count("repin")==1 and "repin-inspect" in calls
            assert len([v for v in native if "Reply exactly OK" in v])==1 and len(logins)==1
        finally:store.stop()
    asyncio.run(run())


def test_reconcile_unused_generation_must_be_unused_on_host(tmp_path,monkeypatch):
    async def run():
        j,request,pin=await split_journey(tmp_path,monkeypatch)
        store,runtime,host,old,original,calls,native,logins,cell,_=j
        try:
            with pytest.raises(VerbError,match="account_generation_exists"):
                await runtime.operation(dict(request,new_generation="successor"))
            await assert_split(runtime,old)
            assert len([v for v in native if "Reply exactly OK" in v])==1
        finally:store.stop()
    asyncio.run(run())



def test_reconcile_retains_original_consumer_proof_across_coordinator_update(tmp_path,monkeypatch):
    import provider_cli_artifact
    async def run():
        j,request,pin=await split_journey(tmp_path,monkeypatch)
        store,runtime,host,old,original,calls,native,logins,cell,_=j
        try:
            monkeypatch.setattr(provider_cli_artifact,"code_identity",lambda:"e"*64)
            result=(await runtime.operation(request))["result"]
            resumed=await runtime.choose_launch(host=old.host,provider=old.provider,model="fixture",effort="high",
                message={"resume_session_id":"old","_account_resume_binding":old.public()})
            assert resumed.generation==result["binding"]["generation"]=="successor"
            assert len([v for v in native if "Reply exactly OK" in v])==1
        finally:store.stop()
    asyncio.run(run())
