"""Fleet release admission through the real durable account/launch boundary."""
import asyncio
import json

import pytest
import launch
from sessions import VerbError
from spawnctl import SpawnCtl
from test_account_runtime import setup, AUTH


from provider_cli_fixtures import release_pair,seed_pin
from dataclasses import replace
from test_account_rotation import binding


def test_wrong_fleet_release_refuses_real_launch_resolution(tmp_path):
    async def run():
        store, runtime = await setup(str(tmp_path / "state.db"))
        try:
            await seed_pin(runtime, release_pair())
            await runtime.run(lambda a:a.register(replace(binding("old"),version="0.1.0")))
            await runtime.run(lambda a:a.set_default(host="host-a",provider="codex",alias="old"))
            ctl = SpawnCtl(store, runtime.sessions, tmux=object(),
                           machine=launch.local_machine("host-a", cwd=str(tmp_path), codex_bin="/bin/echo"))
            ctl.account_runtime = runtime
            with pytest.raises(VerbError, match="provider_cli_pin_mismatch"):
                await ctl._resolve_launch({"provider": "codex", "model": "gpt-5.6-sol",
                                           "effort": "high", "_defer_launch_token_stage": True},
                                          "host-a", "wrong-version")
            assert await store.fetch_session("host-a", "wrong-version") is None
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("launch_request", [
    {"provider": "codex", "model": "gpt-5.6-sol", "effort": "high"},
    {"command": "/bin/echo bypass"},
])
def test_retained_enrollment_fences_policy_off(tmp_path, launch_request):
    async def run():
        store, runtime = await setup()
        try:
            runtime.policy["enabled"] = False
            ctl = SpawnCtl(store, runtime.sessions, tmux=object(),
                           machine=launch.local_machine("host-a", cwd=str(tmp_path), codex_bin="/bin/echo"))
            ctl.account_runtime = runtime
            expected = "managed_command_forbidden" if "command" in launch_request else "provider_cli_pin_unavailable"
            with pytest.raises(VerbError) as refused:
                await ctl._resolve_launch(dict(launch_request, _defer_launch_token_stage=True), "host-a", "off")
            assert refused.value.code == expected
        finally:
            store.stop()
    asyncio.run(run())


def test_authenticated_fd_can_initialize_pair_by_cas():
    async def run():
        store, runtime = await setup(initialize_pin=False)
        try:
            result = await runtime.operation({"action": "pin-set", "pin": release_pair(),
                "expected_revision": 0, "operation_id": "initialize-pair", "reason": "reviewed release",
                "_auth_context": AUTH})
            assert result["pin"]["revision"] == 1
            status = await runtime.operation({"action": "pin-status", "_auth_context": AUTH})
            assert status["pin"]["revision"] == 1
        finally:
            store.stop()
    asyncio.run(run())


def test_pin_cas_replay_conflict_and_restart(tmp_path):
    from copy import deepcopy
    async def run():
        path=str(tmp_path/"state.db")
        store,runtime=await setup(path)
        request={"action":"pin-set","pin":dict(release_pair(),revision=2),"expected_revision":1,
                 "operation_id":"advance","reason":"reviewed","_auth_context":AUTH}
        try:
            first=await runtime.operation(request)
            assert (await runtime.operation(request))["receipt"]==first["receipt"]
            with pytest.raises(VerbError,match="provider_cli_pin_id_conflict"):
                await runtime.operation(dict(request,reason="different"))
            with pytest.raises(VerbError,match="provider_cli_pin_stale"):
                await runtime.operation(dict(request,operation_id="stale"))
        finally: store.stop()
        store,runtime=await setup(path,initialize_pin=False)
        try:
            assert (await runtime.operation(request))["receipt"]==first["receipt"]
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("case",["range","alias","missing-provider","missing-platform","hash","extra","revision","duplicate"])
def test_invalid_release_snapshot(case):
    from copy import deepcopy
    from _shared.provider_cli_release import loads,validate
    pin=deepcopy(release_pair())
    if case=="range": pin["providers"]["codex"]["version"]="^0.160.0"
    if case=="alias": pin["providers"]["claude"]["version"]="latest"
    if case=="missing-provider": del pin["providers"]["codex"]
    if case=="missing-platform": del pin["providers"]["codex"]["artifacts"]["linux-x64"]
    if case=="hash": pin["providers"]["codex"]["artifacts"]["linux-x64"]["resource_sha256"]=""
    if case=="extra": pin["hosts"]={"host-a":{}}
    if case=="revision": pin["revision"]=True
    if case=="duplicate":
        with pytest.raises(ValueError): loads('{"schema":1,"schema":1}')
    else:
        with pytest.raises(ValueError): validate(pin)


