from __future__ import annotations

import json
from argparse import Namespace

from agent_orch import cli
from agent_orch.config import Config


def _args(**overrides):
    data = {
        "text": ["Foo Bar"],
        "timeout": 1.0,
    }
    data.update(overrides)
    return Namespace(**data)


def test_title_parser_accepts_quoted_or_unquoted_words():
    quoted = cli.build_parser().parse_args(["title", "Foo Bar"])
    unquoted = cli.build_parser().parse_args(["title", "Foo", "Bar"])

    assert quoted.text == ["Foo Bar"]
    assert unquoted.text == ["Foo", "Bar"]


def test_title_uses_agent_orch_stream_id_and_splits_session_on_first_colon(monkeypatch, capsys, tmp_path):
    calls = []

    async def fake_rename_once(config, host, session_name, display_name, *, source, timeout):
        calls.append(
            {
                "config": config,
                "host": host,
                "session_name": session_name,
                "display_name": display_name,
                "source": source,
                "timeout": timeout,
            }
        )
        return {"type": "rename.ok", "session": {"stream_id": f"{host}:{session_name}", "display_name": display_name}}

    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "hostb:codex-session-with-dashes")
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "rename_once", fake_rename_once)

    assert cli.title(_args()) == 0

    assert calls == [
        {
            "config": calls[0]["config"],
            "host": "hostb",
            "session_name": "codex-session-with-dashes",
            "display_name": "Foo Bar",
            "source": "agent",
            "timeout": 1.0,
        }
    ]
    assert json.loads(capsys.readouterr().out)["type"] == "rename.ok"


def test_title_splits_only_on_first_colon_when_session_has_extra_colons(monkeypatch, capsys, tmp_path):
    # Regression guard: the resolved stream id is host:session_name, but a
    # session_name may itself contain colons. The split must be on the FIRST
    # colon only; a future rsplit/full-split regression would route the rename
    # to the wrong host/session. (Hardening from Stage A code QA.)
    calls = []

    async def fake_rename_once(_config, host, session_name, display_name, *, source, timeout):
        calls.append((host, session_name, display_name, source, timeout))
        return {"type": "rename.ok"}

    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "hostb:codex-hostb-1:2-foo")
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "rename_once", fake_rename_once)

    assert cli.title(_args()) == 0

    assert calls == [("hostb", "codex-hostb-1:2-foo", "Foo Bar", "agent", 1.0)]


def test_title_prefers_pentacle_stream_id_over_agent_orch_stream_id(monkeypatch, tmp_path):
    calls = []

    async def fake_rename_once(_config, host, session_name, display_name, *, source, timeout):
        calls.append((host, session_name, display_name, source, timeout))
        return {"type": "rename.ok"}

    monkeypatch.setenv("PENTACLE_STREAM_ID", "hosta:claude-top")
    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "hostb:codex-ignored")
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "rename_once", fake_rename_once)

    assert cli.title(_args(text=["Better", "Title"], timeout=2.0)) == 0

    assert calls == [("hosta", "claude-top", "Better Title", "agent", 2.0)]


def test_title_requires_resolved_valid_self_stream_id(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: None)

    assert cli.title(_args()) == 2

    captured = capsys.readouterr()
    assert "stream_id_unknown" in captured.err

