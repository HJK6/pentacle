"""An unreadable pre-paste watermark must not turn a delivered brief into a failure.

A spawn creates a brand-new, generation-fenced seat, so the only USER event that
can carry the brief is the spawn's own paste. When the Store cannot be read
before the paste (restart / load), delivery is proved against the fence alone
(boundary 0), both in the live spawn and in adoption; it is never left
`starting` and never classified `agent_never_started` while the event exists.
"""

from __future__ import annotations

import asyncio

import pytest

import spawnctl as spawnctl_mod  # noqa: E402
import tmux_transport  # noqa: E402
from sessions import Sessions  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from store import Store  # noqa: E402
from submission_events import DurableUserEventProof, EventProof, EventWatermark  # noqa: E402

from test_adopt_reconcile import DEAD_PID, AdoptTmux  # noqa: E402
from test_spawn_error_no_phantom_row import BootReadyTmux  # noqa: E402


class ClaudeReadyTmux(BootReadyTmux):
    async def capture(self, name: str) -> str:
        return "⏵⏵ bypass permissions on (bypass)\n❯ "

HOST = "localhost"
NAME = "v2-unreachable-wm"
BRIEF = "Read /tmp/public-test and follow the complete prompt exactly."
UNREACHABLE = {
    "transport": "direct",
    "proof_watermark": 0,
    "proof_watermark_state": "unreachable",
    "proof_watermark_reason": "event_store_timeout",
}


class FencedProof:
    """Store whose watermark read fails but whose fenced USER event exists."""

    def __init__(self, *, event_present: bool) -> None:
        self.event_present = event_present

    async def wait_for_watermark(self, stream_id: str, **_kw: object) -> EventWatermark:
        return EventWatermark(stream_id, 0, "unreachable", "event_store_timeout")

    async def watermark(self, stream_id: str) -> EventWatermark:
        return EventWatermark(stream_id, 0, "unreachable", "event_store_timeout")

    async def lookup(self, stream_id: str, *, expected_text: str,
                     watermark: EventWatermark, timeout_s: float | None = None) -> EventProof:
        if watermark.state != "reachable":
            return EventProof("unreachable", stream_id, watermark.daemon_seq,
                              reason=watermark.reason or "event_watermark_unreachable")
        if self.event_present:
            return EventProof("proven", stream_id, watermark.daemon_seq, event_id=7)
        return EventProof("pending", stream_id, watermark.daemon_seq)

    async def wait(self, stream_id: str, *, expected_text: str,
                   watermark: EventWatermark, timeout_s: float) -> EventProof:
        return await self.lookup(stream_id, expected_text=expected_text, watermark=watermark)


def _settle(event_present: bool, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, str]:
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", 0.3)

    async def run() -> tuple[str, str, str]:
        db = Store(":memory:")
        db.start()
        try:
            tmux = AdoptTmux(alive=True, pane_pid=DEAD_PID, screen="$ ")
            sessions = Sessions(db, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(db, sessions, tmux=tmux, submission_proof=FencedProof(event_present=event_present))
            return await ctl._settle_adoption(HOST, NAME, BRIEF, tmux, delivery_receipt=dict(UNREACHABLE))
        finally:
            db.stop()

    return asyncio.run(run())


def test_adoption_proves_delivery_from_fenced_event_when_watermark_was_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, evidence, _reason = _settle(True, monkeypatch)
    assert (state, evidence) == ("delivered", "event_store")


def test_adoption_still_reports_agent_never_started_without_any_event_or_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, evidence, _reason = _settle(False, monkeypatch)
    assert (state, evidence) == ("failed", "agent_never_started")


def test_spawn_with_unreachable_watermark_confirms_from_fenced_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tmux_transport, "POLL_INTERVAL_S", 0.001)

    async def run() -> dict:
        store = Store(":memory:")
        store.start()
        try:
            tmux = ClaudeReadyTmux()
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux, submission_proof=FencedProof(event_present=True))
            await store.reserve_stream_id(
                HOST, NAME, ttl_s=600, request_id="r-wm", nonce="n-wm", owner_instance_id="inst",
            )
            await store.record_spawn_intent(
                HOST, NAME, {"open_fields": {"objective": "Exercise the existing spawn contract"}, "brief": BRIEF},
            )
            await asyncio.wait_for(ctl._spawn_fenced(
                HOST, NAME, "r-wm", "provider-command", BRIEF, "READY", {}, [False], [False],
                {**tmux_transport.open_fields({"provider": "claude"}), "session_generation": "g1"},
                {"resolved_launch_tuple": {"provider": "claude"}},
                tmux, {"transport": "direct", "state": "requested"}, nonce="n-wm",
            ), timeout=20)
            return await store.get_spawn_outcome(HOST, NAME)
        finally:
            store.stop()

    outcome = asyncio.run(run())
    assert outcome["state"] == "delivered"


def _settle_real(monkeypatch, events):
    """Adoption against the REAL durable proof: events = [(kind, text, generation)]
    where generation is "current" or "prior"."""
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", 0.3)
    stream = f"localhost:{NAME}"

    async def run():
        db = Store(":memory:")
        db.start()
        try:
            await db.open_session("localhost", NAME, provider="claude")
            prior = [e for e in events if e[2] == "prior"]
            current = [e for e in events if e[2] == "current"]
            for i, (kind, text, _g) in enumerate(prior):
                await db.append_session_event(stream, {"stream_id": stream, "kind": kind, "text": text,
                                                       "timestamp": "2026-10-01T00:00:00Z"}, identity=f"p{i}", limit=500)
            if prior:
                await db.submit(lambda conn: (conn.execute(
                    "UPDATE sessions SET created_at=? WHERE host=? AND session_name=?",
                    ("2099-01-01T00:00:00Z", "localhost", NAME)), conn.commit()))
            for i, (kind, text, _g) in enumerate(current):
                await db.append_session_event(stream, {"stream_id": stream, "kind": kind, "text": text,
                                                       "timestamp": "2026-10-02T00:00:00Z"}, identity=f"c{i}", limit=500)
            tmux = AdoptTmux(alive=True, pane_pid=DEAD_PID, screen="$ ")
            sessions = Sessions(db, tmux=tmux, local_host="localhost")
            ctl = SpawnCtl(db, sessions, tmux=tmux, submission_proof=DurableUserEventProof(db, local_host="localhost"))
            return await ctl._settle_adoption("localhost", NAME, BRIEF, tmux, delivery_receipt=dict(UNREACHABLE))
        finally:
            db.stop()

    return asyncio.run(run())


@pytest.mark.parametrize("events,delivered", [
    ([("USER", BRIEF, "current")], True),
    ([("USER", "unrelated operator text", "current")], False),
    ([("USER", BRIEF, "prior")], False),
], ids=["matching-current", "unrelated-body", "prior-generation"])
def test_unreachable_watermark_settles_only_on_matching_current_generation_user(monkeypatch, events, delivered):
    state, evidence, _reason = _settle_real(monkeypatch, events)
    if delivered:
        assert (state, evidence) == ("delivered", "event_store")
    else:
        # failed/agent_never_started, or indeterminate when the bounded store read expires; never delivered.
        assert state != "delivered" and evidence != "event_store"
