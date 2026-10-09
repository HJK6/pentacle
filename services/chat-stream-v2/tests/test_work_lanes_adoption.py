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


@pytest.mark.parametrize("routing", [False, True])
@pytest.mark.parametrize("state", ["active", "paused", "blocked"])
def test_adopt_member_conflict_is_atomic(routing, state):
    from test_work_lane_progress import A, a1_holder, a1_snapshot
    async def body(env):
        holder = await a1_holder(env, state)
        payload = {"adoption_key": "request:collision", "title": "Collision candidate", "owner_kind": "operator",
                   "work_state": "paused", "visible_chat": {"stream_id": ASSISTANT}, "members": [A], "emit_started": True}
        if routing:
            await _admit_routing_lane(env, "routing-candidate", "Candidate route", "routing-request")
            payload["lane_id"] = "routing-candidate"
        before = await a1_snapshot(env)
        with pytest.raises(ValueError, match="work_lane_member_conflict") as error:
            await env.op("adopt", payload)
        assert holder["lane_id"] in str(error.value)
        assert await a1_snapshot(env) == before
    run(body)


@pytest.mark.parametrize("epic_members", [None, [], ["spec_demo__clear"], ["spec_demo__shared"]])
def test_final_preview_conflicts_after_epic_substitution(epic_members):
    from types import SimpleNamespace
    from server import Server
    from sessions import Sessions
    from work_lanes_projection import WorkLanesInventory
    from test_work_lane_progress import A, a1_holder, a1_snapshot
    async def body(env):
        holder = await a1_holder(env)
        # Provenance-qualified membership is hydrated by the real preview builder.
        sid, _ = await env.seat("preview-lead", role="lead", parent_stream_id=FD)
        await _admit_routing_lane(env, "preview-route", "Candidate", "preview-request")
        sessions = Sessions(env.store, tmux=None, local_host="fixture-root")
        await sessions.refresh()
        server = Server(store=env.store, sessions=sessions, local_host="fixture-root")
        server.assistant_composite = env.composite
        server.work_lanes = WorkLanesInventory(env.store, sessions, server.broadcast,
            specs=SimpleNamespace(epic_spec_members=lambda epic: epic_members))
        # Real Store preview with ordinary (non-epic) candidate plus simulated prior flags;
        # final handler must always recompute from final members, including empty epic lists.
        original = env.store.work_lane_adopt_preview
        async def preview(**kwargs):
            values = await original(**kwargs)
            for value in values:
                value["members"] = [A] if epic_members != [A] else ["spec_demo__clear"]
                value["member_conflicts"] = [{"spec_id": "spec_obsolete", "lane_id": "wl-obsolete"}]
            return values
        env.store.work_lane_adopt_preview = preview
        before = await a1_snapshot(env)
        result = await server._on_work_lanes_adopt_preview({"_auth_context": {"operator_authenticated": True},
                                                          **({"epic": "epic_demo"} if epic_members is not None else {})})
        expected = [{"spec_id": A, "lane_id": holder["lane_id"]}] if epic_members is None or epic_members == [A] else []
        assert result["candidates"]
        assert all(c["member_conflicts"] == expected for c in result["candidates"])
        assert await a1_snapshot(env) == before
    run(body)


def test_store_preview_always_has_conflict_flags():
    async def body(env):
        await env.seat("preview-flags", role="lead", parent_stream_id=FD)
        values = await env.store.work_lane_adopt_preview(composite_stream_id=ASSISTANT,
                                                         env_binding=env.composite._env_binding())
        assert values and all(c["member_conflicts"] == [] for c in values)
    run(body)


def test_typed_wire_error_and_unchanged_value_error_mapping():
    from server import Server
    from sessions import Sessions, VerbError
    from test_work_lane_progress import A, a1_holder
    async def body(env):
        holder = await a1_holder(env)
        target = (await env.adopt("stream:target", state="paused", owner="operator"))["lane"]
        sessions = Sessions(env.store, tmux=None, local_host="fixture-root")
        await sessions.refresh()
        server = Server(store=env.store, sessions=sessions, local_host="fixture-root")
        server.assistant_composite = env.composite
        message = {"type": "assistant.operation", "request_id": "wire-conflict", "composite_stream_id": ASSISTANT,
                   "dispatch_id": "none", "operation": "work_lane.set_members", "lane_id": target["lane_id"],
                   "expected_lane_version": 1, "payload": {"members": [A]},
                   "_auth_context": {"token_verified": True, "stream_id": FD, "session_generation": env.gen}}
        with pytest.raises(VerbError) as error:
            await server._on_assistant_operation(message)
        assert error.value.code == "work_lane_member_conflict" and holder["lane_id"] in str(error.value)
        message.update(request_id="wire-cas", expected_lane_version=0, payload={"members": ["spec_demo__new"]})
        with pytest.raises(VerbError) as error:
            await server._on_assistant_operation(message)
        assert error.value.code == str(error.value) == "assistant_lane_version_conflict"
    run(body)


