"""Provider-scoped rollout preserves legacy launches without account fallback."""
import asyncio
import json
from dataclasses import asdict

import pytest

from account_executor import AccountExecutor
from account_runtime import AccountRuntime, load_policy
from provider_accounts import AccountError
from sessions import VerbError
from test_account_rotation import binding
from test_account_runtime import AUTH, setup


def scoped(providers=None):
    return {"enabled": True, "managed_hosts": ["host-a"],
        "managed_providers": {"host-a": ["codex"] if providers is None else providers}}


async def choose(runtime, provider="claude", **message):
    return await runtime.choose_launch(host="host-a", provider=provider,
        model="fixture", effort="medium", message=message)


def test_codex_only_scope_keeps_never_managed_claude_legacy():
    async def journey():
        store, runtime = await setup()
        try:
            runtime.policy.update(scoped())
            before = await runtime.run(lambda a: a.defaults())
            assert await choose(runtime) is None
            assert (await choose(runtime, "codex")).key == binding().key
            assert (await choose(runtime, "codex", account="primary", _auth_context=AUTH)).key == binding().key
            assert await runtime.run(lambda a: a.defaults()) == before
            # Existing conservative host-level raw-command guards still work.
            assert runtime.manages("host-a")
            assert not runtime.manages("host-untouched")
        finally:
            store.stop()
    asyncio.run(journey())


@pytest.mark.parametrize("message", [
    {"account": "primary", "_auth_context": AUTH},
    {"resume_session_id": "native", "_account_resume_binding": binding(provider="claude").public()},
])
def test_excluded_explicit_account_and_bound_resume_refuse(message):
    async def journey():
        store, runtime = await setup()
        try:
            runtime.policy.update(scoped())
            with pytest.raises(AccountError, match="account_feature_off"):
                await choose(runtime, **message)
        finally:
            store.stop()
    asyncio.run(journey())


@pytest.mark.parametrize("retired_scope", [scoped([]),
    {"enabled": True, "managed_hosts": [], "managed_providers": {}}])
def test_retained_binding_prevents_legacy_fallback_after_scope_removal(retired_scope):
    async def journey():
        store, runtime = await setup()
        try:
            runtime.policy.update(retired_scope)
            with pytest.raises(AccountError, match="provider_cli_pin_unavailable"):
                await choose(runtime, "codex")
            assert await choose(runtime) is None
        finally:
            store.stop()
    asyncio.run(journey())


def test_omitted_scope_preserves_host_wide_admission_and_missing_default_refusal():
    async def journey():
        store, runtime = await setup()
        try:
            runtime.policy["managed_hosts"] = ["host-a"]
            with pytest.raises(AccountError, match="account_default_unconfigured"):
                await choose(runtime)
            runtime.policy.update(scoped())
            await runtime.record_failure(binding(), "auth")
            with pytest.raises(AccountError, match="account_unavailable"):
                await choose(runtime, "codex")
            with pytest.raises(AccountError, match="account_unconfigured"):
                await choose(runtime, "codex", account="missing", _auth_context=AUTH)
            assert (await runtime.run(lambda a: a.defaults()))[0]["alias"] == "primary"
        finally:
            store.stop()
    asyncio.run(journey())


@pytest.mark.parametrize("patch", [
    {"managed_providers": None}, {"managed_providers": []},
    {"managed_providers": {"host-a": "codex"}},
    {"managed_providers": {"host-a": ["unknown"]}},
    {"managed_providers": {"host-a": ["codex", "codex"]}},
    {"managed_providers": {"host-a": [None]}},
    {"managed_providers": {}},
    {"managed_providers": {"host-a": ["codex"], "host-b": ["claude"]}},
    {"managed_hosts": "host-a"}, {"managed_hosts": ["host-a", "host-a"]},
    {"managed_hosts": ["../host-a"], "managed_providers": {"../host-a": ["codex"]}},
    {"managed_hosts": None},
])
def test_malformed_scope_fails_closed_at_policy_load_and_runtime_init(tmp_path, patch):
    policy = scoped() | patch
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy)); path.chmod(0o600)
    with pytest.raises(AccountError, match="account_policy_invalid"):
        load_policy(path)
    with pytest.raises(AccountError, match="account_policy_invalid"):
        AccountRuntime(None, None, policy=policy)


