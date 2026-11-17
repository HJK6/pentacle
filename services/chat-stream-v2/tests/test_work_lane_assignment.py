"""Acceptance owners require work links; parented supporting roles inherit optional attribution.

Lane tracking minimal A1-A5 with QA corrections R1, R4, R5, R6.
"""
from __future__ import annotations

import json

import pytest

from sessions import VerbError
from store import UNASSIGNED, Store, effective_assignment
from store_work_lanes import _assigned_seats_conn
from test_spawn_wip_cap import HOST, setup

SUMMARY = "---\nid: work_{stem}\n---\n**Progress** — Created the acceptance fixture.\n\n**Status** — Underway.\n\n**Next action** — Continue.\n"
SPEC = ("---\nid: spec_{stem}\ntitle: Fixture {stem}\n---\n## Acceptance Criteria\n- [ ] Done.\n\n"
        "## Estimate\n- remaining_work_h: 1–2 (median 1.5) as_of 2026-10-09\n")


def item(root, stem, *, summary=SUMMARY):
    folder = root / "work" / "in_progress" / stem
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "spec.md").write_text(SPEC.format(stem=stem))
    (folder / "summary.md").write_text(summary.format(stem=stem))
    return "spec_" + stem


async def lane(store, lane_id, *members, state="active"):
    def op(conn):
        from work_lane_link import link_specs_conn
        link_specs_conn(conn, *members, lane_id=lane_id)
        conn.execute(
            "UPDATE v2_assistant_composite_lanes SET work_state=?,version=4 WHERE lane_id=?", (state,lane_id))
        conn.commit()
    await store.submit(op)


async def seat(store, name, *spec_ids, assigned=None, role="lead", visibility="hidden"):
    row = await store.open_session(HOST, name, provider="claude", role=role, visibility=visibility, spec_ids=list(spec_ids),
                                   spec_binding_provenance=[{"spec_id": s, "provenance": "spawn_explicit",
                                                             "granting_principal": "operator",
                                                             "granted_at": "2026-10-09T00:00:00Z"}
                                                            for s in spec_ids],
                                   assigned_spec_id=assigned)
    return f"{HOST}:{name}", row["session_generation"]


def agent(caller, *, spec=None, parent=True, name="child", key=None, role="worker", **extra):
    msg = {"objective": "Exercise managed commission", "session_name": name, "command": "run", "role": role,
           "_auth_context": {"token_verified": True, "stream_id": caller[0], "session_generation": caller[1]},
           **extra}
    if parent:
        msg["parent_stream_id"] = caller[0]
    if spec:
        msg["spec_id"] = spec
    if key:
        msg["idempotency_key"] = key
    return msg


async def refused(ctl, msg):
    with pytest.raises(VerbError) as exc:
        await ctl.spawn(msg, HOST)
    return exc.value


@pytest.fixture
def env(tmp_path, monkeypatch):
    class Env:
        pass
    e = Env()
    e.root = tmp_path / "memory"
    e.store = Store(":memory:")
    e.store.start()
    e.monkeypatch = monkeypatch
    yield e
    e.store.stop()


@pytest.mark.asyncio
async def test_unlinked_explicit_commission_is_admitted(env):
    target, home = item(env.root, "demo__target"), item(env.root, "demo__home")
    ctl, _sessions, tmux = await setup(env.root, env.store, env.monkeypatch)
    await lane(env.store, "wl-home", home)
    caller = await seat(env.store, "caller", home, assigned=home)
    reply = await ctl.spawn(agent(caller, spec=target, role="lead"), HOST)
    assert reply["type"] == "spawn.ok"
    assert tmux.created == 1
    assert (await env.store.fetch_session(HOST, "child"))["assigned_spec_id"] == target


@pytest.mark.asyncio
async def test_linked_commission_persists_assignment_and_warns_on_checkpoint(env):
    target = item(env.root, "demo__target", summary="---\nid: w\n---\nProse only.\n")
    ctl, _sessions, tmux = await setup(env.root, env.store, env.monkeypatch)
    await lane(env.store, "wl-a", target)
    caller = await seat(env.store, "caller", target, assigned=target)
    reply = await ctl.spawn(agent(caller, spec=target), HOST)
    assert reply["type"] == "spawn.ok"
    row = await env.store.fetch_session(HOST, "child")
    assert row["assigned_spec_id"] == target
    # A malformed display record is a warning, never an extra refusal (FD GO).
    assert reply["work_checkpoint"]["state"] == "invalid"
    assert [d["code"] for d in reply["work_checkpoint"]["deficiencies"]] == ["status_missing", "next_action_missing"]


@pytest.mark.asyncio
async def test_portfolio_parent_without_assignment_requires_spec_id(env):
    specs = [item(env.root, f"demo__p{i}") for i in range(3)]
    ctl, _sessions, tmux = await setup(env.root, env.store, env.monkeypatch)
    await lane(env.store, "wl-p", *specs)
    caller = await seat(env.store, "fd", *specs)  # multi-tag, unassigned (legacy portfolio)
    err = await refused(ctl, agent(caller, role="lead"))
    assert err.code == "work_assignment_required"
    assert "--spec-id" in str(err) and "3 specs" in str(err)
    assert tmux.created == 0


