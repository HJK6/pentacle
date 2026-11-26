"""Admission-signalled self-recovery, driven only by explicit collector passes."""
import asyncio
import json

import pytest

from account_executor import AccountExecutor
from sessions import VerbError
from test_account_cap_delivery import fixture as delivery_fixture
from test_account_rotation import binding, observation
from test_account_pool_control import PEER


def _automatic_world(runtime, account, outcomes):
    calls = []
    attempts = []

    async def execute(selected, action, operation_id, generation=None, **kwargs):
        calls.append((selected.key, selected.generation, action))
        if action == "observe-bound":
            attempts.append(operation_id)
            result = outcomes.pop(0) if outcomes else "ok"
            if callable(result): return await result(selected, action, operation_id, generation)
            if isinstance(result, dict): return result
            if result == "ok":
                return {"outcome": "ok", "binding_key": selected.key,
                        "observation": observation(selected)}
            return {"outcome": result}
        return {"outcome": "ok", "binding_key": selected.key,
                "observation": observation(selected)}

    runtime.executor = AccountExecutor(runtime, None, execute=execute)
    runtime.policy["enabled"] = True
    runtime._next_probe = 0
    return calls, attempts


async def _external(deliver, verb, msg):
    """Enter ``deliver`` as server dispatch does for a verified external request."""
    from account_runtime import RECOVERY_INGRESS, mint_ingress
    from comms import _INGRESS_ROUTE
    from server import Server
    token = None
    if Server._recovery_ingress(msg.get("_auth_context") or {}, msg):
        token = mint_ingress(verb)
    try:
        return await deliver({**msg, _INGRESS_ROUTE: True} if verb != "spawn" else msg)
    finally:
        if token is not None:
            RECOVERY_INGRESS.reset(token)


async def _collector_pass(runtime):
    await runtime.tick()
    task = runtime._collector_task
    if task is not None:
        await task


async def _health(runtime, key):
    return await runtime.run(lambda ledger: ledger.conn.execute(
        "SELECT health,health_revision FROM v2_account_bindings WHERE binding_key=?", (key,)
    ).fetchone())


async def _recovery_state(runtime, key):
    def read(ledger):
        from account_executor import AccountExecutor as Executor
        return Executor._read_recovery(ledger, key)
    return await runtime.run(read)


async def _await_dispatch_or_pass_end(entered, task):
    waiter=asyncio.create_task(entered.wait())
    done,pending=await asyncio.wait({waiter,task},return_when=asyncio.FIRST_COMPLETED)
    if task in done and not entered.is_set():
        task.result()
        pytest.fail("observation pass ended before the bound-call barrier")
    if waiter in pending:
        waiter.cancel()
        await asyncio.gather(waiter,return_exceptions=True)


@pytest.mark.parametrize("verb", ["tell", "send"])
def test_incident_journey_refuses_then_recovers_and_resubmits_once(tmp_path, verb, monkeypatch):
    async def run():
        store, runtime, comms, tmux, worker, _meter = await delivery_fixture(tmp_path)
        account = binding(provider="claude")
        try:
            await runtime.run(lambda ledger: (ledger.conn.execute(
                "DELETE FROM kv WHERE k=?", ("account.pool-control:claude:pool-primary",)), ledger.conn.commit()))
            await runtime.record_failure(account, "auth")
            clock = {"mono": 10.0, "wall": runtime.clock()}
            runtime.monotonic = lambda: clock["mono"]
            runtime.clock = lambda: clock["wall"]
            import account_executor as executor_module
            validated=[]
            original_validator=executor_module._valid_observation
            def validate(selected,value):
                validated.append(selected.key)
                return original_validator(selected,value)
            monkeypatch.setattr(executor_module,"_valid_observation",validate)
            calls, attempts = _automatic_world(runtime, account, ["provider_auth_failed", "ok"])
            message = {"stream_id": worker["stream_id"], "message": "one refused payload",
                "from_stream_id": PEER["stream_id"], "_auth_context": PEER,
                "tell_id": "self-recovery-same-key", "request_id": "self-recovery-same-key"}
            deliver = lambda value, _deliver=getattr(comms, verb): _external(_deliver, verb, value)
            try:
                first = await deliver(dict(message))
                assert first.get("submission_confirmed") is False
            except VerbError as exc:
                assert str(exc) == "account_unavailable"
            assert tmux.pastes == []
            assert not [c for c in calls if c[2] == "observe-bound"]

            await _collector_pass(runtime)
            assert len(attempts) == 1
            assert (await _health(runtime, account.key))[0] == "auth_unhealthy"
            assert tmux.pastes == []

            clock["mono"] += 121
            clock["wall"] += 61
            try:
                second = await deliver(dict(message))
                assert second.get("submission_confirmed") is False
            except VerbError as exc:
                assert str(exc) == "account_unavailable"
            await _collector_pass(runtime)
            assert len(attempts) == 2 and attempts[0] != attempts[1]
            assert validated == [account.key]
            assert (await _health(runtime, account.key))[0] == "healthy"
            assert len((await _recovery_state(runtime, account.key)).get("receipts", {})) == 1

            first_landed = await deliver(dict(message))
            second_landed = await deliver(dict(message))
            assert first_landed.get("submission_confirmed") is True
            assert second_landed.get("submission_confirmed") is True
            assert len(tmux.pastes) == 1
        finally:
            store.stop()
    asyncio.run(run())


