"""Gemini boot-readiness and submission-evidence predicates.

The fixtures are plain `tmux capture-pane` captures of a live detached
gemini-cli pane in its three evidence states: idle composer, unsubmitted
draft, and a submitted turn. They pin the structural rules the predicates
match, so a gemini-cli UI change fails loudly here instead of silently
un-proving spawns.
"""
from __future__ import annotations

from pathlib import Path

from boot_ready import (
    DRAFT_PREDICATES,
    READY_PREDICATES,
    SUBMIT_PREDICATES,
    gemini_prompt_in_active_draft,
    gemini_prompt_ready,
    gemini_prompt_submitted,
    submission_proven_after,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PROMPT = "PREDICATE_TEST_ONE"


def _pane(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_gemini_idle_pane_is_ready_and_proves_nothing() -> None:
    pane = _pane("gemini_idle_pane.txt")
    assert gemini_prompt_ready(pane)
    assert not gemini_prompt_in_active_draft(pane, PROMPT)
    assert not gemini_prompt_submitted(pane, PROMPT)


def test_gemini_draft_is_vetoed_not_proven() -> None:
    pane = _pane("gemini_draft_pane.txt")
    assert gemini_prompt_ready(pane)
    assert gemini_prompt_in_active_draft(pane, PROMPT)
    assert not gemini_prompt_submitted(pane, PROMPT)


def test_gemini_submitted_prompt_is_proven() -> None:
    pane = _pane("gemini_submitted_pane.txt")
    assert gemini_prompt_ready(pane)
    assert not gemini_prompt_in_active_draft(pane, PROMPT)
    assert gemini_prompt_submitted(pane, PROMPT)


def test_gemini_submission_gate_uses_marker_delta() -> None:
    """`submission_proven_after` must require NEW transcript evidence after the
    pre-paste baseline: the unsubmitted draft proves nothing even though its
    composer contains the prompt."""
    baseline = _pane("gemini_idle_pane.txt")
    draft = _pane("gemini_draft_pane.txt")
    submitted = _pane("gemini_submitted_pane.txt")
    assert not submission_proven_after(draft, baseline, PROMPT, "gemini")
    assert submission_proven_after(submitted, draft, PROMPT, "gemini")


def test_gemini_is_wired_into_the_dispatch_tables() -> None:
    assert READY_PREDICATES["gemini"] is gemini_prompt_ready
    assert SUBMIT_PREDICATES["gemini"] is gemini_prompt_submitted
    assert DRAFT_PREDICATES["gemini"] is gemini_prompt_in_active_draft
