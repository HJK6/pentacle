"""Hidden FD-owned leads (spec_pentacle__work_lane_hidden_fd_leads_2026_10, T1-T7).

Lead eligibility is separate from visible-chat eligibility: an FD-owned lane
keeps a hidden, explicitly bound lead/nexus; operator-owned lanes, visible
chats and the adoption preview keep the visible-only rule.
"""
import pytest

from sessions import Sessions
from work_lanes_projection import WorkLanesInventory
from test_work_lanes import FD, code, lead_lost_episodes, run
from test_assistant_prose_mirror import ASSISTANT


async def inventory(env):
    sessions = Sessions(env.store, tmux=None, local_host="host-a")
    await sessions.refresh()

    async def broadcast(_frame):
        return None
    return sessions, WorkLanesInventory(env.store, sessions, broadcast, refresh_min_s=0)


async def refresh(inv):
    """One real drain pass: lead-loss reconcile, then the projection read."""
    inv.refresh()
    await inv._task
    return {lane["lane_id"]: lane for lane in (await inv.current())["lanes"]}


async def stored(env, lane_id):
    shown = await env.store.get_work_lane(lane_id)
    return shown["lane"], [e["operation"] for e in shown["events"]]


def lead_of(seat):
    return {"lead": {"stream_id": seat[0], "generation": seat[1]}}


@pytest.mark.parametrize("role", ["lead", "nexus"])
def test_fd_lead_hidden_keeps_lane_active_t1(role):
    async def body(env):
        lead = await env.seat("t1-lead", role=role, parent_stream_id=FD)
        lid = (await env.adopt(key="stream:t1", lead=lead))["lane"]["lane_id"]
        sessions, inv = await inventory(env)
        await sessions.set_visibility("host-a", "t1-lead", "hidden")
        lane = (await refresh(inv))[lid]
        assert (lane["state"], lane["state_reason"]) == ("active", "fd")
        assert lane["lead"]["qualifies"] is True and lane["lead"]["visibility"] == "hidden"
        row, ops = await stored(env, lid)
        assert row["work_state"] == "active" and "lead_lost" not in ops
        assert (await refresh(inv))[lid]["state"] == "active"  # every later pass agrees
    run(body)


def test_fd_lane_hidden_lead_eligibility_t2(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "fixture-assistant")

    async def body(env):
        hidden_lead = await env.seat("t2-lead", role="lead", parent_stream_id=FD, visibility="hidden")
        lane = (await env.adopt(key="stream:t2", lead=hidden_lead))["lane"]
        assert lane["work_state"] == "active"
        nexus = await env.seat("t2-nexus", role="nexus", parent_stream_id=FD, visibility="hidden")
        lane = (await env.op("set_lead", lead_of(nexus), lane=lane["lane_id"], version=lane["version"]))["lane"]
        assert (lane["work_state"], lane["bound_stream_id"]) == ("active", nexus[0])
        for name, fields in (("t2-worker", {"parent_stream_id": FD, "visibility": "hidden"}),
                             ("t2-top", {"visibility": "hidden"}),
                             ("t2-qa", {"role": "qa", "parent_stream_id": FD, "visibility": "hidden"}),
                             ("t2-sub", {"role": "lead", "parent_stream_id": FD, "visibility": "subagent"}),
                             ("t2-protected", {"role": "fixture-assistant", "visibility": "hidden"})):
            seat = await env.seat(name, **fields)
            with pytest.raises(ValueError) as e:
                await env.op("set_lead", lead_of(seat), lane=lane["lane_id"], version=lane["version"])
            assert code(e) == "work_lane_lead_ineligible", name
        # A paused FD lane resumes on an explicit set_state with its hidden lead.
        first = await env.seat("t2-first", role="lead", parent_stream_id=FD, visibility="hidden")
        paused = (await env.adopt(key="stream:t2p", state="paused", lead=first))["lane"]
        late = await env.seat("t2-late", role="lead", parent_stream_id=FD, visibility="hidden")
        paused = (await env.op("set_lead", lead_of(late), lane=paused["lane_id"], version=1))["lane"]
        assert paused["work_state"] == "paused"  # binding never resumes
        r = await env.op("set_state", {"to": "active"}, lane=paused["lane_id"], version=paused["version"])
        assert r["lane"]["work_state"] == "active"
    run(body)


