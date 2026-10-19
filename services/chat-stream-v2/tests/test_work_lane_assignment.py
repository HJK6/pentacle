"""Item (1): a new managed commission names one assigned spec in an open product lane.

Lane tracking minimal A1-A5 with QA corrections R1, R4, R5, R6.
"""
from __future__ import annotations

import json

import pytest

from sessions import VerbError
from store import UNASSIGNED, Store, effective_assignment
from store_work_lanes import _assigned_seats_conn
from test_spawn_wip_cap import HOST, setup

SUMMARY = "---\nid: work_{stem}\n---\n**Status** — Underway.\n\n**Next action** — Continue.\n"
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
        conn.execute(
            "INSERT INTO v2_assistant_composite_lanes (lane_id,stream_id,phase,summary,version,created_at,updated_at,"
            "work_state,work_state_reason,owner_kind,title,members_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (lane_id, "fixture:assistant", "execution", "", 4, "2026-10-09T00:00:00Z", "2026-10-09T00:00:00Z",
             state, "fd", "fd", "Lane " + lane_id, json.dumps(list(members))))
        conn.commit()
    await store.submit(op)


async def seat(store, name, *spec_ids, assigned=None, role="lead"):
    row = await store.open_session(HOST, name, provider="claude", role=role, spec_ids=list(spec_ids),
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
async def test_unlinked_explicit_commission_is_refused_before_any_pane(env):
    target, home = item(env.root, "demo__target"), item(env.root, "demo__home")
    ctl, _sessions, tmux = await setup(env.root, env.store, env.monkeypatch)
    await lane(env.store, "wl-home", home)
    caller = await seat(env.store, "caller", home, assigned=home)
    err = await refused(ctl, agent(caller, spec=target))
    assert err.code == "work_lane_assignment_unlinked"
    # The repair names the caller's lane with its current members and version filled in.
    assert (f"agent-orch work-lane set-members wl-home --member {home} --member {target} "
            "--expected-version 4") in str(err)
    assert tmux.created == 0 and await env.store.fetch_session(HOST, "child") is None


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
    err = await refused(ctl, agent(caller))
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
@pytest.mark.parametrize("role, exempt", [("assistant", True), ("lead", False)])
async def test_protected_assistant_caller_is_exempt_and_other_roles_are_not(env, role, exempt):
    # A2/R5: a protected assistant seat is exempt; a seat's ordinary role never is.
    env.monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    target = item(env.root, "demo__target")
    ctl, _sessions, tmux = await setup(env.root, env.store, env.monkeypatch)
    caller = await seat(env.store, "caller", role=role)  # unassigned, no spec, no lane
    msg = agent(caller, spec=target, parent=False)
    if exempt:
        assert (await ctl.spawn(msg, HOST))["type"] == "spawn.ok"
        assert tmux.created == 1
    else:
        assert (await refused(ctl, msg)).code == "work_lane_assignment_unlinked"
        assert tmux.created == 0


@pytest.mark.asyncio
async def test_operator_elevated_seat_token_is_still_managed(env):
    target = item(env.root, "demo__target")
    ctl, _sessions, _tmux = await setup(env.root, env.store, env.monkeypatch)
    caller = await seat(env.store, "caller", target, assigned=target)
    msg = agent(caller, spec=target)
    msg["_auth_context"].update(operator_authenticated=True, operator_authority_source="stream_token")
    assert (await refused(ctl, msg)).code == "work_lane_assignment_unlinked"


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
async def test_done_lane_membership_is_not_a_link(env):
    target = item(env.root, "demo__target")
    ctl, _sessions, _tmux = await setup(env.root, env.store, env.monkeypatch)
    await lane(env.store, "wl-old", target, state="done")
    caller = await seat(env.store, "caller", target, assigned=target)
    assert (await refused(ctl, agent(caller, spec=target))).code == "work_lane_assignment_unlinked"


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
async def test_fd_direct_execution_assign_checks_link_and_unassign_keeps_tags(env):
    specs = [item(env.root, f"demo__p{i}") for i in range(2)]
    target = item(env.root, "demo__target")
    ctl, sessions, _tmux = await setup(env.root, env.store, env.monkeypatch)
    await lane(env.store, "wl-p", *specs)
    fd = await seat(env.store, "fd", *specs)
    agent_auth = {"token_verified": True, "stream_id": fd[0], "session_generation": fd[1]}
    with pytest.raises(VerbError) as exc:
        await spec_update(env, ctl, sessions, "assign", fd[0], target, agent_auth)
    assert exc.value.code == "work_lane_assignment_unlinked"
    assert (await env.store.fetch_session(HOST, "fd"))["assigned_spec_id"] is None
    reply = await spec_update(env, ctl, sessions, "assign", fd[0], specs[0], agent_auth)
    assert reply["session"]["assigned_spec_id"] == specs[0] and reply["work_checkpoint"]["state"] == "valid"
    reply = await spec_update(env, ctl, sessions, "unassign", fd[0], auth=agent_auth)
    assert reply["session"]["assigned_spec_id"] == UNASSIGNED and reply["session"]["spec_ids"] == specs
    assert effective_assignment(reply["session"]) is None


@pytest.mark.asyncio
async def test_operator_assign_is_recorded_without_refusal_and_detach_clears(env):
    target = item(env.root, "demo__target")
    ctl, sessions, _tmux = await setup(env.root, env.store, env.monkeypatch)
    chat = await seat(env.store, "chat", role=None)
    operator = {"operator_authenticated": True, "operator_principal": "operator:fixture"}
    reply = await spec_update(env, ctl, sessions, "assign", chat[0], target, operator)
    assert reply["session"]["assigned_spec_id"] == target and reply["session"]["spec_ids"] == [target]
    reply = await spec_update(env, ctl, sessions, "detach", chat[0], target, operator)
    assert reply["session"]["assigned_spec_id"] == UNASSIGNED and effective_assignment(reply["session"]) is None


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
    # A new commission from it must name its spec; the tag is not an implicit assignment.
    assert (await refused(ctl, agent(lead, name="kid"))).code == "work_assignment_required"
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
