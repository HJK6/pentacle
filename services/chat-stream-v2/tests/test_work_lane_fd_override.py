"""Bound FD overrides through a disposable daemon and the real work-lane CLI."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest
from assistant_composite import AssistantComposite
from notification_answer_fixture import fixture
from server import Server
from store import STREAM_TOKEN_HASH_VERSION
from test_assistant_prose_mirror import ASSISTANT, _config_for
from test_work_lanes import FD, run


def test_fd_done_override_real_cli(tmp_path):
    async def go():
        async with fixture(tmp_path, host="fixture") as (notify, queue, comms, provider, sessions, store):
            actor = "fixture:v2-test"
            row = await store.fetch_session("fixture", "v2-test")
            composite = AssistantComposite(store, config=_config_for(actor, row["session_generation"]))
            await composite.ensure_projection()
            token = "override-fixture-token"
            assert await store.grant_stream_token("fixture", "v2-test", hashlib.sha256(token.encode()).hexdigest(),
                                                  STREAM_TOKEN_HASH_VERSION) == "ok"
            server = Server(port=0, store=store, sessions=sessions, comms=comms, local_host="fixture")
            server.assistant_composite = composite
            try:
                port = await server.bind()
                lane = (await composite.operation({"type": "assistant.operation", "request_id": "adopt:override",
                    "composite_stream_id": ASSISTANT, "dispatch_id": "none", "operation": "work_lane.adopt",
                    "payload": {"adoption_key": "stream:override", "title": "Override lane", "owner_kind": "operator",
                                "work_state": "paused", "visible_chat": {"stream_id": ASSISTANT},
                                "no_spec_reason": "Disposable override fixture."},
                    "_auth_context": {"token_verified": True, "stream_id": actor,
                                      "session_generation": row["session_generation"]}}, actor_stream_id=actor))["lane"]
                env = dict(PATH=os.environ.get("PATH", "/usr/bin:/bin"), LANG="C.UTF-8", PYTHONPATH=str(Path(__file__).resolve().parents[2] / "agent-orch"),
                           AGENT_ORCH_WS_URL=f"ws://127.0.0.1:{port}", AGENT_ORCH_TOKEN="",
                           AGENT_ORCH_HOST_ID="fixture", AGENT_ORCH_STREAM_ID=actor, AGENT_ORCH_STREAM_TOKEN=token,
                           AGENT_ORCH_RUNTIME_DIR=str(tmp_path / "cli"), AGENT_ORCH_MEMORY_REPO=str(tmp_path / "memory"))
                async def cli(*args):
                    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "agent_orch.cli", "work-lane", *args,
                        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    try:
                        stdout, stderr = await asyncio.wait_for(proc.communicate(), 15)
                    except BaseException:
                        proc.kill(); await proc.wait(); raise
                    assert stdout, stderr.decode()
                    return proc.returncode, json.loads(stdout)
                args = ["set-state", lane["lane_id"], "--to", "done", "--outcome", "Completed",
                        "--reason", "FD closes completed work", "--expected-version", str(lane["version"]),
                        "--request-id", "override-done", "--composite-stream-id", ASSISTANT]
                rc, result = await cli(*args)
                assert rc == 0, result
                assert result["lane"]["work_state"] == "done"
                assert result["lane"]["work_state_reason"] == "fd"
                assert result["event"]["payload"]["override"] == {
                    "schema": "WorkLaneOverrideV1", "action": "set_state:done", "from": "paused", "to": "done",
                    "reason": "FD closes completed work", "actor_stream_id": actor,
                    "actor_generation": row["session_generation"]}
                rc, replay = await cli(*args)
                assert rc == 0 and replay["duplicate"] is True
                rc, owned = await cli("set-owner", lane["lane_id"], "--to", "fd", "--reason", "FD takes responsibility",
                    "--expected-version", str(result["lane"]["version"]), "--request-id", "override-owner",
                    "--composite-stream-id", ASSISTANT)
                assert rc == 0, owned
                assert owned["event"]["payload"]["override"]["action"] == "set_owner:fd"
                assert owned["event"]["payload"]["override"]["from"] == "operator"
                shown = await store.get_work_lane(lane["lane_id"])
                assert len(shown["events"]) == 3
                assert all(e["consumed_question_id"] is None for e in shown["events"])
            finally:
                await server.close(); await composite.stop()
                assert server._ws_server is None
    asyncio.run(go())


@pytest.mark.parametrize("operation,payload", [("set_state", {"to": "done", "outcome": "Shipped"}),
    ("set_state", {"to": "blocked", "blocker": "Dependency"}), ("set_state", {"to": "active"}),
    ("set_owner", {"to": "fd"})])
def test_override_audit_and_replay(operation, payload):
    async def body(env):
        lead = await env.seat("override-lead", role="lead", parent_stream_id=FD)
        lane = (await env.adopt(owner="operator", state="paused", lead=lead))["lane"]
        payload_with_reason = dict(payload, reason="FD manages this work")
        result = await env.op(operation, payload_with_reason, lane=lane["lane_id"], version=1, request_id="override")
        audit = result["event"]["payload"]["override"]
        assert audit == {"schema": "WorkLaneOverrideV1", "action": operation + ":" + payload["to"],
            "from": "operator" if operation == "set_owner" else "paused", "to": payload["to"],
            "reason": "FD manages this work", "actor_stream_id": FD, "actor_generation": env.gen}
        assert result["event"]["consumed_question_id"] is None
        replay = await env.op(operation, payload_with_reason, lane=lane["lane_id"], version=1, request_id="override")
        assert replay["duplicate"] is True and replay["event"] == result["event"]
        with pytest.raises(ValueError, match="work_lane_idempotency_conflict"):
            await env.op(operation, dict(payload, reason="Different reason"), lane=lane["lane_id"], version=1,
                         request_id="override")
    run(body)


@pytest.mark.parametrize("operation,payload", [("set_state", {"to": "done", "outcome": "Shipped"}),
    ("set_state", {"to": "blocked", "blocker": "Dependency"}), ("set_owner", {"to": "fd"})])
@pytest.mark.parametrize("reason", [None, "", "  "])
def test_override_reason_required(operation, payload, reason):
    async def body(env):
        lane = (await env.adopt(owner="operator", state="paused"))["lane"]
        wire = dict(payload) if reason is None else dict(payload, reason=reason)
        with pytest.raises(ValueError, match="work_lane_override_reason_required"):
            await env.op(operation, wire, lane=lane["lane_id"], version=1)
        assert len((await env.store.get_work_lane(lane["lane_id"]))["events"]) == 1
    run(body)


@pytest.mark.parametrize("unverified", ["non_fd", "stale_generation"])
def test_override_does_not_widen_actor_authority(unverified):
    async def body(env):
        lane = (await env.adopt(owner="operator", state="paused"))["lane"]
        actor, generation = await env.seat("other-lead", role="lead", parent_stream_id=FD) if unverified == "non_fd" else (FD, "stale")
        with pytest.raises(ValueError, match=("work_lane_actor_unverified" if unverified == "non_fd"
                                             else "assistant_actor_generation_unverified")):
            await env.op("set_state", {"to": "done", "outcome": "Shipped", "reason": "FD manages this work"},
                         lane=lane["lane_id"], version=1, actor=actor, gen=generation)
        assert len((await env.store.get_work_lane(lane["lane_id"]))["events"]) == 1
    run(body)


def test_override_store_refuses_stale_binding_and_rolls_back_audit():
    async def body(env):
        lane = (await env.adopt(owner="operator", state="paused"))["lane"]
        payload = {"to": "done", "outcome": "Shipped", "reason": "FD closes work"}
        with pytest.raises(ValueError, match="work_lane_actor_unverified"):
            await env.store.apply_work_lane_operation(stream_id=ASSISTANT, request_id="stale", operation="set_state",
                lane_id=lane["lane_id"], expected_lane_version=1, payload=payload, actor_stream_id=FD,
                actor_generation="stale", binding_name=env.composite.config.name,
                env_binding=env.composite._env_binding())
        def fail(_operation):
            raise RuntimeError("fixture fault")
        env.store._work_lane_fault = fail
        try:
            with pytest.raises(RuntimeError, match="fixture fault"):
                await env.op("set_state", payload, lane=lane["lane_id"], version=1, request_id="override-fault")
        finally:
            env.store._work_lane_fault = None
        shown = await env.store.get_work_lane(lane["lane_id"])
        assert shown["lane"]["work_state"] == "paused" and shown["lane"]["version"] == 1
        assert len(shown["events"]) == 1
        result = await env.op("set_state", payload, lane=lane["lane_id"], version=1, request_id="override-fault")
        assert result["event"]["payload"]["override"]["reason"] == "FD closes work"
    run(body)


def test_fd_owned_explicit_confirmation_keeps_existing_state_reason():
    async def body(env):
        lane = (await env.adopt(owner="fd"))["lane"]
        confirmation = env.confirm("q-fd-owned", lane["lane_id"], "set_state:done")
        result = await env.op("set_state", {"to": "done", "outcome": "Shipped",
            "operator_confirmation": confirmation}, lane=lane["lane_id"], version=1)
        assert result["lane"]["work_state_reason"] == "fd"
        assert result["event"]["consumed_question_id"] == "q-fd-owned"
        assert "override" not in result["event"]["payload"]
    run(body)
