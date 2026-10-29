"""Disposable real host/Store upgrade journey; provider and browser are counterparts."""
import asyncio
from dataclasses import asdict
from dataclasses import replace
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from account_executor import AccountExecutor
from provider_account_host import NativeHost
from provider_accounts import AccountError, Binding
from sessions import VerbError
from test_account_rotation import NOW, observation
from test_account_runtime import AUTH, setup


async def journey(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    old_bin, new_bin = tmp_path / "old", tmp_path / "new"
    old_bin.write_text("old fixture"); new_bin.write_text("new fixture")
    cell = dict(enabled=True, expected_provider_id="native-ours", profile_ref="fixture-profile",
        executable=str(old_bin), version="fixture-old", platform="wsl", model="fixture-model",
        effort="high", cwd=str(tmp_path), executable_sha256=hashlib.sha256(old_bin.read_bytes()).hexdigest())
    config = dict(host="host-target", registry=str(private / "registry.json"), root=str(private / "homes"),
        cells={"codex:ours": cell})
    logins, calls = [], []
    def login(binding, browser):
        logins.append(binding.generation)
        path = Path(binding.home) / "auth.json"
        path.write_text("fake-fixture-only"); path.chmod(0o600)
        return "ok"
    def read(binding):
        return observation(binding) | dict(provider_id="native-ours", pool_id="pool-ours")
    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=(cell["version"] if argv[-1] == "--version" else "OK"))
    host = NativeHost(config, run=run, login=login, observation=read)
    first = host.handle("enroll", provider="codex", alias="ours", expected_generation="unenrolled",
        operation_id="initial", generation="old")
    assert first["outcome"] == "ok"
    old = Binding.from_public(first["binding"])
    store, runtime = await setup(str(tmp_path / "state.db"))
    await runtime.run(lambda a: a.register(old))
    await runtime.run(lambda a: a.set_default(host="host-target", provider="codex", alias="ours"))
    await store.set_spawn_admission_hold("host-target", held_until=NOW + 1000, reason="fixture-upgrade")
    async def execute(binding, action, op, generation=None):
        calls.append(action)
        return host.handle(action, provider=binding.provider, alias=binding.alias,
            expected_generation=binding.generation, operation_id=hashlib.sha256(op.encode()).hexdigest(), generation=generation)
    runtime.executor = AccountExecutor(runtime, None, execute=execute)
    assert (await runtime.executor.recover(old, relogin=True, operation_id="healthy"))["outcome"] == "healthy"
    assert logins == ["old"]
    cell.update(executable=str(new_bin), version="fixture-new", executable_sha256=hashlib.sha256(new_bin.read_bytes()).hexdigest())
    op = "fixture-upgrade"
    result = host.handle("enroll", provider="codex", alias="ours", expected_generation="old",
        operation_id=hashlib.sha256(op.encode()).hexdigest(), generation="new")
    assert result["outcome"] == "ok" and logins == ["old", "new"]
    new = Binding.from_public(result["binding"])
    runtime.policy["bindings"] = [asdict(new)]
    await runtime.start()
    assert (await runtime.run(lambda a: a._cell("host-target", "codex", "ours")))["generation"] == "old"
    request = dict(action="upgrade", host="host-target", provider="codex", account="ours", login_generation="old",
        new_generation="new", operation_id=op, expected_version=new.version,
        expected_executable_sha256=new.executable_sha256, reason="approved_bound_executable_upgrade", _auth_context=AUTH)
    return store, runtime, host, old, new, request, calls


def test_healthy_host_enrollment_requires_explicit_receiver_import(tmp_path):
    async def run():
        store, runtime, host, old, new, request, calls = await journey(tmp_path)
        try:
            result = await runtime.operation(request)
            assert result["result"]["outcome"] == "upgraded"
            assert calls == ["observe", "status", "observe"]
            assert (await runtime.run(lambda a: a.select(host="host-target", provider="codex"))).generation == "new"
            assert await runtime.run(lambda a: a.bound(old.key)) == old
            assert next(d for d in await runtime.run(lambda a: a.defaults()) if d["host"] == "host-target")["revision"] == 1
        finally:
            store.stop()
    asyncio.run(run())


