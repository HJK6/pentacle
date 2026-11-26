"""Self-recovery repair r2: winning write, evidence, ingress and lifecycle.

Real pin rows, a real reopened Store, real server dispatch and explicit barriers.
No assertion depends on how long healthy work took.
"""
import asyncio
import json
import threading

import pytest

from account_executor import AccountExecutor
from provider_accounts import AccountError
from test_account_rotation import binding, observation
from test_account_runtime import setup


async def _world(path, *, pin=False):
    store, runtime = await setup(str(path), initialize_pin=pin)
    account = binding(provider="claude")
    await runtime.run(lambda a: a.register(account))
    await runtime.record_failure(account, "auth")
    revision = await _revision(runtime, account)
    runtime._mark_auth_self_recovery(account, revision)
    return store, runtime, account, revision


async def _revision(runtime, account):
    return (await _health(runtime, account))[1]


async def _health(runtime, account):
    return tuple(await runtime.run(lambda a: a.conn.execute(
        "SELECT health,health_revision FROM v2_account_bindings WHERE binding_key=?", (account.key,)).fetchone()))


async def _receipts(runtime, account):
    state = await runtime.run(lambda a: AccountExecutor._read_recovery(a, account.key))
    return state.get("receipts", {})


async def _proof(runtime, account):
    row = await runtime.run(lambda a: a.conn.execute(
        "SELECT v FROM kv WHERE k=?", ("provider_cli.verified:" + account.key,)).fetchone())
    return row[0] if row else None


async def _current_pin(runtime):
    from provider_cli_pin import current
    return await runtime.run(current)


def _artifact_proof(account, pin):
    from provider_cli_artifact import code_identity
    from provider_cli_pin import digest
    artifact = pin["providers"][account.provider]["artifacts"]["darwin-arm64"]
    return {"outcome": "ok", "binding_key": account.key, "platform": "darwin-arm64", "artifact": artifact,
            "pin_revision": pin["revision"], "pin_digest": digest(pin), "code_identity": code_identity()}


def _bound_host(runtime, during=None, *, reply=None):
    """A host whose single bound call runs ``during`` before it answers."""
    calls = []

    async def execute(selected, action, operation_id, generation=None, **kwargs):
        calls.append((action, kwargs.get("pin")))
        if during is not None:
            await during()
        if reply is not None:
            return reply(selected)
        result = {"outcome": "ok", "binding_key": selected.key, "observation": observation(selected)}
        if kwargs.get("pin"):
            result["artifact_proof"] = _artifact_proof(selected, kwargs["pin"])
        return result
    runtime.executor = AccountExecutor(runtime, None, execute=execute)
    return calls


def _second_pair():
    from provider_cli_fixtures import release_pair
    pair = release_pair()
    for artifact in pair["providers"]["codex"]["artifacts"].values():
        artifact["provenance"] += "-replacement"
    return pair


# -- F5 / F8: one winning write -------------------------------------------------

def test_stable_configured_pin_commits_proof_health_and_receipt_together(tmp_path):
    async def run():
        store, runtime, account, revision = await _world(tmp_path / "s.db", pin=True)
        try:
            calls = _bound_host(runtime)
            result = await runtime.executor.observe(account)
            pin = await _current_pin(runtime)
            assert result["outcome"] == "ok"
            assert calls == [("observe-bound", pin)]
            assert await _health(runtime, account) == ("healthy", revision + 1)
            assert len(await _receipts(runtime, account)) == 1
            assert json.loads(await _proof(runtime, account))["proof"] == _artifact_proof(account, pin)
        finally:
            store.stop()
    asyncio.run(run())


def test_pin_installed_during_pristine_call_writes_nothing(tmp_path):
    async def run():
        from provider_cli_fixtures import seed_pin
        store, runtime, account, revision = await _world(tmp_path / "p.db")
        try:
            calls = _bound_host(runtime, during=lambda: seed_pin(runtime))
            result = await runtime.executor.observe(account)
            assert calls == [("observe-bound", None)]
            assert result["outcome"] == "provider_cli_pin_stale"
            assert await _health(runtime, account) == ("auth_unhealthy", revision)
            assert await _receipts(runtime, account) == {} and await _proof(runtime, account) is None
        finally:
            store.stop()
    asyncio.run(run())


def test_configured_pin_replaced_during_call_keeps_prior_proof_bytes(tmp_path):
    async def run():
        from provider_cli_fixtures import seed_pin
        store, runtime, account, revision = await _world(tmp_path / "r.db", pin=True)
        try:
            prior = json.dumps({"proof": {"prior": True}, "verified_at": 1})
            await runtime.run(lambda a: (a.conn.execute("INSERT INTO kv VALUES (?,?)",
                ("provider_cli.verified:" + account.key, prior)), a.conn.commit()))
            _bound_host(runtime, during=lambda: seed_pin(runtime, _second_pair()))
            result = await runtime.executor.observe(account)
            assert result["outcome"] == "provider_cli_pin_stale"
            assert await _health(runtime, account) == ("auth_unhealthy", revision)
            assert await _receipts(runtime, account) == {}
            assert await _proof(runtime, account) == prior
        finally:
            store.stop()
    asyncio.run(run())


def test_newer_health_revision_during_call_writes_nothing(tmp_path):
    async def run():
        store, runtime, account, revision = await _world(tmp_path / "h.db", pin=True)
        try:
            _bound_host(runtime, during=lambda: runtime.record_failure(account, "auth"))
            result = await runtime.executor.observe(account)
            assert result["outcome"] == "account_health_revision_stale"
            assert await _health(runtime, account) == ("auth_unhealthy", revision + 1)
            assert await _receipts(runtime, account) == {} and await _proof(runtime, account) is None
        finally:
            store.stop()
    asyncio.run(run())


