"""File-derived work survives a paused lane without a running lead."""
from test_work_lanes import run


def test_paused_leadless_lane_accepts_file_members(tmp_path, monkeypatch):
    folder = tmp_path / "memory" / "work" / "in_progress" / "demo__paper_bridge"
    folder.mkdir(parents=True)
    folder.joinpath("spec.md").write_text(
        "---\nid: spec_demo__paper_bridge\ntitle: Paper bridge\n---\n"
        "## Acceptance Criteria\n- [ ] Assemble the deck.\n")
    folder.joinpath("summary.md").write_text("**Next action** — Attach the deck.\n")
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(tmp_path / "memory"))

    async def body(env):
        lane = (await env.adopt(state="paused", lead=False, owner="operator"))["lane"]
        assert lane["bound_stream_id"] is None
        result = await env.op("set_members", {"members": ["spec_demo__paper_bridge"], "no_spec_reason": None},
                              lane=lane["lane_id"], version=lane["version"], request_id="bind-paper")
        assert result["lane"]["members"] == ["spec_demo__paper_bridge"]
    run(body, str(tmp_path / "fixture.db"))


def test_membership_replay_cas_auth_bounds_and_unknown_ids():
    import pytest
    async def body(env):
        lane = (await env.adopt(state="paused", lead=False, owner="operator"))["lane"]
        lid = lane["lane_id"]
        payload = {"members": ["spec_demo__second", "spec_demo__first"], "no_spec_reason": None}
        result = await env.op("set_members", payload, lane=lid, version=1, request_id="members-once")
        assert result["event"]["event_id"] == result["event"]["source_id"] == "members-once"
        assert result["event"]["payload"] == payload
        assert result["lane"]["version"] == 2
        assert (await env.op("set_members", payload, lane=lid, version=1, request_id="members-once"))["duplicate"]
        with pytest.raises(ValueError, match="work_lane_idempotency_conflict"):
            await env.op("set_members", {**payload, "members": list(reversed(payload["members"]))},
                         lane=lid, version=1, request_id="members-once")
        with pytest.raises(ValueError, match="work_lane_members_unchanged"):
            await env.op("set_members", payload, lane=lid, version=2)
        with pytest.raises(ValueError, match="assistant_lane_version_conflict"):
            await env.op("set_members", payload, lane=lid, version=1)
        actor, generation = await env.seat("unrelated-reader")
        with pytest.raises(ValueError, match="work_lane_actor_unverified"):
            await env.op("set_members", payload, lane=lid, version=2, actor=actor, gen=generation)
        for bad in ([], ["spec_demo__same"] * 2, ["work_demo__other"], [f"spec_demo__n{i}" for i in range(33)]):
            with pytest.raises(ValueError):
                await env.op("set_members", {"members": bad}, lane=lid, version=2)
        result = await env.store.get_work_lane(lid)
        assert [m["spec_id"] for m in result["members"]] == payload["members"]
        assert all(m["status"] == "missing" for m in result["members"])
        assert result["updates"] == []
        cleared = await env.op("set_members", {"members": [], "no_spec_reason": "Exploratory planning."}, lane=lid, version=2)
        assert cleared["lane"]["members"] == [] and cleared["lane"]["version"] == 3
    run(body)


