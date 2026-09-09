"""Peer-message provenance: agent-orch tell/send must inject a `[from …]`
envelope so the transcript can render the message as a distinct peer/agent row
instead of the operator's own bubble.

Root cause pinned here (spec_example_2026_01):
the v2 inject path pastes a BARE message string (`comms._route`,
`comms._prepare_send_plan`). Both provider normalizers classify a peer delivery
as `kind:TELL` ONLY when the injected text opens with the shared envelope
`[from <sender>] [<anchor>:<id>]\n<payload>` (`claude_jsonl_norm._PEER_TELL_RE`,
imported by the Codex normalizer). No envelope ⇒ `USER` ⇒ operator bubble.

The fix stamps that envelope for TRUSTED peer traffic only (a real
`from_stream_id` + an authenticated actor); operator sends and daemon
housekeeping stay bare. The delivery-proof matcher
(`_tail_event_matches_submission`) must keep confirming, because the normalizer
stores only the header-stripped payload as the event text.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_jsonl_norm import normalize_claude_jsonl_records
from codex_rollout_norm import normalize_codex_rollout_records
from comms import Comms, payload_digest
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store

HOST = "localhost"
NAME = "v2-target"
STREAM_ID = f"{HOST}:{NAME}"
PEER = "hostb:claude-hostb-11111111"
# A genuine peer seat verifies its own stream token per message; the server sets
# token_verified and binds stream_id to the claimed sender (WRONG_SEAT otherwise).
PEER_AUTH = {"token_verified": True, "stream_id": PEER}


class RecordingClaude:
    """A claude pane that submits on paste and records every pasted string."""

    def __init__(self) -> None:
        self.screen = "❯ \n"
        self.pastes: list[str] = []
        self.alive = True

    async def has_session(self, name: str) -> bool:
        return self.alive

    async def pane_pid(self, name: str) -> str:
        return ""

    async def capture(self, name: str) -> str:
        return self.screen

    async def paste(self, name: str, text: str) -> None:
        self.pastes.append(text)
        self.screen = f"⏺ {text}\n❯ \n"

    async def run(self, *args: str, **kw) -> tuple[int, str]:
        return 0, ""


def _comms(tmux) -> tuple[Comms, Store, Sessions]:
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=tmux))
    return comms, store, sessions


def _run_tell(tmux, **extra) -> dict:
    async def _go() -> dict:
        comms, store, sessions = _comms(tmux)
        try:
            await sessions.open(HOST, NAME, provider="claude")
            return await comms.tell({"stream_id": STREAM_ID, "message": "hello peer", **extra})
        finally:
            store.stop()

    return asyncio.run(_go())


def _run_send(tmux, **extra) -> dict:
    async def _go() -> dict:
        comms, store, sessions = _comms(tmux)
        try:
            await sessions.open(HOST, NAME, provider="claude")
            return await comms.send({"stream_id": STREAM_ID, "message": "hello peer", **extra})
        finally:
            store.stop()

    return asyncio.run(_go())


# -- injection stamping -------------------------------------------------------


def test_peer_tell_stamps_from_envelope() -> None:
    tmux = RecordingClaude()
    _run_tell(tmux, from_stream_id=PEER, tell_id="tell-xyz", _auth_context=PEER_AUTH)
    assert tmux.pastes == [f"[from {PEER}] [tell:tell-xyz]\nhello peer"]


def test_peer_send_stamps_from_envelope() -> None:
    # Wire clients mint request_id; the envelope anchor IS that request_id so the
    # receipt projection can correlate the normalized event by raw.tell_id.
    tmux = RecordingClaude()
    _run_send(tmux, from_stream_id=PEER, request_id="send-req7", _auth_context=PEER_AUTH)
    assert tmux.pastes == [f"[from {PEER}] [send:send-req7]\nhello peer"]


def test_operator_tell_and_send_stay_bare() -> None:
    """No from_stream_id ⇒ the operator's own message, never stamped."""
    tmux = RecordingClaude()
    _run_tell(tmux, tell_id="op-1")
    assert tmux.pastes == ["hello peer"]

    tmux2 = RecordingClaude()
    _run_send(tmux2, msg_id="op-2")
    assert tmux2.pastes == ["hello peer"]


