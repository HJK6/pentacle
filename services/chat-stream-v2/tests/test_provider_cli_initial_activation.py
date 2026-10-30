"""Initial activation must preserve service until every serving cell is ready."""
import asyncio
import copy
import json

import pytest

from sessions import VerbError
from test_account_rotation import binding
from test_account_runtime import AUTH, setup
from test_account_repin import host_journey
from provider_cli_fixtures import release_pair
from provider_cli_pin import digest


async def ready_fixture(tmp_path, monkeypatch, *, serving_seat=True,provider="codex",platform="macos"):
    from account_executor import AccountExecutor
    from account_runtime import AccountRuntime
    from sessions import Sessions
    from store import Store
    from test_account_rotation import NOW, observation
    from types import SimpleNamespace
    import os
    old_store,old_runtime,host,old,request,calls,native,logins,cell,pin = await host_journey(tmp_path,monkeypatch,provider,platform)
    try:
        await old_runtime.operation(request)
        selected=host.registry.active(old.provider,old.alias,old.host)
        execute=old_runtime.executor.execute
    finally:
        old_store.stop()
    store=Store(str(tmp_path/"ready-state.db"));store.start()
    runtime=AccountRuntime(store,Sessions(store,local_host="host-a"),policy={"enabled":True},clock=lambda:NOW)
    runtime.attach(SimpleNamespace(assistant_composite=None,handlers={}),SimpleNamespace(_background_spawns=set()),SimpleNamespace())
    runtime.executor=AccountExecutor(runtime,None,execute=execute)
    await runtime.run(lambda a:a.register(selected))
    await runtime.run(lambda a:a.set_default(host=selected.host,provider=selected.provider,alias=selected.alias))
    await runtime.run(lambda a:a.observe(selected.key,dict(observation(selected),provider_id=selected.provider_id,pool_id=selected.pool_id),now=NOW))
    target=copy.deepcopy(pin);target["revision"]=1
    row=None
    if serving_seat:
        started="Thu Oct 29 10:00:00 2026"
        row=await store.open_session("host-a","current-ready",session_generation="current-generation",provider=provider,
            pane_pid="42",bootstrap_state="ready",account_binding=selected.public(),observer_binding={
                "pane_pid":"42","pane_started_at":started,"generation":"current-generation","executable":selected.executable})
        actual_run=host.run
        def current(argv,**kwargs):
            if argv[0]=="ps":
                if "pid=,comm=" in argv:
                    return SimpleNamespace(returncode=0,stdout="42 "+selected.executable+"\n")
                return SimpleNamespace(returncode=0,stdout=f"42 1 {os.getuid()} 42 42 {started} {selected.executable}\n")
            if argv[0]=="tmux":
                return SimpleNamespace(returncode=0,stdout="42|%42|/dev/ttys042|/fixture/tmux.sock|current-ready\n")
            if argv[0] in {"cat","sysctl"}:
                return SimpleNamespace(returncode=0,stdout="fixture-boot\n")
            return actual_run(argv,**kwargs)
        host.run=current
        await runtime.admit_launch(row)
    return store,runtime,host,selected,target,row,native