def test_index_revision_history_and_re_read_membership(tmp_path, monkeypatch):
    import time
    from _shared.specs_service import SpecsSubsystem
    folder = tmp_path / "memory" / "work" / "in_progress" / "demo__span"
    folder.mkdir(parents=True)
    spec = folder / "spec.md"
    template = "---\nid: spec_demo__span\ntitle: Span\n---\n## Acceptance Criteria\n- [{}] Assemble.\n"
    spec.write_text(template.format(" "))
    (folder / "summary.md").write_text("**Next action** — Inspect.\n")
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(tmp_path / "memory"))
    subsystem = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None, debounce_s=0)
    async def body(env):
        lid = (await env.adopt(state="paused", lead=False, owner="operator", members=["spec_demo__span"]))["lane"]["lane_id"]
        async def scan():
            await env.store.reconcile_work_observations(subsystem.scan_work_observations(), settle_s=0, sweep=True)
        await scan()
        stale_baseline = subsystem.scan_work_observations()
        shown = await env.store.get_work_lane(lid)
        assert shown["members"][0]["obs_rev"] == 1
        assert not [e for e in shown["events"] if e["operation"] == "item_change"]
        for revision, mark in ((2, "x"), (3, " "), (4, "x")):
            spec.write_text(template.format(mark))
            await scan()
            await scan()
            shown = await env.store.get_work_lane(lid)
            assert shown["members"][0]["obs_rev"] == revision
            assert shown["lane"]["version"] == 1
        events = [e for e in shown["events"] if e["operation"] == "item_change"]
        assert [e["event_id"] for e in events] == [f"item:{lid}:spec_demo__span:{n}" for n in (2, 3, 4)]
        assert all(e["actor_stream_id"] == "daemon:index" and e["publication_event_id"] is None for e in events)
        await env.store.reconcile_work_observations(stale_baseline, settle_s=0, sweep=True)
        assert (await env.store.get_work_lane(lid))["members"][0]["obs_rev"] == 4
        # The scan was read before a concurrent membership replacement; the commit must re-read lanes.
        spec.write_text(template.format(" "))
        captured = subsystem.scan_work_observations()
        await env.op("set_members", {"members": ["spec_demo__other"]}, lane=lid, version=1)
        await env.store.reconcile_work_observations(captured, settle_s=0, sweep=True)
        shown = await env.store.get_work_lane(lid)
        assert [m["spec_id"] for m in shown["members"]] == ["spec_demo__other"]
        assert len([e for e in shown["events"] if e["operation"] == "item_change"]) == 3
    run(body, str(tmp_path / "fixture.db"))


def test_all_members_show_and_inline_inventory_keep_membership_order():
    from sessions import Sessions
    from server import Server
    from work_lanes_projection import WorkLanesInventory
    async def body(env):
        ids = [f"spec_demo__item_{i}" for i in range(32)]
        lane = (await env.adopt(state="paused", lead=False, owner="operator", members=ids))["lane"]
        sessions = Sessions(env.store, tmux=None, local_host="fixture-root")
        await sessions.refresh()
        server = Server(store=env.store, sessions=sessions, local_host="fixture-root")
        server.work_lanes = WorkLanesInventory(env.store, sessions, server.broadcast)
        frame = await server.work_lanes.current()
        assert frame["lanes"][0]["members_total"] == 32
        assert [m["spec_id"] for m in frame["lanes"][0]["members"]] == ids[:8]
        shown = await server._on_work_lanes_show({"lane_id": lane["lane_id"], "members": True,
                                                 "_auth_context": {"operator_authenticated": True}})
        assert [m["spec_id"] for m in shown["members"]] == ids
        assert shown["projection"]["members"] == shown["members"]
    run(body)


def test_legacy_id_title_does_not_block_other_operations():
    import pytest
    async def body(env):
        lane = (await env.adopt(state="paused", lead=False, owner="operator"))["lane"]
        def legacy(conn):
            conn.execute("UPDATE v2_assistant_composite_lanes SET title='Track spec_demo__legacy' WHERE lane_id=?", (lane["lane_id"],))
            conn.commit()
        await env.store.submit(legacy)
        changed = await env.op("set_members", {"members": ["spec_demo__legacy"]}, lane=lane["lane_id"], version=1)
        assert changed["lane"]["version"] == 2
        with pytest.raises(ValueError, match="work_lane_title_invalid"):
            await env.op("set_text", {"title": "Track spec_demo__legacy"}, lane=lane["lane_id"], version=2)
        repaired = await env.op("set_text", {"title": "Legacy project"}, lane=lane["lane_id"], version=2)
        assert repaired["lane"]["title"] == "Legacy project"
    run(body)


# A1 conflict cells intentionally use unknown canonical ids: sync is not admission authority.
import pytest
from test_work_lanes import FD
from test_assistant_prose_mirror import ASSISTANT

A = "spec_demo__shared"
B = "spec_demo__other"


async def a1_snapshot(env):
    return (await env.store.submit(lambda conn: tuple(conn.iterdump())), list(env.broadcasts))