@pytest.mark.asyncio
async def test_child_inherits_parent_assignment_not_portfolio(env):
    specs = [item(env.root, f"demo__p{i}") for i in range(3)]
    ctl, _sessions, _tmux = await setup(env.root, env.store, env.monkeypatch)
    await lane(env.store, "wl-p", *specs)
    caller = await seat(env.store, "lead", *specs, assigned=specs[1])
    await ctl.spawn(agent(caller), HOST)
    row = await env.store.fetch_session(HOST, "child")
    assert row["assigned_spec_id"] == specs[1]
    assert row["spec_ids"] == specs  # authority tags are inherited exactly as before


@pytest.mark.asyncio
async def test_handoff_of_unassigned_portfolio_seat_is_never_refused(env):
    specs = [item(env.root, f"demo__p{i}") for i in range(3)]
    ctl, _sessions, _tmux = await setup(env.root, env.store, env.monkeypatch)
    pred = await seat(env.store, "fd", *specs)  # no lane membership, no assignment
    msg = agent(pred, parent=False, name="fd-successor", handoff=True, handoff_from_stream_id=pred[0])
    reply = await ctl.spawn(msg, HOST)
    assert reply["type"] == "spawn.ok"
    assert (await env.store.fetch_session(HOST, "fd-successor"))["assigned_spec_id"] is None


@pytest.mark.asyncio
async def test_handoff_carries_predecessor_assignment(env):
    target = item(env.root, "demo__target")
    ctl, _sessions, _tmux = await setup(env.root, env.store, env.monkeypatch)
    pred = await seat(env.store, "lead", target, assigned=target)
    msg = agent(pred, parent=False, name="successor", handoff=True, handoff_from_stream_id=pred[0])
    await ctl.spawn(msg, HOST)  # unlinked: still admitted, continuity first (R1)
    assert (await env.store.fetch_session(HOST, "successor"))["assigned_spec_id"] == target


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", [
    {"operator_authenticated": True, "operator_principal": "operator:fixture"},
    {},  # anonymous loopback: a recorded local bypass (R5)
    {"service_authenticated": True, "service_actor": "daemon:scheduler"},
    {"service_authenticated": True, "service_actor": "daemon:host-transfer"},
])
async def test_exempt_callers_are_never_refused(env, auth):
    target = item(env.root, "demo__target")
    ctl, _sessions, _tmux = await setup(env.root, env.store, env.monkeypatch)
    msg = {"objective": "Independent chat", "session_name": "free", "command": "run", "spec_id": target,
           "_auth_context": auth}
    assert (await ctl.spawn(msg, HOST))["type"] == "spawn.ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["assistant", "lead"])
async def test_caller_role_does_not_require_lane_membership(env, role):
    # Lane membership is not an execution authority check.
    env.monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    target = item(env.root, "demo__target")
    ctl, _sessions, tmux = await setup(env.root, env.store, env.monkeypatch)
    caller = await seat(env.store, "caller", role=role)  # unassigned, no spec, no lane
    msg = agent(caller, spec=target, parent=False)
    assert (await ctl.spawn(msg, HOST))["type"] == "spawn.ok"
    assert tmux.created == 1


@pytest.mark.asyncio
async def test_operator_elevated_seat_token_is_still_managed(env):
    target = item(env.root, "demo__target")
    ctl, _sessions, _tmux = await setup(env.root, env.store, env.monkeypatch)
    caller = await seat(env.store, "caller", target, assigned=target)
    msg = agent(caller, spec=target, role="lead")
    msg["_auth_context"].update(operator_authenticated=True, operator_authority_source="stream_token")
    assert (await ctl.spawn(msg, HOST))["type"] == "spawn.ok"


@pytest.mark.asyncio
async def test_admitted_retry_replays_after_membership_changes(env):
    target = item(env.root, "demo__target")
    ctl, _sessions, tmux = await setup(env.root, env.store, env.monkeypatch)
    await lane(env.store, "wl-a", target)
    caller = await seat(env.store, "caller", target, assigned=target)
    first = await ctl.spawn(agent(caller, spec=target, key="k-1"), HOST)

    def drop(conn):
        conn.execute("UPDATE v2_assistant_composite_lanes SET members_json='[]'")
        conn.commit()
    await env.store.submit(drop)
    again = await ctl.spawn(agent(caller, spec=target, key="k-1"), HOST)
    assert again["stream_id"] == first["stream_id"] and tmux.created == 1


@pytest.mark.asyncio
async def test_done_lane_membership_does_not_refuse_spawn(env):
    target = item(env.root, "demo__target")
    ctl, _sessions, _tmux = await setup(env.root, env.store, env.monkeypatch)
    await lane(env.store, "wl-old", target, state="done")
    caller = await seat(env.store, "caller", target, assigned=target)
    assert (await ctl.spawn(agent(caller, spec=target, role="lead"), HOST))["type"] == "spawn.ok"


