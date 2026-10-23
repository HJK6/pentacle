import asyncio
from dataclasses import replace
import json

import pytest
from account_runtime import AccountRuntime
from provider_accounts import AccountError
from sessions import Sessions
from store import Store
from test_account_rotation import binding, observation, policy, NOW


def test_binding_is_immutable_and_old_generation_keeps_its_original_attribution(tmp_path):
    async def run():
        store = Store(str(tmp_path / "state.db")); store.start(); sessions = Sessions(store, local_host="host-a")
        runtime = AccountRuntime(store, sessions, policy=policy(), clock=lambda: NOW)
        account = binding()
        try:
            await runtime.run(lambda ledger: ledger.register(account))
            await runtime.run(lambda ledger: ledger.observe(account.key, observation(account), now=NOW))
            row = await store.open_session("host-a", "seat", provider="codex", session_generation="one", pane_pid="100",
                account_binding=account.public(), effective_model="fixture-model", effective_effort="high")
            await runtime.bind_launch(row)
            with pytest.raises(ValueError, match="immutable"):
                await store.update_session("host-a", "seat", account_binding=binding("spare").public())
            with pytest.raises(ValueError, match="immutable"):
                await store.open_session("host-a", "seat", session_generation="one", account_binding=binding("spare").public())
            await store.update_session("host-a", "seat", status="closed")
            new = replace(account, generation="next-login", home="/private-fixture/next-login")
            await runtime.run(lambda ledger: ledger.register(new))
            await store.open_session("host-a", "seat", provider="codex", session_generation="two", account_binding=new.public())
            # Historical accounting joins the original immutable binding,
            # never today's host default or the reopened seat's account.
            snapshot = await store.submit(lambda conn: __import__("store_usage").snapshot_conn(conn, {
                "host": "host-a", "session_name": "seat", "stream_id": "host-a:seat", "session_generation": "one", "provider": "codex"}))
            assert snapshot["account_binding"] == account.public()
            assert (await store.fetch_session("host-a", "seat"))["account_binding"] == new.public()
            assert await runtime.run(lambda accounts: accounts.conn.execute(
                "SELECT COUNT(*) FROM v2_account_session_bindings").fetchone()[0]) == 1
        finally: store.stop()
    asyncio.run(run())
