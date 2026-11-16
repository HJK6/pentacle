"""Account entry points share the collector's host mutex and retain safe failures."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from account_executor import AccountExecutor
from provider_accounts import AccountError
from test_account_repin import host_journey
from test_account_rotation import binding, observation
from test_account_runtime import AUTH


@pytest.mark.parametrize("entry", ["admit", "input", "status"])
@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_account_entry_waits_for_collector_registry_flock(tmp_path, monkeypatch, entry, provider):
    async def run():
        store, runtime, host, _, request, _, _, _, _, _ = await host_journey(
            tmp_path, monkeypatch, provider, transport=True)
        tasks = []
        entered, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
        try:
            await runtime.operation(request)
            selected = host.registry.active(provider, "repin", "host-a")
            await store.clear_spawn_admission_hold(selected.host)
            row = await store.open_session(selected.host, "mutex-proof", provider=provider,
                session_generation="mutex-generation", account_binding=selected.public())
            # Independent helper instances share only the real kernel flock.
            host.separate_dispatch_registries = True
            transport = runtime.executor.hosts.run_command
            host_errors = []

            async def recorded_transport(*args, **kwargs):
                code, output = await transport(*args, **kwargs)
                if code:
                    host_errors.append(json.loads(output)["outcome"])
                return code, output

            runtime.executor.hosts.run_command = recorded_transport
            actual = runtime.executor.execute
            calls = []
            other = replace(selected, host="other-host")
            await runtime.run(lambda a: a.register(other))

            class ObservedLock(asyncio.Lock):
                async def acquire(self):
                    if self.locked():
                        queued.set()
                    return await super().acquire()

            lock = ObservedLock()
            runtime.executor._host_locks[selected.host] = lock

            async def execute(account, action, *args, **kwargs):
                if account.host == other.host:
                    return {"outcome": "ok", "binding_key": other.key,
                            "observation": observation(other)}
                calls.append(action)
                if action == "observe":
                    with host.registry.lock():
                        entered.set()
                        await release.wait()
                return await actual(account, action, *args, **kwargs)

            runtime.executor.execute = execute
            cause = {"operation_id": "fixture", "cause": "uncertain", "key": "fixture", "reason": "fixture"}
            if entry == "status":
                import account_repin_state
                # An unresolved intent is the existing status host-probe trigger.
                monkeypatch.setattr(account_repin_state, "causes", lambda *_: [cause])

            collector = asyncio.create_task(runtime.executor.observe(selected))
            tasks.append(collector)
            await asyncio.wait_for(entered.wait(), 1)
            action = (runtime.admit_launch(row) if entry == "admit" else
                      runtime.before_input(row) if entry == "input" else
                      runtime.operation({"action": "status", "host": selected.host,
                          "provider": provider, "account": selected.alias, "_auth_context": AUTH}))
            operation = asyncio.create_task(action)
            waiter = asyncio.create_task(queued.wait())
            tasks.extend((operation, waiter))
            done, _ = await asyncio.wait((operation, waiter), timeout=1,
                                        return_when=asyncio.FIRST_COMPLETED)
            error = operation.exception() if operation.done() else None
            assert waiter in done, f"{entry} bypassed collector: {error}; host outcomes={host_errors}"
            assert not operation.done() and calls == ["observe"]
            assert (await asyncio.wait_for(runtime.executor.observe(other), 1))["outcome"] == "ok"
            release.set()
            # The status fixture's pending intent also fences normal collection.
            assert (await collector)["outcome"] == (
                "account_repin_reconciliation_required" if entry == "status" else "ok")
            result = await asyncio.wait_for(operation, 2)
            if entry == "status":
                assert result["type"] == "account.ok"
                assert result["recovery"][0]["fence_causes"] == [cause]
            assert not host_errors and not lock.locked()
            assert calls == ["observe", "recovery-status" if entry == "status" else "verify-artifact"]
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("entry", ["admit", "input"])
def test_cancelled_account_waiter_keeps_collectors_lock(tmp_path, monkeypatch, entry):
    async def run():
        store, runtime, host, _, request, _, _, _, _, _ = await host_journey(
            tmp_path, monkeypatch, transport=True)
        task = None
        try:
            await runtime.operation(request)
            selected = host.registry.active("codex", "repin", "host-a")
            await store.clear_spawn_admission_hold(selected.host)
            row = await store.open_session(selected.host, "cancel-proof", provider="codex",
                session_generation="cancel-generation", account_binding=selected.public())
            queued = asyncio.Event()

            class ObservedLock(asyncio.Lock):
                async def acquire(self):
                    if self.locked():
                        queued.set()
                    return await super().acquire()

            lock = ObservedLock()
            runtime.executor._host_locks[selected.host] = lock
            await lock.acquire()
            task = asyncio.create_task(runtime.admit_launch(row) if entry == "admit"
                                       else runtime.before_input(row))
            await asyncio.wait_for(queued.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert lock.locked(), "cancelled waiter must not release another owner's lock"
            lock.release()
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["account_busy", "provider_executable_missing", "future_host_failure"])
def test_nonallowlisted_host_failure_preserves_only_scalar_code(outcome):
    async def run():
        async def command(*args, **kwargs):
            return 1, json.dumps({"outcome": outcome, "detail": "private output", "context": {"secret": "private"}})
        runtime = SimpleNamespace(policy={"executors": {"host-a": {"python": "/fixture/python", "script": "/fixture/host"}}})
        executor = AccountExecutor(runtime, SimpleNamespace(run_command=command))
        with pytest.raises(AccountError) as refused:
            await executor._execute(binding(), "verify-artifact", "scalar-proof")
        assert str(refused.value) == outcome
        assert refused.value.context == {}
    asyncio.run(run())


@pytest.mark.parametrize("outcome", [None, {"code": "account_busy"}, "private output\nsecret", "https://private.test/key", "x" * 81])
def test_host_outcome_must_be_a_bounded_code(outcome):
    async def run():
        async def command(*args, **kwargs):
            return 1, json.dumps({"outcome": outcome})
        runtime = SimpleNamespace(policy={"executors": {"host-a": {"python": "/fixture/python", "script": "/fixture/host"}}})
        executor = AccountExecutor(runtime, SimpleNamespace(run_command=command))
        with pytest.raises(AccountError, match="^account_host_response_invalid$"):
            await executor._execute(binding(), "verify-artifact", "invalid-proof")
    asyncio.run(run())


@pytest.mark.parametrize("code,outcome", [(0, "ok"), (1, "provider_auth_failed")])
def test_success_and_existing_allowed_failure_return_unchanged(code, outcome):
    async def run():
        result = {"outcome": outcome}
        async def command(*args, **kwargs):
            return code, json.dumps(result)
        runtime = SimpleNamespace(policy={"executors": {"host-a": {"python": "/fixture/python", "script": "/fixture/host"}}})
        executor = AccountExecutor(runtime, SimpleNamespace(run_command=command))
        assert await executor._execute(binding(), "verify-artifact", "unchanged-proof") == result
    asyncio.run(run())