@pytest.mark.parametrize("provider,platform",[("codex","macos"),("codex","wsl"),("claude","macos"),("claude","wsl")])
def test_first_pin_cas_preserves_a_proved_serving_seat_and_replays(tmp_path,monkeypatch,provider,platform):
    from types import SimpleNamespace
    async def run():
        store,runtime,host,selected,target,row,native=await ready_fixture(tmp_path,monkeypatch,provider=provider,platform=platform)
        try:
            fd={"token_verified":True,"stream_id":row["stream_id"],"session_generation":row["session_generation"]}
            runtime.server.assistant_composite=SimpleNamespace(enabled=True,
                env_config=SimpleNamespace(direct_primary=True,name="front-desk"),
                _env_binding=lambda:{"stream_id":fd["stream_id"],"generation":fd["session_generation"]})
            status=await runtime.operation({"action":"pin-status","pin":target,"_auth_context":fd})
            assert status.get("initial_readiness",{}).get("missing") == []
            request={"action":"pin-set","pin":target,"expected_revision":0,"operation_id":"ready-first-pair",
                "reason":"all serving cells ready","expected_readiness_digest":status["initial_readiness"]["digest"],
                "_auth_context":fd}
            result=await runtime.operation(request)
            assert result["pin"]==target and result["fleet_complete"]
            assert (await runtime.operation(request))["receipt"]==result["receipt"]
            async with runtime.input_guard(row):
                pass
            assert (await store.fetch_session("host-a","current-ready"))["session_generation"]==row["session_generation"]
            assert (await runtime.choose_launch(host="host-a",provider=provider,model="fixture",effort="high",message={})).key==selected.key
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("missing",["health","clearance","offline","artifact","consumer","seat-binding","stale-seat",
    "parked-seat","bootstrap","routing","pending-send","pending-tell","late-reservation","generation-intent","closed-parent"])
def test_first_pin_refuses_each_missing_readiness_predicate_without_stranding_pristine_service(tmp_path,monkeypatch,missing):
    from provider_accounts import AccountError
    from dataclasses import replace
    from test_account_rotation import NOW
    from pathlib import Path
    async def run():
        store,runtime,host,selected,target,row,native=await ready_fixture(tmp_path,monkeypatch)
        request={"action":"pin-set","pin":target,"expected_revision":0,"operation_id":"missing-"+missing,
            "reason":"fixture boundary","_auth_context":{"peer_loopback":True,"local_admin_verified":True}}
        try:
            if missing=="health":await runtime.record_failure(selected,"auth")
            elif missing=="clearance":host.config.pop("mutation_clearance")
            elif missing=="offline":
                async def offline(*args,**kwargs):raise AccountError("account_host_unreachable")
                runtime.executor.execute=offline
            elif missing=="artifact":Path(selected.executable).write_text("tampered fixture native")
            elif missing=="consumer":runtime.sessions.local_host="different-central-host"
            elif missing=="seat-binding":
                await store.open_session(selected.host,"unknown-binding-seat",provider=selected.provider,session_generation="unknown-binding")
            elif missing=="stale-seat":
                await store.open_session(selected.host,"stale-binding-seat",provider=selected.provider,
                    session_generation="stale-binding",account_binding=replace(selected,generation="stale").public())
            elif missing=="bootstrap":await store.update_session(selected.host,"current-ready",bootstrap_state="starting")
            elif missing=="routing":await store.update_session(selected.host,"current-ready",routing_integrity="mismatch")
            elif missing=="parked-seat":request["initial_parked_cells"]=[{"host":selected.host,"provider":selected.provider,
                "account":selected.alias,"generation":selected.generation,"reason":"explicit park","decision_ref":"fixture"}]
            elif missing=="pending-send":
                await store.append_send_receipt(to_stream_id=row["stream_id"],request_id="pending",receipt_id="pending-receipt",
                    state="accepted",wire_text="fixture",display_text="fixture",attachments=[],delivery="committed_pending_proof",submission_confirmed=False)
            elif missing=="pending-tell":
                await store.put_tell_delivery("pending-tell",{"reply":{"delivery_status":"pasted_unsubmitted"},
                    "delivery":{"to_stream_id":row["stream_id"],"delivery_status":"pasted_unsubmitted"}})
            elif missing=="late-reservation":await store.reserve_stream_id(selected.host,"late",ttl_s=600,refuse_if_held=False)
            elif missing=="generation-intent":
                from account_repin_state import reserve_generation
                await runtime.run(lambda a:reserve_generation(a,selected,"pending-intent","unused-generation","enroll",now=NOW))
            elif missing=="closed-parent":
                await store.open_session(selected.host,"old-parent",provider=selected.provider,account_binding=selected.public())
                await store.open_session(selected.host,"custody-child",provider="shell",parent_stream_id=selected.host+":old-parent")
                await store.update_session(selected.host,"old-parent",status="closed")
            with pytest.raises(VerbError) as refused:await runtime.operation(request)
            assert refused.value.code=="provider_cli_initial_readiness_required"
            from provider_cli_pin import pristine
            assert await runtime.run(pristine)
            assert await store.reserve_stream_id(selected.host,"after-refusal",ttl_s=60,refuse_if_held=False)
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("race",["health","default","population","policy","seat","hold","late-reservation","proof-expiry","pause-epoch"])
def test_initial_final_transaction_rechecks_the_derived_snapshot_after_remote_proof(tmp_path,monkeypatch,race):
    from test_account_rotation import NOW
    from dataclasses import replace
    async def run():
        store,runtime,host,selected,target,row,native=await ready_fixture(tmp_path,monkeypatch)
        verify=runtime.executor.verify_artifact
        async def raced(account,pin):
            proof=await verify(account,pin)
            if race=="health":await runtime.record_failure(selected,"network")
            elif race=="default":
                await runtime.run(lambda a:a.conn.execute("UPDATE v2_account_defaults SET revision=revision+1 WHERE host=?",(selected.host,)))
                await runtime.run(lambda a:a.conn.commit())
            elif race=="population":await runtime.run(lambda a:a.register(replace(selected,alias="new-cell",generation="new-cell")))
            elif race=="policy":runtime.policy["enabled"]=False
            elif race=="seat":await store.update_session(selected.host,"current-ready",status="closed")
            elif race=="hold":await store.set_spawn_admission_hold(selected.host,held_until=NOW+1000,reason="concurrent holder")
            elif race=="late-reservation":
                assert not await store.reserve_stream_id(selected.host,"new-during-pause",ttl_s=60,refuse_if_held=False)
                # Model a durable already-dispatched pre-pause request arriving
                # from an old process; it cannot be made absent by a timeout.
                await store.submit(lambda conn:conn.execute("INSERT INTO v2_stream_reservations(host,session_name,expires_at) VALUES (?,?,?)",
                    (selected.host,"late-old-dispatch",NOW+1000)))
                await store.submit(lambda conn:conn.commit())
            elif race=="proof-expiry":runtime.clock=lambda:NOW+121
            elif race=="pause-epoch":
                await runtime.run(lambda a:a.conn.execute("UPDATE kv SET v=? WHERE k=?",
                    (json.dumps({"operation_id":"new-owner","owner_epoch":"new-epoch"}),"provider_cli.initial_admission_pause")))
                await runtime.run(lambda a:a.conn.commit())
            return proof
        runtime.executor.verify_artifact=raced
        try:
            with pytest.raises(VerbError) as refused:
                await runtime.operation({"action":"pin-set","pin":target,"expected_revision":0,"operation_id":"racing-first",
                    "reason":"fixture","_auth_context":{"operator_authenticated":True}})
            assert refused.value.code=="provider_cli_initial_readiness_required"
            assert (await runtime.operation({"action":"pin-status","_auth_context":{"operator_authenticated":True}}))["pin"] is None
        finally:store.stop()
    asyncio.run(run())