@pytest.mark.parametrize("auth",[{}, {"token_verified":True,"role":"front-desk"},
    {"peer_loopback":True}, {"local_admin_verified":True},
    {"operator_authenticated":True,"dot_principal":True}])
def test_recovery_refuses_unprivileged_principals(auth):
    async def run():
        store,runtime=await setup()
        runtime.server.assistant_composite=None
        try:
            with pytest.raises(VerbError,match="account_control_unauthorized"):
                await runtime.operation({"action":"pin-status","_auth_context":auth})
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("damage",["corrupt","absent","valid-but-unreceipted"])
def test_admin_dead_fd_recovery_uses_raw_and_high_water_cas(damage):
    import hashlib
    async def run():
        store,runtime=await setup()
        admin={"peer_loopback":True,"local_admin_verified":True}
        try:
            # A mismatching approved target fences this default cell.
            pin=dict(release_pair(),revision=2)
            pin["providers"]["codex"]["version"]="0.161.0"
            await runtime.operation({"action":"pin-set","pin":pin,"expected_revision":1,
                "operation_id":"mismatch","reason":"fixture","_auth_context":AUTH})
            runtime.server.assistant_composite=None
            def damage_record(a):
                if damage=="absent": a.conn.execute("DELETE FROM kv WHERE k=?",("provider_cli.release_pin",))
                else: a.conn.execute("UPDATE kv SET v=? WHERE k=?",("broken{" if damage=="corrupt" else json.dumps(dict(pin,revision=9)),"provider_cli.release_pin"))
                a.conn.commit()
            await runtime.run(damage_record)
            status=await runtime.operation({"action":"pin-status","_auth_context":admin,"local_admin_token":"fixture-not-a-secret"})
            assert status["pin"] is None and status["high_water_revision"]==2
            request={"action":"pin-set","recover_receipt":"fixture-pin-1","expected_raw_digest":status["raw_digest"],
                "expected_high_water_revision":2,"operation_id":"recover","reason":"restore FD approved pair","_auth_context":admin}
            with pytest.raises(VerbError,match="provider_cli_pin_stale"):
                await runtime.operation(dict(request,expected_high_water_revision=1))
            result=await runtime.operation(request)
            assert result["pin"]["revision"]==3
            restored=await runtime.choose_launch(host="host-a",provider="codex",model="fixture",effort="high",message={})
            assert restored.alias=="primary"
            assert any(c["reason"]=="artifact_verification_pending" for c in result["cells"])
        finally: store.stop()
    asyncio.run(run())


def test_pin_commit_failure_is_atomic():
    import sqlite3
    async def run():
        store,runtime=await setup(initialize_pin=False)
        def trigger(a):
            a.conn.execute("CREATE TRIGGER stop_pin BEFORE INSERT ON kv WHEN NEW.k='provider_cli.release_pin' BEGIN SELECT RAISE(ABORT,'fixture-crash'); END")
            a.conn.commit()
        try:
            await runtime.run(trigger)
            with pytest.raises(sqlite3.IntegrityError):
                await runtime.operation({"action":"pin-set","pin":release_pair(),"expected_revision":0,
                    "operation_id":"crash","reason":"fixture","_auth_context":AUTH})
            assert await runtime.run(lambda a:a.conn.execute("SELECT count(*) FROM v2_account_recovery WHERE binding_key=?",("provider-cli-pin:crash",)).fetchone()[0])==0
            status=await runtime.operation({"action":"pin-status","_auth_context":AUTH})
            assert status["pin"] is None and not status["receipt_ids"]
        finally: store.stop()
    asyncio.run(run())


