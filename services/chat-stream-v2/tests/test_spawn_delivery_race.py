"""The racy initial-prompt loss: a spawn brief pasted but never submitted.

The bracketed paste can land in a provider input draft while the Enter key is
dropped. A pane echo proves the paste landed, but not that the brief was
submitted.

The current authority is one exact, post-watermark durable USER event. Pane
chrome and transcripts remain useful diagnostics but cannot confirm delivery
or authorize an Enter retry.

Each test drives `_confirm_brief_delivery` and guards against confirming on a
bare echo.
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
from boot_ready import claude_prompt_submitted  # noqa: E402
from sessions import Sessions  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from tmux_transport import receipt_needle
from store import Store  # noqa: E402

HOST = "localhost"
BRIEF = "deliver this brief now"
CHROME = "⏵⏵ bypass permissions on (bypass)\n❯ "
#: The Enter-lost pane: the brief echoes in the input draft, no reply marker.
UNSUBMITTED = "⏵⏵ bypass permissions on (bypass)\n❯ deliver this brief now"
#: After the Enter retry submits it: a Claude reply marker follows in history.
SUBMITTED = "⏺ deliver this brief now\n❯ "


class EnterLostTmux:
    """A claude pane where the first paste lands but no USER event follows."""

    def __init__(self, *, recovers: bool) -> None:
        self._recovers = recovers
        self.pastes = 0
        self.enters = 0
        self.alive = True

    async def has_session(self, name: str) -> bool:
        return self.alive

    async def session_state(self, name: str) -> str:
        return "alive" if self.alive else "gone"

    async def pane_pid(self, name: str) -> str:
        return ""  # no pid -> transcript probe yields no_transcript (remote-style)

    async def capture(self, name: str) -> str:
        if self.pastes == 0:
            return CHROME
        return SUBMITTED if self.enters and self._recovers else UNSUBMITTED

    async def paste(self, name: str, text: str) -> None:
        self.pastes += 1

    async def send_enter(self, name: str) -> None:
        self.enters += 1


def _ctl(tmux) -> SpawnCtl:
    store = Store(":memory:")
    return SpawnCtl(store, Sessions(store, tmux=tmux, local_host=HOST), tmux=tmux)


def test_echo_is_not_submission_proof() -> None:
    """The premise: the unsubmitted draft echoes the brief (the old pane-echo
    receipt would confirm) yet is NOT submitted."""
    needle = receipt_needle(BRIEF)
    tmux = EnterLostTmux(recovers=True)
    tmux.pastes = 1  # the brief echoes in the draft (Enter lost)
    ctl = _ctl(tmux)
    echoed = asyncio.run(ctl._await_marker("s", needle, 0.2, since=CHROME))
    assert echoed is True, "the draft echo is visible — the old receipt confirmed on this"
    assert claude_prompt_submitted(UNSUBMITTED, BRIEF) is False, "but it was never submitted"


def test_enter_lost_pane_does_not_authorize_retry_or_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    """Editable pane chrome is advisory and cannot authorize another input."""
    monkeypatch.setattr(spawnctl_mod, "RESUBMIT_GRACE_S", 0.2)
    tmux = EnterLostTmux(recovers=True)
    tmux.pastes = 1  # the initial `_spawn_fenced` paste already landed (Enter lost)
    ctl = _ctl(tmux)
    confirmed = asyncio.run(ctl._confirm_brief_delivery("s", BRIEF, CHROME, "claude", tmux))
    assert confirmed is False
    assert tmux.pastes == 1, "the initial prompt body must never be pasted twice"
    assert tmux.enters == 0


def test_never_submitted_fails_honestly_and_bounds_the_enter_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a USER event, fail honestly without pane-driven input."""
    monkeypatch.setattr(spawnctl_mod, "RESUBMIT_GRACE_S", 0.2)
    monkeypatch.setattr(tmux_transport, "RECEIPT_TIMEOUT_S", 1.0)
    tmux = EnterLostTmux(recovers=False)
    tmux.pastes = 1
    ctl = _ctl(tmux)
    confirmed = asyncio.run(ctl._confirm_brief_delivery("s", BRIEF, CHROME, "claude", tmux))
    assert confirmed is False
    assert tmux.pastes == 1, "the failed recovery must not duplicate the prompt"
    assert tmux.enters == 0