def test_first_cas_waits_for_already_dispatched_creation_without_locking_existing_input(tmp_path,monkeypatch):
    from test_spawn_cancel_fence_holes import NewSessionGatedTmux
    from provider_cli_initial import PAUSE
    async def run():
        store,runtime,host,selected,target,row,native=await ready_fixture(tmp_path,monkeypatch)
        from spawnctl import SpawnCtl
        tmux=NewSessionGatedTmux()
        ctl=SpawnCtl(store,runtime.sessions,tmux=tmux)
        ctl.account_runtime=runtime;runtime.spawnctl=ctl
        fields={"provider":selected.provider,"session_generation":"already-dispatched-generation",
            "account_binding":selected.public(),"bootstrap_state":"ready"}
        await store.reserve_stream_id(selected.host,"already-dispatched",ttl_s=600,request_id="old-request",nonce="old-nonce")
        await store.record_spawn_intent(selected.host,"already-dispatched",{"open_fields":fields,"brief":"",
            "delivery_receipt":{"state":"not_requested"}},request_id="old-request",nonce="old-nonce")
        created=[False]
        spawn=asyncio.create_task(ctl._spawn_fenced(selected.host,"already-dispatched","old-request","managed-native","","READY",
            {"ready_marker":"READY"},created,[False],fields,{"resolved_launch_tuple":{"provider":selected.provider}},
            tmux,{"state":"not_requested"},"old-nonce"))
        ctl._background_spawns.add(spawn)
        promotion=None
        try:
            await asyncio.wait_for(tmux.entered.wait(),2)
            promotion=asyncio.create_task(runtime.operation({"action":"pin-set","pin":target,"expected_revision":0,
                "operation_id":"promotion-racing-ssh","reason":"fixture","_auth_context":{"operator_authenticated":True}}))
            for _ in range(100):
                paused=await runtime.run(lambda a:a.conn.execute("SELECT 1 FROM kv WHERE k=?",(PAUSE,)).fetchone() is not None)
                if paused:break
                await asyncio.sleep(0)
            assert paused and not promotion.done() and not runtime.pin_lock.locked()
            assert not await store.reserve_stream_id(selected.host,"new-pause-request",ttl_s=60,refuse_if_held=False)
            async with runtime.input_guard(row):pass
            tmux.release.set()
            assert (await spawn)["type"]=="spawn.ok" and created[0] and tmux.created==1
            # The late creation is retained. Its missing launcher/reply proof
            # blocks promotion, rather than cancelling it or assuming absence.
            with pytest.raises(VerbError,match="provider_cli_initial_readiness_required"):await promotion
            assert (await store.fetch_session(selected.host,"already-dispatched"))["status"]=="open"
            assert (await runtime.operation({"action":"pin-status","_auth_context":{"operator_authenticated":True}}))["pin"] is None
        finally:
            tmux.release.set()
            await spawn
            if promotion and not promotion.done():
                try:await promotion
                except VerbError:pass
            ctl._background_spawns.discard(spawn)
            await tmux.kill_session("already-dispatched")
            store.stop()
    asyncio.run(run())


