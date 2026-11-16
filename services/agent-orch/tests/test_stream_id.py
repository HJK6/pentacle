from __future__ import annotations

import subprocess

from agent_orch.config import Config
from agent_orch.stream_id import LeaderSource, _tmux_session_name, discover_leader_stream_id


def test_env_override(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_ORCH_STREAM_ID", raising=False)
    monkeypatch.setenv("PENTACLE_STREAM_ID", "env-stream")
    config = Config("ws://unused", "", "hostb", tmp_path)
    assert discover_leader_stream_id(config, {"sessions": []}) == (
        "env-stream",
        LeaderSource.ENV_PENTACLE_STREAM_ID,
    )


def test_tmux_self_discover(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_ORCH_STREAM_ID", raising=False)
    monkeypatch.delenv("PENTACLE_STREAM_ID", raising=False)
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,1,0")

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, stdout="leader\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    config = Config("ws://unused", "", "hostb", tmp_path)
    snapshot = {"sessions": [{"host": "hostb", "session_name": "leader", "stream_id": "hostb:leader"}]}
    assert discover_leader_stream_id(config, snapshot) == (
        "hostb:leader",
        LeaderSource.SHELL_FALLBACK,
    )


def test_tmux_session_name_uses_inherited_tmux_environment(monkeypatch):
    seen = {}
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,1,0")

    def fake_run(*_args, **kwargs):
        seen["env"] = kwargs.get("env")
        return subprocess.CompletedProcess([], 0, stdout="leader\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert _tmux_session_name() == "leader"
    assert "env" in seen
    assert seen["env"] is None


def test_tmux_session_name_requires_tmux_environment(monkeypatch):
    def fail_run(*_args, **_kwargs):
        raise AssertionError("tmux should not be queried without TMUX")

    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr("subprocess.run", fail_run)

    assert _tmux_session_name() is None


def test_untracked_warning(monkeypatch, tmp_path, caplog):
    monkeypatch.delenv("AGENT_ORCH_STREAM_ID", raising=False)
    monkeypatch.delenv("PENTACLE_STREAM_ID", raising=False)
    monkeypatch.setattr("subprocess.run", lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError()))
    config = Config("ws://unused", "", "hostb", tmp_path)
    assert discover_leader_stream_id(config, {"sessions": []}) == (
        None,
        LeaderSource.SHELL_FALLBACK,
    )
    assert "untracked" in caplog.text

