"""Tells submit immediately without a readiness gate on the delivery path.

Provider interfaces can queue mid-turn input, so the daemon does not need to
inspect recipient draft or turn state before pasting. The placeholder fixture
ensures that an idle pane still accepts a tell.

Submission confirmation survives as POST-HOC EVIDENCE only (`submission_confirmed`
in the reply, a WARNING in the log). It never gates, holds, or fails a delivery.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import boot_ready  # noqa: E402
from comms import Comms  # noqa: E402
from sessions import Sessions  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from store import Store  # noqa: E402

HOST = "localhost"
NAME = "v2-target"

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: A synthetic idle provider pane with a placeholder suggestion.
IDLE_PLACEHOLDER = (FIXTURES / "claude_idle_placeholder_pane.txt").read_text()

#: Mid-turn provider output with a working token.
BUSY = "✻ Forging… (3s · esc to interrupt)\n❯ \n"

#: A user-typed draft. It must not change the outcome: we paste regardless.
TYPED_DRAFT = "⏵⏵ bypass permissions on (bypass)\n❯ half-typed thought\n"


def _submitted(body: str) -> str:
    return f"⏺ {body}\n❯ \n"


class ScriptedClaude:
    """A claude pane whose screen is scripted per paste. `submit_on_paste=False`
    reproduces the Enter-lost race: the paste lands but never submits, so the
    confirmation evidence comes back False while delivery still stands."""

    def __init__(self, *, start: str, submit_on_paste: bool = True) -> None:
        self.screen = start
        self.submit_on_paste = submit_on_paste
        self.pastes: list[str] = []
        self.escapes = 0
        self.alive = True

    async def has_session(self, name: str) -> bool:
        return self.alive

    async def pane_pid(self, name: str) -> str:
        return ""  # no pid -> no transcript probe; the viewport is the only evidence

    async def capture(self, name: str) -> str:
        return self.screen

    async def paste(self, name: str, text: str) -> None:
        self.pastes.append(text)
        if self.submit_on_paste:
            self.screen = _submitted(text)

    async def run(self, *args: str, **kw) -> tuple[int, str]:
        if args[:1] == ("send-keys",) and "Escape" in args:
            self.escapes += 1
        return 0, ""


class ScriptedCodex:
    """Codex's first paste+Enter leaves an editable composer.

    The one Enter-only recovery moves the exact body into Codex's native queue,
    where a scope readback shape is already submitted without a
    competing daemon queue or a second paste.
    """

    def __init__(self, *, submit_on_retry: bool) -> None:
        self.submit_on_retry = submit_on_retry
        self.screen = "OpenAI Codex\n─────────\n› \n  gpt-5-codex high"
        self.pastes: list[str] = []
        self.enter_only = 0
        self.captures = 0

    async def capture(self, name: str) -> str:
        self.captures += 1
        return self.screen

    async def paste(self, name: str, text: str) -> None:
        self.pastes.append(text)
        visible = (
            f"[Pasted Content {len(text)} chars]" if len(text) > 20 else text
        )
        self.screen = f"OpenAI Codex\n─────────\n› {visible}\n  gpt-5-codex high"

    async def send_enter(self, name: str) -> None:
        self.enter_only += 1
        if self.submit_on_retry:
            self.screen = (
                "OpenAI Codex\n"
                "Messages to be submitted after next tool call\n"
                f"↳ {self.pastes[-1]}\n"
                "• scope readback: services/chat-stream-v2\n"
                "› "
            )

    async def run(self, *args: str, **kw) -> tuple[int, str]:
        return 0, ""


class StoreTailProofCodex:
    """A consumed Codex paste whose only proof is a normalized central USER event."""

    def __init__(self, store: Store, stream_id: str, *, append_on_paste: bool) -> None:
        self.store = store
        self.stream_id = stream_id
        self.append_on_paste = append_on_paste
        self.screen = "OpenAI Codex\n─────────\n› \n  gpt-5-codex high"
        self.pastes: list[str] = []
        self.enter_only = 0

    async def capture(self, name: str) -> str:
        return self.screen

    async def paste(self, name: str, text: str) -> None:
        self.pastes.append(text)
        if not self.append_on_paste:
            return
        await self.store.append_session_event(
            self.stream_id,
            {
                "stream_id": self.stream_id,
                "provider": "codex",
                "kind": "USER",
                "text": text,
                "timestamp": "2026-08-14T02:45:00Z",
            },
            identity=f"proof:{text}",
            limit=500,
        )

    async def send_enter(self, name: str) -> None:
        self.enter_only += 1

    async def run(self, *args: str, **kw) -> tuple[int, str]:
        return 0, ""


class SerialLineTmux:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.pastes: list[str] = []

    async def capture(self, name: str) -> str:
        await asyncio.sleep(0.001)
        return ""

    async def paste(self, name: str, text: str) -> None:
        self.pastes.append(text)
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.001)

    async def run(self, *args: str, **kw) -> tuple[int, str]:
        return 0, ""


class SerialLineSpawn:
    def __init__(self, tmux: SerialLineTmux) -> None:
        self.tmux = tmux

    async def _await_marker(self, *args: object, **kwargs: object) -> bool:
        await asyncio.sleep(0.001)
        self.tmux.active -= 1
        return True


def _comms(tmux) -> tuple[Comms, Store, Sessions]:
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=tmux))
    return comms, store, sessions


def _tell(tmux, message: str = "hello", **extra) -> dict:
    """One tell into a registered claude pane, returning the reply."""

    async def _go() -> dict:
        comms, store, sessions = _comms(tmux)
        try:
            await sessions.open(HOST, NAME, provider="claude")
            return await comms.tell(
                {"stream_id": f"{HOST}:{NAME}", "message": message, **extra}
            )
        finally:
            store.stop()

    return asyncio.run(_go())


# -- the delivery contract, pinned on the synthetic capture ------------------------


def test_idle_placeholder_pane_delivers_immediately() -> None:
    """An idle placeholder pane accepts and reports a delivered tell."""
    tmux = ScriptedClaude(start=IDLE_PLACEHOLDER)
    reply = _tell(tmux, "synthetic dispatch")
    assert reply["delivery_status"] == "delivered"
    assert reply["submission_confirmed"] is True
    assert tmux.pastes == ["synthetic dispatch"]


def test_no_readiness_predicate_survives_on_the_delivery_path() -> None:
    """Delivery must not depend on a pane-state readiness predicate."""
    for gone in (
        "claude_draft_occupied",
        "claude_ready_for_delivery",
        "codex_ready_for_delivery",
        "pane_ready_for_delivery",
        "delivery_hold_reason",
        "DELIVERY_READY_PREDICATES",
    ):
        assert not hasattr(boot_ready, gone), f"{gone} must not come back"


@pytest.mark.parametrize(
    "screen,label",
    [
        (IDLE_PLACEHOLDER, "idle_placeholder"),
        (BUSY, "mid_turn"),
        (TYPED_DRAFT, "user_draft"),
        ("", "blank_pane"),
    ],
    ids=["idle_placeholder", "mid_turn", "user_draft", "blank_pane"],
)
def test_every_pane_state_delivers(screen: str, label: str) -> None:
    """No pane state holds any more. Mid-turn and draft-occupied panes were the
    two hold reasons; the TUIs queue that input natively, so both deliver."""
    tmux = ScriptedClaude(start=screen)
    reply = _tell(tmux, f"tell into {label}")
    assert reply["delivery_status"] == "delivered"
    assert tmux.pastes == [f"tell into {label}"]


# -- confirmation is evidence, never a gate -----------------------------------


def test_unconfirmed_submission_still_delivers_with_honest_evidence() -> None:
    """The Enter-lost race: the paste lands but never submits. Delivery still
    stands (the message IS in the pane) and the reply says so honestly rather
    than failing or holding."""
    tmux = ScriptedClaude(start=IDLE_PLACEHOLDER, submit_on_paste=False)
    reply = _tell(tmux, "unconfirmable")
    assert reply["delivery_status"] == "delivered"
    assert reply["submission_confirmed"] is False
    assert tmux.pastes == ["unconfirmable"], "exactly one injection, never a re-paste"


def test_tell_reply_carries_no_hold_vocabulary() -> None:
    tmux = ScriptedClaude(start=BUSY)
    reply = _tell(tmux, "no holds")
    assert "hold_reason" not in reply
    assert reply["delivery_status"] != "held"


def test_send_lands_into_a_busy_pane() -> None:
    async def go() -> dict:
        comms, store, sessions = _comms(tmux)
        try:
            await sessions.open(HOST, NAME, provider="claude")
            return await comms.send(
                {"stream_id": f"{HOST}:{NAME}", "message": "user push", "msg_id": "m1"}
            )
        finally:
            store.stop()

    tmux = ScriptedClaude(start=BUSY)
    reply = asyncio.run(go())
    assert reply["delivery"] == "landed"
    assert "hold_reason" not in reply
    assert tmux.pastes == ["user push"]


# -- idempotency survives the subtraction -------------------------------------


def test_tell_id_replay_does_not_double_inject() -> None:
    async def go() -> tuple[dict, dict]:
        comms, store, sessions = _comms(tmux)
        try:
            await sessions.open(HOST, NAME, provider="claude")
            msg = {"stream_id": f"{HOST}:{NAME}", "message": "once", "tell_id": "t1"}
            return await comms.tell(dict(msg)), await comms.tell(dict(msg))
        finally:
            store.stop()

    tmux = ScriptedClaude(start=IDLE_PLACEHOLDER)
    first, second = asyncio.run(go())
    assert first["delivery_status"] == "delivered"
    assert second.get("duplicate") is True
    assert tmux.pastes == ["once"], "a replayed tell_id must never inject twice"


def test_urgent_tell_escapes_then_delivers() -> None:
    tmux = ScriptedClaude(start=BUSY)
    reply = _tell(tmux, "urgent business", urgent=True)
    assert reply["delivery_status"] == "delivered"
    assert tmux.escapes == 1
    assert tmux.pastes == ["urgent business"]


def test_codex_row_6048_enter_only_recovery_is_truthful_and_single_paste(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import comms as comms_module

    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_POLL_S", 0.001)

    async def go() -> tuple[dict, dict, ScriptedCodex, Store]:
        tmux = ScriptedCodex(submit_on_retry=True)
        comms, store, sessions = _comms(tmux)
        try:
            # _comms opens a Claude-oriented fixture; replace the session row's
            # provider while retaining the normal Comms routing seam.
            await sessions.open(HOST, NAME, provider="codex")
            first = await comms.tell({
                "stream_id": f"{HOST}:{NAME}",
                "message": "read the current scope and report it",
                "tell_id": "tell-6048",
            })
            row = await store.get_tell_delivery("tell-6048")
            return first, row, tmux, store
        except Exception:
            store.stop()
            raise

    first, row, tmux, store = asyncio.run(go())
    try:
        assert first["delivery_status"] == "delivered"
        assert first["submission_confirmed"] is True
        assert first["submission_attempts"] == 2
        assert first["delivery_ack_at"]
        assert row is not None
        assert row["delivery"]["delivery_status"] == "delivered"
        assert row["delivery"]["submission_attempts"] == 2
        assert tmux.pastes == ["read the current scope and report it"]
        assert tmux.enter_only == 1
    finally:
        store.stop()


def test_codex_unsubmitted_row_is_durable_and_replay_is_input_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import comms as comms_module

    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_POLL_S", 0.001)

    async def go() -> tuple[dict, dict, dict, ScriptedCodex, Store]:
        tmux = ScriptedCodex(submit_on_retry=False)
        comms, store, sessions = _comms(tmux)
        await sessions.open(HOST, NAME, provider="codex")
        message = {
            "stream_id": f"{HOST}:{NAME}",
            "message": "a body that remains editable",
            "tell_id": "tell-stuck",
        }
        first = await comms.tell(dict(message))
        row = await store.get_tell_delivery("tell-stuck")
        captures_before = tmux.captures
        replay = await comms.tell(dict(message))
        assert tmux.captures == captures_before
        return first, replay, row, tmux, store

    first, replay, row, tmux, store = asyncio.run(go())
    try:
        assert first["delivery_status"] == "pasted_unsubmitted"
        assert first["submission_confirmed"] is False
        assert first["submission_attempts"] == 2
        assert first["delivery_ack_at"] is None
        assert first["reason"] == "active_draft_present"
        assert first["action_status"] == "committed"
        assert first["confirmation_status"] == "pending"
        assert first["do_not_resubmit"] is True
        assert "DO NOT RESUBMIT" in first["retry_guidance"]
        assert row is not None
        assert row["delivery"]["delivery_status"] == "pasted_unsubmitted"
        assert row["delivery"]["submission_attempts"] == 2
        assert row["delivery"]["delivery_ack_at"] is None
        assert replay["duplicate"] is True
        assert replay["delivery_status"] == "pasted_unsubmitted"
        assert tmux.pastes == ["a body that remains editable"]
        assert tmux.enter_only == 1
    finally:
        store.stop()


@pytest.mark.parametrize(
    ("verb", "expected_delivery"),
    (("tell", "delivered"), ("send", "landed")),
)
def test_post_paste_central_user_event_confirms_codex_submission(
    verb: str, expected_delivery: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lifecycle-admitted central USER event is proof when pane structure is absent."""
    import comms as comms_module

    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_POLL_S", 0.001)

    async def go() -> tuple[dict, StoreTailProofCodex, Store]:
        store = Store(":memory:")
        store.start()
        stream_id = f"{HOST}:{NAME}"
        tmux = StoreTailProofCodex(store, stream_id, append_on_paste=True)
        sessions = Sessions(store, tmux=tmux, local_host=HOST)
        comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=tmux))
        await sessions.open(HOST, NAME, provider="codex")
        if verb == "tell":
            reply = await comms.tell({"stream_id": stream_id, "message": "central proof"})
        else:
            reply = await comms.send({"stream_id": stream_id, "message": "central proof"})
        return reply, tmux, store

    reply, tmux, store = asyncio.run(go())
    try:
        assert reply["submission_confirmed"] is True
        assert reply["delivery_status" if verb == "tell" else "delivery"] == expected_delivery
        assert tmux.pastes == ["central proof"]
        assert tmux.enter_only == 0
    finally:
        store.stop()


