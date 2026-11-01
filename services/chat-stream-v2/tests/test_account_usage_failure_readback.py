"""A failed bound probe keeps its last good meter without authorizing usage."""
import asyncio
from dataclasses import replace

import pytest

from account_executor import AccountExecutor
from account_runtime import AccountRuntime
from provider_accounts import AccountError
from sessions import Sessions
from store import Store
from test_account_rotation import NOW, binding, inventory, observation, policy


@pytest.mark.parametrize("code,raised", [
    ("provider_cache_stale", False), ("provider_cache_stale", True),
    ("account_busy", False), ("account_busy", True), ("provider_auth_failed", False),
])
def test_failed_probe_keeps_last_good_stale_and_exposes_actual_code(tmp_path, raised, code):
    async def run():
        store = Store(str(tmp_path / "accounts.db"))
        store.start()
        clock = [NOW]
        runtime = AccountRuntime(store, Sessions(store), policy=policy(), clock=lambda: clock[0])
        account = binding("ours", "thoth", "claude")
        failure = [False]
        calls = []

        async def execute(selected, action, *_args):
            calls.append(action)
            if failure[0]:
                if raised:
                    raise AccountError(code)
                return {"outcome": code}
            value = observation(selected, clock[0])
            value["windows"]["weekly"]["pct"] = 50
            return {"outcome": "ok", "binding_key": selected.key, "observation": value}

        executor = AccountExecutor(runtime, None, execute=execute)
        runtime.executor = executor
        try:
            await runtime.run(lambda ledger: ledger.register(account))
            assert (await executor.observe(account))["outcome"] == "ok"
            failure[0] = True
            clock[0] += 1
            assert (await executor.observe(account))["outcome"] == code
            status = await runtime.operation({"action": "status", "_auth_context": {"token_verified": True}})
            pool = status["quota_pools"][0]
            assert pool["observation"] is not None, "failed collection blanked a previously good meter"
            assert pool["observation"]["windows"]["weekly"]["pct"] == 50
            assert pool["observation"]["observed_at"] == NOW
            assert pool["observation"]["collection"] == "failed"
            assert pool["fresh"] is False and pool["age_seconds"] == 1
            assert pool["error_codes"] == {account.key: code}
            assert status["accounts"][0]["observation"]["error_code"] == code
            assert status["accounts"][0]["generation"] == account.generation
            if code == "provider_auth_failed":
                assert status["accounts"][0]["health"] == "auth_unhealthy"
                with pytest.raises(AccountError, match="account_unavailable"):
                    await runtime.run(lambda ledger: ledger.ensure_available(account.key))
            assert calls == ["observe", "observe"]
            failure[0] = False
            clock[0] += 1
            assert (await executor.observe(account))["outcome"] == "ok"
            recovered = (await runtime.run(lambda ledger: ledger.readback(now=clock[0])))["quota_pools"][0]
            assert recovered["fresh"] is True
            assert recovered["error_codes"] == {}
            assert recovered["observation"] is not None
            assert recovered["observation"].get("error_code") is None
            if code == "provider_auth_failed":
                with pytest.raises(AccountError, match="account_unavailable"):
                    await runtime.run(lambda ledger: ledger.ensure_available(account.key))
        finally:
            store.stop()

    asyncio.run(run())


def test_no_good_observation_stays_unknown_with_actual_failure_code():
    accounts = inventory()
    account = binding()
    failed = dict(observation(account), collection="failed", observed_at=None, windows={},
                  error_code="provider_timeout")
    accounts.observe(account.key, failed, now=NOW)
    pool = next(row for row in accounts.readback(now=NOW)["quota_pools"] if row["pool_id"] == account.pool_id)
    assert pool["observation"] is None and not pool["fresh"]
    assert pool.get("error_codes") == {account.key: "provider_timeout"}


def test_failed_host_never_hides_a_fresh_same_pool_source_or_changes_identity():
    accounts = inventory()
    first = binding()
    second = replace(first, host="host-b", home="/private-fixture/host-b/original")
    accounts.register(second)
    accounts.observe(first.key, observation(first), now=NOW)
    accounts.observe(second.key, observation(second, NOW + 1), now=NOW + 1)
    accounts.observe(second.key, dict(observation(second), collection="failed", observed_at=None,
                                     windows={}, error_code="provider_cache_stale"), now=NOW + 2)
    pool = next(row for row in accounts.readback(now=NOW + 2)["quota_pools"] if row["pool_id"] == first.pool_id)
    assert pool["fresh"] and pool["source_binding"] == first.key
    assert pool.get("error_codes") == {second.key: "provider_cache_stale"}
    invalid = dict(observation(first, NOW + 3), provider_id="different-native-account")
    accounts.observe(first.key, invalid, now=NOW + 3)
    with pytest.raises(AccountError, match="account_unavailable"):
        accounts.ensure_available(first.key)


def test_last_good_is_not_copied_to_a_successor_generation():
    accounts = inventory()
    old = binding()
    accounts.observe(old.key, observation(old), now=NOW)
    successor = replace(old, generation="successor")
    accounts.register(successor)
    accounts.observe(successor.key, dict(observation(successor), collection="failed", observed_at=None,
                                        windows={}, error_code="provider_cache_stale"), now=NOW + 1)
    pool = next(row for row in accounts.readback(now=NOW + 1)["quota_pools"] if row["pool_id"] == old.pool_id)
    assert pool["observation"] is None
    assert pool.get("error_codes") == {successor.key: "provider_cache_stale"}


def test_repeated_failures_keep_upstream_time_and_latest_safe_code():
    accounts = inventory()
    account = binding()
    accounts.observe(account.key, observation(account), now=NOW)
    for offset, code in enumerate(("provider_cache_stale", "account_busy"), 1):
        accounts.observe(account.key, dict(observation(account), collection="failed", observed_at=None,
                                          windows={}, error_code=code), now=NOW + offset)
        pool = accounts.readback(now=NOW + offset)["quota_pools"][0]
        assert pool["observation"]["observed_at"] == NOW
        assert pool["age_seconds"] == offset and not pool["fresh"]
        assert pool["error_codes"] == {account.key: code}


@pytest.mark.parametrize("code", [None, {"secret": "fixture"}, "raw diagnostic text\n", "x" * 81])
def test_failure_readback_never_carries_arbitrary_host_details(code):
    accounts = inventory()
    account = binding()
    accounts.observe(account.key, dict(observation(account), collection="failed", observed_at=None,
                                      windows={}, error_code=code), now=NOW)
    status = accounts.readback(now=NOW)
    assert status["quota_pools"][0]["error_codes"] == {}
    assert "error_code" not in status["accounts"][0]["observation"]
