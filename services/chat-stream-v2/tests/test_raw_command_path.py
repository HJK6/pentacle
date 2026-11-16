"""Raw-command spawns retain the target host's agent-orch PATH contract."""

from __future__ import annotations

import asyncio
import shlex
import subprocess
from pathlib import Path

import launch
import pytest
from machines import MachineConfig
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store


RAW_COMMAND = "printf raw-command"


LOCAL = launch.local_machine(
    "localhost",
    cwd="/local/cwd",
    claude_bin="/local/bin/claude",
    projects_root="/local/projects",
    agent_orch_bin_dir="/local/.local/bin",
)

PEER = MachineConfig(
    name="loopback-peer",
    ssh_target="localhost",
    tmux_bin="/usr/bin/tmux",
    claude_bin="/remote/.local/bin/claude",
    codex_bin="/remote/.npm-global/bin/codex",
    projects_root="/remote/.claude/projects",
    cwd="/remote/agent-workspace",
    agent_orch_bin_dir="/remote/.local/bin",
)


class _Hosts:
    peers = {PEER.name: PEER}


def _resolve(msg: dict, host: str, *, default_command: str = "") -> str:
    ctl = SpawnCtl(
        store=None, sessions=None, tmux=object(), machine=LOCAL,
        hosts=_Hosts(), default_command=default_command,
    )
    return asyncio.run(ctl._resolve_launch(msg, host, "raw-1"))[0]


@pytest.mark.parametrize(
    ("host", "expected_dir"),
    [("localhost", "/local/.local/bin"), ("loopback-peer", "/remote/.local/bin")],
)
@pytest.mark.parametrize("use_default", [False, True])
def test_raw_command_launches_get_target_host_path_envelope(
    host: str, expected_dir: str, use_default: bool,
) -> None:
    command = _resolve(
        {} if use_default else {"command": RAW_COMMAND},
        host,
        default_command=RAW_COMMAND if use_default else "",
    )

    assert command.startswith(
        f"export PATH={shlex.quote(expected_dir)}:$PATH && "
    )
    assert f"export PATH={shlex.quote(expected_dir)}:$PATH" in command
    assert command.endswith(RAW_COMMAND)


