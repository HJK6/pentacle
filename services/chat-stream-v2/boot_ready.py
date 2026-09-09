"""Pure readiness and submission predicates for provider pane text.

The functions in this module have no I/O.  They recognize stable prompt,
reset-menu, submission, and draft shapes in captured text so callers can keep
provider interaction behind an explicit adapter.
"""

from __future__ import annotations

import re


# A Codex reset interstitial is an operator-only decision point: accepting it
# consumes provider quota, so automated input stops while that surface replaces
# the composer.  Availability text may remain in scrollback after the normal
# composer returns and must not disable the seat.  Keep the value stable because
# it is persisted in session/outcome state and distinguishes the block from an
# ordinary boot timeout.
CODEX_RESET_BLOCKED = "reset_blocked"
# Exact labels from a provider's ``usage-menu`` and
# ``rate-limit-reset-confirmation`` surfaces. Keep these labels synchronized with the adapter.
_CODEX_RESET_MENU_TITLE = "view account usage or redeem an earned reset"
_CODEX_RESET_MENU_OPTIONS = frozenset({"show usage", "redeem usage limit reset"})
_CODEX_RESET_CONFIRM_TITLE = "use this reset?"
_CODEX_RESET_CONFIRM_OPTIONS = frozenset({"yes, use reset", "no, go back"})


def codex_reset_interstitial_visible(pane_text: str) -> bool:
    """Recognize the current ``/usage`` menu or reset confirmation surface."""
    lines = (pane_text or "").splitlines()[-48:]
    normalized: list[str] = []
    for line in lines:
        choice = re.sub(r"^[\s›❯>•●○◉✓-]+", "", line).strip().casefold()
        choice = re.sub(r"^\d+[.)]\s*", "", choice).rstrip(".")
        normalized.append(re.sub(r"\s+", " ", choice))
    choices = set(normalized)
    surface = (
        _CODEX_RESET_MENU_TITLE in choices
        and bool(_CODEX_RESET_MENU_OPTIONS & choices)
    ) or (
        _CODEX_RESET_CONFIRM_TITLE in choices
        and bool(_CODEX_RESET_CONFIRM_OPTIONS & choices)
    )
    if not surface:
        return False
    reset_choices = _CODEX_RESET_MENU_OPTIONS | _CODEX_RESET_CONFIRM_OPTIONS
    surface_indexes = [
        index for index, choice in enumerate(normalized)
        if choice in reset_choices
        or choice in {_CODEX_RESET_MENU_TITLE, _CODEX_RESET_CONFIRM_TITLE}
    ]
    later_composer = any(
        index > max(surface_indexes)
        and line.strip().startswith(("›", "❯"))
        and normalized[index] not in reset_choices
        for index, line in enumerate(lines)
    )
    return not later_composer


def codex_tui_readiness(pane_text: str) -> str:
    """Classify current Codex chrome, ignoring passive reset text in history."""
    if codex_reset_interstitial_visible(pane_text):
        return CODEX_RESET_BLOCKED
    if codex_tui_ready(pane_text):
        return "ready"
    return "not_ready"


def claude_prompt_ready(pane_text: str) -> bool:
    # A booted Claude TUI in bypass mode persistently renders the
    # permission-mode footer and an input caret; the welcome banner scrolls
    # off and newer TUIs omit it, so it must not be required (earlier fix note).
    # The trust dialog shows neither an accepted footer nor a usable caret.
    return "⏵⏵ bypass permissions" in pane_text and "❯" in pane_text


def _codex_input_line(line: str) -> bool:
    return line.strip().startswith(("›", "❯"))


