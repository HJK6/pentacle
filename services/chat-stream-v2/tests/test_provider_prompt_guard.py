"""One-worker focused regression suite for Thoth with no daemon or pane I/O."""

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
    # The edit and plan frames are representative of the pinned CLI.
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
    async def settle(_delay, result=None):
        return result

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
        saved = await store.outbound_notice_for_dedupe("approval-guard-notice")
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
        request = {
            "session_name": NAME, "request_id": "approval-guard-spawn",
            "idempotency_key": "approval-guard-spawn",
            "objective": "Exercise spawn guard", "command": "run", "prompt": BODY,
        }
        with pytest.raises(VerbError) as error:
            await spawnctl.spawn(request, HOST)
        assert error.value.code == CODE
        outcome = await store.get_spawn_outcome(HOST, NAME)
        with pytest.raises(VerbError):
            await spawnctl.spawn(request, HOST)
    assert outcome["state"] != "delivered"
    assert outcome["delivery_receipt"]["failure_code"] == CODE
    assert tmux.actions == ([] if prompt_before_paste else ["paste"])


def mixed_claude_frame(draft=BODY):
    prompt, footer = frame("claude_bash").strip().rsplit("\n", 1)
    return f"{prompt}\n❯ {draft}\n{footer}"


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["wrapped-heading", "wrapped-footer", "focused-option-only"])
async def test_codex_narrow_or_short_approval_suppresses_enter(layout):
    # The pinned approval overlay wraps title/footer and its picker clips rows.
    # Transform the pinned capture at exactly those renderer boundaries.
    screen = frame("codex_command")
    if layout == "wrapped-heading":
        screen = screen.replace("following command?", "following\n  command?")
    elif layout == "wrapped-footer":
        screen = screen.replace("confirm or esc", "confirm\n  or esc")
    else:
        header = screen.split("\n›", 1)[0]
        screen = f"{header}\n› 1. Yes, proceed (y)\n↓\n  Press enter to confirm or esc to cancel"
    tmux = RecordingTmux(after_paste=screen)
    with pytest.raises(VerbError) as error:
        await tmux.paste(NAME, BODY)
    assert error.value.code == CODE
    assert tmux.actions == ["paste"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["", "gemini"])
@pytest.mark.parametrize("observed", ["approval", "unreadable"])
async def test_fallback_enter_retry_requires_a_clear_observation(provider, observed, monkeypatch):
    tmux = RecordingTmux()

    async def no_proof(*args, **kwargs):
        tmux.screen = frame("codex_command") if observed == "approval" else ""
        if (observation := kwargs.get("observation")) is not None:
            observation[:] = [tmux.screen]
        return False

    async with delivery(tmux, provider) as (_, comms, spawn):
        monkeypatch.setattr(spawn, "_await_marker", no_proof)
        monkeypatch.setattr(comms, "_submission_evidence", no_proof)
        reply = await comms.send(message())
    assert tmux.actions == ["paste", "Enter"]
    assert reply["delivery"] == "committed_pending_proof"
    assert reply["reason"] == (CODE if observed == "approval" else "enter_failed")
    assert reply["do_not_resubmit"] is True
    if observed == "approval":
        assert reply["phase"] == "enter_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["tell", "send"])
@pytest.mark.parametrize("draft", [BODY, f"1. {BODY}"])
async def test_stale_footer_above_live_composer_does_not_block_initial_paste(verb, draft):
    tmux = RecordingTmux(screen=mixed_claude_frame(draft))
    async with delivery(tmux, "claude") as (_, comms, _):
        reply = await getattr(comms, verb)(message())
    assert tmux.actions == ["paste", "Enter"]
    assert reply.get("delivery_status", reply.get("delivery")) in {"delivered", "landed"}


@pytest.mark.asyncio
async def test_tell_refused_retry_preserves_uncertain_first_enter(monkeypatch):
    tmux = RecordingTmux(screen=CODEX_COMPOSER)

    async def delayed_proof(*args, **kwargs):
        tmux.screen = frame("codex_command")
        return False

    monkeypatch.setattr(Comms, "_submission_evidence", delayed_proof)
    async with delivery(tmux, "codex") as (store, comms, _):
        request = message(tell_id="approval-guard-tell-retry")
        result = await comms.tell(request)
        stored = await store.get_tell_delivery(request["tell_id"])
        duplicate = await comms.tell(request)
    assert result["delivery_status"] == "committed_pending_proof"
    assert result["reason"] == CODE
    assert result["phase"] == "enter_failed"
    assert result["active_draft"] is False
    assert "not submitted" not in result["retry_guidance"]
    assert duplicate["duplicate"] is True
    assert stored["reply"]["phase"] == "enter_failed"
    assert tmux.actions == ["paste", "Enter"]


@pytest.mark.asyncio
async def test_generation_bound_tell_reports_refusal_with_existing_custody():
    tmux = RecordingTmux(after_paste=frame("codex_command"))
    async with delivery(tmux, "codex") as (store, comms, _):
        current = await store.fetch_session(HOST, NAME)
        request = message(
            tell_id="approval-guard-answer",
            _notification_answer_generation=current["session_generation"],
        )
        result = await comms.tell(request)
        row = await store.get_tell_delivery(request["tell_id"])
        duplicate = await comms.tell(request)
    assert result["delivery_status"] == "pasted_unsubmitted"
    assert result["reason"] == CODE
    assert result["phase"] == "body_maybe_pasted"
    assert result["ledger_row_id"] == row["reply"]["ledger_row_id"]
    assert result["do_not_resubmit"] is True
    assert row["reply"]["delivery_status"] == "proof_unavailable"
    assert duplicate["delivery_status"] == "proof_unavailable"
    assert duplicate["duplicate"] is True
    assert row["delivery"]["notification_answer_generation"] == current["session_generation"]
    assert tmux.actions == ["paste"]


