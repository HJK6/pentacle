"""Viewport predicates distinguish a submitted brief from one still sitting in
the input draft.

`*_submitted` must be True only once the brief has left the draft (into history,
or a reply marker follows it); `*_in_active_draft` must be True exactly when the
brief is on screen, unsubmitted, in the input box. A predicate that confirms a
draft must not confirm delivery, while a submitted brief must be recognized
even when the provider redraws its interface.
"""

from __future__ import annotations
from pathlib import Path

from boot_ready import (  # noqa: E402
    claude_prompt_in_active_draft,
    claude_prompt_submitted,
    codex_prompt_in_active_draft,
    codex_prompt_submitted,
    submission_proven_after,
)

BRIEF = "run the delivery check now"

# -- claude --------------------------------------------------------------------
CLAUDE_DRAFT = "⏵⏵ bypass permissions on (bypass)\n❯ run the delivery check now"
CLAUDE_SUBMITTED_HISTORY = "⏺ run the delivery check now\n\n✻ Working…\n❯ "
CLAUDE_SUBMITTED_MARKER = "❯ run the delivery check now\n⎿  reading the repo\n❯ "
CLAUDE_BOOT = "⏵⏵ bypass permissions on (bypass)\n❯ "


def test_claude_unsubmitted_draft_is_not_submitted() -> None:
    assert claude_prompt_submitted(CLAUDE_DRAFT, BRIEF) is False
    assert claude_prompt_in_active_draft(CLAUDE_DRAFT, BRIEF) is True


def test_claude_submitted_to_history_is_submitted() -> None:
    assert claude_prompt_submitted(CLAUDE_SUBMITTED_HISTORY, BRIEF) is True
    assert claude_prompt_in_active_draft(CLAUDE_SUBMITTED_HISTORY, BRIEF) is False


def test_claude_reply_marker_after_input_is_submitted() -> None:
    # Still echoed at the caret, but a reply marker follows it -> submitted.
    assert claude_prompt_submitted(CLAUDE_SUBMITTED_MARKER, BRIEF) is True
    assert claude_prompt_in_active_draft(CLAUDE_SUBMITTED_MARKER, BRIEF) is False


def test_claude_empty_boot_is_neither() -> None:
    assert claude_prompt_submitted(CLAUDE_BOOT, BRIEF) is False
    assert claude_prompt_in_active_draft(CLAUDE_BOOT, BRIEF) is False


# -- codex ---------------------------------------------------------------------
# Fresh-codex layout: chrome + a separator ABOVE the composer, the `gpt-…` footer
# BELOW it (no reply marker). The below-composer footer must not read as a
# reply, else an unsubmitted draft false-confirms.
CODEX_DRAFT = "OpenAI Codex\n─────────\n› run the delivery check now\n  gpt-5-codex high"
CODEX_DRAFT_WITH_FOOTER_DIVIDER = (
    "OpenAI Codex\n─────────\n› run the delivery check now\n"
    "────────────────\n  gpt-5-codex high"
)
CODEX_SUBMITTED = "› run the delivery check now\n• Working (esc to interrupt)\n─────────\n› "
CODEX_BOOT = "OpenAI Codex\n─────────\n› \n  gpt-5-codex high"


def test_codex_unsubmitted_draft_is_not_submitted() -> None:
    assert codex_prompt_submitted(CODEX_DRAFT, BRIEF) is False
    assert codex_prompt_in_active_draft(CODEX_DRAFT, BRIEF) is True


def test_codex_footer_divider_is_not_submission_proof() -> None:
    assert codex_prompt_submitted(CODEX_DRAFT_WITH_FOOTER_DIVIDER, BRIEF) is False
    assert codex_prompt_in_active_draft(CODEX_DRAFT_WITH_FOOTER_DIVIDER, BRIEF) is True


def test_codex_submitted_with_reply_marker_is_submitted() -> None:
    # The brief moved into history above the new composer caret -> submitted.
    assert codex_prompt_submitted(CODEX_SUBMITTED, BRIEF) is True
    assert codex_prompt_in_active_draft(CODEX_SUBMITTED, BRIEF) is False


def test_codex_prompt_submitted_tolerates_terminal_wrap_in_staged_pointer() -> None:
    """hostb Codex wraps the staged pointer at a hyphen and adds indent."""
    pointer = (
        "Read /tmp/public-test/example-pointer.txt "
        "and follow the complete prompt exactly."
    )
    pane = "\n".join(
        [
            "OpenAI Codex",
            "› Read /tmp/public-test/example-",
            "  pointer.txt and",
            "  follow the complete prompt exactly.",
            "• I’ll read the specified prompt and carry it out.",
            "› Use /skills to list available skills",
        ]
    )

    assert codex_prompt_submitted(pane, pointer) is True


