"""Owned tmux/process composition using production paste, capture and ingest."""

import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import uuid

from ingest import Ingest, IngestConfig
from prockill import process_record
from tmux_transport import Tmux


class OwnedProvider(Tmux):
    def __init__(self, root):
        binary = shutil.which("tmux")
        if not binary:
            raise RuntimeError("owned provider requires tmux")
        super().__init__(binary)
        self.root = Path(root)
        self.socket = "pentacle-error-alerts-" + uuid.uuid4().hex
        self.name = "v2-test"
        self.native_id = str(uuid.uuid4())
        self.pid = None
        self.started = False

    def _argv(self, args):
        return [self.bin, "-L", self.socket, "-f", "/dev/null", *args]

    @property
    def pastes(self):
        path = self.root / "provider-input.jsonl"
        return (
            [json.loads(line)["text"] for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    async def start(self, store, sessions):
        actor = Path(__file__).with_name("error_alerts_provider_process.py").resolve()
        command = "exec " + shlex.join(
            [
                sys.executable,
                str(actor),
                "--root",
                str(self.root),
                "--native-id",
                self.native_id,
            ]
        )
        rc, _ = await self.run(
            "new-session", "-d", "-s", self.name, "-x", "180", "-y", "40", command
        )
        if rc:
            raise RuntimeError("owned provider launch failed")
        self.started = True
        for _ in range(50):
            self.pid = await self.pane_pid(self.name)
            record = await process_record(self.pid)
            if record and "OpenAI Codex" in await self.capture(self.name):
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("owned provider readiness failed")
        # macOS framework Python execs its Python.app binary. Bind the actual
        # kernel executable, after checking both the interpreter and actor argv.
        parent = await process_record(str(os.getpid()))
        actual = shlex.split(record["command"])
        interpreter = shlex.split(parent["command"])[0] if parent else ""
        expected_args = [
            str(actor),
            "--root",
            str(self.root),
            "--native-id",
            self.native_id,
        ]
        if (
            not actual
            or actual[1:] != expected_args
            or not os.path.isabs(actual[0])
            or os.path.realpath(actual[0]) != os.path.realpath(interpreter)
        ):
            raise RuntimeError("owned provider executable/actor identity mismatch")
        self.identity = {
            "record": record,
            "executable": actual[0],
            "launcher": sys.executable,
            "actor_argv": actual[1:],
        }
        request = "fixture-launch-" + uuid.uuid4().hex
        nonce = uuid.uuid4().hex
        assert await store.reserve_stream_id(
            "fixture", self.name, ttl_s=60, request_id=request, nonce=nonce
        )
        assert await store.record_spawn_intent(
            "fixture",
            self.name,
            {"open_fields": {"session_generation": self.native_id}},
            request_id=request,
            nonce=nonce,
        )
        assert await store.commit_tmux_created_fenced(
            "fixture",
            self.name,
            request_id=request,
            nonce=nonce,
            pane_pid=self.pid,
            pane_started_at=record["start_id"],
        )
        origin = await sessions.open(
            "fixture",
            self.name,
            fence=request,
            provider="codex",
            visibility="visible",
            pane_pid=self.pid,
            session_generation=self.native_id,
            observer_binding={"executable": actual[0]},
        )
        assert await store.release_stream_id_fenced("fixture", self.name, request)
        return origin

    def ingest(self, store, sessions, broadcast):
        return Ingest(
            store,
            sessions,
            self,
            broadcast,
            local_host="fixture",
            recent_limit=500,
            config=IngestConfig(first_delay_s=0),
        )

    async def stop(self):
        if not self.started:
            return
        await self.run("kill-server")
        self.started = False
        for _ in range(30):
            if not self.pid or await process_record(self.pid) is None:
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("owned provider did not exit")
        rc, _ = await self.run("list-sessions")
        if rc == 0:
            raise RuntimeError("owned tmux server remained live")