async def artifact_executor(runtime,*,prepare=None):
    from account_executor import AccountExecutor
    from provider_cli_artifact import code_identity
    from provider_cli_pin import digest
    async def execute(account,action,operation_id,generation=None,*,pin=None):
        assert action=="verify-artifact"
        if prepare: await prepare()
        return {"outcome":"ok","binding_key":account.key,"platform":"darwin-arm64",
                "artifact":pin["providers"][account.provider]["artifacts"]["darwin-arm64"],
                "pin_revision":pin["revision"],"pin_digest":digest(pin),"code_identity":code_identity()}
    runtime.executor=AccountExecutor(runtime,None,execute=execute)


def test_final_admission_rechecks_pin_after_async_host_preparation():
    async def run():
        store,runtime=await setup()
        async def race():
            await runtime.operation({"action":"pin-set","pin":dict(release_pair(),revision=2),"expected_revision":1,
                "operation_id":"race","reason":"fixture","_auth_context":AUTH})
        await artifact_executor(runtime,prepare=race)
        try:
            with pytest.raises(Exception,match="provider_cli_pin_mismatch"):
                await runtime.admit_launch({"stream_id":"host-a:refused","session_generation":"one","account_binding":binding().public()})
            assert await runtime.run(lambda a:a.conn.execute("SELECT count(*) FROM v2_account_session_bindings WHERE stream_id=?",("host-a:refused",)).fetchone()[0])==0
            assert await store.fetch_session("host-a","refused") is None
        finally: store.stop()
    asyncio.run(run())


def test_committed_old_admission_is_enumerated_and_new_work_is_fenced():
    async def run():
        store,runtime=await setup()
        await artifact_executor(runtime)
        row={"stream_id":"host-a:late","session_generation":"one","account_binding":binding().public()}
        try:
            await runtime.admit_launch(row)
            pin=dict(release_pair(),revision=2)
            pin["providers"]["codex"]["version"]="0.161.0"
            await runtime.operation({"action":"pin-set","pin":pin,"expected_revision":1,
                "operation_id":"after-admission","reason":"fixture","_auth_context":AUTH})
            # Remote creation after its older durable admission is historical, not new-pin success.
            await store.open_session("host-a","late",session_generation="one",provider="codex",account_binding=row["account_binding"])
            status=await runtime.operation({"action":"pin-status","_auth_context":AUTH})
            assert any(s["stream_id"]==row["stream_id"] for c in status["cells"] for s in c["old_seats"])
            with pytest.raises(Exception,match="provider_cli_pin_mismatch"): await runtime.before_input(row)
            assert (await store.fetch_session("host-a","late"))["account_binding"]==row["account_binding"]
            assert not status["fleet_complete"]
        finally: store.stop()
    asyncio.run(run())


def test_existing_compliant_seat_gets_readonly_proof_without_relabeling_admission():
    async def run():
        store,runtime=await setup()
        await artifact_executor(runtime)
        row=await store.open_session("host-a","existing",session_generation="before-enforcement",provider="codex",account_binding=binding().public())
        try:
            await runtime.before_input(row)
            assert await runtime.run(lambda a:a.conn.execute("SELECT v FROM kv WHERE k=?",("provider_cli.admission:host-a:existing:before-enforcement",)).fetchone()) is None
            assert await runtime.run(lambda a:a.conn.execute("SELECT v FROM kv WHERE k=?",("provider_cli.observed:host-a:existing:before-enforcement",)).fetchone()) is not None
            assert (await store.fetch_session("host-a","existing"))["session_generation"]=="before-enforcement"
        finally: store.stop()
    asyncio.run(run())


