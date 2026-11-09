"""Lane execution policy and the uncapped, existing-client activity contract."""
import pytest
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

from test_work_lanes import FD, run
from work_lane_progress import lane_state, lane_lead_lost
from work_lanes_projection import build_frame, project_lane, page_lane_detail
from test_work_lane_assignment import env, item, lane, seat, agent, refused, spec_update
from test_spawn_wip_cap import HOST, setup
from sessions import Sessions, VerbError

NOW = "2026-11-09T10:00:00.000Z"


def motion_lane():
    return {"lane_id": "motion", "version": 1, "work_state": "active", "owner_kind": "fd",
            "bound_stream_id": "fixture:old", "bound_generation": "old-gen", "_qualifies": False,
            "_lead_row": {"status": "closed"}, "_members": [
                {"spec_id": f"spec_demo__s{i:02}", "status": "in_progress"} for i in range(40)]}


@pytest.mark.parametrize("role", ["lead", "nexus", "planner", "worker", "qa"])
@pytest.mark.parametrize("member_index", [0, 27, 39])
def test_any_assigned_role_works_beyond_preview_and_page_caps(role, member_index):
    lane = motion_lane()
    seats = {f"fixture:{i:02}": {"status": "open", "role": role, "online": True,
                                "assigned_spec_id": lane["_members"][member_index]["spec_id"],
                                "working": i == 9} for i in range(10)}
    frame = build_frame([lane], seats, now_iso=NOW)
    projected = frame["lanes"][0]
    assert projected["lead"]["presence"]["working"] is True
    assert projected["lead"]["status"] == "closed" and projected["lead"]["qualifies"] is False
    assert (projected["state"], projected["state_reason"]) == ("active", "lead_lost")
    assert len(projected["members"]) == 8
    full = project_lane(lane, None, NOW, all_members=True, sessions_by_stream=seats)
    shown = {"lane": lane, "events": [], "updates": [], "_work_index": {}}
    assert page_lane_detail(shown, full)["projection"]["lead"]["presence"]["working"] is True


def test_off_preview_question_retains_existing_needs_you_gate():
    lane = motion_lane()
    question = {"spec_demo__s27": {"count": 1, "first_question_id": "q27"}}
    projected = project_lane(lane, None, NOW, sessions_by_stream={}, questions_by_spec=question)
    assert [m["spec_id"] for m in projected["members"]] == [
        *(f"spec_demo__s{i:02}" for i in range(7)), "spec_demo__s27"]
    assert projected["members"][-1]["open_questions"]["count"] == 1


def test_attendance_neither_resumes_pause_nor_hides_owner_loss():
    lane = {**motion_lane(), "_members_working": True}
    assert lane_lead_lost(lane)
    lane.update(work_state="paused", work_state_reason="fd")
    assert lane_state(lane) == ("paused", "fd")


def test_fd_backend_cannot_be_bound_as_lane_execution_lead():
    async def body(env):
        with pytest.raises(ValueError, match="work_lane_lead_ineligible"):
            await env.adopt(key="stream:fd-owner", owner="operator", lead=(FD, env.gen))
    run(body)


def test_active_lead_binding_cannot_be_cleared():
    async def body(env):
        lane = (await env.adopt())["lane"]
        with pytest.raises(ValueError, match="work_lane_visible_lead_required"):
            await env.op("set_lead", {"lead": None}, lane=lane["lane_id"], version=1)
        shown = (await env.store.get_work_lane(lane["lane_id"]))["lane"]
        assert shown["bound_stream_id"] == lane["bound_stream_id"] and shown["version"] == 1
    run(body)