def test_first_pin_database_failure_rolls_back_readiness_observations_together(tmp_path,monkeypatch):
    import sqlite3
    admin={"peer_loopback":True,"local_admin_verified":True}
    async def run():
        store,runtime,host,selected,target,row,native=await ready_fixture(tmp_path,monkeypatch)
        try:
            def fail(a):
                a.conn.execute("CREATE TRIGGER reject_initial_pin BEFORE INSERT ON kv "
                    "WHEN NEW.k='provider_cli.release_pin' BEGIN SELECT RAISE(ABORT,'fixture-only'); END")
                a.conn.commit()
            await runtime.run(fail)
            with pytest.raises(sqlite3.IntegrityError):
                await runtime.operation({"action":"pin-set","pin":target,"expected_revision":0,
                    "operation_id":"atomic-first","reason":"fixture","_auth_context":admin})
            def remaining(a):
                return [r[0] for r in a.conn.execute("SELECT k FROM kv WHERE k IN (?,?,?,?)",
                    ("provider_cli.release_pin","provider_cli.pin_initialized",
                     "provider_cli.initial_admission_pause","provider_cli.observed:host-a:current-ready:current-generation"))]
            assert await runtime.run(remaining)==[]
            assert (await runtime.operation({"action":"pin-status","_auth_context":admin}))["receipt_ids"]==[]
            async with runtime.input_guard(row):
                pass
        finally:
            store.stop()
    asyncio.run(run())


