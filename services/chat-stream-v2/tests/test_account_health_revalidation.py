"""Explicit, audited health repair through the authenticated account boundary."""
import asyncio
import json
import sqlite3
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from account_executor import AccountExecutor
from account_runtime import AccountRuntime
from account_store import AccountStore
from provider_accounts import AccountError
from sessions import Sessions, VerbError
from store import Store
from test_account_rotation import binding, observation, NOW
from test_account_runtime import AUTH, setup


async def fixture(path, provider="codex"):
    store, runtime = await setup(str(path))
    await runtime.start()
    account = binding(provider=provider)
    if provider != "codex":
        await runtime.run(lambda ledger: ledger.register(account))
        await runtime.run(lambda ledger: ledger.set_default(host=account.host, provider=provider, alias=account.alias))
    calls = []
    async def execute(selected, action, operation_id, generation=None):
        calls.append((selected.key, action))
        assert action == "observe" and generation is None  # never enroll/status/login
        return {"outcome": "ok", "binding_key": selected.key, "observation": observation(selected)}
    runtime.executor = AccountExecutor(runtime, None, execute=execute)
    await runtime.record_failure(account, "identity")
    return store, runtime, account, calls


async def state(runtime, account):
    result = await runtime.operation({"action": "status", "host": account.host,
        "provider": account.provider, "account": account.alias, "_auth_context": AUTH})
    return next(row for row in result["accounts"] if row["generation"] == account.generation)


async def request(runtime, account, operation_id="repair-one", **overrides):
    row = await state(runtime, account)
    return dict({"action": "revalidate-health", "host": account.host, "provider": account.provider,
        "account": account.alias, "login_generation": account.generation,
        "expected_revision": row.get("health_revision", 0), "operation_id": operation_id,
        "reason": "stale_cache_misclassification", "_auth_context": AUTH}, **overrides)


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_fresh_plain_probe_cannot_heal_but_explicit_fd_operation_can(tmp_path, provider):
    async def journey():
        store, runtime, account, calls = await fixture(tmp_path / "state.db", provider)
        try:
            before = await state(runtime, account)
            defaults = await runtime.run(lambda ledger: ledger.defaults())
            assert (await runtime.executor.observe(account))["outcome"] == "ok"
            assert (await state(runtime, account))["health"] == "identity_conflict"
            with pytest.raises(AccountError, match="account_unavailable"):
                await runtime.run(lambda ledger: ledger.set_default(host=account.host,
                    provider=account.provider, alias=account.alias))
            reply = await runtime.operation(await request(runtime, account))
            assert reply["result"]["outcome"] == "health_revalidated"
            after = await state(runtime, account)
            assert after["health"] == "healthy" and after["health_revision"] == before["health_revision"] + 1
            assert after["last_failure"] == before["last_failure"]
            assert await runtime.run(lambda ledger: ledger.defaults()) == defaults
            assert (await runtime.run(lambda ledger: ledger.select(host=account.host,
                provider=account.provider))).key == account.key
            assert calls == [(account.key, "observe")] * 2
        finally:
            store.stop()
    asyncio.run(journey())


@pytest.mark.parametrize("auth", [{}, dict(AUTH, token_verified=False), dict(AUTH, session_generation="old"),
    dict(AUTH, stream_id="host-a:worker"), {"role": "front-desk"},
    {"service_authenticated": True, "service_actor": "host-a:watcher"}])
def test_revalidation_requires_current_fd_before_host_or_audit_access(tmp_path, auth):
    async def journey():
        store, runtime, account, calls = await fixture(tmp_path / "state.db")
        try:
            message = await request(runtime, account, _auth_context=auth)
            with pytest.raises(VerbError, match="account_control_unauthorized"):
                await runtime.operation(message)
            assert not calls and (await state(runtime, account))["health"] == "identity_conflict"
        finally:
            store.stop()
    asyncio.run(journey())