def test_winning_transaction_uses_only_keyed_statements(tmp_path):
    async def run():
        store, runtime, account, _revision = await _world(tmp_path / "t.db", pin=True)
        statements = []
        try:
            _bound_host(runtime)
            original = runtime.executor._finish_self_recovery

            def traced(ledger, *args):
                # Trace every statement the winner issues, including indirect calls.
                ledger.conn.set_trace_callback(statements.append)
                try:
                    return original(ledger, *args)
                finally:
                    ledger.conn.set_trace_callback(None)
            runtime.executor._finish_self_recovery = traced
            assert (await runtime.executor.observe(account))["outcome"] == "ok"
        finally:
            store.stop()
        assert statements[0] == "BEGIN IMMEDIATE" and statements[-1] == "COMMIT"
        body = statements[1:-1]
        assert body and all("substr(" not in s and "LIKE" not in s for s in body)
        assert all(" WHERE " in s for s in body if s.startswith(("SELECT", "UPDATE")))
        assert all(s.startswith(("SELECT", "UPDATE", "INSERT OR REPLACE")) for s in body)
    asyncio.run(run())


def test_same_host_writer_waits_until_the_settled_commit(tmp_path):
    async def run():
        store, runtime, account, revision = await _world(tmp_path / "l.db", pin=True)
        order = []
        try:
            lock = None

            async def contender():
                async with lock:
                    order.append(("repin-writer", await _health(runtime, account)))

            started = []

            async def during():
                nonlocal lock
                lock = runtime.executor._host_locks[account.host]
                started.append(asyncio.create_task(contender()))
                await asyncio.sleep(0)
                order.append("bound-call-returns")
            _bound_host(runtime, during=during)
            await runtime.executor.observe(account)
            order.append("observe-returned")
            await started[0]
            assert order == ["bound-call-returns", "observe-returned",
                             ("repin-writer", ("healthy", revision + 1))]
        finally:
            store.stop()
    asyncio.run(run())


def test_cancel_during_winning_write_settles_before_lock_release_and_survives_reopen(tmp_path):
    path = tmp_path / "c.db"

    async def first():
        store, runtime, account, revision = await _world(path, pin=True)
        gate, entered = threading.Event(), threading.Event()
        try:
            _bound_host(runtime)
            original = runtime.executor._finish_self_recovery

            def held(ledger, *args):
                entered.set()
                assert gate.wait(30), "stuck: winner barrier never released"
                return original(ledger, *args)
            runtime.executor._finish_self_recovery = held
            task = asyncio.create_task(runtime.executor.observe(account))
            while not entered.is_set():
                await asyncio.sleep(0)
            task.cancel()
            for _ in range(5):
                await asyncio.sleep(0)
            lock = runtime.executor._host_locks[account.host]
            assert lock.locked() and not task.done()
            gate.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not lock.locked()
            return account, revision
        finally:
            gate.set()
            store.stop()

    async def reopen(account, revision):
        store, runtime = await setup(str(path), initialize_pin=False)
        try:
            assert await _health(runtime, account) == ("healthy", revision + 1)
            assert len(await _receipts(runtime, account)) == 1
            assert await _proof(runtime, account) is not None
        finally:
            store.stop()
    account, revision = asyncio.run(first())
    asyncio.run(reopen(account, revision))


def test_loss_before_commit_leaves_nothing_after_reopen(tmp_path, monkeypatch):
    path = tmp_path / "loss.db"

    async def first():
        store, runtime, account, revision = await _world(path, pin=True)
        try:
            _bound_host(runtime)

            def dies(*_args):
                raise RuntimeError("writer process lost before commit")
            monkeypatch.setattr(AccountExecutor, "_write_recovery", staticmethod(dies))
            result = await runtime.executor.observe(account)
            monkeypatch.undo()
            assert result["outcome"] == "provider_probe_failed"
            return account, revision
        finally:
            store.stop()

    async def reopen(account, revision):
        store, runtime = await setup(str(path), initialize_pin=False)
        try:
            assert await _health(runtime, account) == ("auth_unhealthy", revision)
            assert await _receipts(runtime, account) == {} and await _proof(runtime, account) is None
        finally:
            store.stop()
    account, revision = asyncio.run(first())
    asyncio.run(reopen(account, revision))


@pytest.mark.parametrize("state", ["marker-lost", "raw-absent-with-history"])
def test_inconsistent_pin_state_is_typed_and_never_backfilled(tmp_path, state):
    async def run():
        from provider_cli_pin import INITIALIZED, KEY
        store, runtime, account, revision = await _world(tmp_path / "i.db", pin=True)
        try:
            removed = INITIALIZED if state == "marker-lost" else KEY
            await runtime.run(lambda a: (a.conn.execute("DELETE FROM kv WHERE k=?", (removed,)), a.conn.commit()))
            calls = _bound_host(runtime)
            from provider_cli_pin import check
            with pytest.raises(AccountError, match="provider_cli_pin_unavailable"):
                await runtime.run(lambda a: check(a, account))
            # The ordinary path owns the typed refusal; the mark waits for a suitable visit.
            await runtime.executor.observe(account)
            assert not [c for c in calls if c[0] == "observe-bound"]
            assert runtime._self_recovery_marks == {(account.key, account.generation): revision}
            assert await _health(runtime, account) == ("auth_unhealthy", revision)
            assert await _receipts(runtime, account) == {}
            assert await runtime.run(lambda a: a.conn.execute(
                "SELECT 1 FROM kv WHERE k=?", (removed,)).fetchone()) is None
        finally:
            store.stop()
    asyncio.run(run())