def test_refusal_is_observable_while_bound_probe_is_held(tmp_path):
    async def run():
        store, runtime, comms, tmux, worker, _meter = await delivery_fixture(tmp_path)
        account = binding(provider="claude")
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            await runtime.run(lambda ledger: (ledger.conn.execute(
                "DELETE FROM kv WHERE k=?", ("account.pool-control:claude:pool-primary",)), ledger.conn.commit()))
            await runtime.record_failure(account, "auth")
            calls = []

            async def execute(selected, action, operation_id, generation=None, **kwargs):
                calls.append(action)
                if action == "observe-bound":
                    entered.set()
                    await release.wait()
                    return {"outcome": "ok", "binding_key": selected.key,
                            "observation": observation(selected)}
                return {"outcome": "ok", "binding_key": selected.key,
                        "observation": observation(selected)}

            runtime.executor = AccountExecutor(runtime, None, execute=execute)
            msg = {"stream_id": worker["stream_id"], "message": "held-probe refusal",
                "from_stream_id": PEER["stream_id"], "_auth_context": PEER,
                "tell_id": "held-probe", "request_id": "held-probe"}
            with pytest.raises(VerbError, match="account_unavailable"):
                await _external(comms.tell, "tell", msg)
            assert tmux.pastes == [] and "observe-bound" not in calls
            runtime._next_probe = 0
            await runtime.tick()
            await _await_dispatch_or_pass_end(entered,runtime._collector_task)
            with pytest.raises(VerbError, match="account_unavailable"):
                await _external(comms.tell, "tell", dict(msg, tell_id="competing-refusal", request_id="competing-refusal"))
            assert tmux.pastes == []
            release.set()
            await runtime._collector_task
        finally:
            release.set()
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("verb", ["tell", "send"])
def test_verified_direct_refusal_marks_exact_binding_revision(tmp_path, verb):
    async def run():
        store, runtime, comms, tmux, worker, _meter = await delivery_fixture(tmp_path)
        account = binding(provider="claude")
        try:
            await runtime.run(lambda ledger: (ledger.conn.execute(
                "DELETE FROM kv WHERE k=?", ("account.pool-control:claude:pool-primary",)), ledger.conn.commit()))
            await runtime.record_failure(account, "auth")
            revision=(await _health(runtime,account.key))[1]
            msg={"stream_id":worker["stream_id"],"message":"refusal mark only",
                "from_stream_id":PEER["stream_id"],"_auth_context":PEER,
                "tell_id":"mark-only-"+verb,"request_id":"mark-only-"+verb}
            try:
                result=await _external(getattr(comms,verb),verb,dict(msg))
                assert result.get("submission_confirmed") is False
            except VerbError as exc:
                assert exc.code=="account_unavailable"
            assert runtime._self_recovery_marks=={(account.key,account.generation):revision}
            assert tmux.pastes==[]
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("bad_result", [
    "provider_quota_blocked", "provider_capacity", "provider_transport_failed",
    "provider_permission_denied", "provider_timeout", "provider_probe_failed",
])
def test_failed_or_incomplete_bound_evidence_never_promotes_or_enrolls(tmp_path, bad_result):
    async def run():
        store, runtime, comms, tmux, worker, _meter = await delivery_fixture(tmp_path)
        account = binding(provider="claude")
        try:
            await runtime.run(lambda ledger: (ledger.conn.execute(
                "DELETE FROM kv WHERE k=?", ("account.pool-control:claude:pool-primary",)), ledger.conn.commit()))
            await runtime.record_failure(account, "auth")
            before = (await _health(runtime, account.key))[1]
            calls, attempts = _automatic_world(runtime, account, [bad_result])
            msg = {"stream_id": worker["stream_id"], "message": "evidence must be complete",
                "from_stream_id": PEER["stream_id"], "_auth_context": PEER,
                "tell_id": "bad-evidence", "request_id": "bad-evidence"}
            with pytest.raises(VerbError, match="account_unavailable"):
                await _external(comms.tell, "tell", msg)
            await _collector_pass(runtime)
            assert len(attempts) == 1
            assert tuple(await _health(runtime, account.key)) == ("auth_unhealthy", before)
            assert not (await _recovery_state(runtime, account.key)).get("receipts")
            assert "enroll" not in [c[2] for c in calls]
            assert tmux.pastes == []
        finally:
            store.stop()
    asyncio.run(run())


def test_old_success_cannot_clear_auth_failure_committed_during_probe(tmp_path):
    async def run():
        store, runtime, comms, tmux, worker, _meter = await delivery_fixture(tmp_path)
        account = binding(provider="claude")
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            await runtime.run(lambda ledger: (ledger.conn.execute(
                "DELETE FROM kv WHERE k=?", ("account.pool-control:claude:pool-primary",)), ledger.conn.commit()))
            await runtime.record_failure(account, "auth")

            async def execute(selected, action, operation_id, generation=None, **kwargs):
                if action == "observe-bound":
                    entered.set()
                    await release.wait()
                    return {"outcome": "ok", "binding_key": selected.key,
                            "observation": observation(selected)}
                return {"outcome": "ok", "binding_key": selected.key,
                        "observation": observation(selected)}

            runtime.executor = AccountExecutor(runtime, None, execute=execute)
            msg = {"stream_id": worker["stream_id"], "message": "race refusal",
                "from_stream_id": PEER["stream_id"], "_auth_context": PEER,
                "tell_id": "race-refusal", "request_id": "race-refusal"}
            with pytest.raises(VerbError, match="account_unavailable"):
                await _external(comms.tell, "tell", msg)
            runtime._next_probe = 0
            await runtime.tick()
            await _await_dispatch_or_pass_end(entered,runtime._collector_task)
            await runtime.record_failure(account, "auth")
            release.set()
            await runtime._collector_task
            assert tuple(await _health(runtime, account.key)) == ("auth_unhealthy", 2)
            assert not (await _recovery_state(runtime, account.key)).get("receipts")
            assert tmux.pastes == []
        finally:
            release.set()
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("winner", ["identity", "disable", "rotation", "manual"])
def test_late_probe_cannot_replace_competing_health_winner(tmp_path, winner):
    async def run():
        store, runtime, comms, tmux, worker, _meter = await delivery_fixture(tmp_path)
        account = binding(provider="claude")
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            await runtime.run(lambda ledger: (ledger.conn.execute(
                "DELETE FROM kv WHERE k=?", ("account.pool-control:claude:pool-primary",)), ledger.conn.commit()))
            await runtime.record_failure(account, "auth")

            async def execute(selected, action, operation_id, generation=None, **kwargs):
                if action == "observe-bound":
                    entered.set()
                    await release.wait()
                    return {"outcome": "ok", "binding_key": selected.key,
                            "observation": observation(selected)}
                return {"outcome": "ok", "binding_key": selected.key,
                        "observation": observation(selected)}

            runtime.executor = AccountExecutor(runtime, None, execute=execute)
            msg = {"stream_id": worker["stream_id"], "message": "winner race",
                "from_stream_id": PEER["stream_id"], "_auth_context": PEER,
                "tell_id": "winner-race", "request_id": "winner-race"}
            with pytest.raises(VerbError, match="account_unavailable"):
                await _external(comms.tell, "tell", msg)
            runtime._next_probe = 0
            await runtime.tick()
            await _await_dispatch_or_pass_end(entered,runtime._collector_task)
            if winner == "identity":
                await runtime.record_failure(account, "identity")
                expected = "identity_conflict"
            elif winner == "disable":
                await runtime.run(lambda ledger: ledger.conn.execute(
                    "UPDATE v2_account_bindings SET data=json_set(data,'$.enabled',0) WHERE binding_key=?", (account.key,)))
                expected = "auth_unhealthy"
            elif winner == "rotation":
                from dataclasses import replace
                await runtime.run(lambda ledger: ledger.register(replace(account, generation="next-generation")))
                expected = "auth_unhealthy"
            else:
                def manual_winner(ledger):
                    from account_store import transaction
                    from account_executor import AccountExecutor as Executor
                    with transaction(ledger.conn):
                        transition=ledger.transition_relogin_auth_health(account.key,1)
                        assert transition["outcome"] == "healthy" and transition["changed"]
                        state=Executor._read_recovery(ledger,account.key)
                        state.setdefault("receipts",{})["manual-winner"]={"outcome":"healthy",
                            "operation_id":"manual-winner","health_revision":transition["applied_revision"],
                            "provenance":"manual_relogin"}
                        Executor._write_recovery(ledger,account.key,state)
                await runtime.run(manual_winner)
                expected = "healthy"
            release.set()
            await runtime._collector_task
            row = await runtime.run(lambda ledger: ledger.conn.execute(
                "SELECT health,health_revision FROM v2_account_bindings WHERE binding_key=?", (account.key,)).fetchone())
            assert row[0] == expected
            assert tmux.pastes == []
        finally:
            release.set()
            store.stop()
    asyncio.run(run())