@pytest.mark.parametrize("state", ["paused", "blocked", "done", "orphan", "offline", "generation"])
@pytest.mark.asyncio
async def test_real_spawn_admission_requires_active_owned_lane(env, state):
    target = item(env.root, "demo__target")
    ctl, sessions, tmux = await setup(env.root, env.store, env.monkeypatch)
    await lane(env.store, "wl-a", target, state=state if state in {"paused", "blocked", "done"} else "active")
    caller = await seat(env.store, "caller", target, assigned=target)
    def bind(conn):
        conn.execute("UPDATE v2_assistant_composite_lanes SET bound_stream_id=?,bound_generation=? WHERE lane_id='wl-a'",
                     (caller[0], "stale" if state == "generation" else caller[1]))
        if state == "offline":
            conn.execute("UPDATE sessions SET offline_since_ts=1 WHERE session_name='caller'")
        if state == "orphan":
            conn.execute("UPDATE v2_assistant_composite_lanes SET bound_stream_id=NULL,bound_generation=NULL")
        conn.commit()
    await env.store.submit(bind)
    await refused(ctl, agent(caller, spec=target, role="worker", initial_prompt="Do the scoped work"))
    assert tmux.created == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["active", "paused", "blocked", "done", "orphan"])
async def test_native_resume_rechecks_retained_lane_assignment(env, tmp_path, state):
    from test_spawn_resume import (_machine, _write_transcript, _closed_prior,
                                   ResumeTmux, _install_successful_spawn, RESUME_ID)
    from spawnctl import SpawnCtl
    from _shared.specs_service import SpecsSubsystem
    target = item(env.root, "demo__resume")
    machine = _machine(tmp_path)
    prior = await _closed_prior(env.store, _write_transcript(machine))
    await env.store.update_session("localhost", "v2-original-fable", spec_ids=[target],
        spec_id=target, assigned_spec_id=target,
        spec_binding_provenance=[{"spec_id": target, "provenance": "spawn_explicit",
                                 "granting_principal": "operator", "granted_at": "2026-11-09T00:00:00Z"}])
    await lane(env.store, "wl-resume", target, state="active" if state == "orphan" else state)
    if state == "orphan":
        await env.store.submit(lambda conn: conn.execute(
            "UPDATE v2_assistant_composite_lanes SET bound_generation='lost'"))
    tmux = ResumeTmux()
    sessions = Sessions(env.store, tmux=tmux, local_host="localhost")
    specs = SpecsSubsystem(memory_root=env.root, session_summaries=lambda: [], changed_callback=lambda _: None)
    ctl = SpawnCtl(env.store, sessions, tmux=tmux, machine=machine, specs=specs)
    commands = _install_successful_spawn(ctl, sessions, tmux)
    msg = {"provider": "claude", "model": "claude-fable-5-1", "effort": "high",
           "resume_session_id": RESUME_ID, "request_id": "lane-resume"}
    if state == "active":
        assert (await ctl.spawn(msg, "localhost"))["type"] == "spawn.ok"
        assert len(commands) == 1
    else:
        with pytest.raises(VerbError) as exc:
            await ctl.spawn(msg, "localhost")
        assert exc.value.code in {"work_lane_not_active", "work_lane_assignment_unlinked", "work_lane_visible_lead_required"}
        assert commands == []
        assert (await env.store.fetch_session("localhost", "v2-original-fable"))["session_generation"] == prior["session_generation"]


@pytest.mark.parametrize("to", ["paused", "blocked", "done"])
def test_busy_assigned_worker_blocks_deliberate_transition(to):
    async def body(env):
        owner = await env.seat("drain-lead", role="lead", parent_stream_id=FD)
        lane_row = (await env.adopt(key="stream:drain", lead=owner, members=["spec_demo__drain"]))["lane"]
        worker = await env.seat("drain-worker", role="worker", assigned_spec_id="spec_demo__drain")
        sessions = Sessions(env.store, tmux=None, local_host="host-a")
        await sessions.refresh()
        sessions.apply_live(worker[0], working=True, online=True)
        payload = {"to": to, "reason": "Checkpoint before hold", "next_action": "Resume when keys arrive", "blocker": "keys", "outcome": "Done"}
        with pytest.raises(VerbError) as exc:
            await env.op("set_state", payload, lane=lane_row["lane_id"], version=1)
        assert exc.value.code == "work_lane_drain_required"
        assert exc.value.extra["seat_ids"] == [worker[0]]
        sessions.apply_live(worker[0], working=False)
        assert (await env.op("set_state", payload, lane=lane_row["lane_id"], version=1))["lane"]["work_state"] == to
    run(body)