async def a1_close(env, lane):
    return (await env.op("set_state", {"to": "done", "outcome": "Retained history", "reason": "Fixture decision"},
                         lane=lane["lane_id"], version=lane["version"]))["lane"]


async def a1_holder(env, state="paused"):
    return (await env.adopt("stream:holder", state=state, owner="operator", members=[A],
                            **({"blocker": "Synthetic dependency"} if state == "blocked" else {})))["lane"]


@pytest.mark.parametrize("state", ["active", "paused", "blocked"])
def test_same_composite_product_conflict(state):
    async def body(env):
        holder = await a1_holder(env, state)
        target = (await env.adopt("stream:target", state="paused", owner="operator", members=[B]))["lane"]
        before = await a1_snapshot(env)
        with pytest.raises(ValueError, match="work_lane_member_conflict") as error:
            await env.op("set_members", {"members": [A]}, lane=target["lane_id"], version=target["version"])
        assert holder["lane_id"] in str(error.value)
        assert await a1_snapshot(env) == before
    run(body)


def test_different_composite_allowed():
    async def body(env):
        await a1_holder(env)
        result = await env.store.apply_work_lane_operation(
            stream_id="other:assistant", request_id="other-adopt", operation="adopt", lane_id=None,
            expected_lane_version=None, payload={"adoption_key": "request:other", "title": "Other composite",
                "owner_kind": "operator", "work_state": "paused", "visible_chat": {"stream_id": "other:assistant"},
                "members": [A]}, actor_stream_id=FD, actor_generation=env.gen, binding_name="primary",
            env_binding=env.composite._env_binding())
        assert result["lane"]["members"] == [A]
    run(body)


def test_self_excluded():
    async def body(env):
        lane = await a1_holder(env)
        result = await env.op("set_members", {"members": [A, B]}, lane=lane["lane_id"], version=1)
        assert result["lane"]["members"] == [A, B]
    run(body)


def test_done_lane_excluded():
    async def body(env):
        await a1_close(env, await a1_holder(env))
        assert (await env.adopt("stream:new", state="paused", owner="operator", members=[A]))["lane"]["members"] == [A]
    run(body)


def test_routing_only_excluded():
    from test_work_lanes_adoption import _admit_routing_lane
    async def body(env):
        await _admit_routing_lane(env, "routing", "Synthetic route", "request-routing")
        def seed(conn):
            conn.execute("UPDATE v2_assistant_composite_lanes SET members_json=? WHERE lane_id='routing'", ('["' + A + '"]',))
            conn.commit()
        await env.store.submit(seed)
        assert (await a1_holder(env))["members"] == [A]
    run(body)


def test_done_history_retained():
    async def body(env):
        history = await a1_close(env, await a1_holder(env))
        await env.adopt("stream:current", state="paused", owner="operator", members=[A])
        changed = await env.op("set_members", {"members": [A, B]}, lane=history["lane_id"], version=history["version"])
        assert changed["lane"]["members"] == [A, B] and changed["lane"]["work_state"] == "done"
    run(body)


@pytest.mark.parametrize("to", ["active", "paused", "blocked"])
def test_every_done_to_open_conflicts_without_consuming_confirmation(to):
    async def body(env):
        history = await a1_close(env, (await env.adopt("stream:history", owner="operator", members=[A]))["lane"])
        holder = await a1_holder(env)
        confirmation = env.confirm("reopen-confirm", history["lane_id"], "set_state:" + to)
        payload = {"to": to, "reason": "Reopen fixture", "operator_confirmation": confirmation}
        if to == "blocked":
            payload["blocker"] = "Fixture dependency"
        before = await a1_snapshot(env)
        with pytest.raises(ValueError, match="work_lane_member_conflict") as error:
            await env.op("set_state", payload, lane=history["lane_id"], version=history["version"])
        assert holder["lane_id"] in str(error.value)
        assert await a1_snapshot(env) == before
        await a1_close(env, holder)
        reopened = await env.op("set_state", payload, lane=history["lane_id"], version=history["version"])
        assert reopened["lane"]["work_state"] == to
    run(body)