@pytest.mark.asyncio
async def test_scheduled_handoff_with_portfolio_ids_keeps_predecessor_assignment(env):
    specs = [item(env.root, f"demo__p{i}") for i in range(3)]
    ctl, _sessions, _tmux = await setup(env.root, env.store, env.monkeypatch)
    pred = await seat(env.store, "lead", *specs, assigned=specs[2])
    fire = {"objective": "Scheduled handoff", "session_name": "fired", "command": "run", "role": "lead",
            "handoff": True, "handoff_from_stream_id": pred[0], "spec_ids": specs,
            "_auth_context": {"service_authenticated": True, "service_actor": "daemon:scheduler",
                              "token_verified": True, "stream_id": pred[0], "session_generation": pred[1]}}
    assert (await ctl.spawn(fire, HOST))["type"] == "spawn.ok"
    assert (await env.store.fetch_session(HOST, "fired"))["assigned_spec_id"] == specs[2]


@pytest.mark.asyncio
async def test_ruled_release_is_not_rechecked_at_dispatch(env):
    target = item(env.root, "demo__target")
    ctl, _sessions, _tmux = await setup(env.root, env.store, env.monkeypatch)
    caller = await seat(env.store, "caller", target, assigned=target)
    msg = agent(caller, spec=target)
    msg["_auth_context"]["ruled_release"] = True  # admitted (and checked) when the ruling was requested
    assert (await ctl.spawn(msg, HOST))["type"] == "spawn.ok"


async def spec_update(env, ctl, sessions, action, stream, spec=None, auth=None):
    from uiverbs import UIVerbs
    verbs = UIVerbs(env.store, sessions, ctl, specs=ctl.specs)
    host, name = stream.split(":", 1)
    msg = {"type": "session.spec_update", "request_id": "r", "host": host, "session_name": name,
           "action": action, "_auth_context": auth or {}}
    if spec:
        msg["spec_id"] = spec
    return await verbs.session_spec_update(msg)


@pytest.mark.asyncio
async def test_fd_direct_execution_assign_ignores_lane_and_unassign_keeps_tags(env):
    specs = [item(env.root, f"demo__p{i}") for i in range(2)]
    target = item(env.root, "demo__target")
    ctl, sessions, _tmux = await setup(env.root, env.store, env.monkeypatch)
    await lane(env.store, "wl-p", *specs)
    fd = await seat(env.store, "fd", *specs)
    agent_auth = {"token_verified": True, "stream_id": fd[0], "session_generation": fd[1]}
    assigned = await spec_update(env, ctl, sessions, "assign", fd[0], target, agent_auth)
    assert assigned["session"]["assigned_spec_id"] == target
    reply = await spec_update(env, ctl, sessions, "assign", fd[0], specs[0], agent_auth)
    assert reply["session"]["assigned_spec_id"] == specs[0] and reply["work_checkpoint"]["state"] == "valid"
    reply = await spec_update(env, ctl, sessions, "unassign", fd[0], auth=agent_auth)
    assert reply["session"]["assigned_spec_id"] == UNASSIGNED and reply["session"]["spec_ids"] == specs + [target]
    assert effective_assignment(reply["session"]) is None


@pytest.mark.asyncio
async def test_operator_assign_is_recorded_without_refusal_and_detach_clears(env):
    target = item(env.root, "demo__target")
    ctl, sessions, _tmux = await setup(env.root, env.store, env.monkeypatch)
    chat = await seat(env.store, "chat", role=None)
    await lane(env.store, "wl-operator", target)
    operator = {"operator_authenticated": True, "operator_principal": "operator:fixture"}
    reply = await spec_update(env, ctl, sessions, "assign", chat[0], target, operator)
    assert reply["session"]["assigned_spec_id"] == target and reply["session"]["spec_ids"] == [target]
    reply = await spec_update(env, ctl, sessions, "detach", chat[0], target, operator)
    assert reply["session"]["assigned_spec_id"] == UNASSIGNED and effective_assignment(reply["session"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["attach", "detach"])
@pytest.mark.parametrize("state", ["paused", "orphan", "fd", "active"])
async def test_legacy_spec_tag_change_ignores_lane_execution_state(env, action, state):
    target, other = item(env.root, "demo__target"), item(env.root, "demo__other")
    ctl, sessions, _tmux = await setup(env.root, env.store, env.monkeypatch)
    await lane(env.store, "wl-tags", target, state="paused" if state == "paused" else "active")
    chat = await seat(env.store, "tag-chat", *([target, other] if action == "detach" else []), role="worker")
    if state == "orphan":
        await env.store.submit(lambda conn: (
            conn.execute("UPDATE v2_assistant_composite_lanes SET bound_generation='lost' WHERE lane_id='wl-tags'"),
            conn.commit()))
    if state == "fd":
        # The actual authenticated binding, not a role, title or caller flag.
        await env.store.submit(lambda conn: (
            conn.execute("INSERT INTO v2_assistant_direct_binding(name,stream_id,generation,revision,updated_at) "
                         "VALUES ('assistant',?,?,1,'2026-11-09T00:00:00Z')", chat), conn.commit()))
    before = await env.store.fetch_session(HOST, "tag-chat")
    assert before["assigned_spec_id"] is None and effective_assignment(before) is None
    operator = {"operator_authenticated": True, "operator_principal": "operator:fixture"}
    changed_spec = other if action == "detach" else target
    reply = await spec_update(env, ctl, sessions, action, chat[0], changed_spec, operator)
    assert reply["session"]["assigned_spec_id"] is None
    assert effective_assignment(reply["session"]) == target


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["attach", "detach"])
async def test_coordination_tags_stay_usable_after_explicit_unassignment(env, action):
    target, other = item(env.root, "demo__target"), item(env.root, "demo__other")
    ctl, sessions, _tmux = await setup(env.root, env.store, env.monkeypatch)
    await lane(env.store, "wl-tags", target, state="paused")
    chat = await seat(env.store, "coord-chat", *([target, other] if action == "detach" else []), role="worker")
    operator = {"operator_authenticated": True, "operator_principal": "operator:fixture"}
    unassigned = await spec_update(env, ctl, sessions, "unassign", chat[0], auth=operator)
    assert effective_assignment(unassigned["session"]) is None
    reply = await spec_update(env, ctl, sessions, action, chat[0], other if action == "detach" else target, operator)
    assert reply["session"]["spec_ids"] == [target]
    assert reply["session"]["assigned_spec_id"] == UNASSIGNED
    assert effective_assignment(reply["session"]) is None


