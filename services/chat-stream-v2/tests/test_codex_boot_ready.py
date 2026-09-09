"""codex boot-readiness must not ride the placeholder lottery (this spec).

`boot_ready.codex_tui_ready` used to allow-list the input placeholder examples
{"", "write tests for @filename", "explain this codebase", "ask anything"}.
codex 0.146 rotates NEW examples (observed live: "Improve documentation in
@filename" on a fully-ready TUI), so a ready pane drawing an unlisted placeholder
read as boot_not_ready — success ≈ P(a known example was drawn), the ~40% flap.

The fix stops keying readiness on the rotating placeholder TEXT: a prompt-shaped
input line plus codex chrome is ready PROVIDED the negative guards (trust dialog,
login, working/thinking, esc-to-interrupt) hold — those guards are the real
safety and must stay intact. This locks the new-placeholder case green, keeps the
known-placeholder/empty cases green, and keeps every negative guard red.
"""

from __future__ import annotations
from pathlib import Path

import boot_ready  # noqa: E402

_CHROME = "OpenAI Codex\n─────────\n"
_FOOTER = "\n  gpt-5-codex high"


def _pane(input_line: str) -> str:
    return f"{_CHROME}{input_line}{_FOOTER}"


# -- ready panes: readiness is independent of the placeholder text --------

def test_new_rotating_placeholder_is_ready() -> None:
    """The headline regression: a fully-ready TUI drawing codex 0.146's new
    placeholder must gate READY. Revert-fails at a06b8a0b (unlisted text →
    boot_not_ready over a live TUI)."""
    assert boot_ready.codex_tui_ready(_pane("› Improve documentation in @filename")) is True


def test_known_placeholders_stay_ready() -> None:
    for example in ("› write tests for @filename", "› explain this codebase", "› ask anything"):
        assert boot_ready.codex_tui_ready(_pane(example)) is True, example


def test_empty_input_box_is_ready() -> None:
    assert boot_ready.codex_tui_ready(_pane("› ")) is True


def test_arbitrary_future_placeholder_is_ready() -> None:
    """Any not-yet-seen placeholder rotation must gate ready too — the whole
    point is to stop enumerating them."""
    assert boot_ready.codex_tui_ready(_pane("› Refactor the auth module in @filename")) is True


# -- negative guards MUST still reject (the actual safety) -----------------

def test_active_turn_is_not_ready() -> None:
    """A busy turn (working + esc-to-interrupt) must never receive a paste,
    even though its bottom composer is an empty `›` line."""
    pane = "› run the delivery check now\n• Working (esc to interrupt)\n─────────\n› "
    assert boot_ready.codex_tui_ready(pane) is False


def test_trust_dialog_is_not_ready() -> None:
    """The directory-trust prompt gates unsafe even with codex chrome present —
    proves readiness is not merely 'chrome + input line'."""
    pane = (
        "OpenAI Codex\n"
        "Do you trust the contents of this directory?\n"
        "  1. Yes, proceed\n"
        "  2. No, exit\n"
        "› \n"
        "  gpt-5-codex high"
    )
    assert boot_ready.codex_tui_ready(pane) is False


def test_login_prompt_is_not_ready() -> None:
    pane = "Sign in to Codex\n› \n  gpt-5-codex high"
    assert boot_ready.codex_tui_ready(pane) is False


def test_bare_shell_without_codex_chrome_is_not_ready() -> None:
    """No codex chrome anywhere → not a codex TUI, regardless of a stray caret."""
    assert boot_ready.codex_tui_ready("some-host:~ user$\n› ") is False
