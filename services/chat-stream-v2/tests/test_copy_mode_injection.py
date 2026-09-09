"""Regression coverage for tmux copy-mode send injection."""

from __future__ import annotations

import tmux_transport

import asyncio

import pytest

import comms as comms_module
import spawnctl as spawnctl_module
from comms import Comms
from sessions import Sessions
from spawnctl import SpawnCtl
from tmux_transport import Tmux
from store import Store


HOST = "localhost"
NAME = "copy-mode-target"
BODY = "copy-mode delivery probe"


class ModeTmux(Tmux):
    def __init__(self, *, cancel_succeeds: bool) -> None:
        super().__init__()
        self.cancel_succeeds = cancel_succeeds
        self.in_mode = True
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *args: str, **_kwargs: object) -> tuple[int, str]:
        self.calls.append(args)
        if args[0] == "display-message":
            return 0, f"{int(self.in_mode)} {'copy-mode' if self.in_mode else ''}\n"
        if args[0] == "send-keys" and "-X" in args:
            if self.cancel_succeeds:
                self.in_mode = False
            return 0, ""
        return 0, ""


def test_tmux_paste_cancels_copy_mode_before_paste_and_enter(caplog: pytest.LogCaptureFixture) -> None:
    tmux = ModeTmux(cancel_succeeds=True)

    asyncio.run(tmux.paste(NAME, BODY))

    commands = [call[0] for call in tmux.calls]
    assert commands[:3] == ["display-message", "send-keys", "display-message"]
    assert commands.index("paste-buffer") > commands.index("display-message", 2)
    assert commands.count("paste-buffer") == 1
    assert "pane_mode_cancelled stream=copy-mode-target mode=copy-mode" in caplog.text


def test_tmux_paste_still_submits_when_copy_mode_cannot_cancel() -> None:
    tmux = ModeTmux(cancel_succeeds=False)

    reason = asyncio.run(tmux.paste(NAME, BODY))

    assert reason == "pane_in_mode"
    assert "paste-buffer" in [call[0] for call in tmux.calls]
    assert tmux.calls[-1] == ("send-keys", "-t", "=copy-mode-target:", "Enter")


def test_tmux_paste_settles_before_enter(monkeypatch: pytest.MonkeyPatch) -> None:
    tmux = ModeTmux(cancel_succeeds=True)
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(spawnctl_module.asyncio, "sleep", record_sleep)
    asyncio.run(tmux.paste(NAME, BODY))

    assert sleeps == [tmux_transport.POLL_INTERVAL_S]
    assert tmux.calls[-1] == ("send-keys", "-t", "=copy-mode-target:", "Enter")


def test_tmux_enter_cancels_copy_mode_before_submitting() -> None:
    tmux = ModeTmux(cancel_succeeds=True)

    asyncio.run(tmux.send_enter(NAME))

    commands = [call[0] for call in tmux.calls]
    assert commands == ["display-message", "send-keys", "display-message", "send-keys"]


class ClaudeDraftTmux:
    def __init__(self) -> None:
        self.pastes: list[str] = []
        self.enters = 0
        self.screen = f"⏵⏵ bypass permissions on\n❯ {BODY}"

    async def capture(self, _name: str) -> str:
        return self.screen

    async def paste(self, _name: str, text: str) -> None:
        self.pastes.append(text)

    async def send_enter(self, _name: str) -> None:
        self.enters += 1

    async def run(self, *_args: str, **_kwargs: object) -> tuple[int, str]:
        return 0, ""


class CopyModeClaudeTmux(Tmux):
    def __init__(self, *, cancel_succeeds: bool = True) -> None:
        super().__init__()
        self.cancel_succeeds = cancel_succeeds
        self.in_mode = True
        self.paste_buffers = 0
        self.screen = "⏵⏵ bypass permissions on\n❯ "

    async def capture(self, _name: str) -> str:
        return self.screen

    async def run(self, *args: str, **_kwargs: object) -> tuple[int, str]:
        if args[0] == "display-message":
            return 0, f"{int(self.in_mode)} {'copy-mode' if self.in_mode else ''}\n"
        if args[0] == "send-keys" and "-X" in args:
            if self.cancel_succeeds:
                self.in_mode = False
            return 0, ""
        if args[0] == "paste-buffer":
            self.paste_buffers += 1
            return 0, ""
        if args[0] == "send-keys" and args[-1] == "Enter":
            if not self.in_mode:
                self.screen = f"⏺ {BODY}\n❯ "
        return 0, ""


def test_send_cancels_copy_mode_once_then_confirms_submission(tmp_path) -> None:
    tmux = CopyModeClaudeTmux()
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=tmux), attachment_root=tmp_path)
    try:
        async def go() -> dict[str, object]:
            await sessions.open(HOST, NAME, provider="claude")
            return await comms.send({"stream_id": f"{HOST}:{NAME}", "message": BODY})

        result = asyncio.run(go())
        assert result["delivery"] == "landed"
        assert tmux.paste_buffers == 1
    finally:
        store.stop()


def test_send_reports_pane_mode_after_attempting_submission(tmp_path) -> None:
    tmux = CopyModeClaudeTmux(cancel_succeeds=False)
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=tmux), attachment_root=tmp_path)
    try:
        async def go() -> dict[str, object]:
            await sessions.open(HOST, NAME, provider="claude")
            return await comms.send({"stream_id": f"{HOST}:{NAME}", "message": BODY})

        result = asyncio.run(go())
        assert result["delivery"] == "not_landed"
        assert result["reason"] == "pane_in_mode"
        assert result["attempts"] == 1
        assert tmux.paste_buffers == 1
    finally:
        store.stop()


def test_claude_active_draft_redelivery_is_enter_only_and_reports_reason(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_POLL_S", 0.001)
    tmux = ClaudeDraftTmux()
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=tmux), attachment_root=tmp_path)
    try:
        async def go() -> dict[str, object]:
            await sessions.open(HOST, NAME, provider="claude")
            return await comms.send({"stream_id": f"{HOST}:{NAME}", "message": BODY})

        result = asyncio.run(go())
        assert result["delivery"] == "not_landed"
        assert result["reason"] == "active_draft"
        assert tmux.pastes == [BODY]
        assert tmux.enters == 1
    finally:
        store.stop()
