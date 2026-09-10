"""Authoritative post-watermark USER-event submission proof."""

from __future__ import annotations

import asyncio

import pytest

import submission_events as proof_module
from submission_events import (
    DurableUserEventProof,
    EventWatermark,
    PROOF_TERMINAL_BOUND_S,
)
from store import Store


STREAM = "hostb:v2-parent"
BODY = "[public-notice:notice-001]\nexact report notice"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay


class _MeasuredTail:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock

    async def fetch_session_event_tail(self, _stream_id: str, *, limit: int) -> list[dict]:
        events = [
            {"daemon_seq": 253987980, "stream_id": STREAM, "kind": "TOOL_RESULT", "text": "pre"},
        ]
        if self.clock.now > 0:
            events.extend([
                {"daemon_seq": 253996990, "stream_id": STREAM, "kind": "THINKING", "text": "Thinking"},
                {"daemon_seq": 253998493, "stream_id": STREAM, "kind": "ASSIST_TEXT", "text": "busy"},
                {"daemon_seq": 253999998, "stream_id": STREAM, "kind": "TOOL_USE", "text": "exec"},
                {"daemon_seq": 253999999, "stream_id": STREAM, "kind": "TOOL_RESULT", "text": "done"},
            ])
        if self.clock.now >= 10.494:
            events.append({
                "daemon_seq": 254000000,
                "stream_id": STREAM,
                "kind": "USER",
                "text": BODY,
                "timestamp": "2026-08-27T03:43:05.494Z",
            })
        return events[-limit:]


