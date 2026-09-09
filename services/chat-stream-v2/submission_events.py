"""One authoritative post-watermark USER-event submission proof.

Pane text, transcript discovery, queue chrome, and acknowledgement fields are
advisory.  A submitted body is proven only by a generation-fenced durable USER
event from the stream's authoritative Store.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
import time
from typing import Any


# Measured production evidence: remote no-proof baseline 2.25s. Product and
# harness import these exact bounds.
PROOF_FAST_WAIT_S = 2.25
REMOTE_EVENT_LOOKUP_BOUND_S = 3.0
PROOF_TERMINAL_BOUND_S = 20.0
# A newly admitted pane gets a larger one-shot budget for its initial USER
# event: the 2026-09-03 production burst reached a 106.4s ingest tail.
# Keep the general proof bound above short so hard submit failures and outbound
# notice classification do not inherit the spawn-only tail allowance.
SPAWN_SUBMISSION_PROOF_BOUND_S = 120.0
PROOF_POLL_S = 0.25

# The single non-fatal wire status for a durably-committed action (paste left
# the composer, no active draft) whose async USER-event proof has not yet
# landed inside the proof window.  It is exit-0 / do_not_resubmit and reconciles
# asynchronously (promote_tell_delivery / outbound-notice retry).  Writers emit
# COMMITTED_PENDING_PROOF; readers also accept the legacy pre-rename statuses,
# which denoted the identical committed-but-late-proof state.
COMMITTED_PENDING_PROOF = "committed_pending_proof"
COMMITTED_PENDING_PROOF_STATUSES = frozenset(
    {COMMITTED_PENDING_PROOF, "proof_pending", "proof_unavailable"}
)

_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def normalize_submission_text(text: object) -> str:
    cleaned = _ANSI_RE.sub("", str(text or "")).replace("\r", "\n").replace("\u00a0", " ")
    lines = [line.rstrip() for line in cleaned.split("\n")]
    return re.sub(r"\s+", " ", "\n".join(lines).strip()).strip()


@dataclass(frozen=True)
class EventWatermark:
    stream_id: str
    daemon_seq: int
    state: str
    reason: str = ""


@dataclass(frozen=True)
class EventProof:
    state: str
    stream_id: str
    watermark: int
    event_id: int | None = None
    event_ts: str | None = None
    reason: str = ""

    @property
    def proven(self) -> bool:
        return self.state == "proven"

    def audit_fields(self) -> dict[str, Any]:
        return {
            "proof_state": self.state,
            "proof_watermark": self.watermark,
            **({"proof_event_id": self.event_id} if self.event_id is not None else {}),
            **({"proof_event_at": self.event_ts} if self.event_ts else {}),
            **({"proof_reason": self.reason} if self.reason else {}),
        }


class DurableUserEventProof:
    def __init__(self, store: Any, *, local_host: str) -> None:
        self.store = store
        self.local_host = str(local_host or "")

    @staticmethod
    def _seq(event: dict[str, Any]) -> int:
        try:
            return max(0, int(event.get("daemon_seq") or 0))
        except (TypeError, ValueError):
            return 0

    async def _events(
        self, stream_id: str, *, after: int, timeout_s: float | None = None,
    ) -> tuple[str, list[dict], str]:
        if timeout_s is not None and float(timeout_s) <= 0:
            return "unreachable", [], "event_store_deadline_expired"
        host, separator, _name = str(stream_id or "").partition(":")
        if not separator or not host:
            return "unreachable", [], "invalid_stream_id"
        # The satellites do not run a Store.  Their normalized provider events
        # are pushed to coordinator, where `fetch_session_event_tail` joins each row to
        # the current `sessions.created_at` generation.  That deployed Store is
        # the sole confirmation authority for local *and* remote stream IDs.
        try:
            read = self.store.fetch_session_event_tail(stream_id, limit=500)
            events = await (
                asyncio.wait_for(read, timeout=max(0.001, float(timeout_s)))
                if timeout_s is not None else read
            )
        except asyncio.TimeoutError:
            return "unreachable", [], "event_store_timeout"
        except RuntimeError as exc:
            return "unreachable", [], str(exc)
        except Exception as exc:  # noqa: BLE001 - proof failure remains pending
            return "unreachable", [], f"event_store_error:{exc}"
        return "reachable", (
            [
                event for event in events
                if isinstance(event, dict) and self._seq(event) > after
            ]
            if isinstance(events, list) else []
        ), ""

    async def watermark(self, stream_id: str) -> EventWatermark:
        """Take one watermark snapshot.

        This deliberately remains one-shot: callers and tests use it when a
        single pre-action read is the contract.  Retry-capable action paths use
        :meth:`wait_for_watermark` below.
        """
        state, events, reason = await self._events(stream_id, after=0)
        if state != "reachable":
            return EventWatermark(stream_id, 0, "unreachable", reason)
        return EventWatermark(
            stream_id,
            max((self._seq(event) for event in events), default=0),
            "reachable",
        )

    async def wait_for_watermark(
        self,
        stream_id: str,
        *,
        timeout_s: float = PROOF_FAST_WAIT_S,
        absolute_deadline: float | None = None,
    ) -> EventWatermark:
        """Retry the pre-action snapshot inside one absolute deadline.

        A watermark is captured before the side effect.  A transient read
        failure therefore gets a small bounded recovery window, but the
        operation never extends its deadline or fabricates a watermark after
        the action.  Every retry passes the remaining budget to the reader.
        """
        start = time.monotonic()
        deadline = start + max(0.0, float(timeout_s))
        if absolute_deadline is not None:
            deadline = min(deadline, float(absolute_deadline))
        last = EventWatermark(stream_id, 0, "unreachable", "watermark_deadline_expired")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return last
            state, events, reason = await self._events(
                stream_id, after=0, timeout_s=remaining,
            )
            if state == "reachable":
                return EventWatermark(
                    stream_id,
                    max((self._seq(event) for event in events), default=0),
                    "reachable",
                )
            last = EventWatermark(stream_id, 0, "unreachable", reason)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return last
            await asyncio.sleep(min(PROOF_POLL_S, remaining))

    async def lookup(
        self,
        stream_id: str,
        *,
        expected_text: str,
        watermark: EventWatermark,
        timeout_s: float | None = None,
    ) -> EventProof:
        if watermark.stream_id != stream_id:
            return EventProof("unreachable", stream_id, watermark.daemon_seq, reason="watermark_stream_mismatch")
        if watermark.state != "reachable":
            return EventProof(
                "unreachable", stream_id, watermark.daemon_seq,
                reason=watermark.reason or "event_watermark_unreachable",
            )
        state, events, reason = await self._events(
            stream_id, after=watermark.daemon_seq, timeout_s=timeout_s,
        )
        if state != "reachable":
            return EventProof("unreachable", stream_id, watermark.daemon_seq, reason=reason)
        expected = normalize_submission_text(expected_text)
        for event in events:
            event_id = self._seq(event)
            if (
                event_id > watermark.daemon_seq
                and str(event.get("stream_id") or stream_id) == stream_id
                and str(event.get("kind") or "") == "USER"
                and normalize_submission_text(event.get("text")) == expected
            ):
                return EventProof(
                    "proven",
                    stream_id,
                    watermark.daemon_seq,
                    event_id=event_id,
                    event_ts=str(event.get("timestamp") or "") or None,
                )
        return EventProof("pending", stream_id, watermark.daemon_seq)

    async def wait(
        self,
        stream_id: str,
        *,
        expected_text: str,
        watermark: EventWatermark,
        timeout_s: float,
    ) -> EventProof:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        observed = EventProof("pending", stream_id, watermark.daemon_seq)
        if watermark.state != "reachable":
            return await self.lookup(
                stream_id,
                expected_text=expected_text,
                watermark=watermark,
            )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return observed
            observed = await self.lookup(
                stream_id,
                expected_text=expected_text,
                watermark=watermark,
                timeout_s=remaining,
            )
            if observed.state not in {"pending", "unreachable"}:
                return observed
            if time.monotonic() >= deadline:
                return observed
            await asyncio.sleep(min(PROOF_POLL_S, max(0.0, deadline - time.monotonic())))

    async def wait_for_initial_user_event(
        self, stream_id: str, *, expected_text: str, timeout_s: float,
    ) -> EventProof:
        """Confirm a native initial prompt from exactly one matching USER event.

        A newly admitted stream has a generation-fenced event tail, so native
        argv delivery needs no pre-action watermark, pane needle, or paste
        recovery. The complete staged prompt must occur exactly once; unrelated
        provider-injected USER context does not participate in that proof.
        """
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        observed = EventProof("pending", stream_id, 0)
        # Native argv delivery is a literal payload contract. Unlike the legacy
        # post-paste proof, it must not normalize whitespace, line endings, or
        # any other bytes before confirming Codex's first USER event.
        expected = expected_text
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return observed
            state, events, reason = await self._events(
                stream_id, after=0, timeout_s=remaining,
            )
            if state != "reachable":
                observed = EventProof("unreachable", stream_id, 0, reason=reason)
            else:
                user_events = [
                    event for event in events
                    if str(event.get("stream_id") or stream_id) == stream_id
                    and str(event.get("kind") or "") == "USER"
                ]
                if user_events:
                    matching_events = [
                        event for event in user_events
                        if isinstance(event.get("text"), str) and event["text"] == expected
                    ]
                    if not matching_events:
                        first = user_events[0]
                        observed = EventProof(
                            "rejected", stream_id, 0,
                            event_id=self._seq(first),
                            event_ts=str(first.get("timestamp") or "") or None,
                            reason="initial_user_event_mismatch",
                        )
                    elif len(matching_events) != 1:
                        first = matching_events[0]
                        return EventProof(
                            "rejected", stream_id, 0,
                            event_id=self._seq(first),
                            event_ts=str(first.get("timestamp") or "") or None,
                            reason="initial_user_event_not_exactly_once",
                        )
                    else:
                        first = matching_events[0]
                        return EventProof(
                            "proven", stream_id, 0,
                            event_id=self._seq(first),
                            event_ts=str(first.get("timestamp") or "") or None,
                        )
                else:
                    observed = EventProof("pending", stream_id, 0)
            await asyncio.sleep(min(PROOF_POLL_S, max(0.0, deadline - time.monotonic())))
