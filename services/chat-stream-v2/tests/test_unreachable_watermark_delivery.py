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
from submission_events import EventProof, EventWatermark  # noqa: E402

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