def test_restart_drops_marks_and_orphaned_worker_cannot_write(tmp_path):
    async def run():
        store, runtime, comms, tmux, worker, _meter = await delivery_fixture(tmp_path)
        account = binding(provider="claude")
        remote_result = asyncio.get_running_loop().create_future()
        entered = asyncio.Event()
        ids = []
        try:
            await runtime.run(lambda ledger: (ledger.conn.execute(
                "DELETE FROM kv WHERE k=?", ("account.pool-control:claude:pool-primary",)), ledger.conn.commit()))
            await runtime.record_failure(account, "auth")

            async def old_execute(selected, action, operation_id, generation=None, **kwargs):
                if action == "observe-bound":
                    ids.append(operation_id); entered.set()
                    result = await asyncio.shield(remote_result)
                    return result
                return {"outcome": "ok", "binding_key": selected.key,
                        "observation": observation(selected)}

            runtime.executor = AccountExecutor(runtime, None, execute=old_execute)
            msg = {"stream_id": worker["stream_id"], "message": "restart refusal",
                "from_stream_id": PEER["stream_id"], "_auth_context": PEER,
                "tell_id": "restart-refusal", "request_id": "restart-refusal"}
            with pytest.raises(VerbError, match="account_unavailable"):
                await _external(comms.tell, "tell", msg)
            runtime._next_probe = 0
            await runtime.tick()
            await _await_dispatch_or_pass_end(entered,runtime._collector_task)
            old_task = runtime._collector_task
            await runtime.stop()
            assert old_task.cancelled()
            remote_result.set_result({"outcome": "ok", "binding_key": account.key,
                "observation": observation(account)})
            assert not (await _recovery_state(runtime, account.key)).get("receipts")

            replacement = type(runtime)(store, runtime.sessions, policy=runtime.policy, clock=runtime.clock)
            replacement.attach(runtime.server, runtime.spawnctl, comms)
            replacement.executor = AccountExecutor(replacement, None, execute=old_execute)
            await replacement.start()
            replacement._next_probe = 0
            await replacement.tick()
            if replacement._collector_task is not None: await replacement._collector_task
            assert len(ids) == 1  # Startup/pass alone cannot invent a refusal mark.
            with pytest.raises(VerbError, match="account_unavailable"):
                await _external(comms.tell, "tell", dict(msg, tell_id="fresh-after-restart", request_id="fresh-after-restart"))
            replacement._next_probe=0
            await replacement.tick()
            if replacement._collector_task is not None: await replacement._collector_task
            assert len(ids) == 2 and ids[0] != ids[1]
        finally:
            if not remote_result.done(): remote_result.cancel()
            store.stop()
    asyncio.run(run())


def test_unmarked_ordinary_collection_never_clears_auth_health(tmp_path):
    async def run():
        store, runtime, _comms, _tmux, _worker, _meter = await delivery_fixture(tmp_path)
        account = binding(provider="claude")
        try:
            await runtime.record_failure(account, "auth")
            calls, attempts = _automatic_world(runtime, account, ["ok"])
            await _collector_pass(runtime)
            assert not attempts
            assert (await _health(runtime, account.key))[0] == "auth_unhealthy"
            assert not (await _recovery_state(runtime, account.key)).get("receipts")
            assert (account.key, account.generation, "observe") in calls
            assert not [c for c in calls if c[2] == "observe-bound"]
            runtime.policy["watchers"]={"fixture-watcher":{"host":"host-a"}}
            result=await runtime.operation({"action":"probe","host":"host-a","provider":"claude",
                "account":"primary","login_generation":account.generation,
                "_auth_context":{"service_authenticated":True,"service_actor":"fixture-watcher"}})
            assert result["result"]["outcome"]=="ok"
            assert (await _health(runtime,account.key))[0]=="auth_unhealthy"
            assert not runtime._self_recovery_marks
            assert not [c for c in calls if c[2]=="observe-bound"]
        finally:
            store.stop()
    asyncio.run(run())

@pytest.mark.parametrize("auth,from_stream", [
    ({}, "host-a:untrusted"),
    ({"token_verified": True, "stream_id": PEER["stream_id"],
      "session_generation": PEER["session_generation"]}, "host-a:spoofed"),
])
def test_unverified_or_spoofed_ingress_does_not_mark_recovery(tmp_path, auth, from_stream):
    async def run():
        store, runtime, comms, _tmux, worker, _meter = await delivery_fixture(tmp_path)
        account = binding(provider="claude")
        try:
            await runtime.run(lambda ledger: (ledger.conn.execute(
                "DELETE FROM kv WHERE k=?", ("account.pool-control:claude:pool-primary",)), ledger.conn.commit()))
            await runtime.record_failure(account, "auth")
            calls, attempts = _automatic_world(runtime, account, ["ok"])
            msg = {"stream_id": worker["stream_id"], "message": "unverified", "from_stream_id": from_stream,
                   "_auth_context": auth, "tell_id": "untrusted", "request_id": "untrusted"}
            with pytest.raises(VerbError):
                await _external(comms.tell, "tell", msg)
            await _collector_pass(runtime)
            assert not attempts
            assert not [call for call in calls if call[2] == "observe-bound"]
            assert not runtime._self_recovery_marks
            assert (await _health(runtime, account.key))[0] == "auth_unhealthy"
        finally:
            store.stop()
    asyncio.run(run())


