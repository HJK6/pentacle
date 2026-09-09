"""Regression coverage for initial-prompt submit recovery and visibility."""

from __future__ import annotations

import tmux_transport

import asyncio

import pytest

import spawnctl as spawnctl_mod
from server import Server
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from tmux_transport import open_fields
from store import Store
from submission_events import EventProof, EventWatermark, PROOF_TERMINAL_BOUND_S


HOST = "localhost"
NAME = "v2-bootstrap-probe"
POINTER = "Read /tmp/public-test and follow the complete prompt exactly."


class NeverSubmittedTmux:
    """Provider composer keeps the staged pointer after both Enter attempts."""

    def __init__(self) -> None:
        self.alive = False
        self.pastes = 0
        self.enter_retries = 0

    async def new_session(self, *_args: object, **_kwargs: object) -> None:
        self.alive = True

    async def has_session(self, _name: str) -> bool:
        return self.alive

    async def session_state(self, _name: str) -> str:
        return "alive" if self.alive else "gone"

    async def pane_pid(self, _name: str) -> str:
        return ""

    async def capture(self, _name: str) -> str:
        if not self.pastes:
            return "⏵⏵ bypass permissions on (bypass)\n❯ "
        return f"⏵⏵ bypass permissions on (bypass)\n❯ {POINTER}"

    async def paste(self, _name: str, _text: str) -> None:
        # The real Tmux.paste includes the first Enter; model it being swallowed.
        self.pastes += 1

    async def send_enter(self, _name: str) -> None:
        self.enter_retries += 1


class FirstEventStore:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch_session_event_tail(self, _stream_id: str, *, limit: int) -> list[dict]:
        assert limit == 500
        self.calls += 1
        events = [{
            "daemon_seq": 10,
            "stream_id": f"{HOST}:{NAME}",
            "kind": "SYSTEM",
            "text": "pre-watermark",
        }]
        if self.calls > 1:
            events.append({
                "daemon_seq": 11,
                "stream_id": f"{HOST}:{NAME}",
                "kind": "USER",
                "text": POINTER,
            })
        return events


class RepaintingInconclusiveTmux(NeverSubmittedTmux):
    def __init__(self) -> None:
        super().__init__()
        self.captures = 0

    async def capture(self, _name: str) -> str:
        self.captures += 1
        return f"provider repaint {self.captures}"


class DelayedInitialUserProof:
    def __init__(self) -> None:
        self.wait_timeouts: list[float] = []

    async def watermark(self, stream_id: str) -> EventWatermark:
        return EventWatermark(stream_id, 253987980, "reachable")

    async def wait(self, stream_id: str, *, expected_text: str,
                   watermark: EventWatermark, timeout_s: float) -> EventProof:
        self.wait_timeouts.append(timeout_s)
        assert expected_text == POINTER
        return EventProof(
            "proven", stream_id, watermark.daemon_seq,
            event_id=254000000, event_ts="2026-08-27T03:43:05.494Z",
        )

    async def lookup(self, stream_id: str, *, expected_text: str,
                     watermark: EventWatermark) -> EventProof:
        return await self.wait(
            stream_id,
            expected_text=expected_text,
            watermark=watermark,
            timeout_s=0.0,
        )


def test_first_event_is_submit_proof_without_enter_retry() -> None:
    store = FirstEventStore()
    tmux = NeverSubmittedTmux()
    tmux.alive = True
    tmux.pastes = 1
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    ctl = SpawnCtl(store, sessions, tmux=tmux)

    confirmed = asyncio.run(
        ctl._confirm_brief_delivery(NAME, POINTER, "", "claude", tmux)
    )

    assert confirmed is True
    assert tmux.enter_retries == 0


def test_spawn_uses_shared_exact_user_bound_for_delay() -> None:
    store = Store(":memory:")
    tmux = NeverSubmittedTmux()
    tmux.alive = True
    tmux.pastes = 1
    proof = DelayedInitialUserProof()
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    ctl = SpawnCtl(
        store,
        sessions,
        tmux=tmux,
        submission_proof=proof,
    )
    watermark = EventWatermark(f"{HOST}:{NAME}", 253987980, "reachable")

    confirmed = asyncio.run(ctl._confirm_brief_delivery(
        NAME,
        POINTER,
        "",
        "claude",
        tmux,
        watermark=watermark,
    ))

    assert confirmed is True
    assert proof.wait_timeouts == [PROOF_TERMINAL_BOUND_S]
    assert tmux.enter_retries == 0