def test_status_revision_fences_same_kind_same_time_and_masked_auth(tmp_path):
    async def journey():
        store, runtime, account, _ = await fixture(tmp_path / "state.db")
        try:
            first = await state(runtime, account)
            assert "health_revision" in first
            for kind in ("identity", "identity", "auth"):
                await runtime.record_failure(account, kind)
            after = await state(runtime, account)
            assert after["health"] == "identity_conflict"
            assert after["health_revision"] == first["health_revision"] + 3
            assert after["last_failure"] == {"kind": "auth", "observed_at": NOW}
            reply = await runtime.operation(await request(runtime, account))
            assert reply["result"]["outcome"] == "health_revalidated"
            assert (await state(runtime, account))["last_failure"] == after["last_failure"]
        finally:
            store.stop()
    asyncio.run(journey())


def test_cli_transmits_exact_revision_operation_id_and_budget(monkeypatch):
    from agent_orch import cli, config, stream_id, wsclient
    monkeypatch.setattr(config, "load_config", lambda: object())
    monkeypatch.setattr(stream_id, "discover_leader_stream_id_short", lambda _: AUTH["stream_id"])
    seen = []
    async def rpc(_config, payload, **kwargs):
        seen.append((payload, kwargs["timeout"]))
        return {"type": "account.ok"}
    monkeypatch.setattr(wsclient, "account_once", rpc)
    base = ["account", "revalidate-health", "--host", "host-a", "--provider", "codex", "--account", "primary",
        "--login-generation", "login-one", "--expected-revision", "7", "--operation-id", "repair-one",
        "--reason", "stale_cache_misclassification"]
    for extra in ([], ["--timeout", "9"]):
        args = cli.build_parser().parse_args(base + extra)
        assert args.func(args) == 0
    assert [timeout for _, timeout in seen] == [210, 9]
    assert all(p["expected_revision"] == 7 and p["operation_id"] == "repair-one"
        and p["login_generation"] == "login-one" for p, _ in seen)


@pytest.mark.parametrize("overrides", [
    {"expected_revision": None}, {"expected_revision": True}, {"expected_revision": -1},
    {"expected_revision": 0}, {"login_generation": "active"}, {"login_generation": "old-login"},
    {"operation_id": ""}, {"operation_id": "x" * 161}, {"reason": None}, {"reason": "\nprivate"},
])
def test_invalid_or_stale_payload_refuses_before_native_or_intent(tmp_path, overrides):
    async def journey():
        store, runtime, account, calls = await fixture(tmp_path / "state.db")
        try:
            with pytest.raises(VerbError):
                await runtime.operation(await request(runtime, account, **overrides))
            assert not calls
            assert await runtime.run(lambda a: a.conn.execute(
                "SELECT count(*) FROM v2_account_health_revalidations").fetchone()[0]) == 0
        finally:
            store.stop()
    asyncio.run(journey())


@pytest.mark.parametrize("condition", ["off", "unmanaged", "disabled", "inactive", "healthy", "auth_unhealthy", "unknown"])
def test_ineligible_cells_do_not_invoke_native(tmp_path, condition):
    async def journey():
        store, runtime, account, calls = await fixture(tmp_path / "state.db")
        try:
            message = await request(runtime, account)
            if condition == "off": runtime.policy["enabled"] = False
            elif condition == "unmanaged": runtime.policy["managed_hosts"] = []
            else:
                def change(ledger):
                    if condition == "disabled":
                        ledger.conn.execute("UPDATE v2_account_bindings SET data=? WHERE binding_key=?",
                            (json.dumps(asdict(replace(account, enabled=False))), account.key))
                    elif condition == "inactive":
                        ledger.conn.execute("UPDATE v2_account_bindings SET active=0 WHERE binding_key=?", (account.key,))
                    else:
                        ledger.conn.execute("UPDATE v2_account_bindings SET health=? WHERE binding_key=?", (condition, account.key))
                    ledger.conn.commit()
                await runtime.run(change)
            with pytest.raises(VerbError): await runtime.operation(message)
            assert not calls
        finally:
            store.stop()
    asyncio.run(journey())