def test_identity_conflict_and_disabled_binding_never_signal(tmp_path):
    async def run():
        store, runtime, comms, _tmux, worker, _meter = await delivery_fixture(tmp_path)
        account = binding(provider="claude")
        try:
            await runtime.run(lambda ledger: (ledger.conn.execute(
                "DELETE FROM kv WHERE k=?", ("account.pool-control:claude:pool-primary",)), ledger.conn.commit()))
            calls, attempts = _automatic_world(runtime, account, ["ok", "ok"])
            await runtime.record_failure(account, "identity")
            msg = {"stream_id": worker["stream_id"], "message": "identity conflict", "from_stream_id": PEER["stream_id"],
                   "_auth_context": PEER, "tell_id": "identity-conflict", "request_id": "identity-conflict"}
            with pytest.raises(VerbError, match="account_unavailable"):
                await _external(comms.tell, "tell", msg)
            await _collector_pass(runtime)
            assert not attempts
            assert not runtime._self_recovery_marks
            assert (await _health(runtime, account.key))[0] == "identity_conflict"

            def disable(ledger):
                row=ledger.conn.execute("SELECT data FROM v2_account_bindings WHERE binding_key=?", (account.key,)).fetchone()
                data=json.loads(row[0]);data["enabled"]=False
                ledger.conn.execute("UPDATE v2_account_bindings SET data=? WHERE binding_key=?",
                                    (json.dumps(data),account.key))
                ledger.conn.commit()
            await runtime.run(disable)
            with pytest.raises(VerbError):
                await _external(comms.tell, "tell", dict(msg,tell_id="disabled",request_id="disabled"))
            await _collector_pass(runtime)
            assert not attempts and not [call for call in calls if call[2] == "observe-bound"]
            assert not runtime._self_recovery_marks
        finally:
            store.stop()
    asyncio.run(run())


def test_mark_waits_for_explicit_pass_and_is_not_drained_during_probe(tmp_path):
    async def run():
        store, runtime, comms, tmux, worker, _meter = await delivery_fixture(tmp_path)
        account = binding(provider="claude")
        entered, release = asyncio.Event(), asyncio.Event()
        mono = {"now": 50.0, "wall": runtime.clock()}
        calls=[]
        try:
            await runtime.run(lambda ledger: (ledger.conn.execute(
                "DELETE FROM kv WHERE k=?", ("account.pool-control:claude:pool-primary",)), ledger.conn.commit()))
            await runtime.record_failure(account, "auth")
            runtime.monotonic=lambda:mono["now"]
            runtime.clock=lambda:mono["wall"]

            async def execute(selected, action, operation_id, generation=None, **kwargs):
                calls.append((selected.key,action))
                if action == "observe-bound":
                    entered.set();await release.wait()
                    return {"outcome":"provider_probe_failed"}
                return {"outcome":"ok","binding_key":selected.key,"observation":observation(selected)}

            runtime.executor=AccountExecutor(runtime,None,execute=execute)
            msg={"stream_id":worker["stream_id"],"message":"first mark","from_stream_id":PEER["stream_id"],
                 "_auth_context":PEER,"tell_id":"mark-one","request_id":"mark-one"}
            with pytest.raises(VerbError,match="account_unavailable"):
                await _external(comms.tell, "tell", msg)
            assert not [call for call in calls if call[1]=="observe-bound"]
            runtime._next_probe=0
            await runtime.tick()
            await _await_dispatch_or_pass_end(entered,runtime._collector_task)
            for n in range(5):
                with pytest.raises(VerbError,match="account_unavailable"):
                    await _external(comms.tell, "tell", dict(msg,tell_id=f"during-{n}",request_id=f"during-{n}"))
            assert len([call for call in calls if call[1]=="observe-bound"]) == 1
            mono["now"] += 121
            mono["wall"] += 121
            release.set()
            await runtime._collector_task
            assert len([call for call in calls if call[1]=="observe-bound"]) == 1

            # The newer mark is consumed only by a later explicit pass.
            runtime._next_probe=0
            await runtime.tick()
            await runtime._collector_task
            assert len([call for call in calls if call[1]=="observe-bound"]) == 2
            # An ordinary pass uses one slot per binding: no second ordinary observe
            # for the marked binding in addition to the replacement probe.
            assert sum(call==(account.key,"observe") for call in calls)==0
        finally:
            release.set()
            store.stop()
    asyncio.run(run())


def test_unhealthy_spawn_refuses_then_fresh_resubmission_launches_once(tmp_path,monkeypatch):
    async def run():
        from test_account_schedule_journey import fixture as spawn_fixture, SPEC
        store, runtime, pane, ctl, _surface, message = await spawn_fixture(tmp_path)
        account = await runtime.run(lambda ledger: ledger.bound(binding("spare", host="localhost").key))
        try:
            await runtime.record_failure(account, "auth")
            calls, attempts = _automatic_world(runtime, account, ["ok"])
            msg = message("spawn", spec_id=SPEC, provider="codex", account="spare", model="gpt-6.2-sol",
                effort="high", initial_prompt="native user prompt", objective="Self recovery admission",
                idempotency_key="same-spawn-key")
            with pytest.raises(VerbError, match="account_unavailable"):
                await _external(lambda value: ctl._spawn_guarded(value, "localhost"), "spawn", dict(msg))
            assert pane.launches == 0 and not attempts
            revision=(await _health(runtime,account.key))[1]
            assert runtime._self_recovery_marks=={(account.key,account.generation):revision}
            import provider_cli_pin
            monkeypatch.setattr(provider_cli_pin,"check",lambda *_args:None)
            calls,attempts=_automatic_world(runtime,account,["ok"])
            outcome=await runtime.executor.observe(account)
            assert outcome["outcome"]=="ok" and (await _health(runtime,account.key))[0]=="healthy"
            assert len(attempts)==1 and [call[2] for call in calls].count("observe-bound")==1
            await _external(lambda value: ctl._spawn_guarded(value, "localhost"), "spawn", dict(msg))
            await _external(lambda value: ctl._spawn_guarded(value, "localhost"), "spawn", dict(msg))
            assert pane.launches==1
        finally:
            for task in tuple(ctl._background_spawns): task.cancel()
            await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("principal", [
    "peer", "front-desk", "operator",
])
def test_verified_permitted_principals_can_signal_without_admitting_refusal(tmp_path, principal):
    async def run():
        from test_account_runtime import AUTH
        from test_account_pool_control import OPERATOR
        store, runtime, comms, tmux, worker, _meter = await delivery_fixture(tmp_path)
        account = binding(provider="claude")
        try:
            await runtime.run(lambda ledger: (ledger.conn.execute(
                "DELETE FROM kv WHERE k=?", ("account.pool-control:claude:pool-primary",)), ledger.conn.commit()))
            await runtime.record_failure(account, "auth")
            calls, attempts = _automatic_world(runtime, account, ["ok"])
            auth = {"peer": PEER, "front-desk": AUTH, "operator": OPERATOR}[principal]
            from_stream = PEER["stream_id"] if principal == "peer" else AUTH["stream_id"] if principal == "front-desk" else None
            msg = {"stream_id": worker["stream_id"], "message": "verified principal refusal",
                "_auth_context": auth, "tell_id": "principal-" + principal,
                "request_id": "principal-" + principal}
            if from_stream: msg["from_stream_id"] = from_stream
            with pytest.raises(VerbError, match="account_unavailable"):
                await _external(comms.tell, "tell", msg)
            revision=(await _health(runtime,account.key))[1]
            assert not attempts and tmux.pastes == []
            assert runtime._self_recovery_marks=={(account.key,account.generation):revision}
        finally:
            store.stop()
    asyncio.run(run())