@pytest.mark.asyncio
async def test_unassign_of_a_single_spec_seat_sticks_through_handoff(env):
    # A6: the single-spec fallback is for legacy NULL rows only, never an explicit unassignment.
    keep, other = item(env.root, "demo__keep"), item(env.root, "demo__other")
    ctl, sessions, _tmux = await setup(env.root, env.store, env.monkeypatch)
    await lane(env.store, "wl-keep", keep)
    lead = await seat(env.store, "single", keep, assigned=keep)
    operator = {"operator_authenticated": True, "operator_principal": "operator:fixture"}
    reply = await spec_update(env, ctl, sessions, "unassign", lead[0], auth=operator)
    assert reply["session"]["spec_ids"] == [keep] and effective_assignment(reply["session"]) is None
    assert await env.store.submit(lambda c: _assigned_seats_conn(c, [keep])) == []  # no longer "in use"
    # A new lead commission must name its spec; the tag is not an implicit assignment.
    assert (await refused(ctl, agent(lead, name="kid", role="lead"))).code == "work_assignment_required"
    # A handoff successor inherits the unassignment, even when it names specs.
    for name, extra in (("succ-a", {}), ("succ-b", {"spec_id": other})):
        msg = agent(lead, parent=False, name=name, handoff=True, handoff_from_stream_id=lead[0], **extra)
        await ctl.spawn(msg, HOST)
        row = await env.store.fetch_session(HOST, name)
        assert row["assigned_spec_id"] == UNASSIGNED and effective_assignment(row) is None
    # A legacy NULL single-spec row still falls back to its only spec.
    legacy = await seat(env.store, "legacy", keep)
    assert effective_assignment(await env.store.fetch_session(*legacy[0].split(":", 1))) == keep


def test_removing_a_live_assignment_needs_a_recorded_reason():
    from test_work_lanes import FD, code, run

    async def body(env):
        lead = await env.seat("rm-lead", role="lead", parent_stream_id=FD)
        lid = (await env.adopt(key="stream:rm", lead=lead,
                               members=["spec_demo__keep", "spec_demo__live"]))["lane"]["lane_id"]
        await env.seat("rm-worker", role="worker", spec_ids=["spec_demo__live", "spec_demo__keep"],
                       assigned_spec_id="spec_demo__live")
        await env.seat("rm-closed", role="worker", spec_ids=["spec_demo__keep"])
        await env.store.mark_closed("host-a", "rm-closed", closed_at="2026-10-09T00:00:00Z", pane_status="pane_dead")
        with pytest.raises(ValueError) as exc:
            await env.op("set_members", {"members": ["spec_demo__keep"]}, lane=lid, version=1)
        assert "work_lane_member_in_use" in code(exc) and "host-a:rm-worker" in code(exc)
        assert (await env.store.get_work_lane(lid))["lane"]["version"] == 1
        done = await env.op("set_members", {"members": ["spec_demo__keep"], "reason": "Moving to the rack lane."},
                            lane=lid, version=1)
        event = [e for e in (await env.store.get_work_lane(lid))["events"] if e["operation"] == "set_members"][-1]
        assert event["payload"]["reason"] == "Moving to the rack lane."
        assert event["payload"]["assigned_seats"] == [{"spec_id": "spec_demo__live", "stream_id": "host-a:rm-worker"}]
        # Removing a spec only a closed seat carried needs no reason.
        await env.op("set_members", {"members": [], "no_spec_reason": "Paused intake."},
                     lane=lid, version=done["lane"]["version"])
    run(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["worker", "evaluator", "planner", "reviewer-advisory", None])