def test_delayed_exact_user_event_proves_within_shared_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(proof_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(proof_module.asyncio, "sleep", clock.sleep)
    proof = DurableUserEventProof(_MeasuredTail(clock), local_host="hosta")

    async def run() -> tuple[EventWatermark, object]:
        watermark = await proof.watermark(STREAM)
        observed = await proof.wait(
            STREAM,
            expected_text=BODY,
            watermark=watermark,
            timeout_s=PROOF_TERMINAL_BOUND_S,
        )
        return watermark, observed

    watermark, observed = asyncio.run(run())
    assert watermark.daemon_seq == 253987980
    assert observed.state == "proven"
    assert observed.event_id == 254000000
    assert observed.event_ts == "2026-08-27T03:43:05.494Z"
    assert clock.now >= 10.494
    assert clock.now < PROOF_TERMINAL_BOUND_S


def test_only_post_watermark_exact_user_is_authoritative() -> None:
    events = [
        {"daemon_seq": 10, "stream_id": STREAM, "kind": "USER", "text": BODY},
        {"daemon_seq": 11, "stream_id": STREAM, "kind": "SYSTEM", "text": BODY},
        {"daemon_seq": 12, "stream_id": STREAM, "kind": "TELL", "text": BODY},
        {"daemon_seq": 13, "stream_id": "hosta:other", "kind": "USER", "text": BODY},
        {"daemon_seq": 14, "stream_id": STREAM, "kind": "USER", "text": BODY + " changed"},
    ]

    class Tail:
        async def fetch_session_event_tail(self, _stream_id: str, *, limit: int) -> list[dict]:
            return events[-limit:]

    async def run() -> tuple[object, object]:
        proof = DurableUserEventProof(Tail(), local_host="hosta")
        watermark = EventWatermark(STREAM, 10, "reachable")
        rejected = await proof.lookup(STREAM, expected_text=BODY, watermark=watermark)
        events.append({
            "daemon_seq": 15,
            "stream_id": STREAM,
            "kind": "USER",
            "text": "[public-notice:notice-001]   exact\nreport notice",
            "timestamp": "2026-08-27T03:43:05.494Z",
        })
        accepted = await proof.lookup(STREAM, expected_text=BODY, watermark=watermark)
        return rejected, accepted

    rejected, accepted = asyncio.run(run())
    assert rejected.state == "pending"
    assert accepted.state == "proven"
    assert accepted.event_id == 15


def test_native_initial_prompt_allows_injected_user_context_but_requires_one_exact_match() -> None:
    events = [
        {
            "daemon_seq": 1,
            "stream_id": STREAM,
            "kind": "USER",
            "text": "# AGENTS.md instructions\nInjected provider context",
        },
        {"daemon_seq": 2, "stream_id": STREAM, "kind": "USER", "text": BODY},
        {"daemon_seq": 3, "stream_id": STREAM, "kind": "USER", "text": "More injected context"},
    ]

    class Tail:
        async def fetch_session_event_tail(self, _stream_id: str, *, limit: int) -> list[dict]:
            return events[-limit:]

    proof = DurableUserEventProof(Tail(), local_host="hosta")
    observed = asyncio.run(proof.wait_for_initial_user_event(
        STREAM, expected_text=BODY, timeout_s=0.1,
    ))
    assert observed.proven
    assert observed.event_id == 2
    events.append({"daemon_seq": 4, "stream_id": STREAM, "kind": "USER", "text": BODY})
    duplicate = asyncio.run(proof.wait_for_initial_user_event(
        STREAM, expected_text=BODY, timeout_s=0.1,
    ))
    assert duplicate.state == "rejected"
    assert duplicate.reason == "initial_user_event_not_exactly_once"


def test_native_initial_prompt_waits_past_early_injected_user_context() -> None:
    events = [{"daemon_seq": 1, "stream_id": STREAM, "kind": "USER", "text": "# AGENTS.md"}]

    class Tail:
        calls = 0

        async def fetch_session_event_tail(self, _stream_id: str, *, limit: int) -> list[dict]:
            self.calls += 1
            if self.calls == 2:
                events.append({"daemon_seq": 2, "stream_id": STREAM, "kind": "USER", "text": BODY})
            return events[-limit:]

    observed = asyncio.run(DurableUserEventProof(Tail(), local_host="hosta").wait_for_initial_user_event(
        STREAM, expected_text=BODY, timeout_s=0.6,
    ))
    assert observed.proven
    assert observed.event_id == 2


def test_native_initial_prompt_rejects_whitespace_normalized_lookalike() -> None:
    events = [{
        "daemon_seq": 1,
        "stream_id": STREAM,
        "kind": "USER",
            "text": "[public-notice:notice-001]   exact\nreport notice",
    }]

    class Tail:
        async def fetch_session_event_tail(self, _stream_id: str, *, limit: int) -> list[dict]:
            return events[-limit:]

    proof = DurableUserEventProof(Tail(), local_host="hosta")
    observed = asyncio.run(proof.wait_for_initial_user_event(
        STREAM, expected_text=BODY, timeout_s=0.1,
    ))
    assert observed.state == "rejected"
    assert observed.reason == "initial_user_event_mismatch"


def test_peer_confirmation_uses_an_in_memory_store() -> None:
    class InMemoryTailStore:
        calls = 0

        async def fetch_session_event_tail(self, stream_id: str, *, limit: int) -> list[dict]:
            self.calls += 1
            assert stream_id == STREAM
            return [{
                "daemon_seq": 11,
                "stream_id": STREAM,
                "kind": "USER",
                "text": BODY,
            }]

    tail_store = InMemoryTailStore()
    proof = DurableUserEventProof(tail_store, local_host="hosta")
    observed = asyncio.run(proof.lookup(
        STREAM,
        expected_text=BODY,
        watermark=EventWatermark(STREAM, 10, "reachable"),
    ))

    assert observed.proven
    assert observed.event_id == 11
    assert tail_store.calls == 1


def test_pre_action_watermark_retries_transient_unreachable_with_one_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(proof_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(proof_module.asyncio, "sleep", clock.sleep)

    class InMemoryTailStore:
        def __init__(self) -> None:
            self.calls = 0

        async def fetch_session_event_tail(self, _stream_id: str, *, limit: int) -> list[dict]:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient")
            return [{"daemon_seq": 7, "stream_id": STREAM, "kind": "TOOL_RESULT"}]

    tail_store = InMemoryTailStore()
    proof = DurableUserEventProof(tail_store, local_host="hosta")
    watermark = asyncio.run(proof.wait_for_watermark(STREAM, timeout_s=1.0))

    assert watermark == EventWatermark(STREAM, 7, "reachable")
    assert tail_store.calls == 2
    assert clock.now == proof_module.PROOF_POLL_S


def test_post_submit_lookup_recovers_from_transient_unreachable_without_resetting_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(proof_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(proof_module.asyncio, "sleep", clock.sleep)

    class InMemoryTailStore:
        def __init__(self) -> None:
            self.calls = 0

        async def fetch_session_event_tail(self, _stream_id: str, *, limit: int) -> list[dict]:
            self.calls += 1
            if self.calls == 1:
                return [{"daemon_seq": 10, "stream_id": STREAM, "kind": "TOOL_RESULT"}]
            if self.calls == 2:
                raise RuntimeError("transient")
            return [{
                "daemon_seq": 11, "stream_id": STREAM, "kind": "USER", "text": BODY,
            }]

    tail_store = InMemoryTailStore()
    proof = DurableUserEventProof(tail_store, local_host="hosta")

    async def run() -> tuple[EventWatermark, EventProof]:
        watermark = await proof.watermark(STREAM)
        observed = await proof.wait(
            STREAM, expected_text=BODY, watermark=watermark, timeout_s=1.0,
        )
        return watermark, observed

    watermark, observed = asyncio.run(run())

    assert watermark.daemon_seq == 10
    assert observed.proven
    assert observed.event_id == 11
    assert tail_store.calls == 3
    assert clock.now == proof_module.PROOF_POLL_S


def test_pushed_peer_tail_is_generation_fenced_and_exact() -> None:
    async def run() -> tuple[object, object, list[dict]]:
        store = Store(":memory:")
        store.start()
        try:
            await store.open_session(
                "hostb", "v2-parent", provider="codex",
                pane_pid="3251384", pane_status="pane_alive",
            )
            old_lifecycle = await store.fetch_open_session_lifecycle(
                STREAM, pane_pid="3251384",
            )
            assert old_lifecycle is not None
            old_entry = {
                "stream_id": STREAM,
                "event": {"stream_id": STREAM, "kind": "USER", "text": BODY},
                "identity": "codex-old-generation",
                "lifecycle": old_lifecycle,
            }
            assert await store.append_session_events_lifecycle_cas([old_entry], limit=500)
            assert await store.mark_closed(
                "hostb", "v2-parent", closed_at="2026-08-28T11:27:00Z",
                pane_status="pane_dead", expected_generation=old_lifecycle["generation"],
                close_kind="test",
            )
            await store.open_session(
                "hostb", "v2-parent", provider="codex",
                pane_pid="3253878", pane_status="pane_alive",
            )
            current_lifecycle = await store.fetch_open_session_lifecycle(
                STREAM, pane_pid="3253878",
            )
            assert current_lifecycle is not None

            proof = DurableUserEventProof(store, local_host="hosta")
            stale = await proof.lookup(
                STREAM, expected_text=BODY,
                watermark=EventWatermark(STREAM, 0, "reachable"),
            )
            assert stale.state == "pending"

            await store.open_session(
                "hostb", "v2-sibling", provider="codex",
                pane_pid="3254000", pane_status="pane_alive",
            )
            sibling_lifecycle = await store.fetch_open_session_lifecycle(
                "hostb:v2-sibling", pane_pid="3254000",
            )
            assert sibling_lifecycle is not None
            assert await store.append_session_events_lifecycle_cas([{
                "stream_id": "hostb:v2-sibling",
                "event": {
                    "stream_id": "hostb:v2-sibling", "kind": "USER", "text": BODY,
                },
                "identity": "codex-sibling",
                "lifecycle": sibling_lifecycle,
            }], limit=500)
            assert await store.append_session_events_lifecycle_cas([
                {
                    "stream_id": STREAM,
                    "event": {
                        "stream_id": STREAM, "kind": "USER", "text": BODY,
                        "timestamp": "2026-08-28T11:26:39.537Z",
                    },
                        "identity": "codex-current-user",
                    "lifecycle": current_lifecycle,
                },
                {
                    "stream_id": STREAM,
                    "event": {
                        "stream_id": STREAM, "kind": "ASSIST_TEXT", "text": "assistant",
                        "timestamp": "2026-08-28T11:26:44.379Z",
                    },
                        "identity": "codex-current-assistant",
                    "lifecycle": current_lifecycle,
                },
                {
                    "stream_id": STREAM,
                    "event": {
                        "stream_id": STREAM, "kind": "TOOL_RESULT", "text": "tool",
                        "timestamp": "2026-08-28T11:26:50.212Z",
                    },
                        "identity": "codex-current-tool",
                    "lifecycle": current_lifecycle,
                },
            ], limit=500)
            tail = await store.fetch_session_event_tail(STREAM, limit=500)
            observed = await proof.lookup(
                STREAM, expected_text=BODY,
                watermark=EventWatermark(STREAM, 0, "reachable"),
            )
            return stale, observed, tail
        finally:
            store.stop()

    stale, observed, tail = asyncio.run(run())
    assert stale.state == "pending"
    assert observed.proven
    assert [event["kind"] for event in tail] == ["USER", "ASSIST_TEXT", "TOOL_RESULT"]
