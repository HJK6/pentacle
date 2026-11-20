"""The originating commission and the final voluntary departure share attendance."""
import asyncio
import time

import pytest

from _shared.work_observations import read_work_candidate
from sessions import VerbError
from test_assistant_lane_rulings import admission_rig, approve_admission
from test_work_lane_assignment import HOST, agent, env, seat, spec_update
from test_lead_close_gate import environment, request
from work_attendance import assigned_owners_conn


async def accept(store, folder, spec):
    now = time.time() + 2
    candidate = read_work_candidate(folder, folder.parent.name, now=now, quiet_s=0)
    await store.reconcile_work_observations({"available": True, "root_configured": True,
        "candidates": {spec: [candidate]}, "errors": {}, "scanned_at": now}, settle_s=0, sweep=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("small", [False, True])
async def test_correct_role_before_ruling_and_single_seat_claim(env, small):
    server, _, tmux, caller, advisor, _, spec = await admission_rig(env, small=small)
    from test_spawn_objective_compat import EchoPane
    echo = EchoPane()
    tmux.paste, tmux.capture = echo.paste, echo.capture
    source = env.root / "work/in_progress/admission__target"
    await accept(env.store, source, spec)
    msg = agent(caller, spec=spec, role=None, key="fixture-work", initial_prompt="Check the fixture.")
    try:
        with pytest.raises(VerbError) as error:
            await server._on_spawn(msg)
        assert [item["code"] for item in error.value.extra["errors"]] == ["work_owner_required"]
        assert f"--role lead --spec-id {spec}" in error.value.extra["errors"][0]["correction"]
        assert await env.store.reservations(include_expired=True) == [] and tmux.created == 0
        assert await env.store.submit(lambda c: c.execute("SELECT COUNT(*) FROM v2_assistant_lane_rulings").fetchone()[0]) == 0
        msg["role"] = "lead"
        held = await server._on_spawn(msg)
        released = await approve_admission(server, held, advisor)
        assert released["state"] == "done", released.get("outcome")
        assert (await server._on_spawn(msg))["ruling_request_id"] == held["ruling_request_id"]
        assert tmux.created == 1
        row = await env.store.fetch_session(HOST, "child")
        assert row["role"] == "lead" and row["assigned_spec_id"] == spec
        await env.store.update_session(HOST, "child", pane_status="pane_alive")
        assert (await env.store.submit(assigned_owners_conn))[spec] == [HOST + ":child"]
    finally:
        await server.lane_rulings.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["desk", "helper", "planner", "stale", "unknown_pane", "slow_store"])
async def test_admit_served_or_unverified_work(env, case):
    server, ctl, _, caller, _, _, spec = await admission_rig(env)
    await accept(env.store, env.root / "work/in_progress/admission__target", spec)
    if case == "desk":
        await spec_update(env, ctl, server.sessions, "assign", caller[0], spec)
        await env.store.update_session(HOST, "front-desk", pane_status="pane_alive")
    if case in {"helper", "unknown_pane"}:
        await seat(env.store, "lead", spec, assigned=spec)
        if case == "helper":
            await env.store.update_session(HOST, "lead", pane_status="pane_alive")
    if case == "stale":
        await env.store.mark_work_index_pending()
    if case == "slow_store":
        import work_attendance
        original = work_attendance.commission_ownership_conn
        def delayed(*args):
            time.sleep(0.025)
            return original(*args)
        env.monkeypatch.setattr("sessions.LEAD_CLOSE_REFRESH_TIMEOUT_S", 0.01)
        env.monkeypatch.setattr(work_attendance, "commission_ownership_conn", delayed)
    try:
        msg = agent(caller, role="planner" if case == "planner" else None, spec=spec,
                    initial_prompt="Check the fixture.", key="fixture-work")
        assert (await server._on_spawn(msg))["type"] == "spawn.pending_ruling"
        rows = await env.store.submit(lambda c: [dict(r) for r in c.execute("SELECT * FROM v2_work_attendance_causes WHERE gate='commission'")])
        causes = {"stale": "source_unverified", "unknown_pane": "seat_liveness_unverified", "slow_store": "store_timeout"}
        if case in causes:
            assert [(r["spec_id"], r["generation"], r["gate"], r["cause"]) for r in rows] == [
                (spec, caller[1], "commission", causes[case])]
        else:
            assert rows == []
    finally:
        await server.lane_rulings.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["unassign", "detach", "assign", "role"])