@pytest.mark.parametrize("attribution", ["none", "portfolio", "unlinked"])
async def test_parented_supporting_spawn_needs_no_manual_work_link(env, role, attribution):
    specs = [] if attribution == "none" else [item(env.root, f"demo__support_{i}") for i in range(2)]
    ctl, _sessions, tmux = await setup(env.root, env.store, env.monkeypatch)
    caller = await seat(env.store, "operator-chat", *specs, role=None, visibility="visible")
    if attribution == "portfolio":
        await lane(env.store, "wl-support", *specs)
    msg = agent(caller, role=role, key="support-key")
    if role is None:
        msg.pop("role")
    if attribution == "unlinked":
        msg["spec_id"] = specs[0]
    reply = await ctl.spawn(msg, HOST)
    row = await env.store.fetch_session(HOST, "child")
    assert reply["type"] == "spawn.ok"
    assert row["parent_stream_id"] == caller[0]
    assert row["objective"] == msg["objective"]
    assert row["spec_ids"] == (specs[:1] if attribution == "unlinked" else specs)
    if attribution == "portfolio":
        assert row["assigned_spec_id"] is None
        assert all(b["provenance"] == "parent_inherited" for b in row["spec_binding_provenance"])
        assert [l["lane_id"] for l in await env.store.open_lanes_for_spec(row["spec_ids"][0])] == ["wl-support"]
    again = await ctl.spawn(msg, HOST)
    assert again["stream_id"] == reply["stream_id"] and tmux.created == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("role,extra,resolved_role", [
    ("lead", {}, None), ("qa", {}, None), ("worker", {"phase": "qa"}, None),
    (None, {"qa_spec_id": "spec_demo__review"}, None), ("worker", {}, "lead"),
])
async def test_acceptance_spawns_still_require_work_binding(env, role, extra, resolved_role):
    ctl, _sessions, tmux = await setup(env.root, env.store, env.monkeypatch, resolved_role=resolved_role)
    caller = await seat(env.store, "operator-chat", role=None)
    assert (await refused(ctl, agent(caller, role=role, **extra))).code == "work_assignment_required"
    assert tmux.created == 0


def test_synthetic_membership_over_32_keeps_shape_and_duplicate_validation():
    """Synthetic cap replay; historical input identities were not supplied."""
    from work_lane_members import validate_members
    members = [f"spec_demo__member_{i}" for i in range(40)]
    assert validate_members(members, None) == (members, None)
    with pytest.raises(ValueError, match="work_lane_members_invalid"):
        validate_members(members + [members[0]], None)
    with pytest.raises(ValueError, match="work_lane_members_invalid"):
        validate_members(["not-a-spec"], None)


async def front_desk(env, *, small=False, linked=True, state="active"):
    """A real configured FD and real store; only the provider transport is fake."""
    from assistant_composite import AssistantComposite, AssistantCompositeConfig
    home = item(env.root, "admission__home")
    target = item(env.root, "admission__target")
    if small:
        path = env.root / "work/in_progress/admission__target/spec.md"
        path.write_text(path.read_text().replace("title:", "tags: [small]\ntitle:", 1))
    env.monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(env.root))
    ctl, sessions, tmux = await setup(env.root, env.store, env.monkeypatch)
    caller = await seat(env.store, "front-desk", home, assigned=home, role="assistant")
    composite = AssistantComposite(env.store, config=AssistantCompositeConfig.from_env({
        "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
        "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": HOST + ":assistant",
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": caller[0],
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": caller[1],
        "PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID": HOST + ":advisor",
    }))
    sessions.assistant.front_desk = composite
    await env.store.submit(lambda conn: conn.execute(
        "INSERT OR REPLACE INTO v2_work_index_state(singleton,state_json) VALUES(1,?)",
        (json.dumps({"available": True, "root_configured": True}),)))
    if linked:
        await lane(env.store, "wl-admission", target, state=state)
    return ctl, sessions, tmux, caller, home, target, composite


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["lead", "worker", "evaluator", "", None, "reviewer-advisory"])
async def test_fd_work_requires_its_own_explicit_item(env, role):
    ctl, _, tmux, caller, _, _, _ = await front_desk(env)
    error = await refused(ctl, agent(caller, role=role))
    assert error.code == "work_assignment_required"
    assert "own work item" in str(error)
    assert tmux.created == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["lead", "worker", "evaluator", ""])
async def test_fd_explicit_item_in_done_lane_is_admitted(env, role):
    ctl, _, tmux, caller, _, target, _ = await front_desk(env, state="done")
    await ctl.spawn(agent(caller, spec=target, role=role), HOST)
    row = await env.store.fetch_session(HOST, "child")
    assert row["assigned_spec_id"] == target
    assert row["spec_binding_provenance"][0]["provenance"] == "spawn_explicit"
    assert tmux.created == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("exemption", ["planner", "promptless"])
async def test_fd_exempt_inherits_tags_but_has_no_assignment(env, exemption):
    ctl, _, _, caller, home, _, _ = await front_desk(env)
    msg = agent(caller, role="planner" if exemption == "planner" else "worker")
    if exemption == "promptless":
        msg.pop("command")
    await ctl.spawn(msg, HOST)
    row = await env.store.fetch_session(HOST, "child")
    assert row["spec_ids"] == [home]
    assert row["assigned_spec_id"] == UNASSIGNED
    assert effective_assignment(row) is None
    assigned = await env.store.submit(lambda conn: _assigned_seats_conn(conn, [home]))
    assert HOST + ":child" not in {entry["stream_id"] for entry in assigned}
    assert row["usage"]["attribution"]["spec_id"] == home