def test_owned_restart_aborts_old_promotion_pause_without_forgetting_late_creation():
    async def run():
        store,runtime=await setup(initialize_pin=False)
        try:
            assert await store.reserve_stream_id("host-a","old-in-flight",ttl_s=600,request_id="old-request")
            await runtime.run(lambda a:a.conn.execute("INSERT INTO kv VALUES (?,?)",
                ("provider_cli.initial_admission_pause",json.dumps({"operation_id":"old-promotion","owner_epoch":"old-epoch"}))))
            # Store.run autocommit is not a mutation authority; commit this fixture.
            await runtime.run(lambda a:a.conn.commit())
            assert not await store.reserve_stream_id("host-a","paused-new",ttl_s=600)
            await runtime.start()
            assert await store.reserve_stream_id("host-a","resumed-new",ttl_s=600)
            with pytest.raises(VerbError,match="provider_cli_initial_readiness_required"):
                await runtime.operation({"action":"pin-set","pin":release_pair(),"expected_revision":0,
                    "operation_id":"after-restart","reason":"fixture","_auth_context":AUTH})
            assert (await runtime.choose_launch(host="host-a",provider="codex",model="fixture",effort="high",message={})).key==binding().key
        finally:
            store.stop()
    asyncio.run(run())


def test_explicitly_parked_matching_cell_stays_unavailable_until_authorized_catchup(tmp_path,monkeypatch):
    from test_account_rotation import NOW
    from types import SimpleNamespace
    from provider_accounts import AccountError
    admin={"peer_loopback":True,"local_admin_verified":True}
    async def run():
        store,runtime,host,selected,target,row,native=await ready_fixture(tmp_path,monkeypatch,serving_seat=False)
        try:
            parked={"host":selected.host,"provider":selected.provider,"account":selected.alias,
                "generation":selected.generation,"reason":"operator chose non-serving","decision_ref":"fixture-explicit-park"}
            result=await runtime.operation({"action":"pin-set","pin":target,"expected_revision":0,
                "operation_id":"initial-explicit-park","reason":"reviewed capacity choice",
                "initial_parked_cells":[parked],"_auth_context":admin})
            assert result["pin"]==target
            with pytest.raises(AccountError,match="provider_cli_cell_parked"):
                await runtime.choose_launch(host=selected.host,provider=selected.provider,model="fixture",effort="high",message={})
            # Actual FD account authorization, a real hold, host receipt and
            # a verified no-op lift parking; no unused generation is created.
            await store.open_session("host-a","front-desk",provider="claude",session_generation=AUTH["session_generation"])
            runtime.server.assistant_composite=SimpleNamespace(enabled=True,
                env_config=SimpleNamespace(direct_primary=True,name="front-desk"),
                _env_binding=lambda:{"stream_id":AUTH["stream_id"],"generation":AUTH["session_generation"]})
            await store.set_spawn_admission_hold(selected.host,held_until=NOW+1000,reason="explicit catchup")
            count=len(host.registry.bindings())
            caught=await runtime.operation({"action":"repin","host":selected.host,"provider":selected.provider,
                "account":selected.alias,"login_generation":selected.generation,"new_generation":"unused-after-no-op",
                "operation_id":"authorized-catchup","reason":"operator authorized catchup",
                "expected_pin_revision":1,"expected_pin_digest":digest(target),"_auth_context":AUTH})
            assert caught["result"]["outcome"]=="no_op"
            assert len(host.registry.bindings())==count
            await store.clear_spawn_admission_hold(selected.host)
            assert (await runtime.choose_launch(host=selected.host,provider=selected.provider,model="fixture",effort="high",message={})).key==selected.key
        finally:
            store.stop()
    asyncio.run(run())