def test_two_recoveries_on_one_generation_have_distinct_attempt_ids(tmp_path):
    async def run():
        store, runtime, account, revision = await _world(tmp_path / "two.db")
        try:
            _bound_host(runtime)
            assert (await runtime.executor.observe(account))["outcome"] == "ok"
            await runtime.record_failure(account, "auth")
            runtime._mark_auth_self_recovery(account, await _revision(runtime, account))
            runtime._self_recovery_last_dispatch.clear()
            assert (await runtime.executor.observe(account))["outcome"] == "ok"
            receipts = await _receipts(runtime, account)
            assert len(receipts) == 2 and len(set(receipts)) == 2
            assert await _health(runtime, account) == ("healthy", revision + 3)
        finally:
            store.stop()
    asyncio.run(run())


# -- F3 / F4: negative evidence ------------------------------------------------

@pytest.mark.parametrize("reply", [
    {"outcome": "subscription_identity_unverified"},
    {"outcome": "provider_identity_changed"},
    {"outcome": "provider_identity_or_cache_unverified"},
    {"outcome": "provider_identity_or_cache_unverified", "identity_evidence": "unknown"},
    {"outcome": "provider_identity_changed", "identity_evidence": "contradiction"},
])
def test_ambiguous_identity_is_typed_and_leaves_row_intact(tmp_path, reply):
    async def run():
        store, runtime, account, revision = await _world(tmp_path / "a.db")
        alerts = []
        try:
            async def alert(_account, code): alerts.append(code)
            runtime._alert = alert
            _bound_host(runtime, reply=lambda _selected: dict(reply))
            result = await runtime.executor.observe(account)
            assert result["outcome"] == reply["outcome"]
            assert await _health(runtime, account) == ("auth_unhealthy", revision) and alerts == []
        finally:
            store.stop()
    asyncio.run(run())


def _enrolled_claude_host(tmp_path):
    """The real host executor and registry with one enrolled Claude cell."""
    import hashlib
    from pathlib import Path
    from types import SimpleNamespace
    from provider_account_host import NativeHost
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    digest = hashlib.sha256(Path("/bin/echo").read_bytes()).hexdigest()
    config = {"host": "host-a", "registry": str(private / "registry.json"), "root": str(private / "homes"),
              "cells": {"claude:primary": {"enabled": True, "expected_provider_id": "id-primary",
                  "profile_ref": "google-primary.one", "executable": "/bin/echo", "version": "fixture",
                  "executable_sha256": digest, "platform": "macos", "model": "fixture-model", "effort": "high",
                  "cwd": str(tmp_path)}}}
    mode = {"fail": None}

    def observation(account):
        if mode["fail"]:
            raise AccountError(mode["fail"])
        return {"provider_id": "id-primary", "pool_id": "pool-primary", "auth_mode": "subscription"}

    def run(argv, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="fixture" if argv[-1] == "--version" else "OK")
    host = NativeHost(config, run=run, login=lambda *_a: "ok", observation=observation)
    enrolled = host.handle("enroll", provider="claude", alias="primary", expected_generation="unenrolled",
                           operation_id="enroll-one", generation="first")
    assert enrolled["outcome"] == "ok"
    return host, mode


@pytest.mark.parametrize("case", ["contradiction", "same-identity", "incomplete", "observe", "other-failure"])
def test_host_identity_evidence_uses_one_fresh_identity_on_that_path_only(tmp_path, monkeypatch, case):
    """Approved ruling (2): the host, not the frozen native observation, is the producer."""
    import provider_account_host as host_module
    host, mode = _enrolled_claude_host(tmp_path)
    reads = []

    def identity(binding, **_kwargs):
        reads.append(binding.key)
        if case == "incomplete":
            raise AccountError("subscription_identity_unverified")
        other = case == "contradiction"
        return {"provider_id": "id-someone-else" if other else binding.provider_id,
                "pool_id": "pool-elsewhere" if other else binding.pool_id,
                "auth_mode": "subscription", "email_verified": False}
    monkeypatch.setattr(host_module, "claude_identity", identity)
    mode["fail"] = "provider_auth_failed" if case == "other-failure" else "provider_identity_or_cache_unverified"
    action = "observe" if case == "observe" else "observe-bound"
    with pytest.raises(AccountError) as raised:
        host.handle(action, provider="claude", alias="primary", expected_generation="first", operation_id="obs")
    expected_reads = 0 if case in {"observe", "other-failure"} else 1
    assert len(reads) == expected_reads
    assert (getattr(raised.value, "identity_contradiction", False) is True) is (case == "contradiction")


@pytest.mark.parametrize("tagged", [True, False])
def test_host_emits_identity_evidence_only_for_tagged_observe_bound(tmp_path, monkeypatch, capsys, tagged):
    import provider_account_host as host_module
    error = AccountError("provider_identity_or_cache_unverified")
    if tagged:
        error.identity_contradiction = True

    class Native:
        def __init__(self, *_a, **_k): pass

        def handle(self, *_a, **_k): raise error
    monkeypatch.setattr(host_module, "NativeHost", Native)
    monkeypatch.setattr(host_module, "_private", lambda *_a, **_k: None)
    config = tmp_path / ".config/pentacle/provider-account-host.json"
    config.parent.mkdir(parents=True)
    config.write_text("{}")
    monkeypatch.setenv("HOME", str(tmp_path))
    for action in ("observe-bound", "observe"):
        code = host_module.main([action, "--provider", "claude", "--account", "primary",
                                 "--login-generation", "first", "--operation-id", "a" * 64])
        reply = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert code == 1 and reply["outcome"] == "provider_identity_or_cache_unverified"
        assert ("identity_evidence" in reply) is (tagged and action == "observe-bound")


