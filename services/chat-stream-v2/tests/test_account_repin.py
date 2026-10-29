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
def test_healthy_same_home_repin_is_a_distinct_receiver_journey(provider):
    async def run():
        store, runtime = await setup()
        old = replace(binding("repin", provider=provider), version="0.1.0", executable_sha256="c" * 64)
        new = replace(old, generation="repin-two", executable="/fixture/new-provider",
                      version=release_pair()["providers"][provider]["version"], executable_sha256="a" * 64)
        calls = []
        async def execute(account, action, operation_id, generation=None, *, pin=None):
            calls.append(action)
            if action == "observe":
                return {"outcome": "ok", "binding_key": new.key, "observation": observation(new)}
            return {"state": "done", "outcome": "ok", "provider": provider, "alias": old.alias,
                    "expected_generation": old.generation, "generation": new.generation,
                    "binding": new.public(), "observation": observation(new),
                    "proof": {"outcome":"ok", "binding_key":new.key,"platform":"darwin-arm64",
                              "artifact":pin["providers"][provider]["artifacts"]["darwin-arm64"],
                              "pin_revision":pin["revision"],"pin_digest":digest(pin),"code_identity":code_identity()}}
        runtime.executor = AccountExecutor(runtime, None, execute=execute)
        try:
            await runtime.run(lambda a: a.register(old))
            await runtime.run(lambda a: a.set_default(host=old.host, provider=provider, alias=old.alias))
            await store.set_spawn_admission_hold(old.host, held_until=NOW + 1000, reason="repin fixture")
            await seed_pin(runtime, release_pair())
            result = await runtime.operation({"action": "repin", "host": old.host, "provider": provider,
                "account": old.alias, "login_generation": old.generation, "new_generation": new.generation,
                "operation_id": "same-home-repin", "expected_pin_revision": 1,
                "expected_pin_digest": digest(release_pair()), "reason": "reviewed release", "_auth_context": AUTH})
            assert result["result"]["binding"]["home"] == old.home
            assert await runtime.run(lambda a: a.bound(old.key)) == old
            assert "enroll" not in calls
        finally:
            store.stop()
    asyncio.run(run())


async def host_journey(tmp_path,monkeypatch,provider="codex",platform="macos",*,adopted=False):
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
        return SimpleNamespace(returncode=0,stdout="" if argv[0]=="ps" else cell["version"] if argv[-1]=="--version" else "OK")
    host=NativeHost({"host":"host-a","registry":str(private/"registry.json"),"root":str(private/"homes"),
        "cells":{provider+":repin":cell}},run=run,login=login,observation=lambda b:dict(observation(b),provider_id="native-repin",pool_id="pool-repin"))
    enrolled=host.handle("adopt" if adopted else "enroll",provider=provider,alias="repin",expected_generation="unenrolled",operation_id="initial",generation="initial")
    assert enrolled["outcome"]=="ok"
    from provider_accounts import Binding
    old=host.registry.active(provider,"repin","host-a")
    store,runtime=await setup()
    await runtime.run(lambda a:a.register(old))
    await runtime.run(lambda a:a.set_default(host="host-a",provider=provider,alias="repin"))
    await store.set_spawn_admission_hold("host-a",held_until=NOW+1000,reason="repin fixture")
    pin=release_pair();pin["revision"]=2
    new_hash=hashlib.sha256(new_bin.read_bytes()).hexdigest()
    for artifact in pin["providers"][provider]["artifacts"].values():
        artifact.update(native_sha256=new_hash,resource_sha256=new_hash)
    from provider_cli_pin import set_pin
    await runtime.run(lambda a:set_pin(a,{"pin":pin,"expected_revision":1,"operation_id":"native-pair","reason":"fixture"}))
    cell.update(executable=str(new_bin),executable_sha256=new_hash,resource_root=str(new_bin),version=pin["providers"][provider]["version"])
    monkeypatch.setattr(provider_cli_artifact,"platform_key",lambda:"darwin-arm64" if platform=="macos" else "linux-x64")
    async def execute(account,action,op,generation=None,*,pin=None):
        calls.append(action)
        return host.handle(action,provider=account.provider,alias=account.alias,expected_generation=account.generation,
            operation_id=hashlib.sha256(op.encode()).hexdigest(),generation=generation,pin=pin)
    runtime.executor=AccountExecutor(runtime,None,execute=execute)
    request={"action":"repin","host":"host-a","provider":provider,"account":"repin","login_generation":old.generation,
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
                assert calls==["repin","repin-status"]
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


def test_required_catchup_after_host_success_then_pin_revision_changes(tmp_path,monkeypatch):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch)
        execute=runtime.executor.execute
        async def advance(*args,**kwargs):
            result=await execute(*args,**kwargs)
            if args[1]=="repin":
                from provider_cli_pin import set_pin
                await runtime.run(lambda a:set_pin(a,{"pin":dict(pin,revision=3),"expected_revision":2,
                    "operation_id":"benign-revision-advance","reason":"same exact reviewed pair"}))
            return result
        runtime.executor.execute=advance
        try:
            with pytest.raises(VerbError,match="provider_cli_pin_stale"):
                await runtime.operation(request)
            runtime.executor.execute=execute
            assert host.registry.active(old.provider,old.alias,old.host).generation=="successor"
            # The reviewed contract requires reconciliation, then newest-pin catch-up,
            # with no repeated native action and no direct pointer edits.
            latest=dict(request,operation_id="catch-up-current",expected_pin_revision=3,
                expected_pin_digest=digest(dict(pin,revision=3)),new_generation="current-successor")
            result=await runtime.operation(latest)
            assert result["result"]["outcome"] in {"repinned","no_op"}
        finally:store.stop()
    asyncio.run(run())