def test_codex_prompt_submitted_rejects_deleted_pointer_whitespace() -> None:
    pointer = (
        "Read /tmp/public-test/example-pointer.txt "
        "and follow the complete prompt exactly."
    )
    pane = "\n".join(
        [
            "OpenAI Codex",
            "› Read /tmp/public-test",
            "  example-pointer.txtand",
            "  follow the complete prompt exactly.",
            "• I’ll read the specified prompt and carry it out.",
            "› Use /skills to list available skills",
        ]
    )

    assert codex_prompt_submitted(pane, pointer) is False


DIRECT_PROMPT = """Synthetic multiline delivery check. Immediately run:
public-cli complete --id 0 --status done --result '{"summary":"synthetic prompt readback ok","findings":[],"next_action":"readback_complete"}' --close
"""


def test_codex_direct_multiline_brief_tolerates_hyphen_wrap() -> None:
    """A direct peer-host specimen wrapped after ``prompt-``."""
    pane = "\n".join(
        [
            "OpenAI Codex",
            "› Synthetic multiline delivery check. Immediately run:",
            '  public-cli complete --id 0 --status done --result \'{"summary":"synthetic prompt',
            '  readback ok","findings":[],"next_action":"readback_complete"}\' --close',
            "• I will run the requested report.",
            "› ",
        ]
    )

    assert codex_prompt_submitted(pane, DIRECT_PROMPT) is True
    assert codex_prompt_in_active_draft(pane, DIRECT_PROMPT) is False


def test_codex_empty_boot_is_neither() -> None:
    assert codex_prompt_submitted(CODEX_BOOT, BRIEF) is False
    assert codex_prompt_in_active_draft(CODEX_BOOT, BRIEF) is False


# A ~2100-char codex brief collapses to `[Pasted Content N chars]`; its text is
# NOT on screen, so the draft predicate must recognise the placeholder itself —
# else an Enter-lost collapsed draft reads as gone and the receipt's
# placeholder-not-in-draft tier false-confirms an idle pane (Invariant A).
CODEX_COLLAPSED_DRAFT = "OpenAI Codex\n─────────\n› [Pasted Content 2148 chars]\n  gpt-5-codex high"
CODEX_COLLAPSED_SUBMITTED = "› [Pasted Content 2148 chars]\n• Working (esc to interrupt)\n─────────\n› "

# Synthetic evidence: the first receipt is recorded while the complete body is
# still editable, then one Enter-only press moves it into the queued region.
SAMPLE_BODY = "read the current scope and report it"
SAMPLE_EDITABLE = (
    "OpenAI Codex\n"
    "─────────\n"
    "› read the current scope and report it\n"
    "tab to queue message"
)
SAMPLE_QUEUED = (
    "OpenAI Codex\n"
    "Messages to be submitted after next tool call (press esc to interrupt and send immediately)\n"
    "↳ read the current scope and report it\n"
    "• scope readback: services/chat-stream-v2\n"
    "› "
)


def test_codex_collapsed_unsubmitted_draft_is_in_active_draft() -> None:
    assert codex_prompt_in_active_draft(CODEX_COLLAPSED_DRAFT, BRIEF) is True
    assert codex_prompt_submitted(CODEX_COLLAPSED_DRAFT, BRIEF) is False


def test_codex_collapsed_submitted_left_the_draft() -> None:
    # Placeholder moved above the new composer caret -> not in the active draft.
    assert codex_prompt_in_active_draft(CODEX_COLLAPSED_SUBMITTED, BRIEF) is False


def test_sample_editable_receipt_is_false_and_enter_readback_is_true() -> None:
    assert codex_prompt_submitted(SAMPLE_EDITABLE, SAMPLE_BODY) is False
    assert codex_prompt_in_active_draft(SAMPLE_EDITABLE, SAMPLE_BODY, exact=True) is True
    assert codex_prompt_submitted(SAMPLE_QUEUED, SAMPLE_BODY) is True
    assert codex_prompt_in_active_draft(SAMPLE_QUEUED, SAMPLE_BODY, exact=True) is False


def test_codex_stale_history_is_vetoed_by_current_active_composer() -> None:
    baseline = "OpenAI Codex\n─────────\n› "
    pane = "› run the delivery check now\n• old reply\n› run the delivery check now\n"
    assert codex_prompt_submitted(pane, BRIEF) is True  # history alone is ambiguous
    assert submission_proven_after(pane, baseline, BRIEF, "codex") is False