def test_routing_reopen_does_not_open_done_product_lane():
    from test_work_lane_progress import A, a1_close
    async def body(env):
        lane = await a1_close(env, (await env.adopt("stream:history", state="paused", owner="operator", members=[A]))["lane"])
        await env.adopt("stream:current", state="paused", owner="operator", members=[A])
        def seed(conn):
            conn.execute("UPDATE v2_assistant_composite_lanes SET phase='completed' WHERE lane_id=?", (lane["lane_id"],))
            conn.commit()
        await env.store.submit(seed)
        await env.store.apply_assistant_composite_operation(stream_id=ASSISTANT, operation_id="routing-reopen",
            operation="lane.decision", lane_id=lane["lane_id"], actor_stream_id=FD,
            expected_lane_version=lane["version"], payload={"transition": "reopen", "decision_id": "decision-reopen",
                "from_phase": "completed", "to_phase": "discussion", "operator_basis_message_ids": ["fixture-message"]})
        actual = (await env.store.get_work_lane(lane["lane_id"]))["lane"]
        assert actual["phase"] == "discussion" and actual["work_state"] == "done" and actual["members"] == [A]
    run(body)


def test_ordinary_provenance_preview_lists_sorted_conflicts_and_apply_rechecks():
    from test_work_lane_progress import A, B, a1_snapshot
    async def body(env):
        holders = [(await env.adopt("stream:holder-" + str(i), state="paused", owner="operator", members=[identity]))["lane"]
                   for i, identity in enumerate((A, B))]
        bindings = [{"spec_id": identity, "provenance": "spawn_explicit", "granting_principal": "fixture-principal",
                     "granted_at": "2026-10-09T00:00:00Z"} for identity in (A, B)]
        sid, generation = await env.seat("ordinary-preview", role="lead", parent_stream_id=FD,
            spec_ids=[A, B], spec_resolution="resolved", qualified_spec_ids=[A, B], spec_binding_provenance=bindings)
        before = await a1_snapshot(env)
        preview = await env.store.work_lane_adopt_preview(composite_stream_id=ASSISTANT, env_binding=env.composite._env_binding())
        candidate = next(c for c in preview if c["adoption_key"] == "stream:" + sid)
        expected = sorted([{"spec_id": identity, "lane_id": lane["lane_id"]} for identity, lane in zip((A, B), holders)],
                          key=lambda entry: (entry["lane_id"], entry["spec_id"]))
        assert candidate["members"] == [A, B] and candidate["member_sources"] == bindings
        assert candidate["member_conflicts"] == expected
        assert await a1_snapshot(env) == before
        # A clear preview is advisory: a new holder inserted after preview must still refuse apply.
        clear = dict(candidate, members=["spec_demo__later"])
        await env.adopt("stream:later-holder", state="paused", owner="operator", members=clear["members"])
        payload = {k: clear[k] for k in ("adoption_key", "title", "work_state", "lead", "visible_chat", "members")}
        payload["owner_kind"] = "fd"
        with pytest.raises(ValueError, match="work_lane_member_conflict"):
            await env.op("adopt", payload)
    run(body)


def test_invalid_adopt_lead_and_members_do_not_become_conflict_errors():
    from test_work_lane_progress import A, a1_holder
    async def body(env):
        await a1_holder(env)
        payload = {"adoption_key": "request:invalid", "title": "Invalid candidate", "owner_kind": "operator",
                   "work_state": "active", "visible_chat": {"stream_id": ASSISTANT}, "members": [A]}
        with pytest.raises(ValueError, match="^work_lane_visible_lead_required$"):
            await env.op("adopt", payload)
        payload.update(work_state="paused", members=[A, A])
        with pytest.raises(ValueError) as error:
            await env.op("adopt", payload)
        assert "work_lane_member_conflict" not in str(error.value)
    run(body)
