"""The existing CLI must receive a loud refusal without losing its real pane."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

from server import Server
from sessions import Sessions
from store import Store, STREAM_TOKEN_HASH_VERSION
from tmux_transport import Tmux


def test_close_progeny_cli_exits_nonzero_and_preserves_pane(tmp_path, isolated_tmux_env):
    async def run():
        store = Store(str(tmp_path / "sessions.db"))
        store.start()
        tmux = Tmux(isolated_tmux_env)
        sessions = Sessions(store, tmux=tmux, local_host="fixture")
        server = Server(host="127.0.0.1", port=0, store=store, sessions=sessions, local_host="fixture")
        command = None
        try:
            await tmux.new_session("retiring", "exec sleep 600")
            await sessions.open("fixture", "retiring")
            token = "disposable-progeny-test-token"
            await store.grant_stream_token("fixture", "retiring", hashlib.sha256(token.encode()).hexdigest(),
                                           STREAM_TOKEN_HASH_VERSION)
            port = await server.bind()
            root = Path(__file__).resolve().parents[3]
            env = {
                "PATH": os.environ["PATH"],
                "PYTHONPATH": str(root / "services/agent-orch") + os.pathsep + str(root / "services"),
                "AGENT_ORCH_WS_URL": f"ws://127.0.0.1:{port}",
                "AGENT_ORCH_HOST_ID": "fixture",
                "AGENT_ORCH_RUNTIME_DIR": str(tmp_path / "runtime"),
                "AGENT_ORCH_STREAM_ID": "fixture:retiring",
                "AGENT_ORCH_STREAM_TOKEN": token,
                "AGENT_ORCH_TOKEN": "",
            }
            command = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "agent_orch.cli", "close", "--operator-confirm",
                "--progeny", "other-host:successor", "fixture:retiring",
                env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(command.communicate(), timeout=10)
            assert command.returncode == 1, (out, err)
            response = json.loads(out)
            assert response["type"] == "close.error"
            assert response["error_code"] == "close_progeny_unsupported"
            assert "spawn --handoff" in response["error"]
            assert (await store.fetch_session("fixture", "retiring"))["status"] == "open"
            assert await store.latest_close_audit("fixture:retiring") is None
            assert await tmux.has_session("retiring")
        finally:
            if command is not None and command.returncode is None:
                command.kill()
                await command.communicate()
            await server.close()
            await tmux.run("kill-server")
            rc, _ = await tmux.run("list-sessions")
            assert rc != 0
            store.stop()
    asyncio.run(run())
