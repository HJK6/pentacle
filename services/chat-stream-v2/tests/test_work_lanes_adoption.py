"""Adoption preview/apply (V15): read-only preview, explicit owner kind, idempotent apply."""
import pytest

from test_work_lanes import FD, code, run
from test_assistant_prose_mirror import ASSISTANT


async def _table_counts(store):
    def q(conn):
        return (conn.execute("SELECT COUNT(*) FROM v2_assistant_composite_lanes").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM v2_work_lane_events").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM session_event_tail").fetchone()[0])
    return await store.submit(q)


def test_preview_is_read_only_and_never_guesses_owner():
    async def body(env):
        fd_lead = await env.seat("ad-fd-lead", role="lead", parent_stream_id=FD)
        top = await env.seat("ad-top", spec_ids='["spec_x"]')
        await env.seat("ad-hidden", role="lead", parent_stream_id=FD, visibility="hidden")
        await env.seat("ad-qa", role="qa")
        await env.seat("ad-plain-child", parent_stream_id=FD)
        before = await _table_counts(env.store)
        preview = await env.store.work_lane_adopt_preview(composite_stream_id=ASSISTANT,
                                                          env_binding=env.composite._env_binding())
        assert await _table_counts(env.store) == before
        keys = {c["adoption_key"] for c in preview}
        assert keys == {"stream:" + fd_lead[0], "stream:" + top[0]}
        assert all(c["owner_kind"] is None for c in preview)
        by_key = {c["adoption_key"]: c for c in preview}
        assert by_key["stream:" + fd_lead[0]]["evidence"]["parent_stream_id"] == FD
        assert by_key["stream:" + fd_lead[0]]["work_state"] == "active"
        # Apply exactly what the FD classified; rerun creates nothing new.
        entry = dict(by_key["stream:" + fd_lead[0]], owner_kind="fd")
        payload = {k: entry[k] for k in ("adoption_key", "title", "owner_kind", "work_state", "lead",
                                         "visible_chat")}
        first = await env.op("adopt", payload, request_id="adopt:" + entry["adoption_key"])
        again = await env.op("adopt", payload, request_id="adopt:" + entry["adoption_key"])
        assert again["duplicate"] is True and again["lane"]["lane_id"] == first["lane"]["lane_id"]
        assert len(await env.store.work_lane_rows()) == 1
        top_entry = dict(by_key["stream:" + top[0]], owner_kind="fd")
        top_payload = {k: top_entry[k] for k in ("adoption_key", "title", "owner_kind", "work_state",
                                                 "visible_chat")}
        top_payload["work_state"] = "paused"
        with pytest.raises(ValueError) as e:
            await env.op("adopt", top_payload, request_id="adopt:" + top_entry["adoption_key"])
        assert code(e) == "work_lane_owner_kind_unverified"
        # Adopted streams leave the preview.
        preview2 = await env.store.work_lane_adopt_preview(composite_stream_id=ASSISTANT,
                                                           env_binding=env.composite._env_binding())
        assert {c["adoption_key"] for c in preview2} == {"stream:" + top[0]}
    run(body)
