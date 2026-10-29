"""Fleet release admission through the real durable account/launch boundary."""
import asyncio
import json
from dataclasses import replace

import pytest
import launch
from account_runtime import AccountRuntime
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from store import Store
from test_account_rotation import binding, NOW
from test_account_runtime import setup, AUTH


def release_pair():
    return {
        "schema": 1, "revision": 1,
        "providers": {
            provider: {"version": version, "artifacts": {
                platform: {"provenance": "https://releases.example.test/" + provider + "/" + version,
                           "native_sha256": "a" * 64, "resource_sha256": "b" * 64}
                for platform in ("darwin-arm64", "linux-x64")}}
            for provider, version in (("claude", "2.1.312"), ("codex", "0.160.0"))},
    }


async def seed_pin(runtime, value):
    def write(ledger):
        ledger.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)",
                            ("provider_cli.release_pin", json.dumps(value)))
        ledger.conn.commit()
    await runtime.run(write)


def test_wrong_fleet_release_refuses_real_launch_resolution(tmp_path):
    async def run():
        store, runtime = await setup(str(tmp_path / "state.db"))
        try:
            await seed_pin(runtime, release_pair())
            ctl = SpawnCtl(store, runtime.sessions, tmux=object(),
                           machine=launch.local_machine("host-a", cwd=str(tmp_path), codex_bin="/bin/echo"))
            ctl.account_runtime = runtime
            with pytest.raises(VerbError, match="provider_cli_pin_mismatch"):
                await ctl._resolve_launch({"provider": "codex", "model": "gpt-5.6-sol",
                                           "effort": "high", "_defer_launch_token_stage": True},
                                          "host-a", "wrong-version")
            assert await store.fetch_session("host-a", "wrong-version") is None
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("launch_request", [
    {"provider": "codex", "model": "gpt-5.6-sol", "effort": "high"},
    {"command": "/bin/echo bypass"},
])
def test_retained_enrollment_fences_policy_off(tmp_path, launch_request):
    async def run():
        store, runtime = await setup()
        try:
            runtime.policy["enabled"] = False
            ctl = SpawnCtl(store, runtime.sessions, tmux=object(),
                           machine=launch.local_machine("host-a", cwd=str(tmp_path), codex_bin="/bin/echo"))
            ctl.account_runtime = runtime
            expected = "managed_command_forbidden" if "command" in launch_request else "provider_cli_pin_unavailable"
            with pytest.raises(VerbError, match=expected):
                await ctl._resolve_launch(dict(launch_request, _defer_launch_token_stage=True), "host-a", "off")
        finally:
            store.stop()
    asyncio.run(run())


def test_authenticated_fd_can_initialize_pair_by_cas():
    async def run():
        store, runtime = await setup()
        try:
            result = await runtime.operation({"action": "pin-set", "pin": release_pair(),
                "expected_revision": 0, "operation_id": "initialize-pair", "reason": "reviewed release",
                "_auth_context": AUTH})
            assert result["pin"]["revision"] == 1
            status = await runtime.operation({"action": "pin-status", "_auth_context": AUTH})
            assert status["pin"]["revision"] == 1
        finally:
            store.stop()
    asyncio.run(run())
