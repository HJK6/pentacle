"""Pre-enforcement input preserves legacy seats without weakening later fences."""
import asyncio

import pytest

from provider_accounts import AccountError
from provider_cli_pin import pristine
from sessions import VerbError
from test_account_rotation import binding
from test_account_runtime import setup


def test_pristine_unbound_daff_tell_keeps_service_while_first_pin_refuses(tmp_path,monkeypatch):
    from comms import Comms
    from spawnctl import SpawnCtl
    from test_provider_cli_initial_activation import ready_fixture
    from test_tell_immediate_delivery import ScriptedClaude, IDLE_PLACEHOLDER

    async def run():
        store,runtime,host,selected,target,_,_=await ready_fixture(
            tmp_path,monkeypatch,serving_seat=False,provider="claude")
        admin={"peer_loopback":True,"local_admin_verified":True}
        status_request={"action":"pin-status","pin":target,"_auth_context":admin}
        try:
            assert (await runtime.operation(status_request))["initial_readiness"]["missing"]==[]
            tmux=ScriptedClaude(start=IDLE_PLACEHOLDER)
            runtime.sessions.tmux=tmux
            await runtime.sessions.open(selected.host,"legacy-daff",provider="claude",role="daff-assistant",
                session_generation="legacy-generation",bootstrap_state="ready",pane_status="pane_alive")
            stream_id=selected.host+":legacy-daff"
            row=await store.fetch_session(selected.host,"legacy-daff")
            assert row["status"]=="open" and row["account_binding"] is None
            assert row["role"]=="daff-assistant" and row["pane_status"]=="pane_alive"
            assert await runtime.enrolled(selected.host,"claude") and await runtime.run(pristine)
            comms=Comms(store,runtime.sessions,SpawnCtl(store,runtime.sessions,tmux=tmux))
            comms.account_runtime=runtime
            first=await comms.tell({"stream_id":stream_id,"message":"fixture first input","tell_id":"first-input"})
            assert first["delivery_status"]=="delivered" and first["submission_confirmed"]
            assert tmux.pastes==["fixture first input"]
            # Input compatibility is not readiness to enforce an unknown binding.
            with pytest.raises(VerbError) as refused:
                await runtime.operation({"action":"pin-set","pin":target,"expected_revision":0,
                    "operation_id":"unbound-daff-first-pin","reason":"fixture boundary","_auth_context":admin})
            assert refused.value.code=="provider_cli_initial_readiness_required"
            status=await runtime.operation(status_request)
            assert status["pin"] is None and status["mode"]=="pre_enforcement"
            assert {"stream_id":stream_id,"reason":"seat_binding_unverified"} in status["initial_readiness"]["missing"]
            second=await comms.tell({"stream_id":stream_id,"message":"fixture continued input","tell_id":"second-input"})
            assert second["delivery_status"]=="delivered" and second["submission_confirmed"]
            assert tmux.pastes==["fixture first input","fixture continued input"]
            assert (await store.fetch_session(selected.host,"legacy-daff"))["account_binding"] is None
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("provider",["claude","codex"])
@pytest.mark.parametrize("policy_enabled",[True,False])
@pytest.mark.parametrize("marker",["absent","valid","corrupt"])
@pytest.mark.parametrize("history",["absent","valid","corrupt"])
@pytest.mark.parametrize("pin",["absent","valid","corrupt"])
def test_unbound_input_is_available_only_for_exact_pristine_state(provider,policy_enabled,marker,history,pin):
    async def run():
        store,runtime=await setup()
        actions=[]
        try:
            if provider=="claude":await runtime.run(lambda a:a.register(binding(provider="claude")))
            runtime.policy["enabled"]=policy_enabled
            row=await store.open_session("host-a","legacy-input",provider=provider,
                role="daff-assistant" if provider=="claude" else "worker",
                session_generation="legacy-generation",pane_status="pane_alive",bootstrap_state="ready")
            assert row["account_binding"] is None and await runtime.enrolled("host-a",provider)
            def arrange(a):
                if marker=="absent":a.conn.execute("DELETE FROM kv WHERE k=?",("provider_cli.pin_initialized",))
                elif marker=="corrupt":a.conn.execute("UPDATE kv SET v=? WHERE k=?",("invalid-marker","provider_cli.pin_initialized"))
                prefix="provider-cli-pin:"
                if history=="absent":
                    a.conn.execute("DELETE FROM v2_account_recovery WHERE substr(binding_key,1,?)=?",(len(prefix),prefix))
                elif history=="corrupt":
                    a.conn.execute("UPDATE v2_account_recovery SET data=? WHERE substr(binding_key,1,?)=?",("broken{",len(prefix),prefix))
                if pin=="absent":a.conn.execute("DELETE FROM kv WHERE k=?",("provider_cli.release_pin",))
                elif pin=="corrupt":a.conn.execute("UPDATE kv SET v=? WHERE k=?",("broken{","provider_cli.release_pin"))
                a.conn.commit()
            await runtime.run(arrange)
            async def deliver():
                async with runtime.input_guard(row):actions.append("physical-input")
            if (marker,history,pin)==("absent","absent","absent"):
                assert await runtime.run(pristine)
                await deliver()
                assert actions==["physical-input"]
            else:
                with pytest.raises(AccountError) as refused:await deliver()
                assert str(refused.value)=="provider_cli_pin_unavailable"
                assert refused.value.context["reason"]=="binding_unknown"
                assert actions==[]
            assert (await store.fetch_session("host-a","legacy-input"))["account_binding"] is None
        finally:
            store.stop()
    asyncio.run(run())