def test_real_contradiction_quarantines_and_old_host_reply_does_not(tmp_path):
    async def run():
        store, runtime, account, revision = await _world(tmp_path / "q.db")
        alerts = []
        try:
            async def alert(_account, code): alerts.append(code)
            runtime._alert = alert
            _bound_host(runtime, reply=lambda _s: {"outcome": "provider_identity_or_cache_unverified"})
            await runtime.executor.observe(account)
            assert await _health(runtime, account) == ("auth_unhealthy", revision) and alerts == []
            runtime._mark_auth_self_recovery(account, revision)
            runtime._self_recovery_last_dispatch.clear()
            _bound_host(runtime, reply=lambda _s: {"outcome": "provider_identity_or_cache_unverified",
                                                   "identity_evidence": "contradiction"})
            await runtime.executor.observe(account)
            assert await _health(runtime, account) == ("identity_conflict", revision + 1)
            assert alerts == ["identity_unverified"]
        finally:
            store.stop()
    asyncio.run(run())


def test_ordinary_auth_failure_accounting_is_unchanged(tmp_path):
    async def run():
        store, runtime, account, revision = await _world(tmp_path / "o.db")
        alerts = []
        try:
            runtime._self_recovery_marks.clear()

            async def alert(_account, code): alerts.append(code)
            runtime._alert = alert

            async def execute(selected, action, operation_id, generation=None, **kwargs):
                return {"outcome": "provider_auth_failed"}
            runtime.executor = AccountExecutor(runtime, None, execute=execute)
            await runtime.executor.observe(account)
            assert await _health(runtime, account) == ("auth_unhealthy", revision + 1)
            assert alerts == ["login_unavailable"]
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["account_repin_home_busy", "provider_transport_failed"])
def test_busy_or_failed_dispatch_is_charged_and_never_redispatched(tmp_path, outcome):
    async def run():
        store, runtime, account, revision = await _world(tmp_path / "b.db")
        clock = {"now": 100.0}
        runtime.monotonic = lambda: clock["now"]
        try:
            calls = _bound_host(runtime, reply=lambda _s: {"outcome": outcome})
            assert (await runtime.executor.observe(account))["outcome"] == outcome
            assert runtime._self_recovery_last_dispatch[(account.key, account.generation)] == 100.0
            assert not runtime._self_recovery_marks
            assert await _receipts(runtime, account) == {}
            clock["now"] += 500
            await runtime.executor.observe(account)
            assert [c[0] for c in calls] == ["observe-bound", "observe"]
            runtime._mark_auth_self_recovery(account, await _revision(runtime, account))
            clock["now"] = 100.0 + 119
            await runtime.executor.observe(account)
            clock["now"] = 100.0 + 120
            assert [c[0] for c in calls].count("observe-bound") == 1
        finally:
            store.stop()
    asyncio.run(run())


def test_two_generations_of_one_alias_keep_independent_spacing(tmp_path):
    async def run():
        from dataclasses import replace
        store, runtime, old, revision = await _world(tmp_path / "g.db")
        clock = {"now": 10.0}
        runtime.monotonic = lambda: clock["now"]
        try:
            calls = _bound_host(runtime, reply=lambda _s: {"outcome": "provider_transport_failed"})
            await runtime.executor.observe(old)
            new = replace(old, generation="login-two", home=old.home.replace("login-one", "login-two"))
            await runtime.run(lambda a: a.register(new))
            await runtime.record_failure(new, "auth")
            runtime._mark_auth_self_recovery(new, await _revision(runtime, new))
            await runtime.executor.observe(new)
            assert [c[0] for c in calls].count("observe-bound") == 2
            assert (old.key, old.generation) not in runtime._self_recovery_last_dispatch
            assert runtime._self_recovery_last_dispatch[(new.key, new.generation)] == 10.0
        finally:
            store.stop()
    asyncio.run(run())


# -- F1 / F2: one ingress decision ----------------------------------------------

def test_decision_mutations_are_owner_checked_and_fail_closed():
    from account_runtime import (RECOVERY_INGRESS, arm_ingress, mint_ingress, retire_ingress,
                                 take_ingress, transfer_ingress)

    async def run():
        token = mint_ingress("tell")
        try:
            arm_ingress("tell", "host-a:worker")

            async def copied_child():
                # A copied context holds the reference, never the authority.
                retire_ingress()
                transfer_ingress("tell", asyncio.current_task())
                return take_ingress("tell", "host-a:worker")
            assert await asyncio.create_task(copied_child()) is False
            seen = []
            asyncio.get_running_loop().call_soon(lambda: seen.append(
                (take_ingress("tell", "host-a:worker"), retire_ingress())))
            await asyncio.sleep(0)
            assert seen == [(False, None)]
            assert await asyncio.to_thread(lambda: (take_ingress("tell", "host-a:worker"), retire_ingress())) == (False, None)
            assert take_ingress("send", "host-a:worker") is False
            assert take_ingress("tell", "host-a:other") is False
            assert take_ingress("tell", "host-a:worker") is True
            assert take_ingress("tell", "host-a:worker") is False
        finally:
            RECOVERY_INGRESS.reset(token)
        assert take_ingress("tell", "host-a:worker") is False

    async def spawn_transfer():
        token = mint_ingress("spawn")
        try:
            gate = asyncio.Event()

            async def effect():
                await gate.wait()
                transfer_ingress("spawn", asyncio.current_task())  # a second transfer is refused
                arm_ingress("spawn", "localhost:new")
                return take_ingress("spawn", "localhost:new")
            task = asyncio.create_task(effect())
            transfer_ingress("spawn", task)
            transfer_ingress("spawn", asyncio.current_task())
            arm_ingress("spawn", "localhost:new")
            assert take_ingress("spawn", "localhost:new") is False
            gate.set()
            assert await task is True
        finally:
            RECOVERY_INGRESS.reset(token)
    asyncio.run(run())
    asyncio.run(spawn_transfer())
    assert mint_ingress("tell") is None  # no running loop: no decision, no exception