class SlowRemoteReceiptTmux(EnterLostTmux):
    """SSH receipt appears after the local deadline but within the remote one."""

    ssh_target = "hostb"

    def __init__(self) -> None:
        super().__init__(recovers=False)
        self.started = 0.0

    async def capture(self, name: str) -> str:
        if not self.started:
            self.started = asyncio.get_running_loop().time()
        if asyncio.get_running_loop().time() - self.started >= 0.035:
            return SUBMITTED
        return UNSUBMITTED


def test_slow_cross_host_pane_receipt_is_not_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later remote pane repaint is not authoritative submission proof."""
    monkeypatch.setattr(tmux_transport, "RECEIPT_TIMEOUT_S", 0.02)
    monkeypatch.setattr(spawnctl_mod, "RESUBMIT_GRACE_S", 0.005)
    monkeypatch.setattr(tmux_transport, "POLL_INTERVAL_S", 0.001)
    tmux = SlowRemoteReceiptTmux()
    tmux.pastes = 1
    ctl = _ctl(tmux)

    async def no_transcript(*_args: object, **_kwargs: object) -> str:
        return "no_transcript"

    monkeypatch.setattr(ctl, "_transcript_status", no_transcript)
    confirmed = asyncio.run(
        ctl._confirm_brief_delivery(
            "s", BRIEF, CHROME, "claude", tmux, host="hostb",
        )
    )

    assert confirmed is False
    assert tmux.enters == 0


class TranscriptRaceTmux:
    """A LOCAL claude pane whose viewport only ever shows the collapsed-paste
    placeholder (viewport blind), backed by a real held-open `.jsonl`. The brief
    is absent from the log until the Enter retry writes it — the durable-evidence
    twin of the viewport race."""

    def __init__(self, transcript, needle: str) -> None:
        self._transcript = transcript
        self._needle = needle
        self.pastes = 0
        self.enters = 0
        self.alive = True

    async def has_session(self, name: str) -> bool:
        return self.alive

    async def session_state(self, name: str) -> str:
        return "alive" if self.alive else "gone"

    async def pane_pid(self, name: str) -> str:
        return str(os.getpid())

    async def capture(self, name: str) -> str:
        return "❯ [Pasted text #1 +9 lines]\n⏵⏵ bypass permissions on (bypass)"

    async def paste(self, name: str, text: str) -> None:
        self.pastes += 1

    async def send_enter(self, name: str) -> None:
        self.enters += 1
        self._transcript.write(json.dumps({"type": "user", "message": {"content": BRIEF}}) + "\n")
        self._transcript.flush()


class CollapsedInHistoryTmux:
    """A long-brief spawn whose paste collapsed and has SINCE been submitted: the
    `[Pasted text #N]` placeholder now sits in history above an empty caret, with
    a reply marker. No transcript is locatable in this fixture — the viewport
    alone must prove submission."""

    async def has_session(self, name: str) -> bool:
        return True

    async def session_state(self, name: str) -> str:
        return "alive"

    async def pane_pid(self, name: str) -> str:
        return ""  # transcript unlocatable -> the confirm cannot lean on it

    async def capture(self, name: str) -> str:
        return "⏺ [Pasted text #1 +212 lines]\n\n✻ Working…\n❯ "


def test_collapsed_viewport_is_not_authoritative_without_user_event() -> None:
    """A collapsed placeholder outside the draft remains advisory."""
    long_brief = "line one of the brief\n" + "\n".join(f"ctx {i}" for i in range(212))
    tmux = CollapsedInHistoryTmux()
    ctl = _ctl(tmux)
    confirmed = asyncio.run(ctl._confirm_brief_delivery("s", long_brief, "", "claude", tmux))
    assert confirmed is False


class CodexCollapsedEnterLostTmux:
    """A codex spawn whose long brief collapsed to `[Pasted Content N chars]` and
    whose Enter was lost — the placeholder sits in the composer draft, never
    submitted, no transcript locatable. The collapsed brief's text is NOT on
    screen, so a text-only draft check is blind to it; the receipt must still
    refuse to confirm this idle pane."""

    async def has_session(self, name: str) -> bool:
        return True

    async def session_state(self, name: str) -> str:
        return "alive"

    async def pane_pid(self, name: str) -> str:
        return ""

    async def capture(self, name: str) -> str:
        return "OpenAI Codex\n─────────\n› [Pasted Content 2148 chars]\n  gpt-5-codex high"

    async def paste(self, name: str, text: str) -> None:
        pass

    async def send_enter(self, name: str) -> None:
        pass

    async def run(self, *args: str, **kw) -> tuple[int, str]:
        return 0, ""


def test_codex_collapsed_enter_lost_is_not_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant A for codex: a collapsed brief sitting unsubmitted in the
    composer must never confirm as `delivered`. The receipt must distinguish a
    collapsed placeholder from an idle draft."""
    monkeypatch.setattr(tmux_transport, "RECEIPT_TIMEOUT_S", 0.6)
    monkeypatch.setattr(spawnctl_mod, "RESUBMIT_GRACE_S", 0.2)
    long_brief = "do the codex work\n" + "\n".join(f"ctx {i}" for i in range(300))
    tmux = CodexCollapsedEnterLostTmux()
    ctl = _ctl(tmux)
    confirmed = asyncio.run(ctl._confirm_brief_delivery("s", long_brief, "", "codex", tmux))
    assert confirmed is False


