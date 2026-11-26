import asyncio
import pytest
from provider_cli_fixtures import seed_pin
from test_account_self_recovery_r2 import (
    _world, _health, _proof, _receipts, _bound_host, _successor,
)

@pytest.mark.parametrize("scenario", ["stable", "installed"])
@pytest.mark.parametrize("signal", ["contradiction", "success"])
def test_marked_call_decided_against_current_pin(tmp_path, scenario, signal):
    async def run():
        store, runtime, account, revision = await _world(tmp_path / "review.db")
        alert_codes = []
        try:
            async def alert(_account, code):
                alert_codes.append(code)
            runtime._alert = alert
            async def change():
                if scenario == "installed":
                    await seed_pin(runtime)
            reply = None
            if signal == "contradiction":
                reply = lambda selected: {"outcome": "provider_identity_or_cache_unverified",
                                          "identity_evidence": "contradiction"}
            calls = _bound_host(runtime, during=change, reply=reply)
            result = await runtime.executor.observe(account)
            health = await _health(runtime, account)
            receipts = len(await _receipts(runtime, account))
            fact = await runtime.run(lambda a: a.conn.execute(
                "SELECT count(*) FROM v2_account_failures WHERE binding_key=? AND kind='identity'",
                (account.key,)).fetchone()[0])
            print(scenario, signal, [action for action,_ in calls], result["outcome"],
                  health, "identity_facts", fact, "alerts", alert_codes, "receipts", receipts)
            if scenario == "installed":
                assert (health, fact, alert_codes, receipts) == (("auth_unhealthy", revision), 0, [], 0)
            elif signal == "contradiction":
                assert (health, fact, alert_codes, receipts) == (
                    ("identity_conflict", revision + 1), 1, ["identity_unverified"], 0)
            else:
                assert (health, fact, alert_codes, receipts) == (("healthy", revision + 1), 0, [], 1)
        finally:
            store.stop()
    asyncio.run(run())

def test_retirement_work_is_local_to_cell(tmp_path):
    class Visited(dict):
        def __init__(self, entries):
            super().__init__(entries)
            self.visited = []
        def __iter__(self):
            for key in super().__iter__():
                self.visited.append(key)
                yield key
    async def run():
        store, runtime, account, rev = await _world(tmp_path / "cleanup.db")
        try:
            unrelated = ("elsewhere:claude:other:other-generation", "other-generation")
            marked = Visited(runtime._self_recovery_marks)
            marked[unrelated] = rev
            runtime._self_recovery_marks = marked
            await runtime.run(lambda a: a.register(_successor(account, "second")))
            print("unrelated preserved", unrelated in marked, "visited", unrelated in marked.visited)
            assert unrelated in marked and unrelated not in marked.visited
        finally:
            store.stop()
    asyncio.run(run())