def test_inactive_retained_cells_stay_in_status_and_raw_command_fence(tmp_path):
    async def run():
        store,runtime=await setup()
        try:
            def park(a):
                a.conn.execute("UPDATE v2_account_bindings SET active=0 WHERE host=?",("host-a",));a.conn.commit()
            await runtime.run(park)
            runtime.policy["enabled"]=False
            status=await runtime.operation({"action":"pin-status","_auth_context":AUTH})
            assert len(status["cells"])==2 and all(c["reason"]=="no_active_binding" for c in status["cells"])
            ctl=SpawnCtl(store,runtime.sessions,tmux=object(),machine=launch.local_machine("host-a",cwd=str(tmp_path),codex_bin="/bin/echo"))
            ctl.account_runtime=runtime
            with pytest.raises(VerbError) as refused: await ctl._resolve_launch({"command":"true"},"host-a","refused")
            assert refused.value.code=="managed_command_forbidden"
        finally: store.stop()
    asyncio.run(run())


def test_real_remote_creation_after_pin_move_keeps_old_admission(tmp_path):
    from test_spawn_cancel_fence_holes import ReconTmux
    from provider_cli_pin import canonical
    from test_account_rotation import NOW
    async def run():
        store,runtime=await setup()
        await artifact_executor(runtime)
        remote=binding("primary",host="host-b",provider="claude")
        await runtime.run(lambda a:a.register(remote))
        await runtime.run(lambda a:a.set_default(host="host-b",provider="claude",alias="primary"))
        def satellite(a):
            for k,v in (("event_push.target_sha","a"*40),("event_push.runtime.host-b",canonical({"sha":"a"*40,"pid":123,
                "incarnation":"fixture","admitted":True,"observed_at_epoch":NOW}))):
                a.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)",(k,v))
            a.conn.commit()
        await runtime.run(satellite)
        class Remote(ReconTmux):
            async def new_session(self,name,command,cwd=None,env=None):
                next_pin=release_pair();next_pin["revision"]=2
                next_pin["providers"]["claude"]["version"]="2.1.313"
                moved=await runtime.operation({"action":"pin-set","pin":next_pin,"expected_revision":1,
                    "operation_id":"remote-race","reason":"fixture","_auth_context":AUTH})
                assert any(s["stream_id"]=="host-b:remote-late" and s["creation_pending"]
                    for c in moved["cells"] for s in c["old_seats"])
                await super().new_session(name,command,cwd=cwd,env=env)
        tmux=Remote()
        ctl=SpawnCtl(store,runtime.sessions,tmux=tmux)
        ctl.account_runtime=runtime
        fields={"provider":"claude","session_generation":"remote-generation","account_binding":remote.public()}
        try:
            await store.reserve_stream_id("host-b","remote-late",ttl_s=600,request_id="remote-request",nonce="native-nonce")
            await store.record_spawn_intent("host-b","remote-late",{"open_fields":fields,"brief":"",
                "delivery_receipt":{"state":"not_requested"}},request_id="remote-request",nonce="native-nonce")
            created=[False]
            result=await ctl._spawn_fenced("host-b","remote-late","remote-request","managed-native","","READY",
                {"ready_marker":"READY"},created,[False],fields,{"resolved_launch_tuple":{"provider":"claude"}},
                tmux,{"state":"not_requested"},"native-nonce")
            assert result["type"]=="spawn.ok" and created[0] and tmux.created==1
            receipt=await runtime.run(lambda a:a.conn.execute("SELECT v FROM kv WHERE k=?",
                ("provider_cli.admission:host-b:remote-late:remote-generation",)).fetchone())
            assert json.loads(receipt[0])["revision"]==1
            row=await store.fetch_session("host-b","remote-late")
            with pytest.raises(Exception,match="provider_cli_pin_mismatch"): await runtime.before_input(row)
            status=await runtime.operation({"action":"pin-status","_auth_context":AUTH})
            assert any(seat["stream_id"]=="host-b:remote-late" for cell in status["cells"] for seat in cell["old_seats"])
        finally:
            await tmux.kill_session("remote-late")
            store.stop()
    asyncio.run(run())