def test_untrusted_from_stream_id_is_not_stamped() -> None:
    """A `from_stream_id` claim without an authenticated actor cannot spoof a
    peer identity into the visible collapsed row."""
    tmux = RecordingClaude()
    _run_tell(tmux, from_stream_id=PEER, tell_id="spoof-1")  # no _auth_context
    assert tmux.pastes == ["hello peer"]


def test_operator_carrying_a_from_stream_id_claim_is_not_stamped() -> None:
    """An operator (operator_authenticated, no seat stream token → token_verified
    False, empty stream_id) carrying a from_stream_id claim must NOT be stamped —
    operator messages stay bare; no peer attribution spoofing."""
    tmux = RecordingClaude()
    _run_tell(tmux, from_stream_id=PEER, tell_id="op-spoof", _auth_context={
        "operator_authenticated": True, "operator_principal": "operator:abc",
        "token_verified": False, "stream_id": "",
    })
    assert tmux.pastes == ["hello peer"]


def test_service_actor_unverified_claim_is_not_stamped() -> None:
    """A system producer's service_actor is an unverified claim; only a
    token-verified seat bound to its own stream_id is trusted for stamping."""
    tmux = RecordingClaude()
    _run_tell(tmux, from_stream_id=PEER, tell_id="svc-1", _auth_context={
        "service_authenticated": True, "service_actor": PEER, "token_verified": False,
    })
    assert tmux.pastes == ["hello peer"]


def test_token_verified_but_wrong_seat_is_not_stamped() -> None:
    """token_verified with a stream_id that does not match the claimed sender
    (defense-in-depth against a WRONG_SEAT slipping through) stays bare."""
    tmux = RecordingClaude()
    _run_tell(tmux, from_stream_id=PEER, tell_id="wseat-1", _auth_context={
        "token_verified": True, "stream_id": "hostb:some-other-seat",
    })
    assert tmux.pastes == ["hello peer"]


def test_legacy_bare_digest_tell_replay_is_not_a_conflict() -> None:
    """A tell delivered by the pre-envelope daemon stored payload_digest(target, BARE
    body). After this upgrade, a same-tell_id retry (now peer-stamped) must hash the
    ORIGINAL payload so it replays the stored tell.ok — never tell_id_conflict or a
    second paste."""
    async def go() -> tuple[dict, RecordingClaude]:
        tmux = RecordingClaude()
        comms, store, sessions = _comms(tmux)
        try:
            await sessions.open(HOST, NAME, provider="claude")
            bare_digest = payload_digest(STREAM_ID, "hello peer")
            await store.put_tell_delivery("t-legacy", {
                "payload_digest": bare_digest,
                "reply": {"type": "tell.ok", "tell_id": "t-legacy", "delivery_status": "delivered"},
                "delivery": {"tell_id": "t-legacy", "text": "hello peer"},
            })
            reply = await comms.tell({
                "stream_id": STREAM_ID, "message": "hello peer", "tell_id": "t-legacy",
                "from_stream_id": PEER, "_auth_context": PEER_AUTH,
            })
            return reply, tmux
        finally:
            store.stop()

    reply, tmux = asyncio.run(go())
    assert reply.get("duplicate") is True
    assert tmux.pastes == [], "a legacy bare-digest replay must not inject again"


def test_send_form_and_tell_form_human_prose_without_newline_stay_user() -> None:
    """The anchor with no canonical newline is human prose, not a peer delivery."""
    for text in (
        "[from human] [send:meeting] please send the notes",
        "[from bob] [tell:x] ping me later about this",
    ):
        assert _claude_user_event(text)["kind"] == "USER", text
        assert _codex_user_event(text)["kind"] == "USER", text


