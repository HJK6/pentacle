"""Collapsed pane and transcript shapes remain advisory for spawn proof.

The claude/codex TUIs collapse a large paste into a one-line placeholder
(`[Pasted text #1 +212 lines]`) instead of echoing the pasted text, so the
receipt's echo needle — the brief's tail — NEVER repaints. On HEAD the spawn
receipt was once pane/transcript-driven. The shared proof primitive now accepts
only the exact post-watermark USER event from the stream's authoritative Store.

These tests retain the diagnostic fixtures while asserting that neither pane
chrome nor provider transcript alone can cross the delivery boundary.
"""

from __future__ import annotations

import tmux_transport

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

import spawnctl as spawnctl_mod  # noqa: E402
from sessions import Sessions  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from tmux_transport import has_collapsed_paste, receipt_needle
from store import Store  # noqa: E402

HOST = "localhost"
#: A multi-hundred-line brief — the shape that collapses. Its distinctive tail
#: is what the receipt looks for; the CLI hides it inside the placeholder.
BRIEF = (
    "You are a hidden implementation lead. Read your brief and begin.\n"
    + "\n".join(f"context line {i:03d}: background the lead must load" for i in range(300))
    + "\nFINAL-INSTRUCTION verify brief delivery then report"
)
#: What claude shows once it collapses the paste — the brief's tail is gone.
COLLAPSED_SCREEN = "❯ [Pasted text #1 +302 lines]\n⏵⏵ example mode on (shift+tab to cycle)"
PRE_PASTE = "claude ready ❯\n⏵⏵ example mode on (shift+tab to cycle)"


class CollapsedTmux:
    """A live claude pane whose capture only ever shows the collapsed-paste
    placeholder (the brief's echo never repaints), with a controllable pid so
    the transcript probe can locate a real held-open `.jsonl`."""

    def __init__(self, pane_pid: str) -> None:
        self._pid = pane_pid

    async def has_session(self, name: str) -> bool:
        return True

    async def session_state(self, name: str) -> str:
        return "alive"

    async def capture(self, name: str) -> str:
        return COLLAPSED_SCREEN

    async def pane_pid(self, name: str) -> str:
        return self._pid


def _spawnctl(tmux: CollapsedTmux) -> SpawnCtl:
    store = Store(":memory:")
    return SpawnCtl(store, Sessions(store, tmux=tmux, local_host=HOST), tmux=tmux)


def _held_transcript(tmp_path: Path, content: str):
    """A provider `.jsonl` THIS process holds open, so lsof against our own pid
    (handed in as the pane pid) locates it — same probe path as adoption."""
    log = tmp_path / "transcripts" / "example" / "sess.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(content)
    return open(log)  # noqa: SIM115 - held open for the probe's lifetime


def test_placeholder_regex_matches_the_collapsed_paste() -> None:
    assert has_collapsed_paste("claude", COLLAPSED_SCREEN)
    assert has_collapsed_paste("codex", "› [Pasted Content 4096 chars]")
    assert not has_collapsed_paste("claude", PRE_PASTE)
    assert not has_collapsed_paste("", COLLAPSED_SCREEN)  # explicit-command path


def test_collapsed_long_brief_confirms_via_transcript(tmp_path: Path) -> None:
    if shutil.which("lsof") is None or shutil.which("ps") is None:
        pytest.skip("transcript probe needs lsof + ps")
    fh = _held_transcript(tmp_path, json.dumps({"type": "user", "message": {"content": BRIEF}}) + "\n")
    try:
        tmux = CollapsedTmux(pane_pid=str(os.getpid()))
        ctl = _spawnctl(tmux)

        # HEAD's receipt policy — pane echo only — CANNOT see the brief: its
        # tail never repaints once the paste collapses. This is the exact
        # `prompt_delivery_failed` that killed long-brief spawns.
        old_policy = asyncio.run(
            ctl._await_marker("s", receipt_needle(BRIEF), timeout=0.3, since=PRE_PASTE)
        )
        assert old_policy is False

        # Transcript content is advisory; only the authoritative post-watermark
        # USER event can confirm the brief.
        new_policy = asyncio.run(
            ctl._confirm_brief_delivery("s", BRIEF, PRE_PASTE, "claude", tmux)
        )
        assert new_policy is False
    finally:
        fh.close()


def test_collapsed_paste_without_transcript_is_not_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("lsof") is None or shutil.which("ps") is None:
        pytest.skip("transcript probe needs lsof + ps")
    monkeypatch.setattr(tmux_transport, "RECEIPT_TIMEOUT_S", 0.3)  # else it polls the full 15s
    # A transcript IS located, but the brief is not in it: the paste collapsed
    # into the input box yet Enter never submitted it. Screen alone is ambiguous,
    # so the transcript authority correctly withholds confirmation (no false
    # `delivered` — the receipt stays honest and the pane is cleaned up).
    fh = _held_transcript(tmp_path, json.dumps({"type": "user", "message": {"content": "boot only"}}) + "\n")
    try:
        tmux = CollapsedTmux(pane_pid=str(os.getpid()))
        ctl = _spawnctl(tmux)
        confirmed = asyncio.run(
            ctl._confirm_brief_delivery("s", BRIEF, PRE_PASTE, "claude", tmux)
        )
        assert confirmed is False
    finally:
        fh.close()