def test_dead_fd_admin_recovery_through_real_authenticated_socket(tmp_path,monkeypatch):
    import local_admin
    from account_runtime import AccountRuntime
    from types import SimpleNamespace
    from test_loopback_privileged_rpc import scratch_daemon,rpc
    async def run():
        async with scratch_daemon(tmp_path,monkeypatch) as (daemon,store,ctl,token_path):
            runtime=AccountRuntime(store,daemon.sessions,policy={"enabled":True})
            runtime.attach(daemon,ctl,SimpleNamespace())
            account=binding(host="fixture-host")
            await runtime.run(lambda a:a.register(account))
            await runtime.run(lambda a:a.set_default(host=account.host,provider=account.provider,alias=account.alias))
            first=await rpc(daemon,"account",action="pin-set",pin=release_pair(),expected_revision=0,
                operation_id="initialize",reason="reviewed fixture",local_admin_token=local_admin.read(token_path))
            assert first["type"]=="account.ok"
            def corrupt(a):
                a.conn.execute("UPDATE kv SET v=? WHERE k=?",("invalid-primary{","provider_cli.release_pin"));a.conn.commit()
            await runtime.run(corrupt)
            spoofed=await rpc(daemon,"account",action="pin-status",_auth_context={"operator_authenticated":True,"local_admin_verified":True,"peer_loopback":True})
            assert spoofed["type"]=="account.error" and spoofed["error_code"]=="account_control_unauthorized"
            status=await rpc(daemon,"account",action="pin-status",local_admin_token=local_admin.read(token_path))
            assert status["pin"] is None and status["raw_digest"]!="absent"
            result=await rpc(daemon,"account",action="pin-set",recover_receipt="initialize",expected_raw_digest=status["raw_digest"],
                expected_high_water_revision=1,operation_id="recover",reason="approved FD pair",local_admin_token=local_admin.read(token_path))
            assert result["type"]=="account.ok" and result["pin"]["revision"]==2
            assert "invalid-primary" not in json.dumps(result)
            chosen=await runtime.choose_launch(host=account.host,provider=account.provider,model="fixture",effort="high",message={})
            assert chosen.key==account.key
            unrelated=await rpc(daemon,"account",action="default-set",host=account.host,provider=account.provider,account=account.alias,
                local_admin_token=local_admin.read(token_path))
            assert unrelated["type"]=="account.error" and unrelated["error_code"]=="account_control_unauthorized"
    asyncio.run(run())


def test_full_resource_tree_verification_refuses_support_file_change(tmp_path):
    import hashlib
    from types import SimpleNamespace
    from provider_cli_artifact import resource_digest,verify
    package=tmp_path/"release";package.mkdir()
    native=package/"codex";native.write_text("fake native")
    support=package/"support";support.write_text("fake support")
    pin=release_pair()
    entry=pin["providers"]["codex"]["artifacts"]["darwin-arm64"]
    entry.update(native_sha256=hashlib.sha256(native.read_bytes()).hexdigest(),resource_sha256=resource_digest(package))
    account=replace(binding(),executable=str(native),executable_sha256=entry["native_sha256"])
    cell={"executable":str(native),"resource_root":str(package)}
    run=lambda *a,**k:SimpleNamespace(returncode=0,stdout=account.version)
    proof=verify(account,cell,pin,run=run,native_platform="darwin-arm64")
    assert proof["artifact"]["resource_sha256"]==resource_digest(package)
    support.write_text("changed support")
    from provider_accounts import AccountError
    with pytest.raises(AccountError,match="provider_cli_pin_mismatch"):
        verify(account,cell,pin,run=run,native_platform="darwin-arm64")
