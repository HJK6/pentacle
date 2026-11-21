"""Exercise satellite's spawn and account fences on the same durable hold."""
import asyncio

import pytest

from account_runtime import AccountRuntime
from deploy.first_qualification import change_admission
import qualification_admission
from provider_accounts import AccountError
from provider_cli_fixtures import seed_pin
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from store import Store
from test_account_rotation import binding
from test_provider_cli_pin import artifact_executor


def test_only_owned_qualification_requests_cross_both_fences(tmp_path):
    async def exercise():
        import os

        database = str(tmp_path / "admission.db")
        store = Store(database)
        store.start()
        try:
            sessions = Sessions(store, local_host="satellite")
            spawner = SpawnCtl(store, sessions, tmux=object())
            runtime = AccountRuntime(store, sessions, policy={"enabled": True})
            account = binding(host="satellite", alias="ours")
            await runtime.run(lambda ledger: ledger.register(account))
            await seed_pin(runtime)
            await artifact_executor(runtime)

            key = "0123456789abcdef" * 2
            approved = qualification_admission.smoke_request("satellite", key, "codex", "prompted", "gpt-6-luna", "low")
            message = {**approved, "request_id": "fixture-owned", "catalog_version": "fixture-catalog",
                       "_auth_context": {"operator_authenticated": True}}
            cell = {"request": approved, "binding": account.public()}
            receipt = {"accounts": {"database": database}, "receipt_sha256": "a" * 64,
                       "qualification_admission_before": [None, None], "intent": {
                           "first_qualification": {"expires_at": "2099-01-01T00:00:00Z",
                                                   "prior_exclusion": {"host": "satellite"}}}}
            row = {"stream_id": "satellite:" + approved["session_name"], "session_generation": "one",
                   "account_binding": account.public()}
            change_admission(receipt, "fenced")
            with pytest.raises(VerbError, match="frozen"):
                await spawner._spawn_impl({"type": "spawn", "host": "satellite", "session_name": "unrelated",
                                          "command": "/bin/false"}, "satellite")
            change_admission(receipt, "testing", pid=os.getpid(), cells={key: cell})

            async def claim(payload):
                return await store.atomic_claim_or_replay("satellite", approved["session_name"], ttl_s=10,
                    request_id=payload["request_id"], idempotency_key=key, request_payload_hash="owned",
                    refuse_if_held=True, qualification_message=payload)

            assert (await claim({**message, "initial_prompt": "unrelated"}))["status"] == "frozen"
            assert (await claim({**message, "_auth_context": {}}))["status"] == "frozen"
            with pytest.raises(AccountError, match="provider_cli_pin_unavailable") as refused:
                await runtime.admit_launch(row, message=message)
            assert refused.value.context["reason"] == "admission_hold"
            assert (await claim(message))["status"] == "claimed"
            with pytest.raises(AccountError, match="provider_cli_pin_unavailable"):
                await runtime.admit_launch(row, message={**message, "request_id": "different"})
            await runtime.admit_launch(row, message=message)
            assert (await claim(message))["status"] == "replay"
            assert await store.submit(lambda conn: conn.execute(
                "SELECT COUNT(*) FROM v2_account_session_bindings WHERE stream_id=?", (row["stream_id"],)
            ).fetchone()[0]) == 1
            change_admission(receipt, "rollback")
            with pytest.raises(VerbError, match="frozen"):
                await spawner._spawn_impl({"type": "spawn", "host": "satellite", "session_name": "after-rollback",
                                          "command": "/bin/false"}, "satellite")
            with pytest.raises(AccountError, match="provider_cli_pin_unavailable"):
                await runtime.admit_launch(row, message=message)
            assert await store.fetch_session("satellite", "after-rollback") is None
        finally:
            store.stop()

    asyncio.run(exercise())
