import asyncio
import pytest
from sessions import VerbError
from test_account_self_recovery import _external
from test_account_schedule_journey import fixture
from test_account_rotation import binding
from store import effective_assignment


def test_plain_unassigned_promptless_lead(tmp_path):
    async def run():
        store, runtime, pane, ctl, _, message = await fixture(tmp_path)
        try:
            account = await runtime.run(lambda db: db.bound(binding("spare", host="localhost").key))
            await runtime.record_failure(account, "auth")
            msg = message("spawn", provider="codex", account="spare", model="gpt-6.2-sol",
                          effort="high", role="lead", objective="review fixture")
            binding_row = await ctl._resolve_spec_binding(msg, "localhost", "review-lane")
            assigned = effective_assignment(binding_row)
            provision = await ctl._lane_admission(msg, binding_row, "lead")
            mirror = getattr(ctl, "_provisioning_form", None)
            print("effective-assignment", assigned, "provisions", bool(provision),
                  "mirror", mirror(msg, "lead") if mirror else "absent")
            assert not assigned and not provision
            with pytest.raises(VerbError) as denied:
                await _external(lambda value: ctl.spawn(value, "localhost"), "spawn", msg)
            marks = len(runtime._self_recovery_marks)
            print("refusal", denied.value.code, "marks", marks, "launches", pane.launches)
            assert (denied.value.code, marks, pane.launches) == ("account_unavailable", 1, 0)
        finally:
            store.stop()
    asyncio.run(run())