@pytest.mark.parametrize("replacement", [[], [B]])
def test_done_membership_repair_allows_reopen(replacement):
    async def body(env):
        history = await a1_close(env, await a1_holder(env))
        await env.adopt("stream:current", state="paused", owner="operator", members=[A])
        fixed = (await env.op("set_members", {"members": replacement, "no_spec_reason": None if replacement else "Replanned"},
                            lane=history["lane_id"], version=history["version"]))["lane"]
        result = await env.op("set_state", {"to": "paused", "reason": "Replanned"}, lane=fixed["lane_id"], version=fixed["version"])
        assert result["lane"]["work_state"] == "paused"
    run(body)


@pytest.mark.parametrize("operation", ["adopt", "set_members", "reopen"])
def test_replay_before_conflict(operation):
    async def body(env):
        if operation == "adopt":
            payload = {"adoption_key": "request:original", "title": "Original", "owner_kind": "operator", "work_state": "paused",
                       "visible_chat": {"stream_id": ASSISTANT}, "members": [A]}
            kwargs, verb = {}, "adopt"
        else:
            lane = (await env.adopt("stream:original", state="paused", owner="operator", members=[B]))["lane"]
            if operation == "reopen":
                lane = await a1_close(env, lane)
                lane = (await env.op("set_members", {"members": [A]}, lane=lane["lane_id"], version=lane["version"]))["lane"]
                payload, verb = {"to": "paused", "reason": "Resume fixture"}, "set_state"
            else:
                payload, verb = {"members": [A]}, "set_members"
            kwargs = {"lane": lane["lane_id"], "version": lane["version"]}
        first = await env.op(verb, payload, request_id="original-operation", **kwargs)
        await a1_close(env, first["lane"])
        await a1_holder(env)
        if operation == "set_members":
            # Model pre-existing conflicting inventory; public mutations cannot create it.
            def legacy(conn):
                conn.execute("UPDATE v2_assistant_composite_lanes SET work_state='paused' WHERE lane_id=?",
                             (first["lane"]["lane_id"],))
                conn.commit()
            await env.store.submit(legacy)
        assert (await env.op(verb, payload, request_id="original-operation", **kwargs))["duplicate"] is True
        with pytest.raises(ValueError, match="^assistant_actor_generation_unverified$"):
            await env.op(verb, payload, request_id="original-operation", gen="wrong-generation", **kwargs)
    run(body)


def test_digest_conflict_before_member_conflict():
    async def body(env):
        holder = await a1_holder(env)
        lane = (await env.adopt("stream:target", state="paused", owner="operator"))["lane"]
        await env.op("set_members", {"members": [B]}, lane=lane["lane_id"], version=1, request_id="same-id")
        with pytest.raises(ValueError, match="^work_lane_idempotency_conflict$"):
            await env.op("set_members", {"members": [A]}, lane=lane["lane_id"], version=1, request_id="same-id")
    run(body)


@pytest.mark.parametrize("operation", ["set_members", "reopen"])
def test_member_conflict_before_cas(operation):
    async def body(env):
        if operation == "reopen":
            lane = await a1_close(env, await a1_holder(env))
            holder = (await env.adopt("stream:current", state="paused", owner="operator", members=[A]))["lane"]
            verb, payload = "set_state", {"to": "paused", "reason": "Resume"}
        else:
            holder = await a1_holder(env)
            lane = (await env.adopt("stream:target", state="paused", owner="operator"))["lane"]
            verb, payload = "set_members", {"members": [A]}
        before = await a1_snapshot(env)
        with pytest.raises(ValueError, match="work_lane_member_conflict") as error:
            await env.op(verb, payload, lane=lane["lane_id"], version=0)
        assert holder["lane_id"] in str(error.value)
        assert await a1_snapshot(env) == before
    run(body)


def test_nonconflicting_stale_cas():
    async def body(env):
        lane = await a1_holder(env)
        with pytest.raises(ValueError, match="^assistant_lane_version_conflict$"):
            await env.op("set_members", {"members": [B]}, lane=lane["lane_id"], version=0)
    run(body)