def test_explicit_scope_requires_explicit_host_list(tmp_path):
    policy = scoped(); del policy["managed_hosts"]
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy)); path.chmod(0o600)
    with pytest.raises(AccountError, match="account_policy_invalid"):
        load_policy(path)


def test_empty_scope_is_valid_and_does_not_import_excluded_startup_receipts():
    async def journey():
        store, runtime = await setup()
        try:
            runtime.policy.update(scoped([]))
            runtime.policy["bindings"] = [binding(provider="claude").public()]
            await runtime.start()
            assert not runtime.manages("host-a")
            assert await runtime.run(lambda a: a.bound(binding(provider="claude").key)) is None
            assert await choose(runtime) is None
        finally:
            store.stop()
    asyncio.run(journey())


@pytest.mark.parametrize("action", ["default-set", "probe", "relogin", "revalidate-health"])
def test_excluded_account_operations_refuse_before_host_effects(action):
    async def journey():
        store, runtime = await setup(); calls = []
        async def execute(*args):
            calls.append(args); raise AssertionError("excluded native operation")
        runtime.executor = AccountExecutor(runtime, None, execute=execute)
        try:
            runtime.policy.update(scoped())
            with pytest.raises(VerbError, match="account_feature_off"):
                await runtime.operation({"action": action, "host": "host-a", "provider": "claude",
                    "account": "primary", "login_generation": "login-one", "_auth_context": AUTH})
            assert not calls
        finally:
            store.stop()
    asyncio.run(journey())


def test_collection_and_internal_revalidation_share_provider_scope(tmp_path):
    from test_account_health_revalidation import fixture, request, state
    async def journey():
        store, runtime, claude, calls = await fixture(tmp_path / "state.db", "claude")
        try:
            runtime.policy.update(scoped())
            await runtime.executor.collect_all()
            assert calls and all(key.split(":")[1] == "codex" for key, _ in calls)
            calls.clear()
            before = await state(runtime, claude)
            with pytest.raises(AccountError, match="account_feature_off"):
                await runtime.executor.revalidate_health(await request(runtime, claude), AUTH)
            assert not calls
            assert await state(runtime, claude) == before
            # Status and immutable historical bindings survive scope edits.
            assert await runtime.run(lambda a: a.bound(claude.key)) == claude
        finally:
            store.stop()
    asyncio.run(journey())


def test_existing_bound_seat_keeps_identity_after_scope_removal():
    async def journey():
        store, runtime = await setup()
        try:
            account = await choose(runtime, "codex")
            row = await store.open_session("host-a", "bound", session_generation="seat-one",
                provider="codex", account_binding=account.public())
            await runtime.bind_launch(row)
            runtime.policy.update(scoped([]))
            from test_provider_cli_pin import artifact_executor
            await artifact_executor(runtime)
            await runtime.before_input(row)
            assert (await store.fetch_session("host-a", "bound"))["account_binding"] == account.public()
            assert await runtime.run(lambda a: asdict(a.bound(account.key))) == asdict(binding())
        finally:
            store.stop()
    asyncio.run(journey())


@pytest.mark.parametrize("message", [{"command": "/bin/echo"},
    {"command": "/bin/echo", "provider": "claude"}, {}])
def test_host_level_raw_command_guards_remain_conservative(message):
    from spawnctl import SpawnCtl
    from types import SimpleNamespace
    async def journey():
        store, runtime = await setup()
        try:
            runtime.policy.update(scoped())
            with pytest.raises(VerbError) as error:
                await SpawnCtl._resolve_launch(SimpleNamespace(account_runtime=runtime), message, "host-a", "seat")
            assert error.value.code == "managed_command_forbidden"
        finally:
            store.stop()
    asyncio.run(journey())


def test_in_scope_codex_health_revalidation_still_works(tmp_path):
    from test_account_health_revalidation import fixture, request, state
    async def journey():
        store, runtime, account, calls = await fixture(tmp_path / "state.db")
        try:
            runtime.policy.update(scoped())
            result = await runtime.operation(await request(runtime, account))
            assert result["result"]["outcome"] == "health_revalidated"
            assert (await state(runtime, account))["health"] == "healthy"
            assert calls == [(account.key, "observe")]
        finally:
            store.stop()
    asyncio.run(journey())