def test_transcript_change_does_not_authorize_enter_or_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transcript and collapsed-pane changes are advisory and input-free."""
    if shutil.which("lsof") is None or shutil.which("ps") is None:
        pytest.skip("transcript probe needs lsof + ps")
    monkeypatch.setattr(spawnctl_mod, "RESUBMIT_GRACE_S", 0.2)
    log = tmp_path / ".claude" / "projects" / "p" / "s.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({"type": "user", "message": {"content": "boot only"}}) + "\n")
    fh = open(log, "a")  # noqa: SIM115 - held open for the probe's lifetime
    try:
        tmux = TranscriptRaceTmux(fh, receipt_needle(BRIEF))
        tmux.pastes = 1  # initial paste landed, collapsed, unsubmitted
        ctl = _ctl(tmux)
        confirmed = asyncio.run(ctl._confirm_brief_delivery("s", BRIEF, "", "claude", tmux))
        assert confirmed is False
        assert tmux.pastes == 1 and tmux.enters == 0
    finally:
        fh.close()


class StaleStagedPointerTmux:
    """A stale history echo plus a fresh, still-editable pointer draft."""

    def __init__(self, pane: str) -> None:
        self.pane = pane
        self.pastes: list[str] = []
        self.enters = 0

    async def has_session(self, _name: str) -> bool:
        return True

    async def session_state(self, _name: str) -> str:
        return "ready"

    async def capture(self, _name: str) -> str:
        return self.pane

    async def paste(self, _name: str, text: str) -> None:
        self.pastes.append(text)

    async def send_enter(self, _name: str) -> None:
        self.enters += 1

    async def run(self, *_args: str, **_kwargs: str) -> tuple[int, str]:
        return 0, ""


def test_staged_pointer_receipt_rejects_stale_history_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale prior pointer plus the current draft is not submission proof."""
    pointer = (
        "Read /tmp/public-test"
        "example-pointer.txt "
        "and follow the complete prompt exactly."
    )
    before = f"OpenAI Codex\n─────────\n› {pointer}\n• old reply\n"
    pane = f"{before}› {pointer}\n"
    tmux = StaleStagedPointerTmux(pane)
    ctl = _ctl(tmux)

    async def no_transcript(*_args: object) -> str:
        return "no_transcript"

    monkeypatch.setattr(ctl, "_transcript_status", no_transcript)
    monkeypatch.setattr(tmux_transport, "RECEIPT_TIMEOUT_S", 0.02)
    monkeypatch.setattr(spawnctl_mod, "RESUBMIT_GRACE_S", 0.0)
    monkeypatch.setattr(tmux_transport, "POLL_INTERVAL_S", 0.001)

    asyncio.run(tmux.paste("s", pointer))
    confirmed = asyncio.run(
        ctl._confirm_brief_delivery("s", pointer, before, "codex", tmux)
    )

    assert confirmed is False
    assert tmux.pastes == [pointer]
    assert tmux.enters == 0
