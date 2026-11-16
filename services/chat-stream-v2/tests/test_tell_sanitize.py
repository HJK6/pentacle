"""Opt-in sanitize on `tell` (QA #17).

A tell relaying captured terminal output (ANSI colour, a `capture-pane`
excerpt) would otherwise hard-fail `unsafe_payload` on its ESC bytes. With an
explicit `sanitize` flag the daemon STRIPS the control sequences and delivers
the visible text; without the flag the reject default is unchanged, so no
caller ever silently gets a mangled injection.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from comms import Comms  # noqa: E402
from sessions import Sessions, VerbError  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from tmux_transport import assert_injectable
from store import Store  # noqa: E402

HOST = "localhost"
# A colourised log tail: SGR sequences around real text, exactly what an agent
# relaying `capture-pane` output produces.
ANSI_BODY = "\x1b[31mERROR\x1b[0m connection reset — retrying \x1b[1mnow\x1b[0m"


class FakeTmux:
    """A claude pane that starts idle (input caret, ready to receive) and, once a
    message is pasted, renders it SUBMITTED — moved into history above an empty
    caret with a reply marker — so the submission-confirmed receipt observes it.
    Its paste mirrors the real chokepoint by asserting injectability."""

    IDLE = "⏵⏵ bypass permissions on (bypass)\n❯ \n"

    def __init__(self) -> None:
        self.pasted: list[str] = []
        self._screen = self.IDLE

    async def has_session(self, name: str) -> bool:
        return True

    async def capture(self, name: str) -> str:
        return self._screen

    async def pane_pid(self, name: str) -> str:
        return ""  # no pid -> transcript probe yields no_transcript; viewport proves it

    async def kill_session(self, name: str) -> None:
        pass

    async def paste(self, name: str, text: str) -> None:
        assert_injectable(text)  # the real Tmux.paste guarantee
        self.pasted.append(text)
        # Submitted: the message sits in history above an empty caret, behind a
        # reply marker (`claude_prompt_submitted` proof).
        self._screen = f"⏺ {text}\n❯ \n"

    async def run(self, *a: str, **k) -> tuple[int, str]:
        return (0, "")


async def _tell(name: str, msg_extra: dict) -> tuple[list[str], object]:
    store = Store(":memory:")
    store.start()
    try:
        tmux = FakeTmux()
        sessions = Sessions(store, tmux=tmux, local_host=HOST)
        spawnctl = SpawnCtl(store, sessions, tmux=tmux)
        comms = Comms(store, sessions, spawnctl)
        await sessions.open(HOST, name, visibility="default", provider="claude")
        msg = {"stream_id": f"{HOST}:{name}", "message": ANSI_BODY, **msg_extra}
        try:
            reply = await comms.tell(msg)
        except VerbError as exc:
            return tmux.pasted, exc
        return tmux.pasted, reply
    finally:
        store.stop()


def test_tell_without_sanitize_still_rejects_ansi() -> None:
    pasted, result = asyncio.run(_tell("t-reject", {}))
    assert isinstance(result, VerbError)
    assert result.code == "unsafe_payload"
    assert pasted == [], "a rejected tell must never touch the pane"


def test_tell_with_sanitize_strips_and_delivers() -> None:
    pasted, result = asyncio.run(_tell("t-clean", {"sanitize": True}))
    # On revert (flag ignored) this raises unsafe_payload instead of tell.ok.
    assert not isinstance(result, VerbError), result
    assert result["type"] == "tell.ok"
    assert len(pasted) == 1
    delivered = pasted[0]
    assert "\x1b" not in delivered  # every ESC stripped
    # The visible text survives intact.
    assert "ERROR" in delivered and "connection reset" in delivered and "now" in delivered
