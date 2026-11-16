"""Regression coverage for local transcript discovery under the smoke PATH."""

from __future__ import annotations

import tmux_transport

import asyncio
import logging

import ingest as ingest_module
import spawnctl as spawnctl_module


def test_open_transcripts_resolves_lsof_outside_restricted_path(tmp_path, monkeypatch) -> None:
    """The smoke PATH omits bare ``lsof`` although /usr/sbin/lsof is available."""
    transcript = tmp_path / ".claude" / "projects" / "stub" / "provider.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    class Process:
        returncode = 0

        async def communicate(self):
            return f"n{transcript}\n".encode(), b""

    async def create_process(*args, **_kwargs):
        calls.append(args)
        if args[0] == "lsof":
            raise FileNotFoundError(args[0])
        return Process()

    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(
        tmux_transport.os.path,
        "isfile",
        lambda path: path == "/usr/sbin/lsof",
    )
    monkeypatch.setattr(
        tmux_transport.os,
        "access",
        lambda path, _mode: path == "/usr/sbin/lsof",
    )
    monkeypatch.setattr(tmux_transport.asyncio, "create_subprocess_exec", create_process)

    assert asyncio.run(ingest_module._open_transcripts(["42"])) == [str(transcript)]
    assert calls == [("/usr/sbin/lsof", "-p", "42", "-Fn")]


def test_exec_logs_missing_executable_distinctly(monkeypatch, caplog) -> None:
    async def create_process(*args, **_kwargs):
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(tmux_transport.asyncio, "create_subprocess_exec", create_process)

    with caplog.at_level(logging.ERROR, logger="chat_streamd_v2.spawnctl"):
        result = asyncio.run(tmux_transport._exec("missing-program"))

    assert result == (127, "executable not found: missing-program")
    assert "subprocess executable not found: missing-program" in caplog.messages
