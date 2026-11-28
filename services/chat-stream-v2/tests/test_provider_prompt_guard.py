"""Exercise daemon delivery against approval frames with a recording transport."""

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import comms as comms_module
import tmux_transport
from comms import Comms
from outbound_notices import OutboundNoticeQueue
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from store import Store
from test_codex_reset_consumption_guard import RESET_INTERSTITIAL
from test_spawn_error_no_phantom_row import BootReadyTmux
from tmux_transport import Tmux


HOST = "localhost"
NAME = "approval-guard-target"
TARGET = f"{HOST}:{NAME}"
BODY = "Ordinary message for the composer"
CODE = "provider_prompt_waiting"
FIXTURES = Path(__file__).with_name("fixtures")

APPROVALS = [
    pytest.param("claude", "claude_bash", id="claude-bash"),
    pytest.param("claude", "claude_edit", id="claude-edit"),
    pytest.param("claude", "claude_plan", id="claude-plan"),
    pytest.param("codex", "codex_command", id="codex-command"),
    pytest.param("codex", "codex_command_overflow", id="codex-command-overflow"),
    pytest.param("codex", "codex_edit", id="codex-edit"),
]
TRUST = "Do you trust the contents of this directory?\n1. Yes, continue\n2. No, exit\nPress enter to continue"
CLAUDE_COMPOSER = "❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle)"
CODEX_COMPOSER = "› Ask Codex to do anything\n  gpt-6.2-sol high"


def frame(name):
    return (FIXTURES / f"approval_{name}.txt").read_text()


class RecordingTmux(BootReadyTmux, Tmux):
    def __init__(self, *, screen="", after_paste=None):
        BootReadyTmux.__init__(self)
        Tmux.__init__(self)
        self.screen = screen
        self.after_paste = after_paste
        self.capture_fails = False
        self.actions = []
        self.body = ""

    async def capture(self, name, **kwargs):
        return await Tmux.capture(self, name, **kwargs)

    async def paste(self, name, text, **kwargs):
        self.body = text
        return await Tmux.paste(self, name, text, **kwargs)

    async def run(self, *args, **kwargs):
        command = args[0]
        if command == "capture-pane":
            return (1, "test capture failure") if self.capture_fails else (0, self.screen)
        if command == "display-message":
            return 0, "0"
        if command == "paste-buffer":
            self.actions.append("paste")
            if self.after_paste is not None:
                self.screen = self.after_paste
        if command == "send-keys":
            self.actions.append(args[-1])
            self.screen = f"READY\n⏺ {self.body}\n{CLAUDE_COMPOSER}"
        return 0, ""


@pytest.fixture(autouse=True)
def immediate_transport_ticks(monkeypatch):
    async def settle(_delay):
        pass

    monkeypatch.setattr(tmux_transport.asyncio, "sleep", settle)
    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_TIMEOUT_S", 0)


@asynccontextmanager
async def delivery(tmux, provider):
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    try:
        if provider is not None:
            await sessions.open(HOST, NAME, provider=provider)
        spawnctl = SpawnCtl(store, sessions, tmux=tmux)
        yield store, Comms(store, sessions, spawnctl), spawnctl
    finally:
        store.stop()


def message(**fields):
    return {"stream_id": TARGET, "message": BODY, **fields}


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "fixture"), APPROVALS)
async def test_prompt_before_paste_sends_nothing(provider, fixture):
    tmux = RecordingTmux(screen=frame(fixture))
    async with delivery(tmux, provider) as (_, comms, _):
        with pytest.raises(VerbError) as error:
            await comms.tell(message())
    assert error.value.code == CODE
    assert error.value.extra["phase"] == "not_started"
    assert tmux.actions == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "fixture"), APPROVALS)
async def test_prompt_after_paste_sends_no_enter(provider, fixture):
    tmux = RecordingTmux(after_paste=frame(fixture))
    with pytest.raises(VerbError) as error:
        await tmux.paste(NAME, "Yes")  # The selected option is not an active draft.
    assert error.value.code == CODE
    assert error.value.extra["phase"] == "body_maybe_pasted"
    assert tmux.actions == ["paste"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "fixture"), APPROVALS)
async def test_enter_retry_refused_on_prompt(provider, fixture, monkeypatch):
    tmux = RecordingTmux()

    async def prompt_during_proof(*args, **kwargs):
        tmux.screen = frame(fixture)
        return False

    monkeypatch.setattr(Comms, "_submission_evidence", prompt_during_proof)
    async with delivery(tmux, provider) as (_, comms, _):
        reply = await comms.send(message())
    assert reply["reason"] == CODE
    assert reply["delivery"] == "committed_pending_proof"
    assert tmux.actions == ["paste", "Enter"]


@pytest.mark.asyncio
async def test_refused_tell_leaves_no_custody_row():
    tmux = RecordingTmux(screen=frame("claude_bash"))
    async with delivery(tmux, "claude") as (store, comms, _):
        request = message(tell_id="approval-guard-retry")
        with pytest.raises(VerbError, match="nothing was sent"):
            await comms.tell(request)
        assert await store.get_tell_delivery(request["tell_id"]) is None
        tmux.screen = CLAUDE_COMPOSER
        assert (await comms.tell(request))["delivery_status"] == "delivered"
        assert (await comms.tell(request))["duplicate"] is True
    assert tmux.actions == ["paste", "Enter"]


