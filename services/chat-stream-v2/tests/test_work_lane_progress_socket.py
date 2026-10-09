"""Real CLI -> loopback daemon -> disposable file Store, with no running lead."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

from server import Server
from sessions import Sessions
from store import STREAM_TOKEN_HASH_VERSION
from test_work_lanes import FD, run
from test_assistant_prose_mirror import ASSISTANT
from work_lanes_projection import WorkLanesInventory
from _shared.specs_service import SpecsSubsystem


async def cli_call(port, tmp_path, *args):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
           "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "agent-orch"),
           "AGENT_ORCH_WS_URL": f"ws://127.0.0.1:{port}", "AGENT_ORCH_TOKEN": "",
           "AGENT_ORCH_HOST_ID": "fixture-root", "AGENT_ORCH_STREAM_ID": FD,
           "AGENT_ORCH_STREAM_TOKEN": "fixture-lane-token",
           "AGENT_ORCH_RUNTIME_DIR": str(tmp_path / "cli"), "AGENT_ORCH_MEMORY_REPO": str(tmp_path / "memory")}
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "agent_orch.cli", "work-lane", *args,
                                                env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 15)
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    assert proc.returncode == 0, (proc.returncode, stdout.decode(), stderr.decode())
    return json.loads(stdout)


def test_leadless_progress_changes_within_one_automatic_sweep(tmp_path, monkeypatch):
    memory = tmp_path / "memory"
    folder = memory / "work" / "in_progress" / "demo__bridge"
    folder.mkdir(parents=True)
    spec = folder / "spec.md"
    content = "---\nid: spec_demo__bridge\ntitle: Paper bridge\n---\n## Acceptance Criteria\n- [ ] Assemble.\n"
    spec.write_text(content)
    (folder / "summary.md").write_text("**Next action** — Inspect the span.\n")
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(memory))

    async def body(env):
        token_hash = hashlib.sha256(b"fixture-lane-token").hexdigest()
        assert await env.store.grant_stream_token("fixture-root", "visible", token_hash, STREAM_TOKEN_HASH_VERSION) == "ok"
        lane = (await env.adopt(state="paused", lead=False, owner="operator"))["lane"]
        sessions = Sessions(env.store, tmux=None, local_host="fixture-root")
        await sessions.refresh()
        server = Server(port=0, store=env.store, sessions=sessions, local_host="fixture-root")
        server.assistant_composite = env.composite
        specs = SpecsSubsystem(session_summaries=sessions.list_open, changed_callback=lambda ids: None, debounce_s=0)
        inv = WorkLanesInventory(env.store, sessions, server.broadcast, specs=specs, sweep_interval_s=0.2, settle_s=0)
        server.work_lanes = inv
        try:
            port = await server.bind()
            bound = await cli_call(port, tmp_path, "set-members", lane["lane_id"], "--member", "spec_demo__bridge",
                                   "--expected-version", str(lane["version"]), "--request-id", "socket-members",
                                   "--composite-stream-id", ASSISTANT)
            assert bound["lane"]["work_state"] == "paused"
            inv.start()
            await inv._task
            first = await cli_call(port, tmp_path, "show", lane["lane_id"], "--members", "--json")
            assert first["members"][0]["ac_checked"] == 0
            version = first["lane"]["version"]
            spec.write_text(content.replace("[ ]", "[x]"))
            # Only read the resulting projection: no agent turn and no manual refresh.
            async with asyncio.timeout(3):
                while True:
                    await asyncio.sleep(0.05)
                    shown = await cli_call(port, tmp_path, "show", lane["lane_id"], "--members", "--json")
                    if shown["members"][0]["ac_checked"] == 1:
                        break
            assert shown["projection"]["state"] == "paused" and shown["projection"]["lead"] is None
            assert shown["lane"]["version"] == version
            assert shown["members"][0]["next_action_text"] == "Inspect the span."
            assert len([e for e in shown["events"] if e["operation"] == "item_change"]) == 1
            assert shown["updates"] == []
        finally:
            await inv.stop()
            await server.close()
            assert server._ws_server is None
    run(body, str(tmp_path / "fixture.db"))