def test_pristine_existing_input_is_usable_without_a_new_launch():
    async def run():
        store, runtime = await setup(initialize_pin=False)
        try:
            row = await store.open_session("host-a", "existing-before-enforcement",
                session_generation="existing-generation", provider="codex",
                account_binding=binding().public())
            await runtime.before_input(row)
            async with runtime.input_guard(row):
                pass
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("damage", ["marker-only", "history-only", "bad-history", "bad-pin"])
def test_non_pristine_absence_or_corruption_never_becomes_pre_enforcement(damage):
    from provider_accounts import AccountError
    async def run():
        store, runtime = await setup(initialize_pin=False)
        try:
            def corrupt(a):
                if damage == "marker-only":
                    a.conn.execute("INSERT INTO kv VALUES (?,?)", ("provider_cli.pin_initialized", "1"))
                elif damage == "bad-pin":
                    a.conn.execute("INSERT INTO kv VALUES (?,?)", ("provider_cli.release_pin", "broken{"))
                else:
                    from provider_cli_pin import set_pin
                    set_pin(a, {"pin":release_pair(), "expected_revision":0,
                        "operation_id":"fixture-partial-history", "reason":"fixture only"})
                    a.conn.execute("DELETE FROM kv WHERE k IN (?,?)",
                        ("provider_cli.release_pin", "provider_cli.pin_initialized"))
                    if damage == "bad-history":
                        a.conn.execute("UPDATE v2_account_recovery SET data=? WHERE binding_key=?",
                            ("broken{", "provider-cli-pin:fixture-partial-history"))
                a.conn.commit()
            await runtime.run(corrupt)
            with pytest.raises(AccountError):
                await runtime.choose_launch(host="host-a", provider="codex",
                    model="fixture", effort="high", message={})
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("marker",["absent","valid","corrupt"])
@pytest.mark.parametrize("history",["absent","valid","corrupt"])
@pytest.mark.parametrize("pin",["absent","valid","corrupt"])
def test_pristine_is_exactly_the_empty_pin_marker_history_state(marker,history,pin):
    from provider_accounts import AccountError
    async def run():
        store,runtime=await setup()
        try:
            def mutate(a):
                if marker=="absent":a.conn.execute("DELETE FROM kv WHERE k=?",("provider_cli.pin_initialized",))
                elif marker=="corrupt":a.conn.execute("UPDATE kv SET v=? WHERE k=?",("broken-marker","provider_cli.pin_initialized"))
                prefix="provider-cli-pin:"
                if history=="absent":a.conn.execute("DELETE FROM v2_account_recovery WHERE substr(binding_key,1,?)=?",(len(prefix),prefix))
                elif history=="corrupt":a.conn.execute("UPDATE v2_account_recovery SET data=? WHERE substr(binding_key,1,?)=?",("broken{",len(prefix),prefix))
                if pin=="absent":a.conn.execute("DELETE FROM kv WHERE k=?",("provider_cli.release_pin",))
                elif pin=="corrupt":a.conn.execute("UPDATE kv SET v=? WHERE k=?",("broken{","provider_cli.release_pin"))
                a.conn.commit()
            await runtime.run(mutate)
            async def choose():return await runtime.choose_launch(host="host-a",provider="codex",model="fixture",effort="high",message={})
            if (marker,history,pin) in {("absent","absent","absent"),("valid","valid","valid")}:
                assert (await choose()).alias=="primary"
            else:
                with pytest.raises(AccountError):await choose()
        finally:store.stop()
    asyncio.run(run())


def test_pristine_managed_launch_and_existing_input_remain_available():
    async def run():
        store, runtime = await setup(initialize_pin=False)
        try:
            selected = await runtime.choose_launch(host="host-a", provider="codex",
                model="fixture", effort="high", message={})
            assert selected.key == binding().key
            row = await store.open_session("host-a", "before-enforcement",
                session_generation="before-generation", provider="codex",
                account_binding=selected.public())
            await runtime.admit_launch(row)
            await runtime.before_input(row)
            admission=await runtime.run(lambda a:a.conn.execute("SELECT v FROM kv WHERE k=?",
                ("provider_cli.admission:host-a:before-enforcement:before-generation",)).fetchone()[0])
            assert json.loads(admission)["mode_epoch"]==runtime._health_epoch
            async with runtime.input_guard(row):
                pass
            status = await runtime.operation({"action": "pin-status", "_auth_context": AUTH})
            assert status["pin"] is None
            assert status["mode"] == "pre_enforcement"
        finally:
            store.stop()
    asyncio.run(run())