def test_health_signal_does_not_bypass_later_independent_resource_denial(tmp_path):
    async def run():
        from provider_cli_fixtures import seed_pin
        from test_account_self_recovery_r2 import _artifact_proof
        store, runtime, comms, tmux, worker, _meter = await delivery_fixture(tmp_path)
        account = binding(provider="claude")
        try:
            await runtime.run(lambda ledger: (ledger.conn.execute(
                "DELETE FROM kv WHERE k=?", ("account.pool-control:claude:pool-primary",)), ledger.conn.commit()))
            await seed_pin(runtime)
            await runtime.record_failure(account, "auth")
            revision = (await _health(runtime, account.key))[1]
            calls = []

            async def execute(selected, action, operation_id, generation=None, **kwargs):
                calls.append(action)
                if action == "observe-bound":
                    return {"outcome": "ok", "binding_key": selected.key, "observation": observation(selected),
                            "artifact_proof": _artifact_proof(selected, kwargs["pin"])}
                # The independent input gate: this seat's artifact cannot be verified.
                return {"outcome": "ok", "binding_key": "another-binding"}
            counted = AccountExecutor(runtime, None, execute=execute)
            runtime.executor = counted
            msg = {"stream_id": worker["stream_id"], "message": "independent gate remains",
                "from_stream_id": PEER["stream_id"], "_auth_context": PEER,
                "tell_id": "independent-gate", "request_id": "independent-gate"}
            with pytest.raises(VerbError, match="account_unavailable"):
                await _external(comms.tell, "tell", dict(msg))
            assert runtime._self_recovery_marks == {(account.key, account.generation): revision}
            # Recover through the executor being counted.
            assert (await counted.observe(account))["outcome"] == "ok"
            assert calls == ["observe-bound"] and runtime.executor is counted
            assert tuple(await _health(runtime, account.key)) == ("healthy", revision + 1)
            # A fresh submission still meets the independent gate.
            with pytest.raises(VerbError, match="provider_cli_pin_mismatch"):
                await _external(comms.tell, "tell", dict(msg, tell_id="fresh-after", request_id="fresh-after"))
            assert calls == ["observe-bound", "verify-artifact"]
            assert tmux.pastes == [] and runtime._self_recovery_marks == {}
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("observer", ["collector", "probe", "report-failure", "recover"])
def test_each_existing_observation_owner_can_consume_only_a_marked_binding(tmp_path, observer):
    async def run():
        store,runtime,comms,tmux,worker,_meter=await delivery_fixture(tmp_path)
        account=binding(provider="claude")
        try:
            await runtime.run(lambda ledger:(ledger.conn.execute(
                "DELETE FROM kv WHERE k=?",("account.pool-control:claude:pool-primary",)),ledger.conn.commit()))
            await runtime.record_failure(account,"auth")
            revision=(await _health(runtime,account.key))[1]
            runtime._mark_auth_self_recovery(account,revision)
            calls,attempts=_automatic_world(runtime,account,["ok"])
            if observer=="collector":
                await _collector_pass(runtime)
                result=None
            elif observer=="probe":
                runtime.policy["watchers"]={"fixture-watcher":{"host":"host-a"}}
                result=await runtime.operation({"action":"probe","host":"host-a","provider":"claude",
                    "account":"primary","login_generation":account.generation,
                    "_auth_context":{"service_authenticated":True,"service_actor":"fixture-watcher"}})
            elif observer=="report-failure":
                result=await runtime.operation({"action":"report-failure","host":"host-a","provider":"claude",
                    "account":"primary","login_generation":account.generation,"kind":"auth",
                    "_auth_context":{"token_verified":True,"stream_id":worker["stream_id"],
                        "session_generation":worker["session_generation"]}})
            else:
                result=await runtime.executor.recover(account,relogin=False,operation_id="read-only-recover")
            assert len(attempts)==1 and len(set(attempts))==1
            assert len([call for call in calls if call[2]=="observe-bound"])==1
            assert not [call for call in calls if call[0]==account.key and call[2]=="observe"]
            assert (await _health(runtime,account.key))[0]=="healthy"
            receipt=(await _recovery_state(runtime,account.key))["receipts"]
            assert len(receipt)==1 and next(iter(receipt.values()))["provenance"]=="admission_signalled_self_recovery"
            if observer in {"probe","report-failure"}:
                assert result["type"]=="account.ok" and result["accepted"] is True
                assert result["result"]["outcome"]=="ok"
                assert result["result"]["binding_key"]==account.key
                assert result["result"]["observation"]==observation(account)
            elif observer=="recover":
                assert result["outcome"]=="ok" and result["binding_key"]==account.key
                assert result["observation"]==observation(account)
            assert not runtime._self_recovery_marks
            assert not runtime._self_recovery_inflight
            assert tmux.pastes==[]
        finally:
            store.stop()
    asyncio.run(run())


def test_unauthorized_probe_and_stale_report_leave_mark_unspent(tmp_path):
    async def run():
        store,runtime,_comms,_tmux,worker,_meter=await delivery_fixture(tmp_path)
        account=binding(provider="claude")
        try:
            await runtime.record_failure(account,"auth")
            revision=(await _health(runtime,account.key))[1]
            runtime._mark_auth_self_recovery(account,revision)
            calls,attempts=_automatic_world(runtime,account,["ok","ok"])
            mark={(account.key,account.generation):revision}
            with pytest.raises(VerbError,match="account_control_unauthorized"):
                await runtime.operation({"action":"probe","host":"host-a","provider":"claude",
                    "account":"primary","login_generation":account.generation,"_auth_context":{}})
            assert runtime._self_recovery_marks==mark
            with pytest.raises(VerbError,match="account_report_unauthorized"):
                await runtime.operation({"action":"report-failure","host":"host-a","provider":"claude",
                    "account":"primary","login_generation":account.generation,"kind":"auth",
                    "_auth_context":{"token_verified":True,"stream_id":"host-a:stale-seat",
                        "session_generation":"stale-generation"}})
            assert runtime._self_recovery_marks==mark and not attempts
            assert not [call for call in calls if call[2]=="observe-bound"]
        finally:
            store.stop()
    asyncio.run(run())