@pytest.mark.parametrize("outcome", ["provider_cache_stale", "provider_cache_unavailable", "provider_auth_failed",
    "provider_identity_or_cache_unverified", "provider_windows_invalid", "provider_timeout", "account_host_unreachable"])
def test_failed_observation_never_revalidates_or_retries(tmp_path, outcome):
    async def journey():
        store, runtime, account, calls = await fixture(tmp_path / "state.db")
        try:
            async def failed(*args):
                calls.append("observe")
                return {"outcome": outcome}
            runtime.executor.execute = failed
            message = await request(runtime, account)
            result = (await runtime.operation(message))["result"]
            assert result["outcome"] == outcome and result["applied_revision"] is None
            assert (await state(runtime, account))["health"] == "identity_conflict"
            assert (await runtime.operation(message))["result"]["receipt_replay"] is True
            assert calls == ["observe"]
        finally:
            store.stop()
    asyncio.run(journey())


@pytest.mark.parametrize("corruption", ["pool", "provider", "auth_mode", "windows", "nan", "stamp"])
def test_host_ok_requires_matched_daemon_observation_and_valid_windows(tmp_path, corruption):
    async def journey():
        store, runtime, account, calls = await fixture(tmp_path / "state.db")
        try:
            async def bad(selected, *args):
                calls.append("observe")
                obs = observation(selected)
                if corruption == "pool": obs["pool_id"] = "other-pool"
                elif corruption == "provider": obs["provider_id"] = "other-identity"
                elif corruption == "auth_mode": obs["auth_mode"] = "api_key"
                elif corruption == "windows": obs["windows"] = {}
                elif corruption == "nan": obs["windows"]["weekly"]["pct"] = float("nan")
                else: obs["observed_at"] = None
                return {"outcome": "ok", "binding_key": selected.key, "observation": obs}
            runtime.executor.execute = bad
            result = (await runtime.operation(await request(runtime, account)))["result"]
            assert result["outcome"] != "health_revalidated"
            assert (await state(runtime, account))["health"] == "identity_conflict" and calls == ["observe"]
        finally:
            store.stop()
    asyncio.run(journey())


def test_replay_is_historical_after_new_failure_and_fd_handoff(tmp_path):
    async def journey():
        store, runtime, account, calls = await fixture(tmp_path / "state.db")
        try:
            message = await request(runtime, account)
            first = (await runtime.operation(message))["result"]
            assert first["outcome"] == "health_revalidated"
            await runtime.record_failure(account, "identity")
            changed = await state(runtime, account)
            again = (await runtime.operation(message))["result"]
            assert again == dict(first, receipt_replay=True)
            assert (await state(runtime, account)) == changed and len(calls) == 1
            with pytest.raises(VerbError, match="account_revalidation_id_conflict"):
                await runtime.operation(dict(message, reason="different-reason"))
            new = dict(AUTH, stream_id="host-a:front-desk-two", session_generation="generation-two")
            await store.open_session("host-a", "front-desk-two", session_generation="generation-two", provider="codex")
            runtime.server.assistant_composite._env_binding = lambda: {"stream_id": new["stream_id"], "generation": new["session_generation"]}
            assert (await runtime.operation(dict(message, _auth_context=new)))["result"] == again
            with pytest.raises(VerbError, match="account_control_unauthorized"):
                await runtime.operation(message)
            audit = await runtime.run(lambda a: json.loads(a.conn.execute(
                "SELECT data FROM v2_account_health_revalidations WHERE operation_id='repair-one'").fetchone()[0]))
            assert audit["requested_by"] == {"stream_id": AUTH["stream_id"], "generation": AUTH["session_generation"]}
            assert len(calls) == 1
        finally:
            store.stop()
    asyncio.run(journey())