@pytest.mark.asyncio
async def test_real_graph_fence_pause_versus_admitted_spawn_preserves_delivery(env):
    target = item(env.root, "demo__race")
    ctl, sessions, tmux = await setup(env.root, env.store, env.monkeypatch)
    caller = await seat(env.store, "caller", target, assigned=target)
    from assistant_composite import AssistantComposite
    from test_assistant_prose_mirror import _config
    from test_work_lanes import Env
    root = await env.store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
    composite = AssistantComposite(env.store, config=_config(root["session_generation"]))
    await composite.ensure_projection()
    fd = Env(env.store, composite, root["session_generation"])
    await env.store.update_session(*caller[0].split(":", 1), parent_stream_id=FD)
    lid = (await fd.adopt(key="stream:race", lead=caller, members=[target]))["lane"]["lane_id"]
    entered, release = asyncio.Event(), asyncio.Event()
    original = tmux.new_session
    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    env.monkeypatch.setattr(tmux, "new_session", held)
    task = asyncio.create_task(ctl.spawn(agent(caller, spec=target, name="race-child"), HOST))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(VerbError) as exc:
            await fd.op("set_state", {"to": "paused", "reason": "Checkpoint hold", "next_action": "Resume later"},
                        lane=lid, version=1)
        assert exc.value.code == "work_lane_drain_required"
        assert exc.value.extra["delivery_ids"]
        assert (await env.store.get_work_lane(lid))["lane"]["work_state"] == "active"
    finally:
        release.set()
        result = await asyncio.wait_for(task, 3)
        await composite.stop()
    assert result["type"] == "spawn.ok" and tmux.created == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["paused", "blocked", "done", "orphan"])
async def test_ordinary_send_never_executes_after_hold_or_owner_loss(tmp_path, state):
    from test_send_semantics import FakeTmux, _new_comms, _open, HOST as SEND_HOST, NAME
    tmux = FakeTmux()
    comms, store, sessions = _new_comms(tmux, tmp_path)
    try:
        await _open(comms, sessions)
        await lane(store, "wl-send", "spec_demo__send", state="active" if state == "orphan" else state)
        await store.update_session(SEND_HOST, NAME, assigned_spec_id="spec_demo__send")
        if state == "orphan":
            await store.submit(lambda conn: conn.execute("UPDATE v2_assistant_composite_lanes SET bound_generation='lost'"))
        with pytest.raises(VerbError):
            await comms.send({"stream_id": f"{SEND_HOST}:{NAME}", "text": "Resume the assigned work", "request_id": "held"})
        assert tmux.pastes == []
    finally:
        store.stop()