@pytest.mark.parametrize("path", ["closed", "reconciled_dead", "new_generation", "missing"])
def test_fd_hidden_lead_loss_still_reconciles_once_t3(path):
    async def body(env):
        lead = await env.seat("t3-lead", role="lead", parent_stream_id=FD, visibility="hidden")
        lid = (await env.adopt(key="stream:t3", lead=lead))["lane"]["lane_id"]
        if path == "closed":
            await env.store.mark_closed("host-a", "t3-lead", closed_at="2026-10-15T20:00:00Z",
                                        pane_status="pane_dead", close_kind="manager_close")
        elif path == "reconciled_dead":
            await env.store.mark_reconciled_dead("host-a", "t3-lead", expected_generation=lead[1],
                                                 presumed_dead_at="2026-10-15T20:00:00Z",
                                                 closed_at="2026-10-15T20:00:00Z")
        elif path == "new_generation":
            await env.store.mark_closed("host-a", "t3-lead", closed_at="2026-10-15T20:00:00Z",
                                        pane_status="pane_dead")
            await env.store.open_session("host-a", "t3-lead", provider="claude", role="lead",
                                         parent_stream_id=FD, visibility="hidden")
        else:
            def drop(conn):
                conn.execute("DELETE FROM sessions WHERE host='host-a' AND session_name='t3-lead'")
                conn.commit()
            await env.store.submit(drop)
        _, inv = await inventory(env)
        lane = (await refresh(inv))[lid]
        assert (lane["state"], lane["state_reason"]) == ("active", "lead_lost")
        row, ops = await stored(env, lid)
        assert row["work_state"] == "active" and "lead_lost" not in ops
        assert len(await lead_lost_episodes(env.store, lid)) == 1
        await refresh(inv)
        assert len(await lead_lost_episodes(env.store, lid)) == 1
    run(body)


def test_explicit_paused_blocked_done_fd_lanes_unchanged_t4():
    async def body(env):
        lead = await env.seat("t4-lead", role="lead", parent_stream_id=FD, visibility="hidden")
        paused = (await env.adopt(key="stream:t4p", state="paused", lead=lead))["lane"]
        blocked = (await env.adopt(key="stream:t4b", state="blocked", lead=lead, blocker="keys"))["lane"]
        done = (await env.adopt(key="stream:t4d", lead=lead))["lane"]
        done = (await env.op("set_state", {"to": "done", "outcome": "Shipped"}, lane=done["lane_id"],
                             version=done["version"]))["lane"]
        _, inv = await inventory(env)
        presented = await refresh(inv)
        assert (presented[paused["lane_id"]]["state"], presented[paused["lane_id"]]["state_reason"]) \
            == ("paused", "fd")
        assert (presented[blocked["lane_id"]]["state"], presented[blocked["lane_id"]]["blocker"]) \
            == ("blocked", "keys")
        assert done["lane_id"] not in presented
        for lane, state in ((paused, "paused"), (blocked, "blocked"), (done, "done")):
            row, ops = await stored(env, lane["lane_id"])
            assert row["work_state"] == state and row["version"] == lane["version"]
            assert "lead_lost" not in ops
    run(body)


def test_operator_lanes_keep_visible_lead_rule_t5():
    async def body(env):
        # Hiding an operator lane's lead still loses it (former V4 visibility_hidden case).
        lead = await env.seat("t5-lead", role="lead", parent_stream_id=FD)
        lid = (await env.adopt(key="stream:t5", owner="operator", lead=lead))["lane"]["lane_id"]
        sessions, inv = await inventory(env)
        await sessions.set_visibility("host-a", "t5-lead", "hidden")
        assert ((await refresh(inv))[lid]["state_reason"]) == "lead_lost"
        assert len(await lead_lost_episodes(env.store, lid)) == 1
        # A hidden seat cannot be bound to an operator lane.
        hidden = await env.seat("t5-hidden", role="lead", parent_stream_id=FD, visibility="hidden")
        row, _ = await stored(env, lid)
        with pytest.raises(ValueError) as e:
            await env.op("set_lead", lead_of(hidden), lane=lid, version=row["version"])
        assert code(e) == "work_lane_lead_ineligible"
        with pytest.raises(ValueError) as e:
            await env.adopt(key="stream:t5-adopt", owner="operator", lead=hidden)
        assert code(e) == "work_lane_lead_ineligible"
        # Re-owning an FD lane to the operator re-applies the visible rule.
        fd_lane = (await env.adopt(key="stream:t5-fd", lead=hidden))["lane"]
        fd_lane = (await env.op("set_owner", {"to": "operator"}, lane=fd_lane["lane_id"],
                                version=fd_lane["version"]))["lane"]
        rows = {r["lane_id"]: r for r in await env.store.work_lane_rows()}
        assert rows[fd_lane["lane_id"]]["_qualifies"] is False
        frame = {lane["lane_id"]: lane for lane in (await inv.current())["lanes"]}
        assert frame[fd_lane["lane_id"]]["state_reason"] == "lead_lost"
        assert (await refresh(inv))[fd_lane["lane_id"]]["state_reason"] == "lead_lost"
        assert len(await lead_lost_episodes(env.store, fd_lane["lane_id"])) == 1
        # Handing it back to the FD re-qualifies its hidden lead: the fault is repaired, never a pause.
        fd_lane, _ = await stored(env, fd_lane["lane_id"])
        back = (await env.op("set_owner", {"to": "fd", "reason": "FD resumes management"},
                             lane=fd_lane["lane_id"], version=fd_lane["version"]))["lane"]
        assert back["work_state"] == "active"
        assert (await lead_lost_episodes(env.store, fd_lane["lane_id"]))[0]["cleared_at"] is not None
    run(body)


