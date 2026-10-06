"""Unresolved product finding from TH-H4; intentionally remains RED, never xfail."""
import asyncio
import pytest
from front_desk_ingress_replay import harness, DESK

pytestmark = pytest.mark.timeout(20)


def test_held_tell_identity_cannot_be_reused_to_publish_changed_wake_body(tmp_path):
    async def run():
        async with harness(tmp_path) as h:
            original={"type":"tell","to_stream_id":DESK,"tell_id":"synthetic-held-replay",
                      "message":"START: synthetic held work"}
            first=await h["dispatch"](original,"peer")
            assert first["delivery_status"]=="persisted"
            before=await h["counts"]()
            assert before["held_rows"]==1 and before["pane_submissions"]==0
            changed=await h["dispatch"]({**original,"message":"GATE: changed payload under held identity"},"peer")
            # A reused caller identity must not publish different content.
            assert (await h["counts"]())["pane_submissions"]==0, changed
            assert str(changed.get("type","")).endswith(".error"), changed
            assert await h["counts"]()==before
    asyncio.run(run())
