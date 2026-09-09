"""SpawnCtl distinguishes hostc's auth-context failure from a slow Claude boot.

The production-adjacent shell shim is covered by the durable marker test below.
These tests retain the legacy capture-injection case as a strict expected failure:
it proves only a parser and must never be mistaken for transport evidence. The
private-tmux witness remains red until the lane-5 durable emitter exists.
"""

from __future__ import annotations

import os as _os, pytest as _pytest  # env-gate: needs a live provider CLI
pytestmark = _pytest.mark.skipif(not _os.environ.get("PENTACLE_LIVE_TESTS"), reason="set PENTACLE_LIVE_TESTS=1")

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import uuid
from pathlib import Path

import pytest

import launch  # noqa: E402
import spawnctl as spawnctl_mod  # noqa: E402
from hosts import Hosts  # noqa: E402
from server import Server  # noqa: E402
from sessions import Sessions, VerbError  # noqa: E402
from spawnctl import (  # noqa: E402
    AUTH_CONTEXT_FAILURE_MARKER,
    AUTH_CONTEXT_MARKER_BYTES,
    AUTH_CONTEXT_MARKER_DIR,
    SpawnCtl,
    )
from tmux_transport import Tmux  # noqa: E402
from store import Store  # noqa: E402

HOST = "localhost"
AUTH_MARKER = "provider_auth_context_unavailable: ssm_fetch_failed"
SHIM = Path(__file__).resolve().parents[1] / "tools" / "hostc_claude_auth_context.sh"


class ClaudeBootTmux:
    """A non-live Claude boot outcome represented entirely in memory."""

    def __init__(self, capture_text: str) -> None:
        self.capture_text = capture_text
        self.alive = False
        self.new_sessions = 0
        self.pastes = 0

    async def new_session(self, name: str, command: str, cwd=None, env=None) -> None:
        self.alive = True
        self.new_sessions += 1

    async def has_session(self, name: str) -> bool:
        return self.alive

    async def session_state(self, name: str) -> str:
        return "alive" if self.alive else "gone"

    async def capture(self, name: str) -> str:
        return self.capture_text

    async def pane_pid(self, name: str) -> str:
        return ""

    async def kill_session(self, name: str) -> None:
        self.alive = False

    async def paste(self, name: str, text: str) -> None:
        self.pastes += 1

    async def run(self, *args: str, **kwargs: object) -> tuple[int, str]:
        return 0, ""


def _claude_resolve(msg: dict, host: str, name: str):
    async def _inner():
        return "claude --tui", {"resolved_launch_tuple": {"provider": "claude"}}, {}

    return _inner()