async def test_final_assignment_loss_refuses_until_live_successor(env, action):
    server, ctl, _, caller, _, home, spec = await admission_rig(env)
    await accept(env.store, env.root / "work/in_progress/admission__target", spec)
    stream, generation = await seat(env.store, "owner", assigned=spec)
    await env.store.update_session(HOST, "owner", pane_status="pane_alive", spec_ids=[spec],
                                   spec_binding_provenance=[])
    async def mutate():
        if action == "role":
            return await server.sessions.set_role(HOST, "owner", "worker", auth_context={"operator_authenticated": True})
        return await spec_update(env, ctl, server.sessions, action, stream, home if action == "assign" else spec)
    try:
        with pytest.raises(VerbError) as error:
            await mutate()
        assert error.value.code == "work_last_owner" and error.value.extra["expected_generation"] == generation
        assert [item["spec_id"] for item in error.value.extra["items"]] == [spec]
        await spec_update(env, ctl, server.sessions, "assign", caller[0], spec)
        await env.store.update_session(HOST, "front-desk", pane_status="pane_alive")
        await mutate()
        assert (await env.store.submit(assigned_owners_conn))[spec] == [caller[0]]
    finally:
        await server.lane_rulings.stop()


@pytest.mark.asyncio
async def test_simultaneous_ordinary_closes_leave_one_live_generation(tmp_path):
    from test_close_defer_and_attribution import HOST as close_host
    store, sessions, server, tmux, first = await environment()
    spec = "spec_demo__item_00"
    folder = tmp_path / "work/in_progress/demo__item_00"
    folder.mkdir(parents=True)
    (folder / "spec.md").write_text(f"---\nid: {spec}\ntitle: Concurrent close fixture\n---\n")
    (folder / "summary.md").write_text("Working.\n")
    await accept(store, folder, spec)
    second = await store.open_session(close_host, "second", role="lead", pane_status="pane_alive", assigned_spec_id=spec)
    # Independent pane state: terminating one seat does not change its peer.
    alive = {first["session_name"], second["session_name"]}
    async def has_session(name):
        return name in alive
    async def kill_session(name):
        alive.remove(name)
    tmux.has_session, tmux.kill_session = has_session, kill_session
    try:
        results = await asyncio.gather(*(server._on_close(request(row)) for row in (first, second)), return_exceptions=True)
        assert [r["type"] for r in results if isinstance(r, dict)] == ["close.ok"]
        assert [e.code for e in results if isinstance(e, VerbError)] == ["work_last_owner"]
        remaining, = await store.list_sessions("open")
        assert remaining["session_name"] in alive and len(alive) == 1
    finally:
        store.stop()


@pytest.mark.asyncio
async def test_same_approved_close_retries_after_accepted_disposition(tmp_path):
    from test_approved_close_retry import HOST as peer, journey
    from test_work_owed import observe
    spec = "spec_demo__active"
    folder = tmp_path / "work/in_progress/demo__active"
    folder.mkdir(parents=True)
    (folder / "spec.md").write_text(f"---\nid: {spec}\ntitle: Repair fixture\n---\n")
    (folder / "summary.md").write_text("Implementing.\n")
    async with journey() as rig:
        rig.sessions.assistant.front_desk = rig.server.assistant_composite
        await rig.store.update_session(peer, "lane", assigned_spec_id=spec)
        await accept(rig.store, folder, spec)
        rig.tmux.text = "ready\n"
        pending = await rig.request()
        refused = await rig.approve()
        assert refused["state"] == "approved_but_not_closed"
        assert refused["outcome"]["error_code"] == "work_last_owner" and rig.tmux.kills == 0
        assert refused["outcome"]["items"][0]["spec_id"] == spec
        await observe(rig.store, "blocked", "accepted-pause", time.time() + 3, identity=spec)
        closed = await rig.request()
        assert closed["type"] == "close.ok" and rig.tmux.kills == 1
        assert closed["ruling_request_id"] == pending["ruling_request_id"]