def test_serialized_concurrent_membership_has_one_winner():
    import asyncio
    async def body(env):
        lanes = [(await env.adopt("stream:" + key, state="paused", owner="operator"))["lane"] for key in ("one", "two")]
        outcomes = await asyncio.gather(*(env.op("set_members", {"members": [A]}, lane=l["lane_id"], version=1)
                                          for l in lanes), return_exceptions=True)
        assert sum(isinstance(r, dict) for r in outcomes) == 1
        assert sum(isinstance(r, ValueError) and "work_lane_member_conflict" in str(r) for r in outcomes) == 1
        assert sum(A in [m["spec_id"] for m in row["_members"]] for row in await env.store.work_lane_rows()) == 1
    run(body)


def test_additive_estimate_metadata_persists_projection_restart_last_good(tmp_path, monkeypatch):
    import asyncio
    from _shared.specs_service import SpecsSubsystem
    from store import Store
    from sessions import Sessions
    from server import Server
    from work_lanes_projection import WorkLanesInventory
    from test_work_lane_remaining_estimate import write_spec, NUMERIC, EXEMPT
    root, path = tmp_path / "memory", str(tmp_path / "fixture.db")
    write_spec(root, A)
    write_spec(root, B, NUMERIC)
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(root))
    scanner = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None, debounce_s=0)
    lid = None
    async def body(env):
        nonlocal lid
        lid = (await env.adopt("stream:durable", state="paused", owner="operator", members=[A, B]))["lane"]["lane_id"]
        async def scan():
            await env.store.reconcile_work_observations(scanner.scan_work_observations(), settle_s=0, sweep=True)
        await scan()
        assert (await env.store.get_work_lane(lid))["members"][0]["estimate_exempt"] is False
        write_spec(root, A, EXEMPT)
        await scan()
        await scan()
        shown = await env.store.get_work_lane(lid)
        assert shown["members"][0]["estimate"] is None and shown["members"][0]["estimate_exempt"] is True
        assert shown["members"][0]["obs_rev"] == 2 and shown["lane"]["version"] == 1
        events = [e for e in shown["events"] if e["operation"] == "item_change"]
        assert len(events) == 1 and events[0]["payload"]["prior"]["estimate_exempt"] is False
        assert events[0]["payload"]["next"]["estimate_exempt"] is True
        write_spec(root, B, NUMERIC.replace("2026-10-09", "2026-10-10"))
        await scan()
        sessions = Sessions(env.store, tmux=None, local_host="fixture-root")
        await sessions.refresh()
        server = Server(store=env.store, sessions=sessions, local_host="fixture-root")
        server.work_lanes = WorkLanesInventory(env.store, sessions, server.broadcast)
        frame = await server.work_lanes.current()
        listed = await server._on_work_lanes_list({"_auth_context": {"operator_authenticated": True}})
        shown = await server._on_work_lanes_show({"lane_id": lid, "_auth_context": {"operator_authenticated": True}})
        for lane in (frame["lanes"][0], listed["lanes"][0], shown["projection"]):
            assert lane["members"][0]["estimate_exempt"] is True
            assert lane["members"][1]["estimate"]["as_of"] == "2026-10-10"
            assert lane["members"][1]["obs_rev"] == 2
            assert lane["estimate_complete"] and lane["open_estimated"] == 1
            assert lane["version"] == 1
        # A malformed summary preserves both independent last-good facts.
        (root / "work" / "in_progress" / A / "summary.md").write_text("---\na: [broken\n---\n")
        await scan()
        preserved = (await env.store.get_work_lane(lid))["members"][0]
        assert preserved["estimate_exempt"] and preserved["observation"]["quality"] == "error" and preserved["obs_rev"] == 2
    run(body, path)
    root.rename(tmp_path / "offline-memory")
    async def restart():
        store = Store(path)
        store.start()
        try:
            await store.reconcile_work_observations(scanner.scan_work_observations(), settle_s=0, sweep=True)
            shown = await store.get_work_lane(lid)
            assert shown["members"][0]["estimate_exempt"] and shown["members"][0]["obs_rev"] == 2
            assert shown["members"][0]["observation"]["quality"] == "stale"
            assert shown["members"][1]["estimate"]["as_of"] == "2026-10-10" and shown["members"][1]["obs_rev"] == 2
            assert shown["lane"]["version"] == 1
        finally:
            store.stop()
    asyncio.run(restart())