def test_report_for_a_different_binding_cannot_spend_its_mark(tmp_path):
    async def run():
        store,runtime,_comms,_tmux,worker,_meter=await delivery_fixture(tmp_path)
        account=binding(provider="claude",alias="spare")
        try:
            await runtime.run(lambda ledger:(ledger._register(account),ledger.conn.commit()))
            await runtime.record_failure(account,"auth")
            revision=(await _health(runtime,account.key))[1]
            runtime._mark_auth_self_recovery(account,revision)
            calls,attempts=_automatic_world(runtime,account,["ok"])
            with pytest.raises(VerbError,match="account_report_unauthorized"):
                await runtime.operation({"action":"report-failure","host":account.host,"provider":account.provider,
                    "account":account.alias,"login_generation":account.generation,"kind":"auth",
                    "_auth_context":{"token_verified":True,"stream_id":worker["stream_id"],
                        "session_generation":worker["session_generation"]}})
            assert runtime._self_recovery_marks=={(account.key,account.generation):revision}
            assert not attempts and not [call for call in calls if call[2]=="observe-bound"]
        finally:
            store.stop()
    asyncio.run(run())


def test_mark_revision_never_moves_back_and_unmarked_observer_cannot_create_one(tmp_path):
    async def run():
        store,runtime,_comms,_tmux,_worker,_meter=await delivery_fixture(tmp_path)
        account=binding(provider="claude")
        try:
            runtime._mark_auth_self_recovery(account,8)
            runtime._mark_auth_self_recovery(account,7)
            assert runtime._self_recovery_marks[(account.key,account.generation)]==8
            runtime._self_recovery_marks.clear()
            calls,attempts=_automatic_world(runtime,account,["ok"])
            result=await runtime.executor.observe(account)
            assert result["outcome"]=="ok"
            assert not attempts and not runtime._self_recovery_marks
            assert (account.key,account.generation,"observe") in calls
        finally:
            store.stop()
    asyncio.run(run())


def test_fake_clock_spacing_119_then_exactly_120_and_mark_stays_during_wait(tmp_path):
    async def run():
        store,runtime,_comms,_tmux,_worker,_meter=await delivery_fixture(tmp_path)
        account=binding(provider="claude")
        mono={"now":20.0}
        calls=[]
        try:
            await runtime.record_failure(account,"auth")
            revision=(await _health(runtime,account.key))[1]
            runtime.monotonic=lambda:mono["now"]
            async def execute(selected,action,operation_id,generation=None,**kwargs):
                calls.append(action)
                if action=="observe-bound":return {"outcome":"provider_probe_failed"}
                return {"outcome":"ok","binding_key":selected.key,"observation":observation(selected)}
            runtime.executor=AccountExecutor(runtime,None,execute=execute)
            runtime._mark_auth_self_recovery(account,revision)
            await runtime.executor.observe(account)
            assert calls==["observe-bound"]
            runtime._mark_auth_self_recovery(account,revision)
            mono["now"]=139.999
            await runtime.executor.observe(account)
            assert calls[-1]=="observe" and calls.count("observe-bound")==1
            assert runtime._self_recovery_marks[(account.key,account.generation)]==revision
            mono["now"]=140.0
            await runtime.executor.observe(account)
            assert calls[-1]=="observe-bound" and calls.count("observe-bound")==2
            assert not runtime._self_recovery_marks
        finally:
            store.stop()
    asyncio.run(run())


def test_cancellation_waiting_for_host_lock_preserves_mark(tmp_path):
    class HeldLock:
        def __init__(self):
            self.lock=asyncio.Lock();self.waiting=asyncio.Event()
        async def __aenter__(self):
            if self.lock.locked(): self.waiting.set()
            await self.lock.acquire()
            return self
        async def __aexit__(self,*_args): self.lock.release()

    async def run():
        store,runtime,_comms,_tmux,_worker,_meter=await delivery_fixture(tmp_path)
        account=binding(provider="claude")
        calls=[]
        try:
            await runtime.record_failure(account,"auth")
            revision=(await _health(runtime,account.key))[1]
            calls,attempts=_automatic_world(runtime,account,["ok"])
            lock=HeldLock();runtime.executor._host_locks[account.host]=lock
            async with lock:
                runtime._mark_auth_self_recovery(account,revision)
                task=asyncio.create_task(runtime.executor.observe(account))
                await lock.waiting.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):await task
            assert runtime._self_recovery_marks=={(account.key,account.generation):revision}
            assert not runtime._self_recovery_inflight and not attempts
            assert not [call for call in calls if call[2]=="observe-bound"]
        finally:
            store.stop()
    asyncio.run(run())


def test_cancellation_after_dispatch_spends_attempt_and_releases_ownership(tmp_path):
    async def run():
        store,runtime,_comms,_tmux,_worker,_meter=await delivery_fixture(tmp_path)
        account=binding(provider="claude")
        entered=asyncio.Event();remote=asyncio.Event();calls=[]
        mono={"now":500.0}
        try:
            await runtime.record_failure(account,"auth")
            runtime.monotonic=lambda:mono["now"]
            revision=(await _health(runtime,account.key))[1]
            runtime._mark_auth_self_recovery(account,revision)
            async def execute(selected,action,operation_id,generation=None,**kwargs):
                calls.append((action,operation_id))
                if action=="observe-bound":entered.set();await remote.wait()
                return {"outcome":"ok","binding_key":selected.key,"observation":observation(selected)}
            runtime.executor=AccountExecutor(runtime,None,execute=execute)
            task=asyncio.create_task(runtime.executor.observe(account))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
            assert len([row for row in calls if row[0]=="observe-bound"])==1
            assert not runtime._self_recovery_marks and not runtime._self_recovery_inflight
            assert runtime._self_recovery_last_dispatch[(account.key,account.generation)]==500.0
            assert not (await _recovery_state(runtime,account.key)).get("receipts")
            assert (await _health(runtime,account.key))[0]=="auth_unhealthy"
        finally:
            remote.set();store.stop()
    asyncio.run(run())