@pytest.mark.asyncio
async def test_fd_explicit_planner_remains_assigned_even_without_lane(env):
    ctl, _, _, caller, _, target, _ = await front_desk(env, linked=False)
    await ctl.spawn(agent(caller, spec=target, role="planner"), HOST)
    assert (await env.store.fetch_session(HOST, "child"))["assigned_spec_id"] == target


@pytest.mark.asyncio
async def test_fd_unlaned_item_refused_before_reservation(env):
    ctl, _, tmux, caller, _, target, _ = await front_desk(env, linked=False)
    error = await refused(ctl, agent(caller, spec=target))
    assert error.code == "work_lane_membership_required"
    assert await env.store.reservations(include_expired=True) == []
    assert tmux.created == 0


@pytest.mark.asyncio
async def test_fd_unavailable_index_skips_only_membership(env, caplog):
    ctl, _, _, caller, _, target, _ = await front_desk(env, linked=False, small=True)
    await env.store.submit(lambda conn: conn.execute(
        "UPDATE v2_work_index_state SET state_json=?", (json.dumps({"available": False}),)))
    await ctl.spawn(agent(caller, spec=target), HOST)
    assert sum(target in record.message and "index" in record.message for record in caplog.records) == 1
    error = await refused(ctl, agent(caller, name="implicit"))
    assert error.code == "work_assignment_required"

    assert (await refused(ctl, agent(caller, spec=target, name="second"))).code == "work_small_item_limit"

@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [False, True])
async def test_small_item_counts_open_and_closed_seats(env, closed):
    ctl, _, tmux, caller, _, target, _ = await front_desk(env, small=True)
    await seat(env.store, "prior", target, assigned=target)
    if closed:
        await env.store.submit(lambda conn: conn.execute(
            "UPDATE sessions SET status='closed' WHERE session_name='prior'"))
    error = await refused(ctl, agent(caller, spec=target))
    assert error.code == "work_small_item_limit"
    assert tmux.created == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("flags", [{"role": "qa"}, {"phase": "qa"}, {"qa_spec_id": "spec_admission__target"},
                                    {"qa_surface": "implementation"}, {"qa_cycle": 1}])
async def test_small_item_rejects_every_qa_classifier(env, flags):
    ctl, _, tmux, caller, _, target, _ = await front_desk(env, small=True)
    error = await refused(ctl, agent(caller, spec=target, **flags))
    assert error.code == "work_small_item_limit"
    assert tmux.created == 0


@pytest.mark.asyncio
async def test_small_lead_cannot_spawn_supporting_child_and_tag_removal_allows_it(env):
    ctl, _, tmux, _, _, target, _ = await front_desk(env, small=True)
    lead = await seat(env.store, "small-lead", target, assigned=target)
    error = await refused(ctl, agent(lead))
    assert error.code == "work_small_item_limit"
    path = env.root / "work/in_progress/admission__target/spec.md"
    path.write_text(path.read_text().replace("tags: [small]\n", ""))
    assert (await ctl.spawn(agent(lead), HOST))["type"] == "spawn.ok"
    assert tmux.created == 1


@pytest.mark.asyncio
async def test_small_item_two_callers_race_for_one_intent(env):
    import asyncio
    ctl, _, tmux, caller, home, target, _ = await front_desk(env, small=True)
    other = await seat(env.store, "other-caller", home, assigned=home)
    entered = asyncio.Event()
    original = ctl._work_link_refusal
    checks = 0
    async def barrier(msg, binding):
        nonlocal checks
        result = await original(msg, binding)
        checks += 1
        if checks == 2:
            entered.set()
        await asyncio.wait_for(entered.wait(), 2)
        return result
    env.monkeypatch.setattr(ctl, "_work_link_refusal", barrier)
    results = await asyncio.gather(
        ctl.spawn(agent(caller, spec=target, name="first"), HOST),
        ctl.spawn(agent(other, spec=target, name="second", role="lead"), HOST), return_exceptions=True)
    assert sum(isinstance(r, dict) and r.get("type") == "spawn.ok" for r in results) == 1
    assert [r.code for r in results if isinstance(r, VerbError)] == ["work_small_item_limit"]
    assert tmux.created == 1


@pytest.mark.asyncio
async def test_small_item_expired_intent_holds_seat_until_terminalized(env):
    import time
    ctl, _, tmux, caller, _, target, _ = await front_desk(env, small=True)
    assert await env.store.reserve_stream_id(HOST, "recovering", ttl_s=60, request_id="first", nonce="first")
    payload = {"open_fields": {"assigned_spec_id": target}}
    assert await env.store.record_spawn_intent(HOST, "recovering", payload, request_id="first", nonce="first",
                                               small_item_spec_id=target)
    await env.store.submit(lambda conn: conn.execute(
        "UPDATE v2_stream_reservations SET expires_at=? WHERE session_name='recovering'", (time.time() - 60,)))
    await env.store.submit(lambda conn: conn.commit())
    assert (await refused(ctl, agent(caller, spec=target))).code == "work_small_item_limit"
    # Recovery updates its own retained intent, without another claim.
    assert await env.store.record_spawn_intent(HOST, "recovering", payload, request_id="first", nonce="first",
                                               small_item_spec_id=target)
    assert await env.store.fetch_session(HOST, "recovering") is None
    assert tmux.created == 0
    assert await env.store.release_stream_id_fenced(HOST, "recovering", "first", cleanup_confirmed=True)
    assert (await ctl.spawn(agent(caller, spec=target), HOST))["type"] == "spawn.ok"