@pytest.mark.asyncio
async def test_compact_prompt_after_paste_keeps_custody_and_logs_once(monkeypatch, caplog):
    from submission_events import EventProof

    tmux = RecordingTmux(
        screen="────────────────────────\n❯ \n────────────────────────\n  ⏵⏵ bypass permissions on (shift+tab to cycle)",
        after_paste=frame("claude_bash"),
    )
    async with delivery(tmux, "claude") as (store, comms, _):
        current = await store.fetch_session(HOST, NAME)
        generation = current["session_generation"]
        await store.update_session(HOST, NAME, context_level="compact")
        comms.sessions.apply_live(
            TARGET, working=False, capture_generation=generation, capture_liveness="idle",
            pane_status="pane_alive",
        )
        async def pending(*args, **kwargs):
            return EventProof("pending", TARGET, 0)
        monkeypatch.setattr(comms, "_durable_submission_evidence", pending)
        caplog.set_level("INFO", logger="chat_streamd_v2.tmux_transport")
        outcome = await comms.submit_context_compact(
            TARGET, "/compact", expected_generation=current["created_at"], before_input=pending,
        )
    assert outcome["outcome"] == "pending_input"
    assert outcome["reason"] == CODE
    assert tmux.actions == ["paste"]
    refusals = [record for record in caplog.messages if CODE in record]
    assert len(refusals) == 1
    assert "path=compact phase=body_maybe_pasted" in refusals[0]


@pytest.mark.asyncio
async def test_send_now_rechecks_prompt_on_existing_retry_cadence():
    import test_queued_send_now as queued

    harness = await queued.setup()
    try:
        harness.controller.clock = lambda: harness.now
        await harness.controller.send(queued.message())
        await queued.signal(harness, "turn_aborted")
        await queued.intake(harness)
        harness.tmux.screen = frame("codex_command")
        await queued.settle(harness)
        key = harness.controller.operation_key(queued.SID, queued.GEN, "selected")
        waiting, _ = await harness.controller._record(key)
        assert waiting["state"] == "continue_retry_wait"
        request = waiting["continue_request_id"]
        harness.now = waiting["next_retry_at"] + 1
        await queued.settle(harness)
        waiting, _ = await harness.controller._record(key)
        assert waiting["state"] == "continue_retry_wait"
        assert waiting["continue_request_id"] == request
        assert harness.tmux.pastes == []
        harness.tmux.screen = CODEX_COMPOSER
        harness.now = waiting["next_retry_at"] + 1
        await queued.settle(harness)
        assert harness.tmux.pastes == ["continue"]
        assert len(harness.tmux.keys) == 1
    finally:
        await queued.finish(harness)


@pytest.mark.asyncio
async def test_send_now_does_not_retry_a_possibly_pasted_continuation():
    import test_queued_send_now as queued

    harness = await queued.setup()
    try:
        await harness.controller.send(queued.message())
        await queued.signal(harness, "turn_aborted")
        await queued.intake(harness)
        transport = RecordingTmux(after_paste=frame("codex_command"))
        harness.tmux.screen = CODEX_COMPOSER
        harness.tmux.paste = transport.paste
        await queued.settle(harness)
        key = harness.controller.operation_key(queued.SID, queued.GEN, "selected")
        operation, _ = await harness.controller._record(key)
        assert operation["state"] == "continue_pending_proof"
        await queued.settle(harness)
        assert transport.actions == ["paste"]
        assert len(harness.tmux.keys) == 1
    finally:
        await queued.finish(harness)


@pytest.mark.asyncio
async def test_adopted_staged_brief_refuses_before_refencing_and_never_repastes(monkeypatch):
    import time

    tmux = RecordingTmux(screen=f"{CLAUDE_COMPOSER}\n{frame('claude_bash')}")
    tmux.alive = True
    async with delivery(tmux, None) as (store, _, spawnctl):
        request_id = "approval-adopted-spawn"
        nonce = "approval-fixture"
        assert await store.reserve_stream_id(HOST, NAME, ttl_s=60, request_id=request_id, nonce=nonce)
        await spawnctl.sessions.open(HOST, NAME, fence=request_id, provider="claude")
        current = await store.fetch_session(HOST, NAME)
        receipt = {"transport": "staged", "state": "requested"}
        intent = {"brief": BODY, "delivery_receipt": receipt,
                  "open_fields": {"provider": "claude", "session_generation": current["session_generation"]}}
        assert await store.record_spawn_intent(HOST, NAME, intent, request_id=request_id, nonce=nonce)
        reservation, = await store.reservations(include_expired=True)

        async def bound(*args, **kwargs):
            return True

        monkeypatch.setattr(spawnctl, "_commit_pane_bound", bound)
        outcome = await spawnctl._adopt_interrupted_spawn(
            HOST, NAME, request_id, intent, tmux, reservation={**reservation, "expires_at": time.time() + 60},
        )
        assert outcome == "adopted"
        assert tmux.actions == []
        saved = await store.get_spawn_outcome(HOST, NAME)
        assert saved["state"] == "failed"
        assert saved["delivery_receipt"]["failure_code"] == CODE
        remaining, = await store.reservations(include_expired=True)
        assert '"proof_watermark_state"' not in remaining["payload"]
        tmux.screen = CLAUDE_COMPOSER
        await spawnctl.reconcile_spawn_intents()
        assert (await store.get_spawn_outcome(HOST, NAME))["state"] == "failed"
    assert tmux.actions == []