def test_explicit_probe_failure_keeps_account_ok_envelope_and_typed_inner_result(tmp_path):
    async def run():
        store,runtime,_comms,_tmux,_worker,_meter=await delivery_fixture(tmp_path)
        account=binding(provider="claude")
        try:
            runtime.policy["watchers"]={"fixture-watcher":{"host":"host-a"}}
            await runtime.record_failure(account,"auth")
            revision=(await _health(runtime,account.key))[1]
            runtime._mark_auth_self_recovery(account,revision)
            calls,attempts=_automatic_world(runtime,account,["provider_transport_failed"])
            result=await runtime.operation({"action":"probe","host":"host-a","provider":"claude",
                "account":"primary","login_generation":account.generation,
                "_auth_context":{"service_authenticated":True,"service_actor":"fixture-watcher"}})
            assert result["type"]=="account.ok" and result["accepted"] is True
            assert result["result"]=={"outcome":"provider_transport_failed","host":account.host,
                "provider":account.provider,"account":account.alias}
            assert tuple(await _health(runtime,account.key))==("auth_unhealthy",revision)
            assert len(attempts)==1 and calls.count((account.key,account.generation,"observe-bound"))==1
        finally:
            store.stop()
    asyncio.run(run())


def test_marked_probe_skips_usage_write_but_keeps_artifact_pin_proof(tmp_path,monkeypatch):
    async def run():
        import provider_cli_pin,provider_cli_artifact
        store,runtime,_comms,_tmux,_worker,_meter=await delivery_fixture(tmp_path)
        account=binding(provider="claude")
        try:
            await runtime.record_failure(account,"auth")
            await runtime.run(lambda ledger:(ledger.conn.execute("INSERT OR REPLACE INTO v2_account_observations VALUES (?,?,?)",
                (account.key,json.dumps({"provider_id":account.provider_id,"pool_id":account.pool_id,
                    "auth_mode":"subscription","collection":"ok","observed_at":123,"windows":{"five":{"pct":55,
                    "reset_at":9999999999,"reset_id":"prior"}}}),123)),ledger.conn.commit()))
            before=await runtime.run(lambda ledger:ledger.conn.execute(
                "SELECT data FROM v2_account_observations WHERE binding_key=?",(account.key,)).fetchone()[0])
            artifact={"native_sha256":account.executable_sha256}
            pin={"revision":4,"providers":{"claude":{"artifacts":{"darwin-arm64":artifact}}}}
            monkeypatch.setattr(provider_cli_pin,"check",lambda ledger,bound:pin)
            monkeypatch.setattr(provider_cli_pin,"digest",lambda _pin:"pin-digest")
            monkeypatch.setattr(provider_cli_artifact,"code_identity",lambda:"code-identity")
            proof={"outcome":"ok","binding_key":account.key,"platform":"darwin-arm64","artifact":artifact,
                "pin_revision":4,"pin_digest":"pin-digest","code_identity":"code-identity"}
            calls=[]
            async def execute(selected,action,operation_id,generation=None,**kwargs):
                calls.append((action,kwargs.get("pin")))
                if action!="observe-bound":raise AssertionError("substitution must not make a second observation")
                return {"outcome":"ok","binding_key":selected.key,"observation":observation(selected),
                    "artifact_proof":proof}
            runtime.executor=AccountExecutor(runtime,None,execute=execute)
            runtime._mark_auth_self_recovery(account,(await _health(runtime,account.key))[1])
            result=await runtime.executor.observe(account)
            after=await runtime.run(lambda ledger:ledger.conn.execute(
                "SELECT data FROM v2_account_observations WHERE binding_key=?",(account.key,)).fetchone()[0])
            all_verified=await runtime.run(lambda ledger:ledger.conn.execute(
                "SELECT k,v FROM kv WHERE k LIKE 'provider_cli.verified:%'").fetchall())
            verified=dict(all_verified).get("provider_cli.verified:"+account.key)
            assert result["outcome"]=="ok" and result["observation"]==observation(account)
            assert calls==[("observe-bound",pin)]
            assert before==after
            assert verified is not None,(recorded,all_verified)
            assert json.loads(verified)["proof"]==proof
            assert (await _health(runtime,account.key))[0]=="healthy"
            async def ordinary_execute(selected,action,operation_id,generation=None,**kwargs):
                calls.append((action,kwargs.get("pin")))
                return {"outcome":"ok","binding_key":selected.key,"observation":observation(selected)}
            runtime.executor.execute=ordinary_execute
            await runtime.executor.observe(account)
            refreshed=await runtime.run(lambda ledger:ledger.conn.execute(
                "SELECT data FROM v2_account_observations WHERE binding_key=?",(account.key,)).fetchone()[0])
            assert calls[-1][0]=="observe" and refreshed!=before
        finally:
            store.stop()
    asyncio.run(run())


def test_host_lock_delay_does_not_start_cooldown_before_actual_dispatch(tmp_path):
    class HeldLock:
        def __init__(self):self.lock=asyncio.Lock();self.waiting=asyncio.Event()
        async def __aenter__(self):
            if self.lock.locked():self.waiting.set()
            await self.lock.acquire();return self
        async def __aexit__(self,*_args):self.lock.release()
    async def run():
        store,runtime,_comms,_tmux,_worker,_meter=await delivery_fixture(tmp_path)
        account=binding(provider="claude");mono={"now":0.0};calls=[]
        try:
            await runtime.record_failure(account,"auth")
            revision=(await _health(runtime,account.key))[1]
            runtime.monotonic=lambda:mono["now"]
            async def execute(selected,action,operation_id,generation=None,**kwargs):
                calls.append(action)
                return {"outcome":"provider_probe_failed" if action=="observe-bound" else "ok",
                    "binding_key":selected.key,"observation":observation(selected)}
            runtime.executor=AccountExecutor(runtime,None,execute=execute)
            runtime._mark_auth_self_recovery(account,revision)
            await runtime.executor.observe(account)
            assert calls==["observe-bound"]
            runtime._mark_auth_self_recovery(account,revision)
            mono["now"]=119.0
            lock=HeldLock();runtime.executor._host_locks[account.host]=lock
            async with lock:
                task=asyncio.create_task(runtime.executor.observe(account))
                await lock.waiting.wait()
                mono["now"]=120.0
            await task
            assert calls.count("observe-bound")==2
            assert runtime._self_recovery_last_dispatch[(account.key,account.generation)]==120.0
            assert not runtime._self_recovery_marks
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("first_owner",["collector","explicit"])
def test_collector_and_explicit_observer_race_claim_one_mark(first_owner,tmp_path):
    async def run():
        store,runtime,_comms,_tmux,_worker,_meter=await delivery_fixture(tmp_path)
        account=binding(provider="claude")
        entered,release=asyncio.Event(),asyncio.Event()
        calls=[]
        try:
            await runtime.record_failure(account,"auth")
            revision=(await _health(runtime,account.key))[1]
            runtime._mark_auth_self_recovery(account,revision)
            async def execute(selected,action,operation_id,generation=None,**kwargs):
                calls.append((selected.key,action))
                if action=="observe-bound":
                    entered.set();await release.wait()
                return {"outcome":"provider_probe_failed" if action=="observe-bound" else "ok",
                    "binding_key":selected.key,"observation":observation(selected)}
            runtime.executor=AccountExecutor(runtime,None,execute=execute)
            if first_owner=="collector":
                runtime._next_probe=0;await runtime.tick();first=runtime._collector_task
                await entered.wait();second=asyncio.create_task(runtime.executor.observe(account))
            else:
                first=asyncio.create_task(runtime.executor.observe(account))
                await entered.wait();runtime._next_probe=0;await runtime.tick();second=runtime._collector_task
            release.set()
            await asyncio.gather(first,second)
            assert calls.count((account.key,"observe-bound"))==1
            assert calls.count((account.key,"observe"))==1
            assert runtime._self_recovery_inflight==set()
        finally:
            release.set();store.stop()
    asyncio.run(run())