@pytest.mark.asyncio
async def test_small_item_admitted_replay_does_not_claim_twice(env):
    ctl, _, tmux, caller, _, target, _ = await front_desk(env, small=True)
    msg = agent(caller, spec=target, key="one-seat")
    first = await ctl.spawn(msg, HOST)
    assert (await ctl.spawn(msg, HOST))["stream_id"] == first["stream_id"]
    assert tmux.created == 1


@pytest.mark.asyncio
async def test_small_item_handoff_keeps_continuity(env):
    ctl, _, tmux, caller, _, target, _ = await front_desk(env, small=True)
    first = await seat(env.store, "predecessor", target, assigned=target)
    await ctl.spawn(agent(caller, spec=target, name="successor", parent=False,
                          handoff=True, handoff_from_stream_id=first[0]), HOST)
    assert (await env.store.fetch_session(HOST, "successor"))["assigned_spec_id"] == target
    assert tmux.created == 1


@pytest.mark.asyncio
async def test_small_qa_target_refused_even_when_assigned_item_is_ordinary(env):
    ctl, _, tmux, caller, home, target, _ = await front_desk(env, small=True)
    await lane(env.store, "wl-home", home)
    error = await refused(ctl, agent(caller, spec=home, qa_spec_id=target))
    assert error.code == "work_small_item_limit" and target in str(error)
    assert tmux.created == 0


@pytest.mark.asyncio
async def test_index_unavailable_does_not_waive_spec_readability(env):
    from _shared.spec_reader import SpecReader
    from _shared.specs_service import SpecResolutionSnapshot
    ctl, _, tmux, caller, _, target, _ = await front_desk(env, linked=False)
    await env.store.submit(lambda conn: conn.execute(
        "UPDATE v2_work_index_state SET state_json=?", (json.dumps({"available": False}),)))
    def unavailable(*args):
        raise ValueError("spec_changed")
    env.monkeypatch.setattr(SpecReader, "read", unavailable)
    env.monkeypatch.setattr(SpecResolutionSnapshot, "read_spec_text", unavailable)
    assert (await refused(ctl, agent(caller, spec=target))).code == "spec_unavailable"
    assert tmux.created == 0


@pytest.mark.asyncio
async def test_available_errored_index_still_requires_membership(env):
    ctl, _, _, caller, _, target, _ = await front_desk(env, linked=False)
    await env.store.submit(lambda conn: conn.execute(
        "UPDATE v2_work_index_state SET state_json=?", (json.dumps({"available": True, "error": "stale"}),)))
    assert (await refused(ctl, agent(caller, spec=target))).code == "work_lane_membership_required"


@pytest.mark.asyncio
async def test_lane_admission_uses_goal_derived_effective_membership(env):
    ctl, _, _, caller, _, target, _ = await front_desk(env, linked=False)
    await lane(env.store, "wl-goal", state="done")
    def observations(conn):
        conn.execute("UPDATE v2_work_index_state SET state_json=?", (json.dumps({"available": True,
            "goal_observations": {"epic_fixture": {"quality": "fresh", "facts": {"lane_id": "wl-goal"}}}}),))
        conn.execute("INSERT INTO v2_work_item_observations(spec_id,record_json) VALUES(?,?)",
                     (target, json.dumps({"last_good": {"_epic": "epic_fixture"}})))
        conn.commit()
    await env.store.submit(observations)
    assert (await ctl.spawn(agent(caller, spec=target), HOST))["type"] == "spawn.ok"


@pytest.mark.asyncio
async def test_wrong_fd_generation_does_not_apply_front_desk_rules(env):
    ctl, _, _, caller, _, _, _ = await front_desk(env)
    msg = agent((caller[0], "different-generation"))
    # This isolated controller entry assumes upstream caller auth; specifically
    # assert the new FD predicate does not classify the wrong generation.
    from spawnctl import front_desk_commission
    assert not front_desk_commission(msg, ctl.sessions.assistant.front_desk)


@pytest.mark.asyncio
async def test_forged_assignment_exemption_cannot_bypass_direct_admission(env):
    ctl, _, _, caller, _, _, _ = await front_desk(env)
    assert (await refused(ctl, agent(caller, assignment_exempt="planner"))).code == "work_assignment_required"