def test_hidden_seats_never_become_chat_destinations_t6():
    async def body(env):
        hidden = await env.seat("t6-lead", role="lead", parent_stream_id=FD, visibility="hidden")
        hidden_chat = {"stream_id": hidden[0], "generation": hidden[1]}
        with pytest.raises(ValueError) as e:
            await env.adopt(key="stream:t6-chat", lead=hidden, chat=hidden_chat)
        assert code(e) == "work_lane_visible_chat_invalid"
        lane = (await env.adopt(key="stream:t6", lead=hidden))["lane"]
        with pytest.raises(ValueError) as e:
            await env.op("set_chat", {"visible_chat": hidden_chat}, lane=lane["lane_id"], version=lane["version"])
        assert code(e) == "work_lane_visible_chat_invalid"
        with pytest.raises(ValueError) as e:
            await env.op("set_lead", dict(lead_of(hidden), visible_chat=hidden_chat), lane=lane["lane_id"],
                         version=lane["version"])
        assert code(e) == "work_lane_visible_chat_invalid"
        _, inv = await inventory(env)
        presented = (await refresh(inv))[lane["lane_id"]]
        assert presented["state"] == "active"
        assert presented["visible_chat"]["stream_id"] == ASSISTANT
        await env.seat("t6-unadopted", role="lead", parent_stream_id=FD, visibility="hidden")
        preview = await env.store.work_lane_adopt_preview(composite_stream_id=ASSISTANT,
                                                          env_binding=env.composite._env_binding())
        assert not [c for c in preview if c["adoption_key"].startswith("stream:host-a:t6")]
    run(body)


def test_paused_lead_lost_recovery_is_explicit_fd_set_state_t7():
    async def body(env):
        lead = await env.seat("t7-lead", role="lead", parent_stream_id=FD, visibility="hidden")
        lane = (await env.adopt(key="stream:t7", lead=lead))["lane"]

        def as_before_deploy(conn):
            # The pre-fix reconciler's stored result for a hidden FD lead.
            conn.execute("UPDATE v2_assistant_composite_lanes SET work_state='paused',"
                         " work_state_reason='lead_lost', version=version+1 WHERE lane_id=?",
                         (lane["lane_id"],))
            conn.commit()
        await env.store.submit(as_before_deploy)
        _, inv = await inventory(env)
        presented = (await refresh(inv))[lane["lane_id"]]
        assert (presented["state"], presented["state_reason"]) == ("paused", "lead_lost")
        assert presented["lead"]["qualifies"] is True  # eligible, but no automatic unpause
        # A qualifying lead is no owner loss, so no episode opens; resuming the stored pause stays explicit.
        assert await lead_lost_episodes(env.store, lane["lane_id"]) == []
        row, _ = await stored(env, lane["lane_id"])
        r = await env.op("set_state", {"to": "active"}, lane=lane["lane_id"], version=row["version"])
        assert r["lane"]["work_state"] == "active"
        assert (await refresh(inv))[lane["lane_id"]]["state"] == "active"
        assert "lead_lost" not in (await stored(env, lane["lane_id"]))[1]
        assert await lead_lost_episodes(env.store, lane["lane_id"]) == []
    run(body)