@pytest.mark.asyncio
async def test_send_enter_retry_refusal_reports_committed_not_not_landed(monkeypatch):
    tmux = RecordingTmux(screen=CODEX_COMPOSER)

    async def prompt_during_proof(*args, **kwargs):
        tmux.screen = frame("codex_command")
        return False

    monkeypatch.setattr(Comms, "_submission_evidence", prompt_during_proof)
    async with delivery(tmux, "codex") as (_, comms, _):
        result = await comms.send(message(request_id="approval-guard-enter"))
    assert result["delivery"] == "committed_pending_proof"
    assert result["action_committed"] is True
    assert result["do_not_resubmit"] is True
    assert result["reason"] == CODE
    assert tmux.actions == ["paste", "Enter"]


@pytest.mark.asyncio
@pytest.mark.parametrize("composer", [CLAUDE_COMPOSER, CODEX_COMPOSER, frame("claude_draft_before"), frame("claude_draft_after")])
async def test_quoted_prompt_in_scrollback_does_not_block(composer):
    tmux = RecordingTmux(after_paste=f"{frame('claude_bash')}\n{composer}")
    await tmux.paste(NAME, BODY)
    assert tmux.actions == ["paste", "Enter"]


@pytest.mark.asyncio
async def test_draft_visible_overrides_prompt_at_enter():
    # Both pieces of chrome can appear in a transitional capture. The current
    # draft is below the selected options and above their stale footer.
    prompt, footer = frame("claude_bash").strip().rsplit("\n", 1)
    tmux = RecordingTmux(after_paste=f"{prompt}\n❯ {BODY}\n{footer}")
    await tmux.paste(NAME, BODY)
    assert tmux.actions == ["paste", "Enter"]


@pytest.mark.asyncio
async def test_capture_failure_does_not_block(caplog):
    tmux = RecordingTmux()
    tmux.capture_fails = True
    await tmux.paste(NAME, BODY)
    assert tmux.actions == ["paste", "Enter"]
    assert "pre_enter_capture_failed code=tmux_capture_failed" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("screen", [TRUST, RESET_INTERSTITIAL])
async def test_trust_chrome_and_reset_menu_are_not_prompts(screen):
    tmux = RecordingTmux(after_paste=screen)
    await tmux.paste(NAME, BODY)
    assert tmux.actions == ["paste", "Enter"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt_before_paste", [True, False])
async def test_notice_defers_without_attempt_on_prompt(prompt_before_paste):
    prompt = frame("claude_bash")
    tmux = RecordingTmux(
        screen=prompt if prompt_before_paste else CLAUDE_COMPOSER,
        after_paste=None if prompt_before_paste else prompt,
    )
    async with delivery(tmux, "claude") as (store, comms, _):
        queue = OutboundNoticeQueue(store, comms)
        row = await queue.enqueue(
            kind="test", dedupe_key="approval-guard-notice", recipient_stream_id=TARGET,
            tell_id="approval-guard-notice", body=BODY, source_stream_id=f"{HOST}:notice-source",
        )
        await queue.deliver_now(row["notice_id"])
        saved = await store.submit(lambda conn: dict(conn.execute(
            "SELECT * FROM v2_outbound_notices WHERE notice_id=?", (row["notice_id"],),
        ).fetchone()))
        custody = await store.get_tell_delivery("approval-guard-notice")
    assert saved["attempts"] == (0 if prompt_before_paste else 1)
    assert CODE in saved["last_error"]
    assert (custody is None) is prompt_before_paste
    assert tmux.actions == ([] if prompt_before_paste else ["paste"])


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt_before_paste", [True, False])
async def test_tell_and_send_report_prompt_waiting_not_delivered(prompt_before_paste):
    prompt = frame("claude_bash")
    for verb in ("tell", "send"):
        tmux = RecordingTmux(screen=prompt if prompt_before_paste else "", after_paste=prompt)
        async with delivery(tmux, "claude") as (_, comms, _):
            deliver = getattr(comms, verb)
            if prompt_before_paste and verb == "tell":
                with pytest.raises(VerbError, match="nothing was sent") as error:
                    await deliver(message())
                assert error.value.code == CODE
                continue
            result = await deliver(message())
        assert result["reason"] == CODE
        if prompt_before_paste:
            assert result["delivery"] == "not_landed"
            assert tmux.actions == []
        else:
            assert result.get("delivery_status", result.get("delivery")) == (
                "pasted_unsubmitted" if verb == "tell" else "committed_pending_proof"
            )
            assert result["do_not_resubmit"] is True
            assert tmux.actions == ["paste"]
            if verb == "tell":
                assert "may have been discarded" in result["retry_guidance"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt_before_paste", [True, False])
async def test_refused_spawn_brief_records_not_delivered_and_never_repastes(prompt_before_paste):
    prompt = frame("claude_bash")
    tmux = RecordingTmux(
        screen=f"READY\n{prompt}" if prompt_before_paste else "READY",
        after_paste=None if prompt_before_paste else prompt,
    )
    async with delivery(tmux, None) as (store, _, spawnctl):
        with pytest.raises(VerbError) as error:
            await spawnctl.spawn({
                "session_name": NAME, "request_id": "approval-guard-spawn",
                "objective": "Exercise spawn guard", "command": "run", "prompt": BODY,
            }, HOST)
        assert error.value.code == CODE
        outcome = await store.get_spawn_outcome(HOST, NAME)
    assert outcome["state"] != "delivered"
    assert outcome["delivery_receipt"]["failure_code"] == CODE
    assert tmux.actions == ([] if prompt_before_paste else ["paste"])