def test_bare_loopback_and_spoofed_sender_get_no_decision():
    from server import Server
    assert Server._recovery_ingress({}, {}) is False
    assert Server._recovery_ingress({"peer_loopback": True, "local_admin_verified": True}, {}) is False
    verified = {"token_verified": True, "stream_id": "host-a:peer"}
    assert Server._recovery_ingress(verified, {"from_stream_id": "host-a:other"}) is False
    assert Server._recovery_ingress(verified, {"from_stream_id": "host-a:peer"}) is True
    assert Server._recovery_ingress({"service_authenticated": True, "service_actor": "x"}, {}) is True


async def _routed_world(tmp_path):
    from test_account_cap_delivery import fixture as delivery_fixture
    store, runtime, comms, tmux, worker, _meter = await delivery_fixture(tmp_path)
    account = binding(provider="claude")
    await runtime.run(lambda a: (a.conn.execute(
        "DELETE FROM kv WHERE k=?", ("account.pool-control:claude:pool-primary",)), a.conn.commit()))
    await runtime.record_failure(account, "auth")
    return store, runtime, comms, tmux, worker, account


def test_internal_same_task_tell_cannot_spend_an_external_decision(tmp_path):
    async def run():
        from account_runtime import RECOVERY_INGRESS, mint_ingress
        from sessions import VerbError
        from test_account_pool_control import PEER
        store, runtime, comms, tmux, worker, account = await _routed_world(tmp_path)
        token = mint_ingress("tell")
        try:
            msg = {"stream_id": worker["stream_id"], "message": "internal", "from_stream_id": PEER["stream_id"],
                   "_auth_context": PEER, "tell_id": "internal", "request_id": "internal"}
            with pytest.raises(VerbError, match="account_unavailable"):
                await comms.tell(msg)  # no server route marker
            assert runtime._self_recovery_marks == {} and tmux.pastes == []
        finally:
            RECOVERY_INGRESS.reset(token)
            store.stop()
    asyncio.run(run())


def test_urgent_escape_spends_the_pre_effect_opportunity(tmp_path):
    async def run():
        from test_account_self_recovery import _external
        from test_account_pool_control import PEER
        from sessions import VerbError
        store, runtime, comms, tmux, worker, account = await _routed_world(tmp_path)
        try:
            # Healthy at the Escape guard; unhealthy at the later paste guard.
            def heal(a):
                a.conn.execute("UPDATE v2_account_bindings SET health='healthy' WHERE binding_key=?", (account.key,))
                a.conn.commit()
            await runtime.run(heal)
            original_run = tmux.run

            async def run_keys(*args, **kwargs):
                result = await original_run(*args, **kwargs)
                if "Escape" in args:
                    await runtime.record_failure(account, "auth")
                return result
            tmux.run = run_keys
            msg = {"stream_id": worker["stream_id"], "message": "urgent", "urgent": True,
                   "from_stream_id": PEER["stream_id"], "_auth_context": PEER,
                   "tell_id": "urgent", "request_id": "urgent"}
            try:
                reply = await _external(comms.tell, "tell", msg)
                assert reply.get("submission_confirmed") is False
            except VerbError as exc:
                assert exc.code == "account_unavailable"
            assert tmux.pastes == [] and runtime._self_recovery_marks == {}
        finally:
            store.stop()
    asyncio.run(run())


def test_coalesced_send_retry_never_marks(tmp_path):
    async def run():
        from test_account_self_recovery import _external
        from test_account_pool_control import PEER
        from sessions import VerbError
        store, runtime, comms, tmux, worker, account = await _routed_world(tmp_path)
        try:
            msg = {"stream_id": worker["stream_id"], "text": "one send", "optimistic_id": "same-send",
                   "from_stream_id": PEER["stream_id"], "_auth_context": PEER}
            entered, release = asyncio.Event(), asyncio.Event()
            original = runtime.before_input

            async def held(row, **kwargs):
                if kwargs.get("recovery_verb") == "send" and not entered.is_set():
                    entered.set()
                    await release.wait()
                return await original(row, **kwargs)
            runtime.before_input = held
            first = asyncio.create_task(_external(comms.send, "send", dict(msg, request_id="first")))
            await entered.wait()
            second = await _external(comms.send, "send", dict(msg, request_id="second"))
            assert runtime._self_recovery_marks == {}
            release.set()
            try:
                await first
            except VerbError:
                pass
            revision = (await _health(runtime, account))[1]
            assert runtime._self_recovery_marks == {(account.key, account.generation): revision}
            assert second.get("submission_confirmed") is not True and tmux.pastes == []
        finally:
            store.stop()
    asyncio.run(run())


