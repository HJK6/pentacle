from __future__ import annotations

import asyncio

import pytest

import launch
import main as daemon_main
from spawnctl import SpawnCtl


class _StopAfterSpawnCtl(Exception):
    pass


class _SessionsStub:
    local_host = "hosta"


def _machine() -> launch.LocalMachine:
    return launch.local_machine("hosta", codex_bin="/bin/echo")


def test_main_does_not_require_a_codex_manifest(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def capture_spawnctl(*_args, **kwargs):
        captured.update(kwargs)
        raise _StopAfterSpawnCtl

    monkeypatch.setattr(daemon_main, "load_machines", lambda _env: ())
    monkeypatch.setattr(daemon_main, "_spec_catalog", lambda _sessions: None)
    monkeypatch.setattr(daemon_main, "SpawnCtl", capture_spawnctl)
    args = daemon_main.parse_args([
        "--db", str(tmp_path / "sessions.db"),
        "--blob-root", str(tmp_path / "blobs"),
        "--local-host", "hosta",
        "--codex-bin", "/bin/echo",
    ])

    with pytest.raises(_StopAfterSpawnCtl):
        asyncio.run(daemon_main.run(args))

    # The ceremony is retired outright, so the admission knobs must be gone
    # from the construction rather than merely passed as False/None.
    assert "require_codex_release_manifest" not in captured
    assert "release_manifest" not in captured
    assert not hasattr(args, "codex_release_manifest")


def test_codex_spawn_uses_the_machines_launcher_without_a_manifest() -> None:
    ctl = SpawnCtl(None, _SessionsStub(), tmux=object(), machine=_machine())

    command, _resolution, overrides = asyncio.run(
        ctl._resolve_launch({"provider": "codex"}, "hosta", "manifest-retirement")
    )

    assert "exec /bin/echo" in command
    assert "codex_release_attestation" not in overrides