def test_peer_tell_replay_injects_the_stamped_body_once() -> None:
    async def go() -> tuple[dict, dict, RecordingClaude]:
        tmux = RecordingClaude()
        comms, store, sessions = _comms(tmux)
        try:
            await sessions.open(HOST, NAME, provider="claude")
            msg = {
                "stream_id": STREAM_ID, "message": "hello peer",
                "tell_id": "t-dupe", "from_stream_id": PEER, "_auth_context": PEER_AUTH,
            }
            first = await comms.tell(dict(msg))
            second = await comms.tell(dict(msg))
            return first, second, tmux
        finally:
            store.stop()

    first, second, tmux = asyncio.run(go())
    assert first["delivery_status"] == "delivered"
    assert second.get("duplicate") is True
    assert tmux.pastes == [f"[from {PEER}] [tell:t-dupe]\nhello peer"]


# -- normalizer round-trip (both providers, both anchors) ---------------------


def _claude_user_event(text: str) -> dict:
    events = normalize_claude_jsonl_records(
        [{"type": "user", "uuid": "u1", "timestamp": "2026-09-07T00:00:00Z",
          "message": {"content": text}}],
        host=HOST, session_name=NAME,
    )
    assert len(events) == 1, events
    return events[0]


def _codex_user_event(text: str) -> dict:
    events = normalize_codex_rollout_records(
        [{"type": "response_item", "timestamp": "2026-09-07T00:00:00Z",
          "payload": {"type": "message", "role": "user",
                      "content": [{"type": "input_text", "text": text}]}}],
        host=HOST, session_name=NAME,
    )
    assert len(events) == 1, events
    return events[0]


@pytest.mark.parametrize("anchor", ["tell", "send"])
def test_claude_normalizer_classifies_stamped_peer_delivery_as_TELL(anchor: str) -> None:
    event = _claude_user_event(f"[from {PEER}] [{anchor}:id-9]\nplease review the row")
    assert event["kind"] == "TELL"
    assert event["raw"]["sender"] == PEER
    assert event["raw"]["peer_payload"] == "please review the row"
    assert event["text"] == "please review the row"


@pytest.mark.parametrize("anchor", ["tell", "send"])
def test_codex_normalizer_classifies_stamped_peer_delivery_as_TELL(anchor: str) -> None:
    event = _codex_user_event(f"[from {PEER}] [{anchor}:id-9]\nplease review the row")
    assert event["kind"] == "TELL"
    assert event["raw"]["sender"] == PEER
    assert event["raw"]["peer_payload"] == "please review the row"


def test_operator_prose_is_not_misclassified() -> None:
    """A human line that merely starts with a bracket stays a USER turn."""
    assert _claude_user_event("[from 3:30pm] standup, see you there")["kind"] == "USER"
    assert _codex_user_event("[from the desk of example user] hello")["kind"] == "USER"


# -- delivery proof survives the header ---------------------------------------


def test_peer_send_tell_event_projects_clean_display_not_staged_path() -> None:
    """A peer send with an attachment normalizes as TELL. Its send receipt still
    carries the clean display_text/attachments, so the receipt projection must
    correlate the TELL event and restore the clean body into raw.peer_payload —
    never leaving the raw staged attachment path in the card."""
    wire = f"[from {PEER}] [send:send-att]\n[image staged /tmp/staged/x.jpg] look at this"
    async def go() -> dict:
        store = Store(":memory:")
        store.start()
        try:
            await store.append_send_receipt(
                to_stream_id=STREAM_ID, request_id="send-att", receipt_id="r-att",
                state="landed", wire_text=wire, display_text="look at this",
                attachments=[{"key": "a" * 64, "mime": "image/png", "bytes": 12}],
                delivery="landed", submission_confirmed=True,
                from_stream_id=PEER, actor_stream_id=PEER, actor_trusted=True,
            )
            tell_event = {
                "stream_id": STREAM_ID, "kind": "TELL",
                "text": "[image staged /tmp/staged/x.jpg] look at this",
                "raw": {"sender": PEER, "tell_id": "send-att", "peer_payload": "[image staged /tmp/staged/x.jpg] look at this"},
            }
            return await store.stamp_event_with_send_receipt(tell_event)
        finally:
            store.stop()

    stamped = asyncio.run(go())
    assert stamped["text"] == "look at this"
    assert stamped["raw"]["peer_payload"] == "look at this"
    assert "/tmp/staged" not in stamped["raw"]["peer_payload"]
    assert stamped["receipt_id"] == "r-att"
    assert stamped["attachment_count"] == 1


