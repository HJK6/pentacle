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
        payload["no_spec_reason"] = "Synthetic adoption fixture."
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


LONG_SUBJECT = ("Track the existing host migration and setup through a safe assistant reset and shutdown "
                "readiness check. Owner host-a:lead-m; spec_example__host_migration. Preserve existing coordinator "
                "ownership and grants; tracking only, no new reset/shutdown authorization.")
RUN_ON_SUBJECT = ("Investigate why the nightly dashboard export intermittently drops the household board rows "
                  "when the daemon restarts during the export window and the client resumes with a stale cursor")


async def _admit_routing_lane(env, lane_id, subject, request_message_id):
    """Admit a routing lane the way the composite does (no dispatch scope in tests)."""
    return await env.store.apply_assistant_composite_operation(
        stream_id=ASSISTANT, operation_id="admit-" + lane_id, operation="lane.admit", lane_id=lane_id,
        actor_stream_id=FD, payload={"mode": "new", "subject": subject, "request_message_id": request_message_id},
        dispatch_id="dispatch-" + request_message_id)


def test_routing_lanes_from_one_request_get_distinct_adoption_keys():
    """One operator request may split into several admitted lanes (store_routing.py: admission receipts are
    durable lane provenance). Their preview keys must not collide on the UNIQUE adoption_key."""
    async def body(env):
        await _admit_routing_lane(env, "lane-a", LONG_SUBJECT, "msg-1")
        await _admit_routing_lane(env, "lane-b", "Short subject", "msg-1")
        preview = await env.store.work_lane_adopt_preview(composite_stream_id=ASSISTANT,
                                                          env_binding=env.composite._env_binding())
        routing = {c["lane_id"]: c for c in preview if c.get("lane_id")}
        assert set(routing) == {"lane-a", "lane-b"}
        keys = sorted(c["adoption_key"] for c in routing.values())
        assert len(set(keys)) == 2, keys
        assert all(k.startswith("request:msg-1:") for k in keys), keys
        # Apply exactly what the preview proposed (FD adds owner_kind); both land, second is not a key clash.
        for lane_id in ("lane-a", "lane-b"):
            entry = dict(routing[lane_id], owner_kind="fd")
            payload = {k: entry[k] for k in ("adoption_key", "title", "summary", "owner_kind", "work_state",
                                             "visible_chat", "lane_id")}
            payload["no_spec_reason"] = "Synthetic routing fixture."
            await env.op("adopt", payload, request_id="adopt:" + entry["adoption_key"])
        rows = await env.store.work_lane_rows()
        assert sorted(r["adoption_key"] for r in rows) == keys
    run(body)


def test_preview_titles_are_readable_within_bound():
    async def body(env):
        from store_work_lanes import TITLE_MAX
        await _admit_routing_lane(env, "lane-long", LONG_SUBJECT, "msg-2")
        await _admit_routing_lane(env, "lane-run", RUN_ON_SUBJECT, "msg-3")
        await _admit_routing_lane(env, "lane-short", "Short subject", "msg-4")
        preview = await env.store.work_lane_adopt_preview(composite_stream_id=ASSISTANT,
                                                          env_binding=env.composite._env_binding())
        by = {c["lane_id"]: c for c in preview if c.get("lane_id")}
        # A multi-sentence subject titles by its first sentence; the full text stays in summary.
        assert by["lane-long"]["title"] == LONG_SUBJECT.split(". ")[0] + "."
        assert by["lane-long"]["summary"] == LONG_SUBJECT
        # A run-on subject is cut at a word boundary with an ellipsis, never mid-word.
        title = by["lane-run"]["title"]
        assert len(title) <= TITLE_MAX and title.endswith("…"), title
        stem = title[:-1]
        assert RUN_ON_SUBJECT.startswith(stem) and RUN_ON_SUBJECT[len(stem)] == " ", title
        assert by["lane-short"]["title"] == "Short subject"
        # Lead-type candidates get the same treatment.
        sid, _ = await env.seat("ad-long-title", role="lead", parent_stream_id=FD, title=RUN_ON_SUBJECT)
        preview = await env.store.work_lane_adopt_preview(composite_stream_id=ASSISTANT,
                                                          env_binding=env.composite._env_binding())
        lead_title = {c["adoption_key"]: c for c in preview}["stream:" + sid]["title"]
        assert len(lead_title) <= TITLE_MAX and lead_title.endswith("…") and " " not in lead_title[-2:]
    run(body)