def test_off_safe_parking_on_exact_post_upgrade_database_copy(tmp_path):
    import sqlite3
    from store import Store
    async def run():
        store, runtime, host, old, new, request, calls = await journey(tmp_path)
        try:
            await runtime.operation(request)
        finally: store.stop()
        copied = str(tmp_path / "parking.db")
        with sqlite3.connect(str(tmp_path / "state.db")) as source, sqlite3.connect(copied) as target:
            source.backup(target)
        store = Store(copied); store.start(); runtime.store = store; runtime.policy["enabled"] = False
        try:
            await runtime.start()
            with pytest.raises(AccountError, match="provider_cli_pin_unavailable"):
                await runtime.choose_launch(host=old.host, provider="codex", model="fixture", effort="high", message={})
            for message in ({"account":"ours", "_auth_context":AUTH},
                    {"resume_session_id":"native-id", "_account_resume_binding":new.public()}):
                with pytest.raises(AccountError, match="provider_cli_pin_unavailable"):
                    await runtime.choose_launch(host=old.host, provider="codex", model="fixture", effort="high", message=message)
            assert await runtime.run(lambda a: a.bound(old.key)) == old
            assert await runtime.run(lambda a: a.bound(new.key)) == new
            assert host.registry.active("codex", "ours", old.host).generation == "new"
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("auth", [{}, dict(AUTH, token_verified=False), dict(AUTH, stream_id="host-a:worker"),
    dict(AUTH, session_generation="stale"), {"role": "front-desk"},
    {"service_authenticated": True, "service_actor": "host-target:watcher"}])
def test_upgrade_authentication_precedes_host_calls(tmp_path, auth):
    async def run():
        store, runtime, host, old, new, request, calls = await journey(tmp_path)
        try:
            with pytest.raises(VerbError, match="account_control_unauthorized"):
                await runtime.operation(dict(request, _auth_context=auth))
            assert calls == ["observe"]
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("case", ["off", "unmanaged", "no-hold", "expired-hold", "old-stale", "active", "same-new",
    "claude", "other-host", "unhealthy", "identity-conflict", "payload-path", "bad-hash", "empty-reason"])
def test_admission_refusals_never_read_host(tmp_path, case):
    async def run():
        store, runtime, host, old, new, request, calls = await journey(tmp_path)
        try:
            if case == "off": runtime.policy["enabled"] = False
            if case == "unmanaged": runtime.policy.update(managed_hosts=["host-target"], managed_providers={"host-target": []})
            if case == "no-hold": await store.clear_spawn_admission_hold("host-target")
            if case == "expired-hold": await store.set_spawn_admission_hold("host-target", held_until=NOW, reason="expired")
            if case == "old-stale": request["login_generation"] = "stale"
            if case == "active": request["login_generation"] = "active"
            if case == "same-new": request["new_generation"] = "old"
            if case == "claude": request["provider"] = "claude"
            if case == "other-host": request["host"] = "host-a"
            if case == "unhealthy": await runtime.record_failure(old, "auth")
            if case == "identity-conflict": await runtime.record_failure(old, "identity")
            if case == "payload-path": request["executable"] = "/injected"
            if case == "bad-hash": request["expected_executable_sha256"] = "invalid"
            if case == "empty-reason": request["reason"] = " "
            with pytest.raises(VerbError): await runtime.operation(request)
            assert calls == ["observe"]
            assert (await runtime.run(lambda a: a._cell(old.host, old.provider, old.alias)))["generation"] == "old"
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("field,value", [("provider_id", "foreign"), ("pool_id", "foreign"), ("home", "/elsewhere/new"),
    ("version", "wrong"), ("executable_sha256", "0" * 64), ("generation", "wrong"), ("enabled", False),
    ("healthy", False), ("default_revision", 1), ("observer_executable", "/wrapper"), ("alias", "spare"),
    ("executable", "relative"), ("home", "OLD"), ("executable", "OLD"), ("executable_sha256", "OLD")])
