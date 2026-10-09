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
