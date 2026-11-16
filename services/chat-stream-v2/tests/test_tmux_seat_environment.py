"""The daemon's assistant configuration must not reach the tmux server it starts.

tmux takes its global environment from the first client that starts the
server, and every pane inherits it. When the daemon is that client (as after a
host reboot), its PENTACLE_ASSISTANT_* settings would land in every seat shell
and configure the test daemons that gates build there.
"""
from __future__ import annotations

import asyncio

import tmux_transport
from tmux_transport import Tmux


class _Proc:
    returncode = 0

    async def communicate(self, _stdin=None):
        return b"", None


def _run_and_capture(monkeypatch, tmux: Tmux) -> dict:
    seen: dict = {}

    async def fake_exec(*argv, **kwargs):
        seen["argv"], seen["env"] = argv, kwargs.get("env")
        return _Proc()

    monkeypatch.setattr(tmux_transport.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(tmux.new_session("seat", "true", env={"PENTACLE_SPAWN_NONCE": "n1"}))
    return seen


def test_local_tmux_child_gets_everything_except_assistant_configuration(monkeypatch):
    import os
    assistant = {"PENTACLE_ASSISTANT_AUTO_RESTORE": "1", "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1"}
    other = {"PATH": "/fixture/bin", "PENTACLE_MACHINES_FILE": "/fixture/machines.json",
             "PENTACLE_ASSISTANTS": "not-the-prefix", "COSMO_PENTACLE_TOKEN_FILE": "/fixture/token",
             "EXPO_PROJECT_ID": "fixture", "XPC_SERVICE_NAME": "fixture.service"}
    for key, value in {**assistant, **other}.items():
        monkeypatch.setenv(key, value)
    expected = {key: value for key, value in os.environ.items() if key not in assistant}
    seen = _run_and_capture(monkeypatch, Tmux())
    # Exactly the assistant configuration is removed; nothing else is filtered.
    assert seen["env"] == expected
    assert other.items() <= seen["env"].items()
    # Per-seat values still travel as explicit -e pairs.
    assert "PENTACLE_SPAWN_NONCE=n1" in seen["argv"]


def test_daemon_process_environment_is_not_modified(monkeypatch):
    import os
    monkeypatch.setenv("PENTACLE_ASSISTANT_AUTO_RESTORE", "1")
    _run_and_capture(monkeypatch, Tmux())
    assert os.environ["PENTACLE_ASSISTANT_AUTO_RESTORE"] == "1"


def test_remote_transport_environment_is_unchanged(monkeypatch, tmp_path):
    # Building the ssh command prepares a control directory under HOME.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PENTACLE_ASSISTANT_AUTO_RESTORE", "1")
    seen = _run_and_capture(monkeypatch, Tmux(ssh_target="fixture-peer"))
    assert seen["env"] is None
    assert seen["argv"][0] == "ssh"