@pytest.mark.asyncio
async def test_fd_resume_of_small_seat_preserves_identity_without_new_claim(env):
    from test_spawn_resume import (_machine, _write_transcript, _closed_prior,
                                   ResumeTmux, _install_successful_spawn, RESUME_ID)
    from sessions import Sessions
    from spawnctl import SpawnCtl
    original, _, _, caller, _, target, composite = await front_desk(env, small=True)
    machine = _machine(env.root.parent)
    path = _write_transcript(machine)
    await _closed_prior(env.store, path)
    await env.store.update_session("localhost", "v2-original-fable", spec_ids=[target], assigned_spec_id=target)
    tmux = ResumeTmux()
    sessions = Sessions(env.store, tmux=tmux, local_host="localhost")
    sessions.assistant.front_desk = composite
    ctl = SpawnCtl(env.store, sessions, tmux=tmux, machine=machine, specs=original.specs)
    commands = _install_successful_spawn(ctl, sessions, tmux)
    await ctl.spawn({"provider": "claude", "model": "claude-fable-5-1", "effort": "low",
        "resume_session_id": RESUME_ID, "request_id": "resume-small", "objective": "Continue the small item",
        "_auth_context": {"token_verified": True, "stream_id": caller[0], "session_generation": caller[1]}}, "localhost")
    row = await env.store.fetch_session("localhost", "v2-original-fable")
    assert row["assigned_spec_id"] == target and row["claude_session_id"] == RESUME_ID
    assert len(commands) == 1 and "--resume" in commands[0]


@pytest.mark.asyncio
async def test_small_item_read_never_falls_back_to_the_live_memory_root(env):
    """F1: a registry with no memory_root is a defined refusal, never a read of PENTACLE_MEMORY_ROOT."""
    from types import SimpleNamespace
    ctl, _, _, _, _, target, _ = await front_desk(env, small=True)  # sets PENTACLE_MEMORY_ROOT to a small-tagged tree
    ctl.specs = SimpleNamespace(resolution_for=lambda _spec_id: "resolved")
    error = await refused_small(ctl, target)
    assert error.code == "spec_unavailable"


async def refused_small(ctl, target):
    with pytest.raises(VerbError) as exc:
        await ctl._small_assigned_item({"role": "worker"}, {"assigned_spec_id": target})
    return exc.value


@pytest.mark.asyncio
async def test_small_item_is_read_once_from_the_binding_snapshot_outside_the_graph_lock(env):
    """F2: the tag read adds no board scan, happens once per commission, and never under the graph lock."""
    import work_item_admission
    from _shared.specs_service import SpecsSubsystem
    ctl, sessions, tmux, caller, _, target, _ = await front_desk(env)
    scans, reads, during = [], [], []
    scan = SpecsSubsystem._scan_folders_by_id
    tag = work_item_admission.small_tag
    env.monkeypatch.setattr(SpecsSubsystem, "_scan_folders_by_id",
                            lambda self, *a, **k: scans.append(1) or scan(self, *a, **k))
    env.monkeypatch.setattr(work_item_admission, "small_tag",
                            lambda text: reads.append(sessions._graph_lock.locked()) or tag(text))
    small = ctl._small_assigned_item

    async def counted(msg, binding):
        before = len(scans)
        try:
            return await small(msg, binding)
        finally:
            during.append(len(scans) - before)
    env.monkeypatch.setattr(ctl, "_small_assigned_item", counted)
    await ctl.spawn(agent(caller, spec=target), HOST)
    assert tmux.created == 1
    assert during and set(during) == {0}
    assert reads == [False]


@pytest.mark.asyncio
async def test_fd_scheduled_seat_commission_with_explicit_spec_is_admitted(env):
    """F3: schedule insert vouches for an explicit --spec-id; an inherited assignment is still refused."""
    import uuid
    from window_schedule import WindowSchedule
    from test_window_schedule_contract import FakeComms, future_time
    import launch
    ctl, sessions, _, caller, home, target, _ = await front_desk(env)
    ctl.machine = launch.local_machine(HOST, cwd=str(env.root), claude_bin="/test/claude",
                                       projects_root=str(env.root), agent_orch_bin_dir="/test/bin")
    await env.store.update_session(HOST, "front-desk", spec_ids=[home, target], spec_binding_provenance=[
        {"spec_id": s, "provenance": "spawn_explicit", "granting_principal": "operator",
         "granted_at": "2026-10-09T00:00:00Z"} for s in (home, target)])
    schedule = WindowSchedule(env.store, sessions, FakeComms("delivered"), ctl, local_host=HOST)
    schedule.mark_store_ready()

    def insert(**extra):
        return {"type": "schedule.insert", "request_id": str(uuid.uuid4()), "from_stream_id": caller[0],
                "_auth_context": {"token_verified": True, "stream_id": caller[0], "session_generation": caller[1]},
                "objective": "Scheduled front desk work", "fires_at_utc": future_time(), "target_host": HOST,
                "provider": "claude", "model": "claude-opus-4-8", "effort": "high", "role": "worker",
                "initial_prompt": "Do the work.", **extra}

    inserted = await schedule.schedule_insert(insert(spec_ids=[target]))
    assert inserted["schedule"]["schedule_id"]
    with pytest.raises(VerbError) as exc:
        await schedule.schedule_insert(insert())
    assert exc.value.code == "work_assignment_required"