@pytest.mark.parametrize("change", ["same-failure", "generation", "disable", "inactive", "off", "unmanaged"])
def test_concurrent_state_change_wins_over_an_old_proof(tmp_path, change):
    async def journey():
        store, runtime, account, calls = await fixture(tmp_path / "state.db")
        entered, release = asyncio.Event(), asyncio.Event()
        async def held(selected, *args):
            calls.append("observe"); entered.set(); await release.wait()
            return {"outcome": "ok", "binding_key": selected.key, "observation": observation(selected)}
        runtime.executor.execute = held
        task = None
        try:
            message = await request(runtime, account)
            task = asyncio.create_task(runtime.operation(message))
            await asyncio.wait_for(entered.wait(), 2)
            if change == "same-failure": await runtime.record_failure(account, "identity")  # same kind, same NOW
            elif change == "generation":
                await runtime.run(lambda a: a.register(replace(account, generation="login-two", home="/private-fixture/login-two")))
            elif change == "off": runtime.policy["enabled"] = False
            elif change == "unmanaged": runtime.policy["managed_hosts"] = []
            else:
                def mutate(a):
                    if change == "disable":
                        a.conn.execute("UPDATE v2_account_bindings SET data=? WHERE binding_key=?",
                            (json.dumps(asdict(replace(account, enabled=False))), account.key))
                    else: a.conn.execute("UPDATE v2_account_bindings SET active=0 WHERE binding_key=?", (account.key,))
                    a.conn.commit()
                await runtime.run(mutate)
            release.set()
            result = (await task)["result"]
            assert result["outcome"] == "account_revalidation_state_changed"
            assert await runtime.run(lambda a: a.conn.execute(
                "SELECT health FROM v2_account_bindings WHERE binding_key=?", (account.key,)).fetchone()[0]) == "identity_conflict"
            assert calls == ["observe"]
        finally:
            release.set()
            if task and not task.done(): task.cancel(); await asyncio.gather(task, return_exceptions=True)
            store.stop()
    asyncio.run(journey())


def test_audit_failure_rolls_back_health_and_revision(tmp_path):
    async def journey():
        store, runtime, account, calls = await fixture(tmp_path / "state.db")
        try:
            before = await state(runtime, account)
            def fail_audit(a):
                a.conn.execute("CREATE TRIGGER refuse_health_audit BEFORE UPDATE ON v2_account_health_revalidations "
                    "WHEN NEW.state='done' BEGIN SELECT RAISE(ABORT,'fixture audit failure'); END")
                a.conn.commit()
            await runtime.run(fail_audit)
            result = (await runtime.operation(await request(runtime, account)))["result"]
            assert result["outcome"] == "revalidation_unresolved"
            after = await state(runtime, account)
            assert (after["health"], after["health_revision"]) == (before["health"], before["health_revision"])
            assert len(calls) == 1
        finally:
            store.stop()
    asyncio.run(journey())


def test_cancellation_records_interrupted_receipt_without_provider_replay(tmp_path):
    async def journey():
        store, runtime, account, calls = await fixture(tmp_path / "state.db")
        entered = asyncio.Event()
        async def held(*args):
            calls.append("observe"); entered.set(); await asyncio.Event().wait()
        runtime.executor.execute = held
        task = None
        try:
            message = await request(runtime, account)
            task = asyncio.create_task(runtime.operation(message)); await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            assert (await state(runtime, account))["health"] == "identity_conflict"
            result = (await runtime.operation(message))["result"]
            assert result["outcome"] == "revalidation_interrupted" and result["receipt_replay"] is True
            assert calls == ["observe"]
        finally:
            if task and not task.done(): task.cancel(); await asyncio.gather(task, return_exceptions=True)
            store.stop()
    asyncio.run(journey())