def test_readonly_schedule_owns_no_mark(tmp_path):
    async def run():
        from sessions import VerbError
        from test_account_schedule_journey import fixture, SPEC
        store, rt, pane, ctl, surface, msg = await fixture(tmp_path)
        try:
            a = await rt.run(lambda db: db.bound(binding("spare", host="localhost").key))
            await rt.record_failure(a, "auth")
            with pytest.raises(VerbError, match="account_unavailable"):
                await ctl.admit_schedule(msg("spawn", spec_id=SPEC, provider="codex", account="spare",
                    model="gpt-6.2-sol", effort="high", initial_prompt="fixture", objective="QA schedule fixture"),
                    "localhost", admission_name="qa-schedule")
            assert not rt._self_recovery_marks and pane.launches == 0
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("pending", [
    {"qa_spec_id": "spec_fixture"}, {"role": "qa"}, {"phase": "qa"},
    {"handoff": True}, {"provision_only": True},
    {"pool_override_reason": "fixture", "pool_authority_ref": "fixture-order"},
])
def test_spawn_with_a_pending_privilege_decision_never_marks(tmp_path, pending):
    async def run():
        from sessions import VerbError
        from test_account_self_recovery import _external
        from test_account_schedule_journey import fixture, SPEC
        store, rt, pane, ctl, _surface, message = await fixture(tmp_path)
        try:
            a = await rt.run(lambda db: db.bound(binding("spare", host="localhost").key))
            await rt.record_failure(a, "auth")
            msg = message("spawn", spec_id=SPEC, provider="codex", account="spare", model="gpt-6.2-sol",
                effort="high", initial_prompt="fixture", objective="pending privilege", **pending)
            with pytest.raises(VerbError):
                await _external(lambda value: ctl._spawn_guarded(value, "localhost"), "spawn", msg)
            assert rt._self_recovery_marks == {} and pane.launches == 0
        finally:
            for task in tuple(ctl._background_spawns): task.cancel()
            await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            store.stop()
    asyncio.run(run())


def test_retained_same_key_spawn_record_never_marks(tmp_path):
    async def run():
        from sessions import VerbError
        from test_account_self_recovery import _external
        from test_account_schedule_journey import fixture, SPEC
        store, rt, pane, ctl, _surface, message = await fixture(tmp_path)
        try:
            a = await rt.run(lambda db: db.bound(binding("spare", host="localhost").key))
            msg = message("spawn", spec_id=SPEC, provider="codex", account="spare", model="gpt-6.2-sol",
                effort="high", initial_prompt="native user prompt", objective="retained", idempotency_key="retained-key")
            await _external(lambda value: ctl._spawn_guarded(value, "localhost"), "spawn", dict(msg))
            await rt.record_failure(a, "auth")
            try:
                await _external(lambda value: ctl._spawn_guarded(value, "localhost"), "spawn", dict(msg))
            except VerbError:
                pass
            assert rt._self_recovery_marks == {}
        finally:
            for task in tuple(ctl._background_spawns): task.cancel()
            await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            store.stop()
    asyncio.run(run())


# -- F6: committed lifecycle ----------------------------------------------------

def _successor(account, generation):
    from dataclasses import replace
    return replace(account, generation=generation, home=account.home.replace("login-one", generation))


def test_direct_register_retires_mark_and_spacing(tmp_path):
    async def run():
        store, runtime, account, _revision = await _world(tmp_path / "f6.db")
        try:
            runtime._self_recovery_last_dispatch[(account.key, account.generation)] = 1.0
            await runtime.run(lambda db: db.register(_successor(account, "fixture-next")))
            assert runtime._self_recovery_marks == {} and runtime._self_recovery_last_dispatch == {}
        finally:
            store.stop()
    asyncio.run(run())


def test_rolled_back_registration_preserves_mark_and_spacing(tmp_path):
    async def run():
        from account_store import transaction
        store, runtime, account, revision = await _world(tmp_path / "rb.db")
        try:
            runtime._self_recovery_last_dispatch[(account.key, account.generation)] = 1.0

            def fails(db):
                with transaction(db.conn):
                    db._register(_successor(account, "fixture-next"))
                    raise AccountError("account_repin_state_changed")
            with pytest.raises(AccountError):
                await runtime.run(fails)
            assert runtime._self_recovery_marks == {(account.key, account.generation): revision}
            assert runtime._self_recovery_last_dispatch == {(account.key, account.generation): 1.0}
        finally:
            store.stop()
    asyncio.run(run())


def test_retirement_is_cell_local_for_publication_and_restore(tmp_path):
    async def run():
        store, runtime, a, revision_a = await _world(tmp_path / "cells.db")
        b = binding("spare", provider="claude")
        try:
            await runtime.run(lambda db: db.register(b))
            await runtime.record_failure(b, "auth")
            revision_b = await _revision(runtime, b)
            _bound_host(runtime)
            await runtime.run(lambda db: db.register(_successor(a, "fixture-next")))
            # B publishes and restores while A retires; A's late signals are dropped.
            runtime._mark_auth_self_recovery(b, revision_b)
            runtime._mark_auth_self_recovery(a, revision_a)
            assert runtime._self_recovery_marks == {(b.key, b.generation): revision_b}
            runtime._self_recovery_marks.clear()
            runtime.executor._restore_self_recovery_mark((a.key, a.generation), revision_a)
            runtime.executor._restore_self_recovery_mark((b.key, b.generation), revision_b)
            assert runtime._self_recovery_marks == {(b.key, b.generation): revision_b}
        finally:
            store.stop()
    asyncio.run(run())