def test_identical_text_peer_sends_get_distinct_receipts_no_cross_correlation() -> None:
    """Two distinct peer sends carrying identical text must each correlate to
    their OWN receipt via the envelope anchor (raw.tell_id), never collapse onto
    one receipt through the text-digest fallback. Guards the exactly-once /
    no-miscorrelation acceptance criterion (re-QA reject #2, 2026-09-07)."""
    text = "ship it when the gate is green"

    async def go() -> tuple[dict, dict]:
        store = Store(":memory:")
        store.start()
        try:
            for rid, receipt_id in (("send-one", "r-one"), ("send-two", "r-two")):
                await store.append_send_receipt(
                    to_stream_id=STREAM_ID, request_id=rid, receipt_id=receipt_id,
                    state="landed", wire_text=f"[from {PEER}] [send:{rid}]\n{text}",
                    display_text=text, attachments=[],
                    delivery="landed", submission_confirmed=True,
                    from_stream_id=PEER, actor_stream_id=PEER, actor_trusted=True,
                )

            def _event(rid: str) -> dict:
                return {
                    "stream_id": STREAM_ID, "kind": "TELL", "text": text,
                    "raw": {"sender": PEER, "tell_id": rid, "peer_payload": text},
                }

            one = await store.stamp_event_with_send_receipt(_event("send-one"))
            two = await store.stamp_event_with_send_receipt(_event("send-two"))
            return one, two
        finally:
            store.stop()

    one, two = asyncio.run(go())
    assert one["receipt_id"] == "r-one"
    assert two["receipt_id"] == "r-two"
    # A never-sent identical-text peer event correlates to nothing.
    async def unmatched() -> dict:
        store = Store(":memory:")
        store.start()
        try:
            await store.append_send_receipt(
                to_stream_id=STREAM_ID, request_id="send-real", receipt_id="r-real",
                state="landed", wire_text=f"[from {PEER}] [send:send-real]\n{text}",
                display_text=text, attachments=[], delivery="landed",
                submission_confirmed=True, from_stream_id=PEER,
                actor_stream_id=PEER, actor_trusted=True,
            )
            return await store.stamp_event_with_send_receipt({
                "stream_id": STREAM_ID, "kind": "TELL", "text": text,
                "raw": {"sender": PEER, "tell_id": "send-ghost", "peer_payload": text},
            })
        finally:
            store.stop()

    assert "receipt_id" not in asyncio.run(unmatched())


def test_codex_delivery_proof_matches_stamped_body_against_stripped_payload() -> None:
    """The pasted (stamped) wire text must still confirm against the durable
    TELL event whose text is only the header-stripped payload."""
    stamped = f"[from {PEER}] [tell:t-1]\nread the current scope and report it"
    payload = "read the current scope and report it"

    class TailStore:
        async def fetch_session_event_tail(self, _stream_id: str, *, limit: int) -> list[dict]:
            return [
                {"daemon_seq": 20, "stream_id": STREAM_ID, "kind": "TELL", "text": payload},
                {"daemon_seq": 21, "stream_id": STREAM_ID, "kind": "TELL", "text": "a different body"},
            ]

    async def go() -> tuple[bool, bool]:
        comms = Comms(TailStore(), None, None)
        matched = await comms._submission_event_proven(STREAM_ID, stamped, 10)
        # A stamped body whose payload never landed must NOT confirm.
        other = f"[from {PEER}] [tell:t-1]\nnever pasted"
        unmatched = await comms._submission_event_proven(STREAM_ID, other, 10)
        return matched, unmatched

    matched, unmatched = asyncio.run(go())
    assert matched is True
    assert unmatched is False