@pytest.mark.asyncio
async def test_reconciler_death_retains_grant_and_generation(env):
    server, _, _, _, _, _, spec = await admission_rig(env)
    try:
        await accept(env.store, env.root / "work/in_progress/admission__target", spec)
        stream, generation = await seat(env.store, "owner", spec, assigned="")
        await env.store.update_session(HOST, "owner", pane_status="pane_alive")
        assert (await env.store.work_unattended())["count"] == 0
        stamp = "2026-01-01T00:00:00Z"
        await server.sessions.mark_reconciled_dead(HOST, "owner", presumed_dead_at=stamp,
                                                  closed_at=stamp, expected_generation=generation)
        assert [item["spec_id"] for item in (await env.store.work_unattended())["items"]] == [spec]
        cause = await env.store.submit(lambda c: dict(c.execute(
            "SELECT * FROM v2_work_attendance_causes WHERE gate='reconciler_dead'").fetchone()))
        assert (cause["spec_id"], cause["stream_id"], cause["generation"], cause["cause"]) == (
            spec, stream, generation, "confirmed_death")
    finally:
        await server.lane_rulings.stop()


@pytest.mark.asyncio
async def test_multi_grant_departure_checks_each_item_and_only_uncovered_ones(env):
    from test_work_lane_assignment import item
    from test_close_defer_and_attribution import IdleTmux
    server, _, _, _, _, home, spec = await admission_rig(env)
    third = item(env.root, "admission__third")
    try:
        now = time.time() + 2
        candidates = {}
        for identity in (home, spec, third):
            folder = env.root / "work/in_progress" / identity.removeprefix("spec_")
            candidates[identity] = [read_work_candidate(folder, "in_progress", now=now, quiet_s=0)]
        await env.store.reconcile_work_observations({"available": True, "root_configured": True,
            "candidates": candidates, "errors": {}, "scanned_at": now}, settle_s=0, sweep=True)
        stream, generation = await seat(env.store, "multi", home, spec, third, assigned=third)
        await env.store.update_session(HOST, "multi", pane_status="pane_alive")
        await env.store.update_session(HOST, "front-desk", pane_status="pane_alive")
        server.sessions.tmux = IdleTmux()
        with pytest.raises(VerbError) as error:
            await server.sessions.close(HOST, "multi", expected_generation=generation)
        assert {entry["spec_id"] for entry in error.value.extra["items"]} == {spec, third}
        assert all(entry["next_commands"] for entry in error.value.extra["items"])
        assert (await env.store.fetch_session(HOST, "multi"))["status"] == "open"
    finally:
        await server.lane_rulings.stop()


@pytest.mark.asyncio
async def test_unverified_close_records_reason_then_surfaces_after_recovery(env):
    from test_close_defer_and_attribution import IdleTmux
    server, _, _, _, _, _, spec = await admission_rig(env)
    folder = env.root / "work/in_progress/admission__target"
    await accept(env.store, folder, spec)
    stream, generation = await seat(env.store, "owner", spec, assigned=spec)
    await env.store.update_session(HOST, "owner", pane_status="pane_alive")
    server.sessions.tmux = IdleTmux()
    await env.store.mark_work_index_pending()
    try:
        result = await server.sessions.close(HOST, "owner", expected_generation=generation)
        warning, = [w for w in result["warnings"] if w["kind"] == "attendance"]
        assert (warning["spec_id"], warning["cause"]) == (spec, "source_unverified")
        cause = await env.store.submit(lambda c: dict(c.execute(
            "SELECT * FROM v2_work_attendance_causes WHERE gate='session_close'").fetchone()))
        assert (cause["stream_id"], cause["generation"], cause["cause"]) == (stream, generation, warning["cause"])
        await accept(env.store, folder, spec)
        assert [entry["spec_id"] for entry in (await env.store.work_unattended())["items"]] == [spec]
    finally:
        await server.lane_rulings.stop()