def test_first_enforcement_observes_a_pre_enforcement_seat_without_rewriting_its_admission():
    from provider_cli_fixtures import seed_pin
    from test_provider_cli_pin import artifact_executor
    async def run():
        store, runtime = await setup(initialize_pin=False)
        try:
            row = await store.open_session("host-a", "kept-conversation",
                session_generation="kept-generation", provider="codex",
                account_binding=binding().public())
            await runtime.admit_launch(row)
            key = "provider_cli.admission:host-a:kept-conversation:kept-generation"
            async def receipt():
                return await runtime.run(lambda a:a.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()[0])
            historical = await receipt()
            await seed_pin(runtime)
            await artifact_executor(runtime)
            await runtime.before_input(row)
            async with runtime.input_guard(row):
                pass
            assert await receipt() == historical
            assert json.loads(historical)["mode"] == "pre_enforcement"
            status = await runtime.operation({"action":"pin-status", "_auth_context":AUTH})
            cell = next(c for c in status["cells"] if c["account"] == "primary")
            assert not cell["old_seats"]
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("provider,platform", [
    ("codex", "macos"), ("codex", "wsl"),
    ("claude", "macos"), ("claude", "wsl"),
])
def test_pre_pin_same_home_repin_uses_its_own_target_receipt(provider, platform, tmp_path, monkeypatch):
    async def run():
        store, runtime, host, old, request, calls, native, logins, cell, pin = await host_journey(
            tmp_path, monkeypatch, provider, platform)
        try:
            # Construct the never-initialized fixture, not a live rollback.
            def pristine(a):
                a.conn.execute("DELETE FROM kv WHERE k IN (?,?)",
                    ("provider_cli.release_pin", "provider_cli.pin_initialized"))
                prefix = "provider-cli-pin:"
                a.conn.execute("DELETE FROM v2_account_recovery WHERE substr(binding_key,1,?)=?",
                    (len(prefix), prefix))
                a.conn.commit()
            await runtime.run(pristine)
            target = copy.deepcopy(pin)
            target["revision"] = 1
            request.update(expected_pin_revision=0, expected_pin_digest=digest(target), target_pin=target)
            result = await runtime.operation(request)
            successor = result["result"]["binding"]
            assert successor["home"] == old.home
            assert successor["generation"] == request["new_generation"]
            assert host.registry.active(provider, old.alias, old.host).public() == dict(successor, default_revision=0)
            assert len(logins) == 1
            assert len([argv for argv in native if "Reply exactly OK" in argv]) == 1
            assert (await runtime.operation({"action": "pin-status", "_auth_context": AUTH}))["pin"] is None
            replay = await runtime.operation(request)
            assert replay["result"] == result["result"]
            assert len([argv for argv in native if "Reply exactly OK" in argv]) == 1
        finally:
            store.stop()
    asyncio.run(run())


def test_first_pin_cas_cannot_strand_a_serving_cell():
    async def run():
        store, runtime = await setup(initialize_pin=False)
        try:
            wrong = release_pair()
            wrong["providers"]["codex"]["version"] = "0.161.0"
            with pytest.raises(VerbError) as refused:
                await runtime.operation({"action": "pin-set", "pin": wrong,
                    "expected_revision": 0, "operation_id": "must-not-strand",
                    "reason": "initial activation", "_auth_context": AUTH})
            assert refused.value.code == "provider_cli_initial_readiness_required"
            current = await runtime.run(lambda a:a.conn.execute(
                "SELECT v FROM kv WHERE k=?", ("provider_cli.release_pin",)).fetchone())
            assert current is None
        finally:
            store.stop()
    asyncio.run(run())


def test_first_pin_cas_refuses_a_pre_enforcement_creation_still_in_flight():
    async def run():
        store, runtime = await setup(initialize_pin=False)
        try:
            assert await store.reserve_stream_id("host-a", "late-before-first-pin", ttl_s=600,
                request_id="old-create", owner_instance_id="old-instance", nonce="old-nonce")
            with pytest.raises(VerbError) as refused:
                await runtime.operation({"action": "pin-set", "pin": release_pair(),
                    "expected_revision": 0, "operation_id": "no-late-old-create",
                    "reason": "initial activation", "_auth_context": AUTH})
            assert refused.value.code == "provider_cli_initial_readiness_required"
            assert (await runtime.operation({"action": "pin-status", "_auth_context": AUTH}))["pin"] is None
        finally:
            store.stop()
    asyncio.run(run())
