"""Same-home version changes preserve the existing healthy authentication."""
import asyncio
from dataclasses import replace

import pytest
from account_executor import AccountExecutor
from provider_accounts import AccountError
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
        async def execute(account, action, operation_id, generation=None):
            calls.append(action)
            if action == "observe":
                return {"outcome": "ok", "binding_key": new.key, "observation": observation(new)}
            return {"state": "done", "outcome": "ok", "provider": provider, "alias": old.alias,
                    "expected_generation": old.generation, "generation": new.generation,
                    "binding": new.public()}
        runtime.executor = AccountExecutor(runtime, None, execute=execute)
        try:
            await runtime.run(lambda a: a.register(old))
            await runtime.run(lambda a: a.set_default(host=old.host, provider=provider, alias=old.alias))
            await store.set_spawn_admission_hold(old.host, held_until=NOW + 1000, reason="repin fixture")
            await seed_pin(runtime, release_pair())
            result = await runtime.operation({"action": "repin", "host": old.host, "provider": provider,
                "account": old.alias, "login_generation": old.generation, "new_generation": new.generation,
                "operation_id": "same-home-repin", "expected_pin_revision": 1,
                "expected_pin_digest": "d" * 64, "reason": "reviewed release", "_auth_context": AUTH})
            assert result["result"]["binding"]["home"] == old.home
            assert await runtime.run(lambda a: a.bound(old.key)) == old
            assert "enroll" not in calls
        finally:
            store.stop()
    asyncio.run(run())