def test_runtime_restart_terminalizes_pending_only_and_fences_late_old_completion(tmp_path):
    async def journey():
        path = tmp_path / "state.db"
        store, runtime, account, calls = await fixture(path)
        entered, release = asyncio.Event(), asyncio.Event()
        async def held(selected, *args):
            calls.append("old-observe"); entered.set(); await release.wait()
            return {"outcome": "ok", "binding_key": selected.key, "observation": observation(selected)}
        runtime.executor.execute = held
        task = None
        try:
            message = await request(runtime, account)
            before = await state(runtime, account); defaults = await runtime.run(lambda a: a.defaults())
            task = asyncio.create_task(runtime.operation(message)); await asyncio.wait_for(entered.wait(), 2)
            # A mere Store connection open never interrupts an intent.
            connection = sqlite3.connect(path); connection.row_factory = sqlite3.Row
            AccountStore(connection, runtime.policy)
            assert connection.execute("SELECT state FROM v2_account_health_revalidations").fetchone()[0] == "pending"
            connection.close()
            composite = runtime.server.assistant_composite
            store.stop(); store = Store(str(path)); store.start()
            restarted = AccountRuntime(store, Sessions(store, local_host="host-a"), policy={"enabled": False}, clock=lambda: NOW)
            restarted.attach(SimpleNamespace(assistant_composite=composite, handlers={}), SimpleNamespace(), SimpleNamespace())
            await restarted.start()
            current = await state(restarted, account)
            assert (current["health"], current["health_revision"]) == (before["health"], before["health_revision"])
            assert await restarted.run(lambda a: a.defaults()) == defaults
            row = await restarted.run(lambda a: dict(a.conn.execute("SELECT * FROM v2_account_health_revalidations").fetchone()))
            assert row["state"] == "interrupted"
            runtime.store = store  # simulate a late callback against the restarted ledger
            release.set()
            assert (await task)["result"]["outcome"] == "revalidation_interrupted"
            restarted.policy["enabled"] = True
            async def fresh(selected, *args):
                calls.append("new-observe")
                return {"outcome": "ok", "binding_key": selected.key, "observation": observation(selected)}
            restarted.executor = AccountExecutor(restarted, None, execute=fresh)
            assert (await restarted.operation(message))["result"]["outcome"] == "revalidation_interrupted"
            result = (await restarted.operation(await request(restarted, account, operation_id="new-id")))["result"]
            assert result["outcome"] == "health_revalidated" and calls == ["old-observe", "new-observe"]
            store.stop(); store = Store(str(path)); store.start(); restarted.store = store
            assert (await restarted.operation(await request(restarted, account, operation_id="new-id",
                expected_revision=before["health_revision"])))["result"]["receipt_replay"] is True
            assert calls == ["old-observe", "new-observe"]
        finally:
            release.set()
            if task and not task.done(): task.cancel(); await asyncio.gather(task, return_exceptions=True)
            store.stop()
    asyncio.run(journey())


def test_retained_pre_amendment_store_migrates_twice_without_healing(tmp_path):
    async def seed(path):
        store, runtime, account, _ = await fixture(path)
        before = (await state(runtime, account))["last_failure"]
        store.stop()
        return account, before
    path = tmp_path / "state.db"
    account, failure = asyncio.run(seed(path))
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE v2_account_health_revalidations")
        conn.execute("ALTER TABLE v2_account_bindings DROP COLUMN health_revision")
        before = conn.execute("SELECT * FROM v2_account_bindings ORDER BY binding_key").fetchall()
        defaults = conn.execute("SELECT * FROM v2_account_defaults").fetchall()
    for _ in range(2):
        store = Store(str(path)); store.start(); store.stop()
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT binding_key,provider,alias,host,generation,pool_id,data,health,active "
                "FROM v2_account_bindings ORDER BY binding_key").fetchall() == before
            assert conn.execute("SELECT * FROM v2_account_defaults").fetchall() == defaults
            assert {row[0] for row in conn.execute("SELECT health_revision FROM v2_account_bindings")} == {0}
            assert conn.execute("SELECT count(*) FROM v2_account_health_revalidations").fetchone()[0] == 0
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT kind,observed_at FROM v2_account_failures WHERE binding_key=?",
                (account.key,)).fetchone() == (failure["kind"], failure["observed_at"])


