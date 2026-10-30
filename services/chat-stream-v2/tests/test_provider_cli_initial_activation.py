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
            async with runtime.input_guard(row):
                pass
            status = await runtime.operation({"action": "pin-status", "_auth_context": AUTH})
            assert status["pin"] is None
            assert status["mode"] == "pre_enforcement"
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