def test_auth_negative_observation_uses_marked_revision_and_typed_response(tmp_path):
    async def run():
        store,runtime,_comms,_tmux,_worker,_meter=await delivery_fixture(tmp_path)
        account=binding(provider="claude")
        try:
            await runtime.record_failure(account,"auth")
            revision=(await _health(runtime,account.key))[1]
            runtime._mark_auth_self_recovery(account,revision)
            calls,attempts=_automatic_world(runtime,account,["provider_auth_failed"])
            fact=lambda:runtime.run(lambda ledger:tuple(ledger.conn.execute(
                "SELECT binding_key,kind,observed_at FROM v2_account_failures WHERE binding_key=?",(account.key,)).fetchone()))
            before=await fact()
            alerts=[]
            async def alert(selected,code): alerts.append(code)
            runtime._alert=alert
            result=await runtime.executor.observe(account)
            row=await _health(runtime,account.key)
            assert result=={"outcome":"provider_auth_failed","host":account.host,
                "provider":account.provider,"account":account.alias}
            # Automatic confirmation never counts the existing auth failure twice.
            assert tuple(row)==("auth_unhealthy",revision) and await fact()==before and alerts==[]
            assert len(attempts)==1 and calls.count((account.key,account.generation,"observe-bound"))==1
            assert not (await _recovery_state(runtime,account.key)).get("receipts")
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("evidence", ["wrong-key", "missing-observation", "failed-collection", "wrong-provider"])
def test_automatic_probe_rejects_invalid_bound_observation(tmp_path, evidence):
    async def run():
        store, runtime, comms, tmux, worker, _meter = await delivery_fixture(tmp_path)
        account = binding(provider="claude")
        try:
            await runtime.run(lambda ledger: (ledger.conn.execute(
                "DELETE FROM kv WHERE k=?", ("account.pool-control:claude:pool-primary",)), ledger.conn.commit()))
            await runtime.record_failure(account, "auth")
            before = tuple(await _health(runtime, account.key))
            def invalid(selected, _action, _operation_id, _generation):
                if evidence == "wrong-key":
                    return {"outcome": "ok", "binding_key": "wrong-generation", "observation": observation(selected)}
                if evidence == "missing-observation":
                    return {"outcome": "ok", "binding_key": selected.key}
                if evidence == "failed-collection":
                    value = observation(selected); value["collection"] = "failed"
                else:
                    value = observation(selected); value["provider_id"] = "other-provider"
                return {"outcome": "ok", "binding_key": selected.key, "observation": value}
            calls, attempts = _automatic_world(runtime, account, [invalid])
            msg = {"stream_id": worker["stream_id"], "message": "invalid proof",
                "from_stream_id": PEER["stream_id"], "_auth_context": PEER,
                "tell_id": "invalid-proof", "request_id": "invalid-proof"}
            with pytest.raises(VerbError, match="account_unavailable"):
                await _external(comms.tell, "tell", msg)
            await _collector_pass(runtime)
            assert len(attempts) == 1 and tuple(await _health(runtime, account.key)) == before
            assert not (await _recovery_state(runtime, account.key)).get("receipts")
            assert "enroll" not in [call[2] for call in calls] and tmux.pastes == []
        finally:
            store.stop()
    asyncio.run(run())


def test_automatic_path_uses_host_exit_decoder_and_shared_validator(tmp_path, monkeypatch):
    async def run():
        import account_executor as executor_module
        store, runtime, comms, tmux, worker, _meter = await delivery_fixture(tmp_path)
        account = binding(provider="claude")
        commands=[];validated=[]
        try:
            await runtime.run(lambda ledger: (ledger.conn.execute(
                "DELETE FROM kv WHERE k=?", ("account.pool-control:claude:pool-primary",)), ledger.conn.commit()))
            await runtime.record_failure(account, "auth")
            runtime.policy["executors"]={"host-a":{"python":"/fixture/python","script":"/fixture/host.py"}}
            original=executor_module._valid_observation
            def validate(selected, value):
                validated.append(selected.key)
                return original(selected,value)
            monkeypatch.setattr(executor_module,"_valid_observation",validate)
            class Hosts:
                async def run_command(self,host,*argv,timeout):
                    commands.append((host,argv,timeout))
                    value={"outcome":"ok","binding_key":account.key,"observation":observation(account)}
                    return (1 if argv[2]=="observe-bound" else 0),json.dumps(value)
            runtime.executor=AccountExecutor(runtime,Hosts())
            msg={"stream_id":worker["stream_id"],"message":"decoder control",
                 "from_stream_id":PEER["stream_id"],"_auth_context":PEER,
                 "tell_id":"decoder-control","request_id":"decoder-control"}
            with pytest.raises(VerbError,match="account_unavailable"):
                await _external(comms.tell, "tell", msg)
            await _collector_pass(runtime)
            bound=[item for item in commands if item[1][2]=="observe-bound"]
            assert len(bound)==1 and bound[0][2]==200
            assert not validated  # nonzero successful-looking host response fails before validation
            assert (await _health(runtime,account.key))[0]=="auth_unhealthy"
            assert not (await _recovery_state(runtime,account.key)).get("receipts")
            assert tmux.pastes==[]
        finally:
            store.stop()
    asyncio.run(run())