def test_commit_whose_awaiter_was_cancelled_applies_before_next_publication(tmp_path):
    async def run():
        store, runtime, account, revision = await _world(tmp_path / "cx.db")
        gate = threading.Event()
        try:
            blocker = asyncio.ensure_future(store.submit(lambda _conn: gate.wait(30)))
            task = asyncio.create_task(runtime.run(lambda db: db.register(_successor(account, "fixture-next"))))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            gate.set()
            await blocker
            await store.submit(lambda _conn: None)  # the register has run; no runtime boundary yet
            runtime._mark_auth_self_recovery(account, revision)
            assert runtime._self_recovery_marks == {}
        finally:
            gate.set()
            store.stop()
    asyncio.run(run())


def test_cell_commit_handoff_is_bounded_by_cells(tmp_path, monkeypatch):
    async def run():
        store, runtime, account, _revision = await _world(tmp_path / "bound.db")
        b = binding("spare", provider="claude")
        try:
            monkeypatch.setattr(runtime, "_apply_cell_commits", lambda: None)
            for index in range(20):
                for selected in (account, b):
                    await runtime.run(lambda db, s=selected, i=index: db.register(_successor(s, f"g{i}")))
            # One entry per cell however many generations committed.
            assert set(runtime._cell_commits) == {("host-a", "claude", "primary"), ("host-a", "claude", "spare")}
        finally:
            store.stop()
    asyncio.run(run())


# -- F7: real dispatch through the bound front-desk route -----------------------

# The brief producer's dedicated service credential, as in
# test_wmi_brief_tell_principal (whose peer helpers import boto3 via uiverbs).
ACTOR = "examplehost:wmi-daily-brief"
TOKEN = "synthetic-independent-brief-token"
COMPOSITE = "desk:assistant"
OPS = "localhost:ops"
FD = "localhost:frontdesk"


class Peer:
    def __init__(self, address="192.0.2.10"):
        self.remote_address = (address, 54321)


class SocketPeer(Peer):
    def __init__(self, address="127.0.0.1"):
        super().__init__(address)
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    token = tmp_path / "brief-token"
    token.write_text(TOKEN)
    token.chmod(0o600)
    config = tmp_path / "brief-config.json"
    config.write_text(json.dumps({"stream_id": ACTOR, "token_file": str(token),
                                  "composite_stream_id": COMPOSITE, "ops_stream_id": OPS}))
    config.chmod(0o600)
    monkeypatch.setenv("PENTACLE_WMI_BRIEF_PRODUCER_CONFIG", str(config))


def frame(verb="tell", **changes):
    msg = {"type": verb, "request_id": "brief-request", "from_stream_id": ACTOR, "stream_token": TOKEN}
    if verb == "hello":
        msg["subscribe"] = {"snapshot": False, "mode": "rpc"}
    else:
        msg.update(tell_id="brief-tell", to_stream_id=COMPOSITE, text="GATE synthetic brief",
                   ttl_seconds=3600, sanitize=True)
    return {**msg, **changes}


async def dispatch(server, peer, msg):
    return (await server._dispatch(json.dumps(msg), websocket=peer))[0]


async def authenticate(server, peer):
    reply = await dispatch(server, peer, frame("hello"))
    assert reply["type"] == "ready", reply


class _AccountDesk:
    """The brief harness, with both seats bound to one auth-unhealthy account."""

    def __init__(self, path):
        from store import Store
        self.store = Store(str(path))

    async def __aenter__(self):
        from account_runtime import AccountRuntime
        from assistant_composite import AssistantComposite, AssistantCompositeConfig
        from comms import Comms
        from server import Server
        from sessions import Sessions
        from spawnctl import SpawnCtl
        from test_tell_immediate_delivery import ScriptedClaude, IDLE_PLACEHOLDER
        self.store.start()
        self.tmux = ScriptedClaude(start=IDLE_PLACEHOLDER)
        self.sessions = Sessions(self.store, tmux=self.tmux, local_host="localhost")
        self.ctl = SpawnCtl(self.store, self.sessions, tmux=self.tmux)
        self.comms = Comms(self.store, self.sessions, self.ctl)
        self.account = binding(provider="claude", host="localhost")
        for name in ("ops", "frontdesk"):
            await self.sessions.open("localhost", name, provider="claude", account_binding=self.account.public())
        seat = await self.store.fetch_session("localhost", "frontdesk")
        self.composite = AssistantComposite(self.store, config=AssistantCompositeConfig(
            enabled=True, name="briefdesk", stream_id=COMPOSITE, direct_primary_stream_id=FD,
            direct_primary_generation=seat["session_generation"]))
        await self.composite.load_binding()
        await self.composite.ensure_projection()
        await self.sessions.refresh()
        self.server = Server(store=self.store, sessions=self.sessions, comms=self.comms, local_host="localhost")
        self.server.assistant_composites = {"briefdesk": self.composite}
        self.runtime = AccountRuntime(self.store, self.sessions, policy={"enabled": True})
        self.runtime.attach(self.server, self.ctl, self.comms)
        await self.runtime.run(lambda a: a.register(self.account))
        await self.runtime.record_failure(self.account, "auth")
        return self

    async def __aexit__(self, *exc):
        self.store.stop()