def test_inconclusive_repainting_pane_is_bounded_without_pane_driven_enter_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tmux_transport, "RECEIPT_TIMEOUT_S", 0.02)
    monkeypatch.setattr(spawnctl_mod, "RESUBMIT_GRACE_S", 0.0)
    monkeypatch.setattr(tmux_transport, "POLL_INTERVAL_S", 0.001)
    tmux = RepaintingInconclusiveTmux()
    tmux.alive = True
    store = Store(":memory:")
    ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=HOST), tmux=tmux)

    async def run() -> bool:
        return await asyncio.wait_for(
            ctl._confirm_brief_delivery(NAME, POINTER, "", "claude", tmux),
            timeout=0.15,
        )

    assert asyncio.run(run()) is False
    assert tmux.enter_retries == 0


def test_submit_failure_is_bounded_error_with_pane_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> tuple[VerbError, NeverSubmittedTmux]:
        store = Store(":memory:")
        store.start()
        tmux = NeverSubmittedTmux()
        sessions = Sessions(store, tmux=tmux, local_host=HOST)
        ctl = SpawnCtl(store, sessions, tmux=tmux)
        # The spawn path always reserves + records intent before _spawn_fenced; the
        # cancel-fence bind commit is fail-closed on a missing reservation (no
        # legacy bypass), so establish the precondition here.
        await store.reserve_stream_id(
            HOST, NAME, ttl_s=600, request_id="spawn-bootstrap-error",
            nonce="n-bootstrap", owner_instance_id="inst",
        )
        await store.record_spawn_intent(HOST, NAME, {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": POINTER})
        try:
            with pytest.raises(VerbError) as raised:
                await ctl._spawn_fenced(
                    HOST,
                    NAME,
                    "spawn-bootstrap-error",
                    "provider-command",
                    POINTER,
                    "READY",
                    {},
                    [False],
                    [False],
                    {**open_fields({"provider": "claude"}), "session_generation": "g1"},
                    {"resolved_launch_tuple": {"provider": "claude"}},
                    tmux,
                    {"transport": "staged", "state": "requested"},
                    nonce="n-bootstrap",
                )
            return raised.value, tmux
        finally:
            store.stop()

    monkeypatch.setattr(tmux_transport, "RECEIPT_TIMEOUT_S", 0.02)
    monkeypatch.setattr(spawnctl_mod, "RESUBMIT_GRACE_S", 0.0)
    monkeypatch.setattr(spawnctl_mod, "SPAWN_SUBMISSION_PROOF_BOUND_S", 0.02)
    monkeypatch.setattr(tmux_transport, "POLL_INTERVAL_S", 0.001)

    error, tmux = asyncio.run(run())
    assert error.code == "prompt_delivery_failed"
    assert error.extra["bootstrap_state"] == "unsubmitted"
    assert error.extra["submission_attempts"] == 1
    assert POINTER in error.extra["pane_capture"]
    assert tmux.pastes == 1
    assert tmux.enter_retries == 0


def test_zero_event_seat_surfaces_unsubmitted_in_list_and_inspect() -> None:
    async def run() -> tuple[dict, dict]:
        store = Store(":memory:")
        store.start()
        sessions = Sessions(store, local_host=HOST)
        try:
            row = await sessions.open(HOST, NAME, provider="claude")
            sessions.apply_durable(
                row["stream_id"], created_at="2026-08-23T00:00:00Z",
            )
            server = Server(store=store, sessions=sessions, local_host=HOST)
            server.inventory_ready.set()
            listed = await server._on_list_sessions({})
            inspected = await server._on_inspect_stream({
                "stream_id": row["stream_id"], "event_tail": 0,
            })
            return listed, inspected
        finally:
            store.stop()

    listed, inspected = asyncio.run(run())
    assert listed["active"][0]["bootstrap_state"] == "unsubmitted"
    assert inspected["session"]["bootstrap_state"] == "unsubmitted"


def test_refresh_restores_started_state_from_durable_event_history() -> None:
    async def run() -> str:
        store = Store(":memory:")
        store.start()
        try:
            await store.open_session(
                HOST, NAME, provider="claude", created_at="2026-08-23T00:00:00Z",
            )
            await store.append_session_event(
                f"{HOST}:{NAME}",
                {
                    "kind": "USER",
                    "text": "initial prompt",
                    "timestamp": "2026-08-23T00:00:01Z",
                },
                identity="bootstrap-first-event",
                limit=500,
            )
            restarted = Sessions(store, local_host=HOST)
            await restarted.refresh()
            return str(restarted.list_open()[0]["bootstrap_state"])
        finally:
            store.stop()

    assert asyncio.run(run()) == "started"
