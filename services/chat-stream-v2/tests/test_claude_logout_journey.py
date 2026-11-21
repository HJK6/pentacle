"""Exit-one native status crosses the bound account recovery guard."""
import asyncio
import json
from subprocess import CompletedProcess

import pytest
from provider_accounts import AccountError
from sessions import VerbError
from test_account_repin import host_journey
from test_account_runtime import AUTH


@pytest.mark.parametrize("stdout,expected,admitted", [
    ('{"loggedIn": false}', "provider_auth_failed", True),
    ('{', "provider_rpc_failed", False),
    ('{}', "provider_rpc_failed", False),
    ('capacity', "provider_capacity", False),
])
def test_bound_probe_and_relogin_native_exit_one(tmp_path, monkeypatch, stdout, expected, admitted):
    async def run():
        store, runtime, host, old, _, calls, native, logins, cell, pin = await host_journey(
            tmp_path, monkeypatch, "claude", transport=True)
        import provider_cli_pin
        monkeypatch.setattr(provider_cli_pin, "check", lambda *args: None)
        previous_run = host.run
        def native_status(argv, **kwargs):
            if argv[1:] == ["auth", "status", "--json"]:
                assert kwargs["env"]["CLAUDE_CONFIG_DIR"] == old.home
                return CompletedProcess(argv, 1, stdout, "")
            return previous_run(argv, **kwargs)
        host.run = native_status
        host._probe_observation = host._observation
        entered = []
        async def boundary(account, state):
            entered.append((account.key, account.generation))
            return {"outcome": "operator_boundary"}
        runtime.executor._dispatch_recovery = boundary
        message = {"host": old.host, "provider": old.provider, "account": old.alias,
                   "login_generation": old.generation, "_auth_context": AUTH}
        try:
            probe = await runtime.operation(dict(message, action="probe"))
            assert probe["result"]["outcome"] == expected
            with pytest.raises(VerbError, match="account_control_unauthorized"):
                await runtime.operation(dict(message, action="relogin", operation_id="unauthorized", _auth_context={}))
            result = await runtime.operation(dict(message, action="relogin", operation_id="relogin-fixture"))
            assert result["result"]["outcome"] == ("operator_boundary" if admitted else expected)
            assert entered == ([(old.key, old.generation)] if admitted else [])
            assert host.registry.active(old.provider, old.alias, old.host).generation == old.generation
        finally:
            store.stop()
    asyncio.run(run())