@pytest.mark.parametrize(
    ("verb", "delivery_key"),
    (("tell", "delivery_status"), ("send", "delivery")),
)
def test_pre_paste_identical_user_event_is_not_submission_proof(
    verb: str, delivery_key: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only an event after the pre-paste daemon-sequence watermark can prove submission."""
    import comms as comms_module

    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_POLL_S", 0.001)

    async def go() -> tuple[dict, StoreTailProofCodex, Store]:
        store = Store(":memory:")
        store.start()
        stream_id = f"{HOST}:{NAME}"
        tmux = StoreTailProofCodex(store, stream_id, append_on_paste=False)
        sessions = Sessions(store, tmux=tmux, local_host=HOST)
        comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=tmux))
        await sessions.open(HOST, NAME, provider="codex")
        await store.append_session_event(
            stream_id,
            {
                "stream_id": stream_id,
                "provider": "codex",
                "kind": "USER",
                "text": "repeat proof",
                "timestamp": "2026-08-14T02:44:59Z",
            },
            identity="before-paste-proof",
            limit=500,
        )
        if verb == "tell":
            reply = await comms.tell({"stream_id": stream_id, "message": "repeat proof"})
        else:
            reply = await comms.send({"stream_id": stream_id, "message": "repeat proof"})
        return reply, tmux, store

    reply, tmux, store = asyncio.run(go())
    try:
        # Paste left the composer (no active draft) but the post-watermark USER
        # event has not landed inside the window: committed, not failed.
        assert reply[delivery_key] == "committed_pending_proof"
        assert reply["reason"] == (
            "submission_proof_pending" if verb == "tell" else "submit_unconfirmed"
        )
        assert reply["submission_confirmed"] is False
        assert reply["action_committed"] is True
        assert reply["do_not_resubmit"] is True
        assert tmux.pastes == ["repeat proof"]
        assert tmux.enter_only == 0
    finally:
        store.stop()


def test_submission_tail_requires_post_watermark_exact_admitted_event() -> None:
    """Wrong stream, kind, text, token, and stale identical events cannot prove a paste."""
    stream_id = f"{HOST}:{NAME}"
    body = "[public-notice:proof-1]\nexact proof"

    class TailStore:
        def __init__(self) -> None:
            self.events = [
                {"daemon_seq": 10, "stream_id": stream_id, "kind": "USER", "text": body},
                {"daemon_seq": 11, "stream_id": "other:v2", "kind": "USER", "text": body},
                {"daemon_seq": 12, "stream_id": stream_id, "kind": "ASSISTANT", "text": body},
                {"daemon_seq": 13, "stream_id": stream_id, "kind": "USER", "text": "other body"},
                {"daemon_seq": 14, "stream_id": stream_id, "kind": "TELL", "text": "exact proof"},
            ]

        async def fetch_session_event_tail(self, _stream_id: str, *, limit: int) -> list[dict]:
            assert limit == 500
            return list(self.events)

    async def go() -> tuple[bool, bool]:
        store = TailStore()
        comms = Comms(store, None, None)
        rejected = await comms._submission_event_proven(stream_id, body, 10)
        store.events.append(
            {
                "daemon_seq": 15,
                "stream_id": stream_id,
                "kind": "TELL",
                "text": "[public-notice:proof-1]   exact\n proof",
            }
        )
        accepted = await comms._submission_event_proven(stream_id, body, 10)
        return rejected, accepted

    rejected, accepted = asyncio.run(go())
    assert rejected is False
    assert accepted is True


def test_final_poll_submission_event_is_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A matching central event that arrives on the next eligible poll still confirms."""
    import comms as comms_module

    stream_id = f"{HOST}:{NAME}"
    body = "final poll proof"

    class TailStore:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def fetch_session_event_tail(self, _stream_id: str, *, limit: int) -> list[dict]:
            return list(self.events)

    class EmptyTmux:
        async def capture(self, _name: str) -> str:
            return "OpenAI Codex\n─────────\n› \n  gpt-5-codex high"

    async def go() -> bool:
        store = TailStore()
        comms = Comms(store, None, None)

        async def final_poll(_delay: float) -> None:
            store.events.append(
                {
                    "daemon_seq": 11,
                    "stream_id": stream_id,
                    "kind": "USER",
                    "text": body,
                }
            )

        monkeypatch.setattr(comms_module.asyncio, "sleep", final_poll)
        return await comms._submission_evidence(
            EmptyTmux(), NAME, body, "codex", baseline="", stream_id=stream_id, watermark=10,
        )

    assert asyncio.run(go()) is True


def test_one_comms_pane_lock_serializes_tell_tell_and_send() -> None:
    async def go() -> tuple[list[dict], SerialLineTmux, Store, Comms]:
        tmux = SerialLineTmux()
        spawn = SerialLineSpawn(tmux)
        store = Store(":memory:")
        store.start()
        sessions = Sessions(store, tmux=tmux, local_host=HOST)
        await sessions.open(HOST, NAME, provider="shell")
        comms = Comms(store, sessions, spawn)
        replies = await asyncio.gather(
            comms.tell({
                "stream_id": f"{HOST}:{NAME}",
                "message": "tell one",
                "tell_id": "serial-1",
            }),
            comms.tell({
                "stream_id": f"{HOST}:{NAME}",
                "message": "tell two",
                "tell_id": "serial-2",
            }),
            comms.send({
                "stream_id": f"{HOST}:{NAME}",
                "message": "send three",
                "msg_id": "serial-3",
            }),
        )
        return replies, tmux, store, comms

    replies, tmux, store, comms = asyncio.run(go())
    try:
        assert all(reply.get("submission_confirmed") is True for reply in replies)
        assert tmux.peak == 1
        assert len(tmux.pastes) == 3
        assert list(comms._pane_input_locks) == [f"{HOST}:{NAME}"]
    finally:
        store.stop()


def test_legacy_held_tell_table_is_not_created_at_boot() -> None:
    """Held rows are not created at startup, so startup owns no hold table."""

    store = Store(":memory:")
    store.start()
    try:
        table = asyncio.run(store.submit(lambda conn: conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='v2_held_tells'"
        ).fetchone()))
    finally:
        store.stop()

    assert table is None