def test_wrong_host_binding_refuses_before_observation(tmp_path, field, value):
    async def run():
        store, runtime, host, old, new, request, calls = await journey(tmp_path)
        execute = runtime.executor.execute
        async def tampered(binding, action, *args):
            result = await execute(binding, action, *args)
            if action == "status":
                result = json_copy(result)
                result["binding"][field] = getattr(old, field) if value == "OLD" else value
            return result
        runtime.executor.execute = tampered
        try:
            with pytest.raises(VerbError): await runtime.operation(request)
            assert calls == ["observe", "status"]
        finally: store.stop()
    asyncio.run(run())


def json_copy(value):
    import json
    return json.loads(json.dumps(value))


@pytest.mark.parametrize("case", ["absent", "started", "failure", "wrong-operation", "missing-binding", "bad-binding"])
def test_incomplete_or_conflicting_host_receipt_refuses(tmp_path, case):
    async def run():
        store, runtime, host, old, new, request, calls = await journey(tmp_path)
        execute = runtime.executor.execute
        async def tampered(binding, action, *args):
            result = await execute(binding, action, *args)
            if action == "status":
                result = json_copy(result)
                if case in {"absent", "started"}: result["state"] = case
                if case == "failure": result["outcome"] = "account_enrollment_failed"
                if case == "wrong-operation": result["expected_generation"] = "wrong"
                if case == "missing-binding": result.pop("binding")
                if case == "bad-binding": result["binding"] = {"home": "wrong"}
            return result
        runtime.executor.execute = tampered
        try:
            with pytest.raises(VerbError): await runtime.operation(request)
            assert calls == ["observe", "status"]
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("case", ["missing", "failed", "wrong-key", "identity", "pool", "auth", "collection", "windows", "nan"])
def test_incoherent_observation_cannot_commit(tmp_path, case):
    async def run():
        store, runtime, host, old, new, request, calls = await journey(tmp_path)
        execute = runtime.executor.execute
        async def tampered(binding, action, *args):
            result = await execute(binding, action, *args)
            if action == "observe":
                result = json_copy(result)
                if case == "missing": result.pop("observation")
                if case == "failed": result["outcome"] = "provider_auth_failed"
                if case == "wrong-key": result["binding_key"] = old.key
                if case == "identity": result["observation"]["provider_id"] = "foreign"
                if case == "pool": result["observation"]["pool_id"] = "foreign"
                if case == "auth": result["observation"]["auth_mode"] = "api-key"
                if case == "collection": result["observation"]["collection"] = "failed"
                if case == "windows": result["observation"]["windows"] = {}
                if case == "nan": result["observation"]["observed_at"] = float("nan")
            return result
        runtime.executor.execute = tampered
        try:
            with pytest.raises(VerbError): await runtime.operation(request)
            assert await runtime.run(lambda a: a.bound(new.key)) is None
            assert (await runtime.run(lambda a: a._cell(old.host, old.provider, old.alias)))["health"] == "healthy"
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("case", ["default", "health", "hold", "hold-expired", "generation"])
def test_commit_compares_live_revisions_default_and_hold(tmp_path, case):
    async def run():
        store, runtime, host, old, new, request, calls = await journey(tmp_path)
        execute = runtime.executor.execute
        async def concurrent(binding, action, *args):
            result = await execute(binding, action, *args)
            if action == "observe":
                if case == "default": await runtime.run(lambda a: a.set_default(host=old.host, provider=old.provider, alias=old.alias))
                if case == "health": await runtime.record_failure(old, "identity")
                if case == "hold": await store.set_spawn_admission_hold(old.host, held_until=NOW+1000, reason="different-owner")
                if case == "hold-expired": await store.set_spawn_admission_hold(old.host, held_until=NOW, reason="expired")
                if case == "generation": await runtime.run(lambda a: a.register(replace(old, generation="other", home=old.home+"-other")))
            return result
        runtime.executor.execute = concurrent
        try:
            with pytest.raises(VerbError): await runtime.operation(request)
            assert await runtime.run(lambda a: a.bound(new.key)) is None
            assert host.registry.active("codex", "ours", "host-target").generation == "new"
        finally: store.stop()
    asyncio.run(run())