def test_service_authenticated_front_desk_tell_marks_and_resubmission_lands_once(credentials, tmp_path):
    async def run():
        async with _AccountDesk(tmp_path / "desk.db") as h:
            revision = (await _health(h.runtime, h.account))[1]
            # A bare loopback bootstrap reaches the route but supplies no recovery authority.
            loopback = await dispatch(h.server, SocketPeer(), {"type": "tell", "request_id": "bare",
                "tell_id": "bare", "to_stream_id": OPS, "text": "bare loopback"})
            assert loopback.get("error_code") == "account_unavailable", loopback
            assert h.runtime._self_recovery_marks == {}
            peer = Peer()
            await authenticate(h.server, peer)
            denied = await dispatch(h.server, peer, frame(to_stream_id="other:assistant"))
            assert denied.get("error_code") and h.runtime._self_recovery_marks == {}
            refused = await dispatch(h.server, peer, frame())
            assert refused.get("error_code") == "account_unavailable", refused
            assert h.runtime._self_recovery_marks == {(h.account.key, h.account.generation): revision}
            assert h.tmux.pastes == []
            calls = _bound_host(h.runtime)
            assert (await h.runtime.executor.observe(h.account))["outcome"] == "ok"
            assert [c[0] for c in calls] == ["observe-bound"] and h.tmux.pastes == []
            assert await _health(h.runtime, h.account) == ("healthy", revision + 1)
            first = await dispatch(h.server, peer, frame(request_id="again-1"))
            second = await dispatch(h.server, peer, frame(request_id="again-2"))
            assert first["type"] == second["type"] == "tell.ok", (first, second)
            assert len(h.tmux.pastes) == 1
    asyncio.run(run())


# -- Late results against every competing winner --------------------------------

@pytest.mark.parametrize("late", ["success", "contradiction"])
@pytest.mark.parametrize("winner", ["auth", "identity", "disable", "rotation", "manual"])
def test_late_result_never_replaces_a_competing_winner(tmp_path, late, winner):
    async def run():
        from account_store import transaction
        store, runtime, account, revision = await _world(tmp_path / "late.db", pin=True)
        try:
            async def compete():
                if winner == "auth":
                    await runtime.record_failure(account, "auth")
                elif winner == "identity":
                    await runtime.record_failure(account, "identity")
                elif winner == "disable":
                    await runtime.run(lambda a: (a.conn.execute(
                        "UPDATE v2_account_bindings SET data=json_set(data,'$.enabled',json('false')) WHERE binding_key=?",
                        (account.key,)), a.conn.commit()))
                elif winner == "rotation":
                    await runtime.run(lambda a: a.register(_successor(account, "login-two")))
                else:
                    def manual(a):
                        with transaction(a.conn):
                            transition = a.transition_relogin_auth_health(account.key, revision)
                            state = AccountExecutor._read_recovery(a, account.key)
                            state.setdefault("receipts", {})["manual-winner"] = {
                                "outcome": "healthy", "operation_id": "manual-winner",
                                "health_revision": transition["applied_revision"], "relogin_probe": "ok"}
                            AccountExecutor._write_recovery(a, account.key, state)
                    await runtime.run(manual)
            reply = None if late == "success" else (lambda _s: {
                "outcome": "provider_identity_or_cache_unverified", "identity_evidence": "contradiction"})
            _bound_host(runtime, during=compete, reply=reply)
            result = await runtime.executor.observe(account)
            assert result["outcome"] != "ok"
            expected = {"auth": ("auth_unhealthy", revision + 1), "identity": ("identity_conflict", revision + 1),
                        "disable": ("auth_unhealthy", revision), "rotation": ("auth_unhealthy", revision),
                        "manual": ("healthy", revision + 1)}[winner]
            assert await _health(runtime, account) == expected
            receipts = await _receipts(runtime, account)
            assert set(receipts) == ({"manual-winner"} if winner == "manual" else set())
            assert await _proof(runtime, account) is None
            if winner == "rotation":
                assert await runtime.run(lambda a: a.conn.execute(
                    "SELECT active FROM v2_account_bindings WHERE binding_key=?", (account.key,)).fetchone()[0]) == 0
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("spec", [True, False])
@pytest.mark.parametrize("form", [{}, {"role": "qa"}, {"role": "lead"}, {"phase": "qa"}, {"qa_spec_id": "spec_x"},
                                  {"role": "worker", "phase": "build"}])
def test_selection_qa_row_matches_open_row_classification(tmp_path, spec, form):
    async def run():
        import qa_dispatch
        import tmux_transport
        from spawnctl import SpawnCtl
        from test_account_schedule_journey import fixture, SPEC
        store, rt, _pane, ctl, _surface, message = await fixture(tmp_path)
        try:
            msg = message("spawn", provider="codex", account="spare", model="gpt-6.2-sol", effort="high",
                          initial_prompt="fixture", objective="row equivalence",
                          **({"spec_id": SPEC} if spec else {}), **form)
            spec_binding = await ctl._resolve_spec_binding(msg, "localhost", "row-check")
            _command, _resolution, overrides = await ctl._resolve_launch(msg, "localhost", "row-check")
            assert not {"role", "phase"} & set(spec_binding)
            assert {"role", "phase"} & set(overrides) <= {"role"}
            open_flds = {**tmux_transport.open_fields(msg), **spec_binding, **overrides}
            selection = SpawnCtl._refusal_qa_row(msg, overrides.get("role", msg.get("role")))
            assert qa_dispatch.classified(msg, selection) == qa_dispatch.classified(msg, open_flds)
            assert (selection.get("role"), selection.get("phase")) == (open_flds.get("role"), open_flds.get("phase"))
        finally:
            store.stop()
    asyncio.run(run())