@pytest.mark.asyncio
async def test_public_cli_provision_bind_activate_then_send(env, tmp_path):
    from assistant_composite import AssistantComposite
    from test_assistant_prose_mirror import _config, ASSISTANT
    from test_work_lanes import Env
    from server import Server
    from comms import Comms
    from store import STREAM_TOKEN_HASH_VERSION
    from test_send_semantics import FakeTmux
    target = item(env.root, "demo__bootstrap")
    env.monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(env.root))
    ctl, sessions, tmux = await setup(env.root, env.store, env.monkeypatch)
    root = await env.store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
    composite = AssistantComposite(env.store, config=_config(root["session_generation"]))
    sessions.assistant.front_desk = composite
    await composite.ensure_projection()
    fd = Env(env.store, composite, root["session_generation"])
    lid = (await fd.adopt(key="stream:bootstrap", state="paused", owner="operator", members=[target]))["lane"]["lane_id"]
    token = "synthetic-bootstrap-token"
    await env.store.grant_stream_token(*FD.split(":", 1), hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
    comms = Comms(env.store, sessions, ctl, attachment_root=tmp_path / "attachments")
    server = Server(port=0, store=env.store, sessions=sessions, comms=comms, spawnctl=ctl, local_host=HOST)
    server.assistant_composite = composite
    try:
        port = await server.bind()
        cli_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "agent-orch"),
                   "LANG": "C.UTF-8", "AGENT_ORCH_WS_URL": f"ws://127.0.0.1:{port}", "AGENT_ORCH_TOKEN": "",
                   "AGENT_ORCH_HOST_ID": HOST, "AGENT_ORCH_STREAM_ID": FD, "AGENT_ORCH_STREAM_TOKEN": token,
                   "AGENT_ORCH_RUNTIME_DIR": str(tmp_path / "cli"), "AGENT_ORCH_MEMORY_REPO": str(env.root)}
        async def cli(*args):
            proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "agent_orch.cli", *args,
                env=cli_env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 10)
            assert proc.returncode == 0, (stdout.decode(), stderr.decode())
            return json.loads(stdout)
        # Provider launch/transport is synthetic; the public CLI/auth/store and
        # lane transitions are real. No external agent/model is launched.
        await cli("assistant", "authority", "--disable", "--request-id", "fixture-authority-off")
        spawned = await cli("spawn", "--provider", "claude", "--model", "sonnet", "--effort", "high",
                            "--host", HOST, "--role", "lead", "--spec-id", target, "--lane-id", lid,
                            "--provision-only", "--objective", "Own fixture lane", "--no-self-close-on-completion")
        sid = spawned.get("stream_id") or spawned["session"]["stream_id"]
        lead = await env.store.fetch_session(*sid.split(":", 1))
        assert (await env.store.get_work_lane(lid))["lane"]["work_state"] == "paused"
        bound = await cli("work-lane", "set-lead", lid, "--lead-stream-id", sid, "--lead-generation", lead["session_generation"],
                          "--expected-version", "1", "--request-id", "bootstrap-bind", "--composite-stream-id", ASSISTANT)
        version = bound["lane"]["version"]
        await cli("work-lane", "set-state", lid, "--to", "active", "--reason", "FD starts scoped work", "--expected-version", str(version),
                  "--request-id", "bootstrap-activate", "--composite-stream-id", ASSISTANT)
        transport = FakeTmux()
        env.monkeypatch.setattr(tmux, "capture", transport.capture)
        env.monkeypatch.setattr(tmux, "paste", transport.paste)
        result = await cli("send", sid, "0", "Implement the assigned fixture work")
        assert result["submission_confirmed"] is True and len(transport.pastes) == 1
    finally:
        await server.close()
        await composite.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["lead", "worker", "nexus"])
async def test_spoofed_provisioning_has_no_pane_or_task(env, role):
    target = item(env.root, "demo__spoof")
    ctl, sessions, tmux = await setup(env.root, env.store, env.monkeypatch)
    await lane(env.store, "wl-paused", target, state="paused")
    caller = await seat(env.store, "spoof-fd-title", target, assigned=target)
    message = agent(caller, spec=target, role=role, provision_only=True, lane_id="wl-paused")
    message.pop("command")
    assert (await refused(ctl, message)).code == "work_lane_provisioning_forbidden"
    assert tmux.created == 0


@pytest.mark.parametrize("operation", ["close", "unassign", "role"])
def test_active_lead_cannot_exit_through_other_mutations(operation):
    async def body(env):
        lead = await env.seat("exit-lead", role="lead", parent_stream_id=FD)
        lid = (await env.adopt(key="stream:exit", lead=lead))["lane"]["lane_id"]
        sessions = Sessions(env.store, tmux=None, local_host="host-a")
        await sessions.refresh()
        with pytest.raises(VerbError) as exc:
            if operation == "close":
                await sessions.close("host-a", "exit-lead")
            elif operation == "role":
                await sessions.set_role("host-a", "exit-lead", "worker", auth_context={"operator_authenticated": True})
            else:
                from uiverbs import UIVerbs
                await UIVerbs(env.store, sessions, None).session_spec_update({"host": "host-a", "session_name": "exit-lead", "action": "unassign"})
        assert exc.value.code == "work_lane_lead_required"
        successor = await env.seat("exit-successor", role="lead", parent_stream_id=FD, visibility="hidden")
        await env.op("set_lead", {"lead": {"stream_id": successor[0], "generation": successor[1]}}, lane=lid, version=1)
        assert (await env.store.get_work_lane(lid))["lane"]["bound_stream_id"] == successor[0]
        await sessions.set_role("host-a", "exit-lead", "worker", auth_context={"operator_authenticated": True})
    run(body)