def test_import_transaction_rolls_back_and_same_intent_recovers(tmp_path, monkeypatch):
    from account_store import AccountStore
    async def run():
        store, runtime, host, old, new, request, calls = await journey(tmp_path)
        real = AccountStore._observe
        def interrupted(*args, **kwargs): raise RuntimeError("injected mid-commit")
        try:
            monkeypatch.setattr(AccountStore, "_observe", interrupted)
            with pytest.raises(RuntimeError, match="injected"): await runtime.operation(request)
            assert await runtime.run(lambda a: a.bound(new.key)) is None
            assert (await runtime.run(lambda a: a._cell(old.host, old.provider, old.alias)))["generation"] == "old"
            monkeypatch.setattr(AccountStore, "_observe", real)
            runtime.executor = AccountExecutor(runtime, None, execute=runtime.executor.execute)
            assert (await runtime.operation(request))["result"]["outcome"] == "upgraded"
            assert calls == ["observe", "status", "observe", "status", "observe"]
        finally: store.stop()
    asyncio.run(run())


def test_pending_restart_readonly_retry_and_terminal_replay(tmp_path):
    from account_runtime import AccountRuntime
    from sessions import Sessions
    from store import Store
    async def run():
        store, runtime, host, old, new, request, calls = await journey(tmp_path)
        execute = runtime.executor.execute
        async def interrupted(*args, **kwargs): raise RuntimeError("before host read")
        runtime.executor.execute = interrupted
        try:
            with pytest.raises(RuntimeError, match="before host read"): await runtime.operation(request)
            server, policy = runtime.server, runtime.policy
            store.stop(); store = Store(str(tmp_path / "state.db")); store.start()
            runtime = AccountRuntime(store, Sessions(store), policy=policy, clock=lambda: NOW)
            runtime.server = server
            await runtime.start()
            assert calls == ["observe"]
            runtime.executor = AccountExecutor(runtime, None, execute=execute)
            first = await runtime.operation(request)
            store.stop(); store = Store(str(tmp_path / "state.db")); store.start()
            runtime = AccountRuntime(store, Sessions(store), policy=policy, clock=lambda: NOW)
            runtime.server = server
            runtime.executor = AccountExecutor(runtime, None, execute=execute)
            await store.clear_spawn_admission_hold(old.host)
            # A stale policy receipt and later default revision cannot rewrite history.
            runtime.policy["bindings"] = [asdict(old)]
            await runtime.start()
            await runtime.run(lambda a: a.set_default(host=old.host, provider=old.provider, alias=old.alias))
            assert await runtime.operation(request) == first
            assert calls == ["observe", "status", "observe"]
            with pytest.raises(VerbError, match="account_upgrade_id_conflict"):
                await runtime.operation(dict(request, reason="changed"))
            runtime.policy["enabled"] = False
            with pytest.raises(VerbError, match="account_feature_off"): await runtime.operation(request)
        finally: store.stop()
    asyncio.run(run())


def test_concurrent_upgrades_and_immutable_session_attribution(tmp_path):
    async def run():
        store, runtime, host, old, new, request, calls = await journey(tmp_path)
        try:
            selected = await runtime.run(lambda a: a.select(host=old.host, provider=old.provider))
            row = await store.open_session("host-target", "existing", session_generation="fixture-session", provider="codex",
                account_binding=selected.public())
            await runtime.bind_launch(row)
            results = await asyncio.gather(runtime.operation(request), runtime.operation(dict(request, operation_id="competing")), return_exceptions=True)
            assert results[0]["result"]["outcome"] == "upgraded" and isinstance(results[1], VerbError)
            assert calls == ["observe", "status", "observe"]
            assert (await store.fetch_session("host-target", "existing"))["account_binding"] == selected.public()
            assert await runtime.run(lambda a: a.bound(old.key)) == old
        finally: store.stop()
    asyncio.run(run())