def test_host_slot_serializes_revalidation_and_another_host_progresses(tmp_path):
    async def journey():
        store, runtime, account, _ = await fixture(tmp_path / "state.db")
        peer = binding(host="host-b"); spare = binding("spare")
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []; active = {}; maximum = {}
        async def execute(selected, *args):
            calls.append(selected.key)
            active[selected.host] = active.get(selected.host, 0) + 1
            maximum[selected.host] = max(maximum.get(selected.host, 0), active[selected.host])
            try:
                if selected.key == account.key: entered.set(); await release.wait()
                return {"outcome": "ok", "binding_key": selected.key, "observation": observation(selected)}
            finally: active[selected.host] -= 1
        runtime.executor.execute = execute
        tasks = []
        try:
            await runtime.run(lambda a: a.register(peer))
            for chosen in (peer, spare): await runtime.record_failure(chosen, "identity")
            primary_message = await request(runtime, account)
            tasks.append(asyncio.create_task(runtime.operation(primary_message)))
            await asyncio.wait_for(entered.wait(), 2)
            tasks.append(asyncio.create_task(runtime.operation(await request(runtime, spare, operation_id="spare-id"))))
            result = await asyncio.wait_for(runtime.operation(await request(runtime, peer, operation_id="peer-id")), 2)
            assert result["result"]["outcome"] == "health_revalidated"
            assert spare.key not in calls and not tasks[1].done()
            release.set()
            assert all(r["result"]["outcome"] == "health_revalidated" for r in await asyncio.gather(*tasks))
            assert maximum == {"host-a": 1, "host-b": 1}
        finally:
            release.set()
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            store.stop()
    asyncio.run(journey())


def test_real_transport_empty_timeout_refusal_is_not_an_identity_failure(tmp_path):
    async def journey():
        store, runtime, account, _ = await fixture(tmp_path / "state.db")
        seen = []
        class Hosts:
            async def run_command(self, host, *args, **kwargs):
                seen.append((host, args, kwargs))
                return 1, ""  # actual tmux_transport._exec timeout contract
        runtime.policy["executors"] = {account.host: {"python": "/fixture/python", "script": "/fixture/host.py"}}
        runtime.executor = AccountExecutor(runtime, Hosts())
        try:
            before = await state(runtime, account)
            result = (await runtime.operation(await request(runtime, account)))["result"]
            assert result["outcome"] == "account_host_unreachable"
            assert len(seen) == 1 and seen[0][2]["timeout"] == 200 and "observe" in seen[0][1]
            after = await state(runtime, account)
            assert (after["health"], after["health_revision"]) == (before["health"], before["health_revision"])
        finally: store.stop()
    asyncio.run(journey())


@pytest.mark.parametrize("missing", ["--operation-id", "--expected-revision", "--reason", "--login-generation"])
def test_cli_requires_explicit_revalidation_inputs_without_rpc(monkeypatch, missing):
    from agent_orch import cli, wsclient
    async def forbidden(*args, **kwargs): raise AssertionError("RPC must not run")
    monkeypatch.setattr(wsclient, "account_once", forbidden)
    base = ["account", "revalidate-health", "--host", "host-a", "--provider", "codex", "--account", "primary",
        "--login-generation", "login-one", "--expected-revision", "1", "--operation-id", "repair-one", "--reason", "manual"]
    index = base.index(missing); del base[index:index+2]
    args = cli.build_parser().parse_args(base)
    assert args.func(args) == 2