def test_spawnctl_marker_is_byte_contract_with_lane5_shim() -> None:
    """The two lanes cannot silently drift on the parseable, non-secret marker."""
    code = AUTH_CONTEXT_FAILURE_MARKER.removesuffix(":")
    source = SHIM.read_text(encoding="utf-8")
    assert f'AUTH_CONTEXT_CODE="{code}"' in source
    assert "printf '%s: %s\\n' \"$AUTH_CONTEXT_CODE\" \"$1\"" in source


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required for the PTY transport contract")
def test_private_tmux_real_shim_reaches_spawnctl_durable_recognition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The former red witness turns green through the real durable path.

    Only AWS is a failure double.  The real shim, normal launch command,
    private tmux server, atomic artifact, actual ``Hosts.run_command`` local
    transport seam, and SpawnCtl outcome normalization all execute.  The test
    uses a local private hostc topology solely to avoid touching the live
    hostc host; remote argv/timeout placement is covered by the contract tier.
    """
    real_tmux = shutil.which("tmux")
    assert real_tmux is not None
    bin_dir = tmp_path / "bin"
    home = tmp_path / "home"
    bin_dir.mkdir()
    home.mkdir()
    aws = bin_dir / "aws"
    aws.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    aws.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    socket = f"authctx{uuid.uuid4().hex[:10]}"
    wrapper = tmp_path / "tmux-private"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(real_tmux)} -L {shlex.quote(socket)} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    name = f"auth-context-durable-{uuid.uuid4().hex[:10]}"
    digest = hashlib.sha256(f"hostc:{name}".encode("utf-8")).hexdigest()
    marker = Path(AUTH_CONTEXT_MARKER_DIR) / f"{digest}.code"

    async def _resolve(_msg: dict, _host: str, _name: str):
        machine = launch.LocalMachine(
            name="hostc",
            cwd=str(tmp_path),
            codex_cwd=str(tmp_path),
            claude_bin=str(SHIM),
            codex_bin="codex",
            projects_root=str(tmp_path / "projects"),
        )
        command = launch.build_launch(
            machine, provider="claude", tmux_session=name,
            launch_model=None, launch_effort=None,
        ).command
        return command, {"resolved_launch_tuple": {"provider": "claude"}}, {}

    async def _go() -> tuple[dict, dict | None, str]:
        tmux = Tmux(str(wrapper))
        hosts = Hosts(local_host="hostc", tmux_bin=str(wrapper))
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=tmux, local_host="hostc")
            ctl = SpawnCtl(store, sessions, tmux=tmux, hosts=hosts)
            ctl._resolve_launch = _resolve
            server = Server(store=store, sessions=sessions, spawnctl=ctl, local_host="hostc")
            (reply,) = await server._dispatch(json.dumps({"objective": "Exercise the existing spawn contract",
                "type": "spawn",
                "host": "hostc",
                "provider": "claude",
                "session_name": name,
                "request_id": f"auth-context-{name}",
                "initial_prompt": "must not paste",
            }))
            outcome = await store.get_spawn_outcome("hostc", name)
            return reply, outcome, await tmux.capture(name)
        finally:
            store.stop()
            await tmux.run("kill-server")
            (Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}" / socket).unlink(missing_ok=True)

    try:
        reply, outcome, captured = asyncio.run(_go())
        assert captured == "", "the old stderr/capture transport remains unavailable"
        assert reply["type"] == "spawn.error"
        assert reply["error_code"] == AUTH_CONTEXT_FAILURE_MARKER.removesuffix(":")
        assert reply["initial_prompt_delivery"]["failure_code"] == reply["error_code"]
        assert outcome is not None and outcome["reason"].startswith(f"{reply['error_code']}:")
        assert outcome["delivery_receipt"]["failure_code"] == reply["error_code"]
        assert marker.read_bytes() == AUTH_CONTEXT_MARKER_BYTES
        assert len(marker.read_bytes()) == 34
        assert marker.stat().st_mode & 0o777 == 0o600
        assert marker.parent.stat().st_mode & 0o777 == 0o700
    finally:
        marker.unlink(missing_ok=True)
        try:
            marker.parent.rmdir()
        except OSError:
            pass


@pytest.mark.xfail(
    strict=True,
    reason="injected capture text cannot prove the auth-marker transport",
)
def test_auth_context_marker_is_public_error_receipt_and_durable_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Historic parser-only expectation; it must remain red without transport."""

    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(spawnctl_mod, "BOOT_READY_HARD_DEADLINE_S", 0.01)
    monkeypatch.setattr(spawnctl_mod.asyncio, "sleep", _no_sleep)

    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = ClaudeBootTmux(AUTH_MARKER)
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            ctl._resolve_launch = _claude_resolve
            server = Server(store=store, sessions=sessions, spawnctl=ctl, local_host=HOST)
            (reply,) = await server._dispatch(json.dumps({"objective": "Exercise the existing spawn contract",
                "type": "spawn", "host": HOST, "session_name": "auth-context",
                "request_id": "auth-context-request", "initial_prompt": "do not paste",
            }))
            outcome = await store.get_spawn_outcome(HOST, "auth-context")
            return reply, outcome, tmux
        finally:
            store.stop()

    reply, outcome, tmux = asyncio.run(_go())
    assert reply["type"] == "spawn.error"
    assert reply["error_code"] == "provider_auth_context_unavailable"
    receipt = reply["initial_prompt_delivery"]
    assert receipt["failure_code"] == "provider_auth_context_unavailable"
    assert outcome is not None and outcome["state"] == "failed"
    assert outcome["reason"].startswith("provider_auth_context_unavailable:")
    assert outcome["delivery_receipt"]["failure_code"] == "provider_auth_context_unavailable"
    assert tmux.new_sessions == 1, "auth-context failure must not enter the boot retry set"
    assert tmux.pastes == 0


def test_real_readiness_timeout_remains_boot_not_ready_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A marker-free stalled Claude boot keeps the existing retry vocabulary."""

    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(spawnctl_mod, "BOOT_READY_HARD_DEADLINE_S", 0.01)
    monkeypatch.setattr(spawnctl_mod.asyncio, "sleep", _no_sleep)

    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = ClaudeBootTmux("Loading Claude…")
            ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=HOST), tmux=tmux)
            ctl._resolve_launch = _claude_resolve
            with pytest.raises(VerbError) as exc:
                await ctl.spawn({"objective": "Exercise the existing spawn contract",
                    "host": HOST, "session_name": "actual-boot-timeout",
                    "request_id": "boot-timeout-request", "initial_prompt": "not delivered",
                }, HOST)
            outcome = await store.get_spawn_outcome(HOST, "actual-boot-timeout")
            return exc.value, outcome, tmux
        finally:
            store.stop()

    error, outcome, tmux = asyncio.run(_go())
    assert error.code == "boot_not_ready"
    assert outcome is not None and outcome["reason"].startswith("boot_not_ready:")
    assert outcome["delivery_receipt"]["failure_code"] == "boot_not_ready"
    assert tmux.new_sessions == spawnctl_mod.PROMPT_SPAWN_RETRY_ATTEMPTS
    assert tmux.pastes == 0
