"""Remote prompt staging shell quoting, cleanup, and failure receipts."""

from __future__ import annotations

import tmux_transport

import asyncio
import hashlib
import os
from pathlib import Path

import pytest

import spawnctl as spawnctl_mod  # noqa: E402
from sessions import Sessions, VerbError  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from tmux_transport import Tmux
from store import Store  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_shell_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    # SSH markers select Bash's remote startup path instead of the BASH_ENV seam.
    for name in ("BASH_ENV", "SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("startup", ["read -r ignored || true", "read -r -n 4 ignored"])
def test_remote_stage_rejects_consumed_input(tmp_path: Path, monkeypatch, startup):
    hook = tmp_path / "startup.sh"
    hook.write_text(startup + "\n")
    monkeypatch.setenv("BASH_ENV", str(hook))
    monkeypatch.setattr(tmux_transport, "ssh_command", lambda _host, command, **kw: [
        "/bin/bash", "-c", command,
    ])
    destination = tmp_path / "brief.txt"
    destination.write_bytes(b"previous brief")
    with pytest.raises(VerbError, match="remote prompt staging failed"):
        asyncio.run(Tmux(ssh_target="synthetic").stage_text(str(destination), b"remote prompt\n"))
    assert destination.read_bytes() == b"previous brief"
    assert not list(tmp_path.glob("brief.txt.tmp-*"))


@pytest.mark.parametrize("shell", ["/bin/zsh", "/bin/bash"])
def test_remote_stage_accepts_empty_payload(tmp_path: Path, monkeypatch, shell):
    monkeypatch.setattr(tmux_transport, "ssh_command", lambda _host, command, **kw: [
        shell, "-c", command,
    ])
    destination = tmp_path / "empty.txt"
    asyncio.run(Tmux(ssh_target="synthetic").stage_text(str(destination), b""))
    assert destination.read_bytes() == b""


@pytest.mark.parametrize("shell", ["/bin/zsh", "/bin/bash"])
def test_remote_stage_accepts_spaces_and_apostrophes(
    shell: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not Path(shell).exists():
        pytest.fail(f"platform_refusal: required remote shell is missing: {shell}")

    def local_ssh(_target: str, remote_command: str, **_kwargs: object) -> list[str]:
        return [shell, "-c", remote_command]

    monkeypatch.setattr(tmux_transport, "ssh_command", local_ssh)
    path = tmp_path / "remote dir with spaces" / "brief's file.txt"
    data = b"remote prompt\n"

    asyncio.run(Tmux(ssh_target="wsl-test").stage_text(str(path), data))

    assert path.read_bytes() == data
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob(f"{path.name}.tmp-*"))


def test_remote_stage_uses_private_directory_without_chmodding_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "remote-root"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    monkeypatch.setattr(tmux_transport, "PROMPT_STAGE_ROOT", root)

    path, _digest, data = tmux_transport._prompt_stage_path("remote staged prompt " * 30)
    assert path.parent == root / tmux_transport.PROMPT_STAGE_DIR_NAME

    def local_ssh(_target: str, remote_command: str, **_kwargs: object) -> list[str]:
        return ["/bin/bash", "-c", remote_command]

    monkeypatch.setattr(tmux_transport, "ssh_command", local_ssh)
    asyncio.run(Tmux(ssh_target="wsl-test").stage_text(str(path), data))

    assert root.stat().st_mode & 0o777 == 0o755
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("shell", ["/bin/zsh", "/bin/bash"])
def test_remote_stage_failure_removes_temp_file(
    shell: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not Path(shell).exists():
        pytest.fail(f"platform_refusal: required remote shell is missing: {shell}")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_mv = fake_bin / "mv"
    fake_mv.write_text("#!/bin/sh\nexit 23\n")
    fake_mv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    def local_ssh(_target: str, remote_command: str, **_kwargs: object) -> list[str]:
        return [shell, "-c", remote_command]

    monkeypatch.setattr(tmux_transport, "ssh_command", local_ssh)
    path = tmp_path / "remote dir" / "brief.txt"

    with pytest.raises(VerbError) as exc:
        asyncio.run(Tmux(ssh_target="wsl-test").stage_text(str(path), b"brief"))

    assert exc.value.code == "prompt_stage_failed"
    assert not list(path.parent.glob(f"{path.name}.tmp-*"))


class FailingStageTmux:
    ssh_target = "wsl-test"

    def __init__(self) -> None:
        self.live = False

    async def has_session(self, _name: str) -> bool:
        return self.live

    async def stage_text(self, _path: str, _data: bytes) -> None:
        raise VerbError("prompt_stage_failed", "simulated remote mkdir failure")

    async def new_session(self, _name: str, _command: str, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        self.live = True

    async def capture(self, _name: str) -> str:
        return "READY"

    async def paste(self, _name: str, _text: str) -> None:
        raise AssertionError("staging failed before the pointer could be pasted")

    async def pane_pid(self, _name: str) -> str:
        return "1234"

    async def session_state(self, _name: str) -> str:
        return "gone"

    async def kill_session(self, _name: str) -> None:
        self.live = False


def test_staging_failure_persists_structured_receipt_and_await_exposes_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmux_transport, "PROMPT_STAGE_ROOT", tmp_path)
    host = "wsl-test"
    name = "stage-receipt-fails"
    request_id = "r-stage-receipt-fails"
    brief = "x" * 500

    async def run() -> tuple[dict, dict, dict]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = FailingStageTmux()
            ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=host), tmux=tmux)
            with pytest.raises(VerbError) as exc:
                await ctl.spawn(
                    {"objective": "Exercise the existing spawn contract", "command": "stub", "session_name": name, "request_id": request_id, "prompt": brief},
                    host,
                )
            assert exc.value.code == "prompt_stage_failed"
            outcome = await store.get_spawn_outcome(host, name)
            assert outcome is not None
            awaited = await ctl.await_spawn({"stream_id": f"{host}:{name}"})
            return outcome, awaited, await store.reservations(include_expired=True)
        finally:
            store.stop()

    outcome, awaited, reservations = asyncio.run(run())
    receipt = outcome["delivery_receipt"]
    assert outcome["state"] == "failed"
    assert outcome["delivery_evidence"] == "failed"
    assert receipt["transport"] == "staged"
    assert receipt["state"] == "failed"
    assert receipt["delivery_status"] == "failed"
    assert receipt["failure_code"] == "prompt_stage_failed"
    assert receipt["failure_reason"] == "simulated remote mkdir failure"
    assert receipt["stage_host"] == host
    assert receipt["stage_path"].startswith(str(tmp_path))
    assert receipt["prompt_sha256"] == hashlib.sha256(brief.encode()).hexdigest()
    assert receipt["prompt_size_bytes"] == len(brief.encode())
    assert awaited["type"] == "await_spawn.error"
    assert awaited["initial_prompt_delivery"] == receipt
    assert reservations == []
