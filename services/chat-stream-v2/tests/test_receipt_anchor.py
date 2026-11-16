"""Receipt observation is anchored to text captured AFTER the paste (public regression).

The old search looked for the needle anywhere in the whole 200-line capture, so
a repeated short message was observed off a PRIOR identical echo still on screen
and reported delivered before this paste's echo appeared. The receipt now
searches only the pane text that is new since the pre-paste capture.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from sessions import Sessions  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from tmux_transport import new_since
from store import Store  # noqa: E402

MSG = "run the thing"


class ScriptedTmux:
    """Returns a scripted sequence of captures; the last value repeats."""

    def __init__(self, captures: list[str]) -> None:
        self._captures = captures
        self._i = 0

    async def has_session(self, name: str) -> bool:
        return True

    async def session_state(self, name: str) -> str:
        return "alive"

    async def capture(self, name: str) -> str:
        value = self._captures[min(self._i, len(self._captures) - 1)]
        self._i += 1
        return value


def _spawnctl(tmux: ScriptedTmux) -> SpawnCtl:
    store = Store(":memory:")
    return SpawnCtl(store, Sessions(store, tmux=tmux, local_host="hosta"), tmux=tmux)


def test_new_since_returns_only_appended_text() -> None:
    assert new_since("", "anything at all") == "anything at all"
    assert new_since(f"scrollback {MSG}", f"scrollback {MSG}") == ""
    assert new_since("scrollback", f"scrollback {MSG}") == f" {MSG}"


def test_prior_identical_echo_is_not_a_receipt() -> None:
    """The needle already on screen before the paste must NOT count: no new
    echo appears, so the receipt is not observed; searching the whole capture
    would incorrectly count the stale echo."""
    before = f"earlier turn: {MSG} (done)"
    tmux = ScriptedTmux([before])  # capture never changes after the paste
    ctl = _spawnctl(tmux)
    observed = asyncio.run(
        ctl._await_marker("s", MSG, timeout=0.25, since=before)
    )
    assert observed is False


def test_new_echo_after_the_anchor_is_observed() -> None:
    before = "earlier unrelated output"
    tmux = ScriptedTmux([before, before, f"{before} {MSG}"])
    ctl = _spawnctl(tmux)
    observed = asyncio.run(
        ctl._await_marker("s", MSG, timeout=2.0, since=before)
    )
    assert observed is True


FOOTER = "⏵⏵ bypass permissions on (shift+tab to cycle) · ← 1 agent"


def test_persistent_tui_footer_does_not_hide_a_fresh_echo() -> None:
    """A provider TUI may render a persistent
    footer as the LAST line of every capture. Anchoring on `before`'s tail
    (the footer) re-finds it at the bottom of the post-paste capture and
    slices the echo away — a peer tell to a live provider falsely settled
    `delivery_failed` while the message had landed. A needle absent from
    `before` must be searched in the full capture."""
    before = f"chat history above {FOOTER}"
    after = f"chat history above ❯ {MSG} agent replied {FOOTER}"
    tmux = ScriptedTmux([after])
    ctl = _spawnctl(tmux)
    observed = asyncio.run(
        ctl._await_marker("s", MSG, timeout=0.5, since=before)
    )
    assert observed is True


def test_footer_case_still_rejects_a_stale_identical_echo() -> None:
    """public regression survives the footer fix: when the needle IS already on screen
    pre-paste, the strict anchored slice stays in force."""
    before = f"earlier turn: {MSG} (done) {FOOTER}"
    tmux = ScriptedTmux([before])
    ctl = _spawnctl(tmux)
    observed = asyncio.run(
        ctl._await_marker("s", MSG, timeout=0.25, since=before)
    )
    assert observed is False