def test_path_envelope_prioritizes_configured_agent_orch_and_preserves_path(
    tmp_path: Path,
) -> None:
    original_dir = tmp_path / "original-bin"
    configured_dir = tmp_path / "configured-bin"
    provider_dir = tmp_path / "provider-bin"
    for directory in (original_dir, configured_dir, provider_dir):
        directory.mkdir()

    original_agent = original_dir / "agent-orch"
    original_agent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    original_agent.chmod(0o755)
    configured_agent = configured_dir / "agent-orch"
    configured_agent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    configured_agent.chmod(0o755)
    provider_agent = provider_dir / "agent-orch"
    provider_agent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    provider_agent.chmod(0o755)
    capture = tmp_path / "capture.txt"
    for provider_name in ("claude", "codex"):
        provider = provider_dir / provider_name
        provider.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$(command -v agent-orch)\" \"$PATH\" > {shlex.quote(str(capture))}\n",
            encoding="utf-8",
        )
        provider.chmod(0o755)

    machine = launch.local_machine(
        "precedence",
        cwd=str(tmp_path),
        claude_bin=str(provider_dir / "claude"),
        codex_bin=str(provider_dir / "codex"),
        projects_root=str(tmp_path / "projects"),
        agent_orch_bin_dir=str(configured_dir),
    )
    original_path = f"{original_dir}:/usr/bin:/bin"
    for provider in ("claude", "codex"):
        plan = launch.build_launch(
            machine,
            provider=provider,
            tmux_session=f"precedence-{provider}",
            launch_model=None,
            launch_effort=None,
        )
        completed = subprocess.run(
            ["/bin/sh", "-c", plan.command],
            cwd=str(tmp_path),
            env={"PATH": original_path, "PENTACLE_CODEX_ENABLE_APPS": "1"},
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr
        expected_path = (
            f"{configured_dir}:{original_path}"
            if provider == "claude"
            else f"{configured_dir}:{original_path}:{provider_dir}"
        )
        assert capture.read_text(encoding="utf-8").splitlines() == [
            str(configured_agent), expected_path,
        ]


def test_raw_local_path_fallback_uses_machine_provider_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw local spawn still finds agent-orch with the daemon PATH stripped."""
    provider_dir = tmp_path / "provider-bin"
    provider_dir.mkdir()
    provider = provider_dir / "claude"
    provider.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    provider.chmod(0o755)
    agent_orch = provider_dir / "agent-orch"
    agent_orch.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    agent_orch.chmod(0o755)
    monkeypatch.delenv("AGENT_ORCH_BIN", raising=False)
    monkeypatch.delenv("AGENT_ORCH_BIN_DIR", raising=False)
    monkeypatch.setattr(launch.shutil, "which", lambda _name: None)

    machine = launch.local_machine(
        "raw-local",
        cwd=str(tmp_path),
        claude_bin=str(provider),
        projects_root=str(tmp_path / "projects"),
    )
    ctl = SpawnCtl(
        store=None, sessions=None, tmux=object(), machine=machine,
        hosts=_Hosts(),
    )
    command = asyncio.run(ctl._resolve_launch(
        {"command": "command -v agent-orch"}, "localhost", "raw-local-1",
    ))[0]

    completed = subprocess.run(
        ["/bin/sh", "-c", command],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == str(agent_orch)


def test_codex_configured_agent_orch_dir_beats_codex_sibling_dir(
    tmp_path: Path,
) -> None:
    """Appending Codex's sibling path must not shadow configured agent-orch."""
    configured_dir = tmp_path / "configured-bin"
    sibling_dir = tmp_path / "codex-bin"
    configured_dir.mkdir()
    sibling_dir.mkdir()
    configured_agent = configured_dir / "agent-orch"
    sibling_agent = sibling_dir / "agent-orch"
    for agent in (configured_agent, sibling_agent):
        agent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        agent.chmod(0o755)
    capture = tmp_path / "codex-capture.txt"
    codex = sibling_dir / "codex"
    codex.write_text(
        "#!/bin/sh\n"
        f"command -v agent-orch > {shlex.quote(str(capture))}\n",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    machine = launch.local_machine(
        "codex-order",
        cwd=str(tmp_path),
        codex_bin=str(codex),
        projects_root=str(tmp_path / "projects"),
        agent_orch_bin_dir=str(configured_dir),
    )
    plan = launch.build_launch(
        machine,
        provider="codex",
        tmux_session="codex-order",
        launch_model=None,
        launch_effort=None,
    )
    completed = subprocess.run(
        ["/bin/sh", "-c", plan.command],
        env={"PATH": "/usr/bin:/bin", "PENTACLE_CODEX_ENABLE_APPS": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert capture.read_text(encoding="utf-8").strip() == str(configured_agent)


class _ExecutingTmux:
    """Minimal tmux seam that executes new-session through a stripped PATH."""

    def __init__(self, *, env: dict[str, str], cwd: str) -> None:
        self.env = env
        self.cwd = cwd
        self.alive = False
        self.output = ""

    async def new_session(self, name: str, command: str, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        completed = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=cwd or self.cwd,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.output = completed.stdout + completed.stderr
        self.alive = True
        if completed.returncode:
            raise RuntimeError(self.output)

    async def has_session(self, name: str) -> bool:
        return self.alive

    async def session_state(self, name: str) -> str:
        return "alive" if self.alive else "gone"

    async def capture(self, name: str) -> str:
        return self.output

    async def pane_pid(self, name: str) -> str:
        return "1234"

    async def paste(self, name: str, text: str) -> None:
        return None

    async def kill_session(self, name: str) -> None:
        self.alive = False

    async def run(self, *args: str, **kwargs) -> tuple[int, str]:
        return 0, ""


@pytest.mark.parametrize("use_default", [False, True])
def test_spawnctl_raw_command_executes_with_target_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_default: bool,
) -> None:
    """Explicit and default raw commands are tested through SpawnCtl + tmux."""
    provider_dir = tmp_path / "provider-bin"
    provider_dir.mkdir()
    provider = provider_dir / "claude"
    provider.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    provider.chmod(0o755)
    agent_orch = provider_dir / "agent-orch"
    agent_orch.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    agent_orch.chmod(0o755)
    capture = tmp_path / "spawnctl-capture.txt"
    raw = f"command -v agent-orch > {shlex.quote(str(capture))}; printf READY"
    monkeypatch.delenv("AGENT_ORCH_BIN", raising=False)
    monkeypatch.delenv("AGENT_ORCH_BIN_DIR", raising=False)
    monkeypatch.setattr(launch.shutil, "which", lambda _name: None)
    machine = launch.local_machine(
        "spawnctl-local",
        cwd=str(tmp_path),
        claude_bin=str(provider),
        projects_root=str(tmp_path / "projects"),
    )
    tmux = _ExecutingTmux(env={"PATH": "/usr/bin:/bin"}, cwd=str(tmp_path))
    store = Store(":memory:")
    store.start()
    try:
        ctl = SpawnCtl(
            store,
            Sessions(store, tmux=tmux, local_host="localhost"),
            tmux=tmux,
            machine=machine,
            default_command=raw if use_default else "",
        )
        msg = {"objective": "Exercise the target command path", "session_name": f"raw-{use_default}"}
        if not use_default:
            msg["command"] = raw
        reply = asyncio.run(ctl.spawn(msg, "localhost"))
        assert reply["ok"] is True
        assert capture.read_text(encoding="utf-8").strip() == str(agent_orch)
    finally:
        store.stop()
