import asyncio
import pytest
from sessions import VerbError
from test_account_self_recovery import _external
from test_account_schedule_journey import fixture, SPEC
from test_account_rotation import binding

@pytest.mark.parametrize("fail_lookup", [False, True])
def test_unavailable_reply_survives_signal_replay_lookup(tmp_path, monkeypatch, fail_lookup):
    async def run():
        store, runtime, pane, ctl, _, message = await fixture(tmp_path)
        try:
            a = await runtime.run(lambda db: db.bound(binding("spare", host="localhost").key))
            await runtime.record_failure(a, "auth")
            msg = message("spawn", spec_id=SPEC, provider="codex", account="spare", model="gpt-6.2-sol",
                          effort="high", initial_prompt="fixture", objective="review fixture")
            original = store.spawn_record_for_key
            count = 0
            async def replay_read(host, key):
                nonlocal count
                count += 1
                if count == 2 and fail_lookup:
                    raise RuntimeError("store overloaded: pending write queue is full")
                return await original(host, key)
            monkeypatch.setattr(store, "spawn_record_for_key", replay_read)
            try:
                await _external(lambda value: ctl.spawn(value, "localhost"), "spawn", msg)
            except Exception as exc:
                result = getattr(exc, "code", type(exc).__name__)
            else:
                result = "unexpected_success"
            print("fail_lookup", fail_lookup, "reads", count, "result", result,
                  "marks", len(runtime._self_recovery_marks), "launches", pane.launches)
            assert (count, result, pane.launches) == (2, "account_unavailable", 0)
        finally:
            store.stop()
    asyncio.run(run())