def _strip_codex_input_marker(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith(("›", "❯")):
        return stripped[1:].lstrip()
    return stripped


def codex_tui_ready(pane_text: str) -> bool:
    """Return true only for a safely consumable Codex prompt (earlier docstring:
    the welcome banner scrolls out, so readiness keys on the bottom input and
    persistent chrome; negative guards stay conservative — a busy turn,
    trust/login flow, or bare shell must never receive a paste).

    Readiness does NOT key on the input-line TEXT: codex 0.146 rotates its greyed
    placeholder examples (observed live: "Improve documentation in @filename"),
    and an earlier allow-list of the then-known examples read every ready TUI
    that drew an unlisted placeholder as boot_not_ready — success ≈ P(a known
    example was drawn), the ~40% spawn flap (spec
    pentacle__spawn_codex_boot_not_ready_flaky). A prompt-shaped `›`/`❯` input
    line plus codex chrome is ready; the negative guards below (trust dialog,
    login, working/thinking, esc-to-interrupt) are the real safety and stay
    unchanged — boot readiness runs before any paste, so the composer is
    empty-or-placeholder and its text carries no readiness signal."""
    if codex_reset_interstitial_visible(pane_text):
        return False
    lines = pane_text.splitlines()
    input_indexes = [index for index, line in enumerate(lines) if _codex_input_line(line)]
    input_lines = [lines[index] for index in input_indexes]
    if not input_lines:
        return False

    lowered = pane_text.lower()
    recent_input_context = "\n".join(lines[max(0, input_indexes[-1] - 12):]).lower()
    dialog_markers = (
        "do you trust the contents of this directory?",
        "press enter to continue",
    )
    persistent_unsafe_markers = (
        "sign in to codex",
        "log in to codex",
        "login required",
    )
    last_input_index = input_indexes[-1]
    for index, line in enumerate(lines[:last_input_index]):
        if not any(marker in line.lower() for marker in dialog_markers):
            continue
        if not any(
            "openai codex" in later.lower() or later.strip().startswith("• ")
            for later in lines[index + 1: last_input_index]
        ):
            return False
    if any(marker in lowered for marker in persistent_unsafe_markers):
        return False

    if re.search(r"(?m)^\s*(?:•\s*)?(?:working|thinking|running)(?:\s|\()", recent_input_context):
        return False
    if "esc to interrupt" in recent_input_context:
        return False
    if "waiting for agents" in recent_input_context and "finished waiting" not in recent_input_context:
        return False

    has_startup_chrome = "openai codex" in lowered or "codex" in lowered
    has_completed_turn_chrome = any(line.strip().startswith("• ") for line in lines)
    has_codex_footer = any("gpt-" in line.lower() for line in lines)
    return has_startup_chrome or has_completed_turn_chrome or has_codex_footer


def codex_tui_session_visible(pane_text: str) -> bool:
    """Return true for an idle composer or an actively working Codex turn."""
    if codex_reset_interstitial_visible(pane_text):
        return False
    if codex_tui_ready(pane_text):
        return True
    recent = "\n".join((pane_text or "").splitlines()[-24:]).casefold()
    has_chrome = "openai codex" in recent or "gpt-" in recent
    return has_chrome and (
        "esc to interrupt" in recent
        or re.search(r"(?m)^\s*(?:•\s*)?(?:working|thinking|running)(?:\s|\()", recent)
        is not None
    )


READY_PREDICATES = {
    "claude": claude_prompt_ready,
    "codex": codex_tui_ready,
}


# -- submission predicates (Submission predicates) ---------------------
#
# A pane ECHO of a pasted brief proves the paste LANDED, never that Enter
# SUBMITTED it: a short brief sitting unsubmitted in the input box echoes
# identically to a submitted one (an initial-prompt delivery race — spawn.ok with
# an idle pane and nothing delivered). These pure predicates read a capture
# STRUCTURALLY to tell the two apart, so they work over a local OR an ssh
# capture — the transcript probe is local-only, so this is the only submission
# proof available for a remote spawn. `*_submitted` = proof of submission;
# `*_in_active_draft` = proof of NON-submission (the brief sits in the input
# draft with no reply marker after it), the fact that lifts the #16 re-send ban.


def _normalize_for_claude_pane_match(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _claude_prompt_in_lines(lines: list[str], prompt: str) -> bool:
    needle = _normalize_for_claude_pane_match(prompt)
    if not needle:
        return False
    return needle in _normalize_for_claude_pane_match("\n".join(lines))


def _last_claude_input_start(lines: list[str]) -> int | None:
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip().startswith("❯"):
            return index
    return None


def _claude_post_submit_marker_after(lines: list[str], input_start: int) -> bool:
    return any(
        line.strip().startswith(("✻ ", "✶ ", "✳ ", "⏺ ", "⎿ ", "● "))
        for line in lines[input_start + 1:]
    )


def claude_prompt_submitted(pane_text: str, prompt: str) -> bool:
    """True once `prompt` has been SUBMITTED to a claude TUI (moved into the
    history above the input caret, or answered by a reply marker), not merely
    pasted into the active draft."""
    if "API Error:" in pane_text:
        return True
    lines = pane_text.splitlines()
    input_start = _last_claude_input_start(lines)
    if input_start is None:
        return _claude_prompt_in_lines(lines, prompt)
    if _claude_prompt_in_lines(lines[:input_start], prompt):
        return True
    if not _claude_prompt_in_lines(lines[input_start:], prompt):
        return False
    return _claude_post_submit_marker_after(lines, input_start)


def _claude_active_draft_has_collapsed_paste(lines: list[str], input_start: int) -> bool:
    return any(
        re.search(r"\[Pasted text #\d+(?: [^\]]*)?\]", line)
        for line in lines[input_start:]
    )


def claude_prompt_in_active_draft(pane_text: str, prompt: str) -> bool:
    """True when `prompt` is sitting UNSUBMITTED in claude's active input draft
    (proof of non-delivery: the brief is on screen, in the box, no reply marker
    after it). A collapsed paste in the draft counts — its text is hidden but
    the block IS the unsubmitted brief."""
    lines = pane_text.splitlines()
    input_start = _last_claude_input_start(lines)
    if input_start is None:
        return False
    if _claude_active_draft_has_collapsed_paste(lines, input_start):
        return True
    if _claude_prompt_in_lines(lines[:input_start], prompt):
        return False
    if not _claude_prompt_in_lines(lines[input_start:], prompt):
        return False
    return not _claude_post_submit_marker_after(lines, input_start)


def _normalize_for_codex_pane_match(text: str) -> str:
    out = []
    for line in text.splitlines():
        collapsed = re.sub(r"[^\S\n]+", " ", _strip_codex_input_marker(line)).strip()
        if collapsed:
            out.append(collapsed)
    return "\n".join(out)


def _codex_prompt_in_lines(lines: list[str], prompt: str) -> bool:
    needle = _normalize_for_codex_pane_match(prompt)
    if not needle:
        return False
    haystack = _normalize_for_codex_pane_match("\n".join(lines))
    if needle in haystack or needle.replace("\n", " ") in haystack.replace("\n", " "):
        return True
    # Codex hard-wraps messages at terminal boundaries and inserts indentation
    # on continuation lines. This affects both the staged one-line pointer and
    # short direct briefs that contain intentional newlines. Permit whitespace
    # inserted by that layout, but retain every whitespace boundary present in
    # the source prompt. The latter keeps a deleted source space from becoming
    # a false submission match.
    layout_pattern = "".join(
        r"\s+" if character.isspace() else rf"{re.escape(character)}\s*"
        for character in needle
    )
    return re.search(layout_pattern, haystack) is not None


def _last_codex_input_start(lines: list[str]) -> int | None:
    for index in range(len(lines) - 1, -1, -1):
        if _codex_input_line(lines[index]):
            return index
    return None


def _codex_non_draft_line_after_input(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith(("╭", "╰", "│")):
        return False
    # Codex renders a submitted-but-not-yet-consumed prompt in its native
    # queue.  The queue header and its ``↳`` body are input state, not an
    # assistant reply marker.  Likewise, the composer footer describes the
    # editable draft and must never turn an unsubmitted paste into proof.
    if _CODEX_NATIVE_QUEUE_HEADER_RE.match(stripped) or stripped.startswith("↳"):
        return False
    if stripped.lower().startswith(("tab to queue message", "shift+tab to queue message")):
        return False
    # Any "• "-prefixed line is a codex assistant/tool marker (incl. a plain
    # text reply like "• Ack.").  A bare divider is persistent TUI chrome and
    # is intentionally not evidence: it can sit below an editable composer.
    if stripped.startswith("• "):
        return True
    return stripped.startswith("⎿")


_CODEX_NATIVE_QUEUE_HEADER_RE = re.compile(
    r"^messages to be submitted after next tool call\b", re.IGNORECASE,
)


def _codex_native_queue_snapshot(pane_text: str) -> tuple[str, ...]:
    """Return the visible native queued-message region as stable markers."""
    lines = pane_text.splitlines()
    markers: list[str] = []
    in_queue = False
    for line in lines:
        stripped = line.strip()
        if _CODEX_NATIVE_QUEUE_HEADER_RE.match(stripped):
            in_queue = True
            markers.append(stripped)
            continue
        if not in_queue:
            continue
        if _codex_input_line(line):
            in_queue = False
            continue
        if stripped:
            markers.append(stripped)
    return tuple(markers)


def _codex_prompt_in_native_queue(pane_text: str, prompt: str) -> bool:
    """True when the exact prompt is in Codex's native queued region."""
    lines = pane_text.splitlines()
    for index, line in enumerate(lines):
        if not _CODEX_NATIVE_QUEUE_HEADER_RE.match(line.strip()):
            continue
        queued: list[str] = []
        for candidate in lines[index + 1:]:
            if _codex_input_line(candidate):
                break
            queued.append(candidate)
        if _codex_prompt_in_lines(queued, prompt):
            return True
    return False


def codex_prompt_submitted(pane_text: str, prompt: str) -> bool:
    """True once `prompt` has been SUBMITTED to a codex TUI (in history, or a
    reply/tool marker follows it), not merely pasted into the bottom draft."""
    if "API Error:" in pane_text:
        return True
    lines = pane_text.splitlines()
    input_start = _last_codex_input_start(lines)
    history_lines = lines if input_start is None else lines[:input_start]
    if _codex_prompt_in_lines(history_lines, prompt):
        return True
    # A prompt can be submitted to Codex's native working-turn queue before
    # any assistant marker or model output exists.  This region is distinct
    # from the editable bottom composer and is the positive discriminator for
    # the corresponding synthetic shape.
    if _codex_prompt_in_native_queue(pane_text, prompt):
        return True
    if input_start is None:
        return False
    if not _codex_prompt_in_lines(lines[input_start:], prompt):
        return False
    return any(_codex_non_draft_line_after_input(line) for line in lines[input_start + 1:])


def _codex_active_draft_has_collapsed_paste(lines: list[str], input_start: int) -> bool:
    return any(
        re.search(r"\[Pasted Content \d+ chars\]", line)
        for line in lines[input_start:]
    )


def _codex_composer_lines(lines: list[str], input_start: int) -> list[str]:
    """Limit draft matching to the editable composer, before native queue UI."""
    tail = lines[input_start:]
    for index, line in enumerate(tail):
        if _CODEX_NATIVE_QUEUE_HEADER_RE.match(line.strip()):
            return tail[:index]
    return tail


def _codex_collapsed_paste_matches_prompt(line: str, prompt: str) -> bool:
    match = re.search(r"\[Pasted Content (?P<count>\d+) chars\]", line)
    return match is not None and int(match.group("count")) == len(prompt)


def codex_prompt_in_active_draft(
    pane_text: str, prompt: str, *, exact: bool = False,
) -> bool:
    """True when `prompt` sits UNSUBMITTED in codex's bottom input draft
    (adapted from the public baseline: brief in the input-and-after region, no reply marker
    after it). A collapsed paste in the draft counts — the brief's text is
    hidden behind `[Pasted Content N chars]`, so a text match would miss it and
    wrongly read the unsubmitted draft as gone (symmetric with the claude
    branch; without it a collapsed codex brief false-confirms via the
    receipt's placeholder-not-in-draft tier)."""
    if codex_reset_interstitial_visible(pane_text):
        return False
    lines = pane_text.splitlines()
    input_start = _last_codex_input_start(lines)
    if input_start is None:
        return False
    composer_lines = _codex_composer_lines(lines, input_start)
    if exact:
        if any(_codex_collapsed_paste_matches_prompt(line, prompt) for line in composer_lines):
            return True
    elif any(re.search(r"\[Pasted Content \d+ chars\]", line) for line in composer_lines):
        return True
    if not _codex_prompt_in_lines(composer_lines, prompt):
        return False
    return not any(_codex_non_draft_line_after_input(line) for line in composer_lines[1:])


def _codex_draft_chrome_line(line: str) -> bool:
    """Return true for known non-content lines below an editable Codex draft."""
    stripped = line.strip()
    lowered = stripped.casefold()
    if not stripped:
        return True
    if _CODEX_NATIVE_QUEUE_HEADER_RE.match(stripped):
        return True
    if lowered.startswith(("tab to queue message", "shift+tab to queue message")):
        return True
    # The model/effort footer is rendered below the composer and is not part of
    # the message.  Restrict this to the stable Codex footer prefix; a prompt
    # containing arbitrary text must still compare as a whole below.
    if lowered.startswith("gpt-"):
        return True
    # A divider is persistent TUI chrome, not draft content.
    return not any(character.isalnum() for character in stripped)


def codex_prompt_exactly_in_active_draft(pane_text: str, prompt: str) -> bool:
    """Prove that the complete prompt is the current unsubmitted draft.

    ``codex_prompt_in_active_draft(..., exact=True)`` intentionally accepts a
    prompt substring because it is the legacy ordinary-send recovery seam.  An
    initial staged pointer needs a stricter predicate: a later tell appended to
    the pointer must *not* look like the pointer alone.  Compare the entire
    editable composer, tolerating only terminal wrapping and known footer
    chrome.  Collapsed-paste placeholders are rejected because their contents
    are not visibly exact.
    """
    if codex_reset_interstitial_visible(pane_text):
        return False
    expected = re.sub(r"\s+", " ", str(prompt or "")).strip()
    if not expected:
        return False
    lines = pane_text.splitlines()
    input_start = _last_codex_input_start(lines)
    if input_start is None:
        return False
    composer_lines = _codex_composer_lines(lines, input_start)
    if any(_codex_active_draft_has_collapsed_paste([line], 0) for line in composer_lines):
        return False

    content: list[str] = []
    for index, line in enumerate(composer_lines):
        stripped = line.strip()
        if _CODEX_NATIVE_QUEUE_HEADER_RE.match(stripped):
            break
        if index == 0:
            part = _strip_codex_input_marker(line)
            if not part:
                return False
            content.append(part)
            continue
        if _codex_non_draft_line_after_input(line):
            return False
        if _codex_draft_chrome_line(line):
            break
        part = _strip_codex_input_marker(line)
        if part:
            content.append(part)
    actual = re.sub(r"\s+", " ", " ".join(content)).strip()
    if not actual:
        return False
    # A terminal may insert a space at a hard wrap (including immediately after
    # a hyphen) but it may not delete a source boundary or append a follow-up.
    layout_pattern = "".join(
        r"\s+" if character.isspace() else rf"{re.escape(character)}\s*"
        for character in expected
    )
    return re.fullmatch(layout_pattern, actual) is not None


#: Submission proof (`spawn.ok` requires it for a real provider TUI) and its
#: negative (proof the brief is still an unsubmitted draft → re-paste once).
SUBMIT_PREDICATES = {
    "claude": claude_prompt_submitted,
    "codex": codex_prompt_submitted,
}

_CLAUDE_SUBMISSION_MARKERS = ("✻ ", "✶ ", "✳ ", "⏺ ", "⎿ ", "● ")


def submission_marker_count(pane_text: str, provider: str) -> int:
    """Count provider output markers for the pinned post-paste boundary."""
    return len(_submission_marker_snapshot(pane_text, provider))


def _submission_marker_snapshot(pane_text: str, provider: str) -> tuple[str, ...]:
    lines = pane_text.splitlines()
    if provider == "claude":
        input_start = _last_claude_input_start(lines)
        history = lines if input_start is None else lines[:input_start]
        return tuple(line.strip() for line in history if line.strip().startswith(_CLAUDE_SUBMISSION_MARKERS))
    if provider == "codex":
        return (
            tuple(line.strip() for line in lines if _codex_non_draft_line_after_input(line))
            + _codex_native_queue_snapshot(pane_text)
        )
    return ()


def submission_proven_after(pane_text: str, baseline: str, prompt: str, provider: str) -> bool:
    """Require new provider evidence after the exact pre-paste baseline."""
    if "API Error:" in pane_text and "API Error:" not in baseline:
        return True
    # A stale history/body marker must not win over the current editable
    # composer.  This veto is deliberately checked before marker deltas so a
    # row-5985-shaped pane cannot false-confirm merely because the same text
    # appeared in an earlier turn.
    draft_predicate = DRAFT_PREDICATES.get(provider)
    if draft_predicate is not None and draft_predicate(pane_text, prompt):
        return False
    predicate = SUBMIT_PREDICATES.get(provider)
    if predicate is None or not predicate(pane_text, prompt):
        return False
    after_markers = _submission_marker_snapshot(pane_text, provider)
    baseline_markers = _submission_marker_snapshot(baseline, provider)
    if provider == "claude":
        return after_markers != baseline_markers
    return len(after_markers) > len(baseline_markers)
DRAFT_PREDICATES = {
    "claude": claude_prompt_in_active_draft,
    "codex": codex_prompt_in_active_draft,
}

# NOTE: there are deliberately NO pre-paste delivery-readiness predicates here.
# A short-lived pair (`claude_draft_occupied` / `pane_ready_for_delivery`) gated
# tell/send on the recipient's draft and turn state; claude renders a greyed
# placeholder suggestion ON its caret line at idle, `capture-pane` returns it as
# plain text, so the gate read every idle pane as draft-occupied and held every
# tell forever. Tells ALWAYS submit — the TUIs queue mid-turn input natively.
# The predicates above are submission EVIDENCE, gathered after the paste; they
# must never become a precondition for it.

