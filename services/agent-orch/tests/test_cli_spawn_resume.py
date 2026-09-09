"""Unit tests for the public spawn-resume command.

The CLI must: thread resume_session_id into the spawn payload, require
--provider claude, and reject --resume combined with --handoff/--parent
(client-side, before any RPC).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_orch import cli
from agent_orch.config import Config


RESUME_ID = "11111111-2222-3333-4444-555555555555"


def _config(tmp_path: Path) -> Config:
    return Config(
        ws_url="ws://unused",
        token="",
        host_id="hostb",
        runtime_dir=tmp_path / "runtime",
        memory_repo_path=None,
    )


def _resume_args(
    tmp_path: Path,
    *,
    provider: str = "claude",
    parent: str | None = None,
    handoff: bool = False,
    resume: str | None = RESUME_ID,
    top_level: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        objective="Exercise the existing spawn contract", provider=provider,
        host=None,
        role=None,
        phase=None,
        spec_id=None,
        visibility=None,
        parent=parent,
        handoff=handoff,
        resume=resume,
        top_level=top_level,
        at=None,
        delay=None,
        initial_prompt=None,
        initial_prompt_file=None,
        timeout=1.0,
        self_close_on_completion=True,
    )


def _install_capture(monkeypatch, tmp_path: Path) -> dict:
    captured: dict = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))
    # A resume spawn has no parent/handoff, so the leader-discovery path must not
    # graft a parent_stream_id onto the payload.
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: None)

    async def fake_spawn_once(_config, payload, timeout):
        captured["payload"] = dict(payload)
        captured["timeout"] = timeout
        return {"type": "spawn.ok", "session": {"stream_id": "hostb:claude-hostb-11111111"}}

    monkeypatch.setattr(cli, "spawn_once", fake_spawn_once)
    return captured


def test_spawn_resume_payload_includes_resume_session_id(monkeypatch, tmp_path, capsys) -> None:
    captured = _install_capture(monkeypatch, tmp_path)

    result = cli.spawn(_resume_args(tmp_path))
    capsys.readouterr()

    assert result == 0
    payload = captured["payload"]
    assert payload["type"] == "spawn"
    assert payload["provider"] == "claude"
    assert payload["resume_session_id"] == RESUME_ID
    # No lineage grafted, and no self_close (resume has no parent/handoff).
    assert "parent_stream_id" not in payload
    assert "from_stream_id" not in payload
    assert "handoff" not in payload
    assert "self_close_on_completion" not in payload


def test_spawn_resume_top_level_suppresses_in_session_parent_inference(monkeypatch, tmp_path, capsys) -> None:
    captured = _install_capture(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostc:claude-live-caller")

    result = cli.spawn(_resume_args(tmp_path, top_level=True))
    capsys.readouterr()

    assert result == 0
    payload = captured["payload"]
    assert payload["resume_session_id"] == RESUME_ID
    assert "parent_stream_id" not in payload
    assert "handoff_from_stream_id" not in payload
    assert "from_stream_id" not in payload
    assert "self_close_on_completion" not in payload


def test_spawn_resume_without_top_level_still_infers_parent_inside_session(monkeypatch, tmp_path, capsys) -> None:
    captured = _install_capture(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostc:claude-live-caller")

    result = cli.spawn(_resume_args(tmp_path))
    capsys.readouterr()

    assert result == 0
    payload = captured["payload"]
    assert payload["resume_session_id"] == RESUME_ID
    assert payload["parent_stream_id"] == "hostc:claude-live-caller"


def test_spawn_resume_requires_claude_provider(monkeypatch, tmp_path, capsys) -> None:
    _install_capture(monkeypatch, tmp_path)

    result = cli.spawn(_resume_args(tmp_path, provider="codex"))

    assert result == 2
    assert "requires --provider claude" in capsys.readouterr().err


def test_spawn_resume_rejects_parent(monkeypatch, tmp_path, capsys) -> None:
    _install_capture(monkeypatch, tmp_path)

    result = cli.spawn(_resume_args(tmp_path, parent="hostb:claude-leader"))

    assert result == 2
    assert "incompatible with --handoff/--parent" in capsys.readouterr().err


def test_spawn_resume_rejects_handoff(monkeypatch, tmp_path, capsys) -> None:
    _install_capture(monkeypatch, tmp_path)

    result = cli.spawn(_resume_args(tmp_path, handoff=True))

    assert result == 2
    assert "incompatible with --handoff/--parent" in capsys.readouterr().err


def test_spawn_top_level_rejects_parent(monkeypatch, tmp_path, capsys) -> None:
    _install_capture(monkeypatch, tmp_path)

    result = cli.spawn(_resume_args(tmp_path, parent="hostb:claude-leader", top_level=True))

    assert result == 2
    assert "--top-level is incompatible with --handoff/--parent" in capsys.readouterr().err


def test_spawn_top_level_rejects_handoff(monkeypatch, tmp_path, capsys) -> None:
    _install_capture(monkeypatch, tmp_path)

    result = cli.spawn(_resume_args(tmp_path, handoff=True, top_level=True))

    assert result == 2
    assert "--top-level is incompatible with --handoff/--parent" in capsys.readouterr().err


def test_spawn_parser_accepts_resume_flag() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["spawn", "--objective", "Exercise the existing spawn contract", "--provider", "claude", "--resume", RESUME_ID, "--top-level"])
    assert args.resume == RESUME_ID
    assert args.top_level is True
    # Default (no flag) leaves resume unset.
    plain = parser.parse_args(["spawn", "--objective", "Exercise the existing spawn contract", "--provider", "claude"])
    assert plain.resume is None
    assert plain.top_level is False


def test_spawn_help_lists_resume_flag(capsys) -> None:
    # In-process against the worktree parser: a subprocess `python -c "import
    # agent_orch"` would resolve the pip-INSTALLED agent_orch (which lags the
    # worktree and lacks --resume), so it cannot validate this change.
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["spawn", "--objective", "Exercise the existing spawn contract", "--help"])
    out = capsys.readouterr().out
    assert "--resume" in out
    assert "--top-level" in out
