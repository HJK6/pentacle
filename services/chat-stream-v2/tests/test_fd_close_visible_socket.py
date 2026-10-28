"""FD close through the public CLI, scratch daemon and owned real panes."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from server import Server
from sessions import Sessions
from store import Store, STREAM_TOKEN_HASH_VERSION
from tmux_transport import Tmux

HOST = "fixture-fd"
FD = HOST + ":front-desk"
REASON = "Operator fixture-directive: close absorbed disposable kickoff; custody with FD"


@pytest.mark.parametrize("protected", [None, "lead", "chat"], ids=["close", "operator-lead", "operator-chat"])
def test_fd_closes_visible_seat_without_lifecycle_grant(tmp_path, isolated_tmux_env, protected):
    async def run():
        store = Store(str(tmp_path / "sessions.db"))
        store.start()
        tmux = Tmux()
        sessions = Sessions(store, tmux=tmux, local_host=HOST)
        server = Server(host="127.0.0.1", port=0, store=store, sessions=sessions, local_host=HOST)
        composite = None
        try:
            for name in ("front-desk", "target"):
                await tmux.new_session(name, "echo ready; exec sleep 600")
                await sessions.open(HOST, name, provider="codex", pane_status="pane_alive", bootstrap_state="ready")
            fd = await store.fetch_session(HOST, "front-desk")
            target = await store.fetch_session(HOST, "target")
            composite = AssistantComposite(store, config=AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": HOST + ":assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": FD,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": fd["session_generation"],
            }))
            server.assistant_composite = composite
            token = "synthetic-fd-token"
            await store.grant_stream_token(HOST, "front-desk", hashlib.sha256(token.encode()).hexdigest(),
                                           STREAM_TOKEN_HASH_VERSION)
            grant = await store.lifecycle_authority_current()
            assert grant["stream_id"] is None
            port = await server.bind()
            saved_lane = None
            if protected:
                await composite.ensure_projection()
                payload = {"adoption_key": "stream:target", "title": "Disposable operator lane",
                    "summary": "Synthetic protected close fixture", "owner_kind": "operator",
                    "work_state": "paused", "no_spec_reason": "Isolated acceptance",
                    "visible_chat": {"stream_id": HOST + ":target", "generation": target["session_generation"]}}
                if protected == "lead":
                    await sessions.set_role(HOST, "target", "lead", auth_context={"operator_authenticated": True})
                    payload["lead"] = {"stream_id": HOST + ":target", "generation": target["session_generation"]}
                    payload["visible_chat"] = {"stream_id": HOST + ":assistant"}
                adopted = await composite.operation({"type": "assistant.operation", "request_id": "adopt-target",
                    "composite_stream_id": HOST + ":assistant", "dispatch_id": "none", "operation": "work_lane.adopt",
                    "payload": payload, "_auth_context": {"token_verified": True, "stream_id": FD,
                        "session_generation": fd["session_generation"]}}, actor_stream_id=FD)
                saved_lane = adopted["lane"]
            env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8",
                   "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "agent-orch"),
                   "AGENT_ORCH_WS_URL": f"ws://127.0.0.1:{port}", "AGENT_ORCH_TOKEN": "",
                   "AGENT_ORCH_HOST_ID": HOST, "AGENT_ORCH_STREAM_ID": FD,
                   "AGENT_ORCH_STREAM_TOKEN": token, "AGENT_ORCH_RUNTIME_DIR": str(tmp_path / "cli"),
                   "AGENT_ORCH_MEMORY_REPO": str(tmp_path / "memory")}
            async def close():
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "agent_orch.cli", "close", HOST + ":target", "--reason", REASON,
                    "--expected-generation", target["session_generation"], env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), 20)
                except BaseException:
                    proc.kill()
                    await proc.wait()
                    raise
                assert stdout, stderr.decode()
                return proc.returncode, json.loads(stdout)
            code, reply = await close()
            row = await store.fetch_session(HOST, "target")
            alive = await tmux.has_session("target")
            if protected:
                assert code == 1 and reply["error_code"] == "close_operator_lane_protected", reply
                assert row["status"] == "open" and alive
                assert (await store.get_work_lane(saved_lane["lane_id"]))["lane"] == saved_lane
                assert await store.latest_close_audit(HOST + ":target") is None
                rows = await store.lifecycle_authority_audit_rows(limit=20)
                assert any(r["action"] == "fd_close" and r["result"] == "refused"
                           and r["refusal_code"] == "close_operator_lane_protected" for r in rows)
                # Only the independently authenticated fixture operator cleans
                # this protected target up, through the supported close ladder.
                cleaned = await server._on_close({"stream_id": HOST + ":target", "reason": "Dispose owned fixture",
                    "request_id": "cleanup-target", "expected_generation": target["session_generation"],
                    "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:fixture"}})
                assert cleaned["type"] == "close.ok" and not await tmux.has_session("target")
                assert await store.lifecycle_authority_current() == grant
                return
            # Keep refusal/death observations in RED output as well as GREEN.
            assert code == 0, {"reply": reply, "target_status": row["status"], "pane_alive": alive}
            assert reply["type"] == "close.ok" and row["status"] == "closed" and not alive
            rows = [r for r in await store.lifecycle_authority_audit_rows(limit=20)
                    if r["action"] == "fd_close"]
            assert {r["result"] for r in rows} == {"admitted", "applied"}
            assert all(r["actor_kind"] == "front_desk" and r["actor_identity"] == FD
                       and r["actor_generation"] == fd["session_generation"]
                       and r["target_generation"] == target["session_generation"]
                       and r["reason"] == REASON and r["request_id"] for r in rows)
            audit = await store.latest_close_audit(HOST + ":target")
            assert audit["actor_kind"] == "front_desk" and audit["closed_by"] == FD
            assert await store.lifecycle_authority_current() == grant
            code, repeat = await close()
            assert code == 0 and repeat["type"] == "close.already_closed"
        finally:
            await server.close()
            if composite is not None:
                await composite.stop()
            await tmux.run("kill-server")
            rc, _ = await tmux.run("list-sessions")
            assert rc != 0, "owned tmux socket survived cleanup"
            store.stop()
    asyncio.run(run())
