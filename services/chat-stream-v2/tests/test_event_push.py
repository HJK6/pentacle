"""Unit tier for the `event.push` verb (satellite ingest sink, lane 20).

Proves the sink reuses the local-ingest exactly-once floor (insert +
broadcast-iff-inserted), authenticates before any side effect, and drives the
version gate — each assertion fails if its behaviour is reverted.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock

from event_push import WIRE_VERSION, EventPush  # noqa: E402
from inventory import InventoryEmitter  # noqa: E402
from presence import RemotePresence  # noqa: E402
from routing_integrity import RoutingIntegrity  # noqa: E402
from sessions import Sessions  # noqa: E402
from store import Store  # noqa: E402


class _Alerts:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    def emit(self, kind: str, **fields: object) -> None:
        self.emitted.append((kind, dict(fields)))


class _RoutingObserver:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def observe_claude_event(self, payload: dict) -> None:
        self.payloads.append(payload)


def _sink(
    store: Store,
    *,
    secret: str | None = "s3cret",
    enabled: bool = True,
    routing_integrity=None,
    presence=None,
    sessions=None,
    daemon_sha: str = "",
    emit_inventory: bool = False,
):
    broadcasts: list[dict] = []

    async def broadcast(frame: dict) -> None:
        broadcasts.append(frame)

    alerts = _Alerts()
    inventory_emitter = (
        InventoryEmitter(sessions, broadcast, min_interval_s=0)
        if emit_inventory else None
    )
    ep = EventPush(
        store,
        broadcast,
        alerts,
        recent_limit=500,
        enabled=enabled,
        routing_integrity=routing_integrity,
        presence=presence,
        sessions=sessions,
        inventory_emitter=inventory_emitter,
        daemon_sha=daemon_sha,
    )
    # Deterministic secret without touching the process env.
    ep._secret = lambda: _wrap(secret)  # type: ignore[assignment]
    return ep, broadcasts, alerts


async def _wrap(value):
    return value


def _ev(uuid: str, text: str = "hi", *, stream_id: str = "hostc:v2-abc") -> dict:
    return {
        "stream_id": stream_id, "provider": "claude", "kind": "USER", "text": text,
        "timestamp": "2026-08-05T00:00:00Z",
        "raw": {"jsonl_record_uuid": uuid, "jsonl_event_index": 0},
    }


def _claude_fork_ev(uuid: str, session_id: str, text: str, *, stream_id: str) -> dict:
    event = _ev(uuid, text, stream_id=stream_id)
    event["session_id"] = session_id
    event["raw"]["source_session_identity"] = session_id
    return event


OLD_CODEX_ROLLOUT = "11111111-1111-4111-8111-111111111111"
CURRENT_CODEX_ROLLOUT = "22222222-2222-4222-8222-222222222222"
TARGET_SHA = "a" * 40
SATELLITE_SHA = "b" * 40
SATELLITE_AHEAD_SHA = "c" * 40
SATELLITE_DIVERGENT_SHA = "d" * 40


def _codex_ev(rollout_id: str, ordinal: int, *, stream_id: str) -> dict:
    """One valid normalized Codex event as it reached pre-P0 hosta.

    `session_id` and the normalizer-stamped raw identity are deliberately both
    present and equal: this is not the easy empty-identity case.  Pre-P0 had no
    pane/lifecycle admission proof, which is why an old rollout could be
    transiently eligible for a current stream.
    """
    return {"objective": "Exercise the existing spawn contract",
        "stream_id": stream_id,
        "provider": "codex",
        "session_id": rollout_id,
        "session_name": stream_id.split(":", 1)[1],
        "kind": "USER",
        "text": f"foreign Codex event {ordinal}",
        "timestamp": f"2026-08-10T23:3{ordinal // 60}:{ordinal % 60:02d}Z",
        "raw": {
            "jsonl_record_uuid": f"old-{ordinal}",
            "jsonl_event_index": 0,
            "source_session_identity": rollout_id,
        },
    }


def test_p0_prefix_old_rollout_is_transiently_eligible_for_current_stream() -> None:
    """Fail-first incident fixture: 104 old events precede the current rollout.

    The `sessions` row models the current pane/generation.  Before P0, the sink
    ignores both that state and the old-but-valid Codex identity, so it stamps
    all 104 foreign rows with this current stream id and broadcasts them.
    """
    async def _go() -> None:
        stream_id = "hostc:v2-codex-current"
        store = Store(":memory:"); store.start()
        try:
            await store.open_session(
                "hostc", "v2-codex-current", visibility="visible",
                provider="codex", pane_pid="8123",
            )
            ep, casts, _ = _sink(store)
            foreign = [_codex_ev(OLD_CODEX_ROLLOUT, i, stream_id=stream_id) for i in range(104)]
            assert OLD_CODEX_ROLLOUT != CURRENT_CODEX_ROLLOUT
            result = await ep.handle_push(_push(ep, foreign))
            tail = await store.fetch_session_event_tail(stream_id, limit=500)
            counted = await _count_user_and_tell(store, stream_id)
            # P0 boundary: old, non-empty identity must be rejected before any
            # durable append or broadcast.  This tuple exposes the historical
            # pre-fix contamination exactly when it fails.
            assert (
                result.get("type"), result.get("accepted"), result.get("inserted"),
                result.get("dropped"), len(casts), len(tail), counted,
            ) == ("event.push.ok", 0, 0, [{
                "stream_id": stream_id, "reason": "codex_identity_unproven",
            }], 0, 0, 0)
        finally:
            store.stop()

    _run(_go())


def _proven_codex_ev(rollout_id: str, ordinal: int, *, stream_id: str, pane_pid: str = "8123") -> dict:
    event = _codex_ev(rollout_id, ordinal, stream_id=stream_id)
    event["source_pane_pid"] = pane_pid
    return event


async def _raw_tail_rows(store: Store, stream_id: str) -> int:
    def _op(conn) -> int:
        return int(conn.execute(
            "SELECT COUNT(*) FROM session_event_tail WHERE stream_id=?", (stream_id,),
        ).fetchone()[0])

    return await store.submit(_op)


def test_unmatched_codex_entry_is_dropped_not_batch_rejected() -> None:
    async def _go() -> None:
        live_stream = "hostc:v2-codex-live"
        closed_stream = "hostc:fleet-smoke-codex-closed"
        store = Store(":memory:"); store.start()
        try:
            await store.open_session(
                "hostc", "v2-codex-live", visibility="visible",
                provider="codex", pane_pid="8123",
            )
            await store.open_session(
                "hostc", "fleet-smoke-codex-closed", visibility="visible",
                provider="codex", pane_pid="7001",
            )
            lifecycle = await store.fetch_open_session_lifecycle(closed_stream, pane_pid="7001")
            assert lifecycle is not None
            assert await store.mark_closed(
                "hostc", "fleet-smoke-codex-closed",
                closed_at="2026-09-02T12:00:00Z", pane_status="pane_dead",
                expected_generation=lifecycle["generation"],
            ) is not None
            ep, casts, _ = _sink(store)

            result = await ep.handle_push(_push(ep, [
                _proven_codex_ev(CURRENT_CODEX_ROLLOUT, 1, stream_id=live_stream),
                _proven_codex_ev(CURRENT_CODEX_ROLLOUT, 2, stream_id=closed_stream, pane_pid="7001"),
            ]))

            assert result["type"] == "event.push.ok"
            assert result["accepted"] == result["inserted"] == 1
            assert result["dropped"] == [{
                "stream_id": closed_stream, "reason": "codex_identity_unproven",
            }]
            assert len(casts) == 1
            assert len(await store.fetch_session_event_tail(live_stream, limit=500)) == 1
            assert await store.fetch_session_event_tail(closed_stream, limit=500) == []
        finally:
            store.stop()

    _run(_go())


def test_missing_source_pane_pid_drops_only_that_entry() -> None:
    async def _go() -> None:
        live_stream = "hostc:v2-codex-live"
        missing_proof_stream = "hostc:fleet-smoke-codex-missing-proof"
        store = Store(":memory:"); store.start()
        try:
            await store.open_session(
                "hostc", "v2-codex-live", visibility="visible",
                provider="codex", pane_pid="8123",
            )
            ep, casts, _ = _sink(store)

            result = await ep.handle_push(_push(ep, [
                _proven_codex_ev(CURRENT_CODEX_ROLLOUT, 1, stream_id=live_stream),
                _codex_ev(CURRENT_CODEX_ROLLOUT, 2, stream_id=missing_proof_stream),
            ]))

            assert result["type"] == "event.push.ok"
            assert result["accepted"] == result["inserted"] == 1
            assert result["dropped"] == [{
                "stream_id": missing_proof_stream, "reason": "codex_identity_unproven",
            }]
            assert len(casts) == 1
            assert len(await store.fetch_session_event_tail(live_stream, limit=500)) == 1
        finally:
            store.stop()

    _run(_go())


def test_drop_logs_once_per_stream(caplog) -> None:
    async def _go() -> None:
        stream_id = "hostc:fleet-smoke-codex-closed"
        store = Store(":memory:"); store.start()
        try:
            ep, _casts, _ = _sink(store)
            event = _proven_codex_ev(CURRENT_CODEX_ROLLOUT, 1, stream_id=stream_id, pane_pid="7001")
            for _ in range(2):
                result = await ep.handle_push(_push(ep, [event]))
                assert result["type"] == "event.push.ok"
                assert result["dropped"] == [{
                    "stream_id": stream_id, "reason": "codex_identity_unproven",
                }]
        finally:
            store.stop()

    caplog.set_level(logging.WARNING, logger="chat_streamd_v2.event_push")
    _run(_go())
    drops = [record for record in caplog.records if "event.push dropped" in record.getMessage()]
    assert len(drops) == 1
    assert "hostc:fleet-smoke-codex-closed" in drops[0].getMessage()


async def _count_user_and_tell(store: Store, stream_id: str) -> int:
    host, separator, session_name = stream_id.partition(":")
    assert separator and host and session_name
    row = await store.fetch_session(host, session_name)
    assert row is not None
    lifecycle = str(row["created_at"])
    return sum(
        [
            await store.count_session_events(
                stream_id, kind=kind, session_created_at=lifecycle,
            )
            for kind in ("USER", "TELL")
        ]
    )


def test_p0_codex_current_pane_is_admitted_and_legacy_lifecycle_is_hidden() -> None:
    async def _go() -> None:
        stream_id = "hostc:v2-codex-current"
        store = Store(":memory:"); store.start()
        try:
            # This represents the known historical blank-lifecycle contamination:
            # it is retained as evidence, but no current stream read may expose it.
            await store.append_session_event(
                stream_id, _codex_ev(OLD_CODEX_ROLLOUT, 0, stream_id=stream_id),
                identity="old-blank-lifecycle", limit=500,
            )
            await store.open_session(
                "hostc", "v2-codex-current", visibility="visible",
                provider="codex", pane_pid="8123",
            )
            ep, casts, _ = _sink(store)
            current = _proven_codex_ev(CURRENT_CODEX_ROLLOUT, 1, stream_id=stream_id)
            result = await ep.handle_push(_push(ep, [current]))
            tail = await store.fetch_session_event_tail(stream_id, limit=500)

            assert result["type"] == "event.push.ok" and result["inserted"] == 1
            assert len(casts) == 1
            assert len(tail) == 1 and tail[0]["session_id"] == CURRENT_CODEX_ROLLOUT
            assert "source_pane_pid" not in tail[0], "source proof must stay ephemeral"
            assert await _count_user_and_tell(store, stream_id) == 1
            assert await _raw_tail_rows(store, stream_id) == 2
        finally:
            store.stop()

    _run(_go())


def test_p0_codex_rejects_empty_foreign_and_closed_admissions() -> None:
    async def _go() -> None:
        stream_id = "hostc:v2-codex-current"
        store = Store(":memory:"); store.start()
        try:
            await store.open_session(
                "hostc", "v2-codex-current", visibility="visible",
                provider="codex", pane_pid="8123",
            )
            ep, casts, _ = _sink(store)
            empty = _proven_codex_ev("", 1, stream_id=stream_id)
            foreign = _proven_codex_ev(OLD_CODEX_ROLLOUT, 2, stream_id=stream_id, pane_pid="7001")
            wrong_pane = _proven_codex_ev(CURRENT_CODEX_ROLLOUT, 3, stream_id=stream_id, pane_pid="7001")
            unknown = _proven_codex_ev(
                CURRENT_CODEX_ROLLOUT, 4, stream_id="hostc:v2-codex-unknown",
            )
            mismatched_identity = _proven_codex_ev(CURRENT_CODEX_ROLLOUT, 5, stream_id=stream_id)
            mismatched_identity["raw"]["source_session_identity"] = OLD_CODEX_ROLLOUT
            for event in (empty, mismatched_identity):
                result = await ep.handle_push(_push(ep, [event]))
                assert result == {
                    "type": "event.push.error", "request_id": 7,
                    "error": "codex_identity_unproven",
                }
                assert casts == []
                assert await _raw_tail_rows(store, stream_id) == 0

            for event in (foreign, wrong_pane, unknown):
                result = await ep.handle_push(_push(ep, [event]))
                assert result["type"] == "event.push.ok"
                assert result["accepted"] == result["inserted"] == 0
                assert result["dropped"] == [{
                    "stream_id": event["stream_id"], "reason": "codex_identity_unproven",
                }]
                assert casts == []
                assert await _raw_tail_rows(store, stream_id) == 0

            lifecycle = await store.fetch_open_session_lifecycle(stream_id, pane_pid="8123")
            assert lifecycle is not None
            assert await store.mark_closed(
                "hostc", "v2-codex-current", closed_at="2026-08-13T15:06:00Z",
                pane_status="pane_dead", expected_generation=lifecycle["generation"],
            ) is not None
            closed = _proven_codex_ev(CURRENT_CODEX_ROLLOUT, 6, stream_id=stream_id)
            result = await ep.handle_push(_push(ep, [closed]))
            assert result["type"] == "event.push.ok"
            assert result["request_id"] == 7
            assert result["accepted"] == result["inserted"] == 0
            assert result["dropped"] == [{
                "stream_id": stream_id, "reason": "codex_identity_unproven",
            }]
            assert result["version"]["status"] == "ok"
            assert casts == [] and await _raw_tail_rows(store, stream_id) == 0
        finally:
            store.stop()

    _run(_go())


def test_p0_lifecycle_cas_loss_after_admission_drops_without_broadcast() -> None:
    async def _go() -> None:
        stream_id = "hostc:v2-codex-current"
        store = Store(":memory:"); store.start()
        try:
            await store.open_session(
                "hostc", "v2-codex-current", visibility="visible",
                provider="codex", pane_pid="8123",
            )
            lifecycle = await store.fetch_open_session_lifecycle(stream_id, pane_pid="8123")
            assert lifecycle is not None
            original_append = store.append_session_events_lifecycle_cas

            async def _close_after_admission(entries, *, limit):
                assert await store.mark_closed(
                    "hostc", "v2-codex-current", closed_at="2026-08-13T15:07:00Z",
                    pane_status="pane_dead", expected_generation=lifecycle["generation"],
                ) is not None
                return await original_append(entries, limit=limit)

            store.append_session_events_lifecycle_cas = _close_after_admission  # type: ignore[method-assign]
            ep, casts, _ = _sink(store)
            result = await ep.handle_push(_push(ep, [
                _proven_codex_ev(CURRENT_CODEX_ROLLOUT, 4, stream_id=stream_id),
            ]))

            assert result["type"] == "event.push.ok"
            assert result["accepted"] == result["inserted"] == 0
            assert result["dropped"] == [{
                "stream_id": stream_id, "reason": "lifecycle_changed",
            }]
            assert casts == []
            assert await _raw_tail_rows(store, stream_id) == 0
            assert await store.fetch_session_event_tail(stream_id, limit=500) == []
            assert await _count_user_and_tell(store, stream_id) == 0
        finally:
            store.stop()

    _run(_go())


def test_closed_claude_row_drops_only_that_entry() -> None:
    """The live host-wide outage: one leaked pane must not stop the host.

    A reaped-but-alive Claude pane keeps emitting transcript lines with a
    bound `claude_session_id` against a `closed` row.  Before the per-entry
    disposition, its binding predicate rolled the WHOLE push back, so every
    other session on that host stopped ingesting for as long as the stale pane
    lived (hostc: 38 `lifecycle_changed` rejections / 60s).
    """
    async def _go() -> None:
        live_stream = "hostc:v2-claude-live"
        closed_stream = "hostc:fleet-smoke-claude-leaked"
        store = Store(":memory:"); store.start()
        try:
            await store.open_session(
                "hostc", "v2-claude-live", visibility="visible", provider="claude",
            )
            await store.open_session(
                "hostc", "fleet-smoke-claude-leaked", visibility="visible",
                provider="claude", pane_pid="7001",
            )
            leaked = await store.fetch_open_session_lifecycle(closed_stream, pane_pid="7001")
            assert leaked is not None
            assert await store.mark_closed(
                "hostc", "fleet-smoke-claude-leaked",
                closed_at="2026-09-02T12:00:00Z", pane_status="pane_dead",
                expected_generation=leaked["generation"],
            ) is not None
            ep, casts, _ = _sink(store)

            result = await ep.handle_push(_push(ep, [
                _claude_fork_ev("live-1", "sess-live", "live line", stream_id=live_stream),
                _claude_fork_ev("leaked-1", "sess-leaked", "stale line", stream_id=closed_stream),
            ]))

            assert result["type"] == "event.push.ok"
            assert result["accepted"] == result["inserted"] == 1
            assert result["dropped"] == [{
                "stream_id": closed_stream, "reason": "lifecycle_changed",
            }]
            assert [cast["event"]["stream_id"] for cast in casts] == [live_stream]
            assert len(await store.fetch_session_event_tail(live_stream, limit=500)) == 1
            assert await _raw_tail_rows(store, closed_stream) == 0
        finally:
            store.stop()

    _run(_go())


def test_codex_lifecycle_cas_race_drops_only_that_entry() -> None:
    """Same verdict one provider over: a close/reopen race after admission."""
    async def _go() -> None:
        live_stream = "hostc:v2-codex-live"
        raced_stream = "hostc:v2-codex-raced"
        store = Store(":memory:"); store.start()
        try:
            await store.open_session(
                "hostc", "v2-codex-live", visibility="visible",
                provider="codex", pane_pid="8123",
            )
            await store.open_session(
                "hostc", "v2-codex-raced", visibility="visible",
                provider="codex", pane_pid="7001",
            )
            raced = await store.fetch_open_session_lifecycle(raced_stream, pane_pid="7001")
            assert raced is not None
            original_append = store.append_session_events_lifecycle_cas

            async def _close_raced_after_admission(entries, *, limit):
                assert await store.mark_closed(
                    "hostc", "v2-codex-raced", closed_at="2026-09-02T12:00:00Z",
                    pane_status="pane_dead", expected_generation=raced["generation"],
                ) is not None
                return await original_append(entries, limit=limit)

            store.append_session_events_lifecycle_cas = _close_raced_after_admission  # type: ignore[method-assign]
            ep, casts, _ = _sink(store)

            result = await ep.handle_push(_push(ep, [
                _proven_codex_ev(CURRENT_CODEX_ROLLOUT, 1, stream_id=live_stream),
                _proven_codex_ev(CURRENT_CODEX_ROLLOUT, 2, stream_id=raced_stream, pane_pid="7001"),
            ]))

            assert result["type"] == "event.push.ok"
            assert result["accepted"] == result["inserted"] == 1
            assert result["dropped"] == [{
                "stream_id": raced_stream, "reason": "lifecycle_changed",
            }]
            assert [cast["event"]["stream_id"] for cast in casts] == [live_stream]
            assert len(await store.fetch_session_event_tail(live_stream, limit=500)) == 1
            assert await _raw_tail_rows(store, raced_stream) == 0
        finally:
            store.stop()

    _run(_go())


def test_storage_fault_still_rolls_the_whole_batch_back() -> None:
    """A predicate miss drops one entry; a real fault still fails closed."""
    async def _go() -> None:
        live_stream = "hostc:v2-claude-live"
        poisoned_stream = "hostc:v2-claude-poisoned"
        store = Store(":memory:"); store.start()
        try:
            await store.open_session(
                "hostc", "v2-claude-live", visibility="visible", provider="claude",
            )
            await store.open_session(
                "hostc", "v2-claude-poisoned", visibility="visible", provider="claude",
            )

            def _arm_fault(conn) -> None:
                conn.execute(
                    """CREATE TRIGGER forced_storage_fault
                       BEFORE INSERT ON session_event_tail
                       WHEN NEW.stream_id=?
                       BEGIN SELECT RAISE(ABORT, 'forced storage fault'); END"""
                    .replace("?", f"'{poisoned_stream}'"),
                )

            await store.submit(_arm_fault)
            ep, casts, _ = _sink(store)

            result = await ep.handle_push(_push(ep, [
                _ev("live-1", "live line", stream_id=live_stream),
                _ev("poisoned-1", "poisoned line", stream_id=poisoned_stream),
            ]))

            assert result == {
                "type": "event.push.error", "request_id": 7,
                "error": "ingest_failed",
            }
            assert casts == []
            assert await _raw_tail_rows(store, live_stream) == 0
            assert await _raw_tail_rows(store, poisoned_stream) == 0
        finally:
            store.stop()

    _run(_go())


def test_lifecycle_drop_logs_once_per_stream(caplog) -> None:
    """A stale pane pushes every ~1.5s; it must not also spam the daemon log."""
    async def _go() -> None:
        closed_stream = "hostc:fleet-smoke-claude-leaked"
        store = Store(":memory:"); store.start()
        try:
            await store.open_session(
                "hostc", "fleet-smoke-claude-leaked", visibility="visible",
                provider="claude", pane_pid="7001",
            )
            leaked = await store.fetch_open_session_lifecycle(closed_stream, pane_pid="7001")
            assert leaked is not None
            assert await store.mark_closed(
                "hostc", "fleet-smoke-claude-leaked",
                closed_at="2026-09-02T12:00:00Z", pane_status="pane_dead",
                expected_generation=leaked["generation"],
            ) is not None
            ep, _casts, _ = _sink(store)
            for ordinal in range(3):
                result = await ep.handle_push(_push(ep, [
                    _claude_fork_ev(
                        f"leaked-{ordinal}", "sess-leaked", "stale line",
                        stream_id=closed_stream,
                    ),
                ]))
                assert result["type"] == "event.push.ok"
                assert result["dropped"] == [{
                    "stream_id": closed_stream, "reason": "lifecycle_changed",
                }]
        finally:
            store.stop()

    caplog.set_level(logging.WARNING, logger="chat_streamd_v2.event_push")
    _run(_go())
    drops = [record for record in caplog.records if "event.push dropped" in record.getMessage()]
    assert len(drops) == 1
    assert "reason=lifecycle_changed" in drops[0].getMessage()


def test_reopen_signals_a_frozen_tail_and_rearms_drop_logging(caplog) -> None:
    """A closed-row drop becomes eligible again only after a real reopen."""
    async def _go() -> None:
        stream_id = "hostc:v2-claude-reopened"
        store = Store(":memory:"); store.start()
        try:
            await store.open_session(
                "hostc", "v2-claude-reopened", visibility="visible",
                provider="claude", pane_pid="7001",
            )
            closed = await store.fetch_open_session_lifecycle(stream_id, pane_pid="7001")
            assert closed is not None
            assert await store.mark_closed(
                "hostc", "v2-claude-reopened",
                closed_at="2026-09-02T12:00:00Z", pane_status="pane_dead",
                expected_generation=closed["generation"],
            ) is not None
            ep, _casts, _ = _sink(store)
            stale = _claude_fork_ev(
                "stale-1", "session-1", "stale line", stream_id=stream_id,
            )

            first = await ep.handle_push(_push(ep, [stale]))
            assert first["dropped"] == [{
                "stream_id": stream_id, "reason": "lifecycle_changed",
            }]

            await store.open_session(
                "hostc", "v2-claude-reopened", visibility="visible",
                provider="claude", pane_pid="7001",
            )
            reopened = await ep.handle_push(_push(ep, [], frozen_streams=[stream_id]))
            assert reopened["reopened"] == [stream_id]
            # The response can be lost after the daemon writes it. The next
            # frozen-tail cadence must repeat recovery instead of clearing it.
            repeated = await ep.handle_push(_push(ep, [], frozen_streams=[stream_id]))
            assert repeated["reopened"] == [stream_id]
            # Once the satellite receives a recovery ack it sends an empty
            # proof; only then may the daemon clear recovery/log-dedup state.
            acknowledged = await ep.handle_push(_push(ep, [], frozen_streams=[]))
            assert acknowledged["reopened"] == []

            live = await store.fetch_open_session_lifecycle(stream_id, pane_pid="7001")
            assert live is not None
            assert await store.mark_closed(
                "hostc", "v2-claude-reopened",
                closed_at="2026-09-02T12:01:00Z", pane_status="pane_dead",
                expected_generation=live["generation"],
            ) is not None
            second = await ep.handle_push(_push(ep, [
                _claude_fork_ev("stale-2", "session-1", "stale again", stream_id=stream_id),
            ]))
            assert second["dropped"] == [{
                "stream_id": stream_id, "reason": "lifecycle_changed",
            }]
        finally:
            store.stop()

    caplog.set_level(logging.WARNING, logger="chat_streamd_v2.event_push")
    _run(_go())
    drops = [record for record in caplog.records if "event.push dropped" in record.getMessage()]
    assert len(drops) == 2


def test_p0_storage_failure_returns_non_success_without_broadcast() -> None:
    async def _go() -> None:
        stream_id = "hostc:v2-codex-current"
        store = Store(":memory:"); store.start()
        try:
            await store.open_session(
                "hostc", "v2-codex-current", visibility="visible",
                provider="codex", pane_pid="8123",
            )

            async def _storage_failure(_entries, *, limit):
                raise OSError(f"forced storage failure at limit={limit}")

            store.append_session_events_lifecycle_cas = _storage_failure  # type: ignore[method-assign]
            ep, casts, _ = _sink(store)
            result = await ep.handle_push(_push(ep, [
                _proven_codex_ev(CURRENT_CODEX_ROLLOUT, 5, stream_id=stream_id),
            ]))

            assert result == {
                "type": "event.push.error", "request_id": 7,
                "error": "ingest_failed",
            }
            assert casts == []
            assert await _raw_tail_rows(store, stream_id) == 0
            assert await store.fetch_session_event_tail(stream_id, limit=500) == []
            assert await _count_user_and_tell(store, stream_id) == 0
        finally:
            store.stop()

    _run(_go())


def test_remote_first_event_updates_live_inventory_bootstrap_state() -> None:
    """A same-daemon event.push makes list agree with durable inspect state."""
    async def _go() -> None:
        stream_id = "hostc:v2-bootstrap"
        store = Store(":memory:"); store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            await sessions.open(
                "hostc", "v2-bootstrap", provider="codex", pane_pid="8123",
                created_at="2026-08-01T00:00:00Z",
            )
            assert sessions.list_open()[0]["bootstrap_state"] == "unsubmitted"
            ep, casts, _ = _sink(store, sessions=sessions, emit_inventory=True)
            accepted = await ep.handle_push(_push(ep, [
                _proven_codex_ev(
                    CURRENT_CODEX_ROLLOUT, 1, stream_id=stream_id, pane_pid="8123",
                ),
            ]))
            assert accepted["type"] == "event.push.ok" and accepted["inserted"] == 1
            assert sessions.list_open()[0]["bootstrap_state"] == "started"
            inventory = next(frame for frame in casts if frame["type"] == "session.inventory")
            row = next(item for item in inventory["sessions"] if item["stream_id"] == stream_id)
            assert row["last_event_at"]
            assert await store.fetch_session_event_tail(stream_id, limit=1)
        finally:
            store.stop()

    _run(_go())


def test_remote_claude_first_event_updates_live_inventory_bootstrap_state() -> None:
    """Claude has no Codex lifecycle object but must update list immediately."""
    async def _go() -> None:
        stream_id = "hostc:v2-claude-bootstrap"
        store = Store(":memory:"); store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            await sessions.open(
                "hostc", "v2-claude-bootstrap", provider="claude",
                created_at="2026-08-01T00:00:00Z",
            )
            assert sessions.list_open()[0]["bootstrap_state"] == "unsubmitted"
            ep, casts, _ = _sink(store, sessions=sessions)
            accepted = await ep.handle_push(_push(ep, [
                _claude_fork_ev(
                    "claude-bootstrap-1", "claude-session-1", "first turn",
                    stream_id=stream_id,
                ),
            ]))
            assert accepted["type"] == "event.push.ok" and accepted["inserted"] == 1
            assert len(casts) == 1
            assert sessions.list_open()[0]["bootstrap_state"] == "started"
            assert await store.fetch_session_event_tail(stream_id, limit=1)
        finally:
            store.stop()

    _run(_go())


def _push(
    ep: EventPush,
    events,
    *,
    secret="s3cret",
    sha="",
    host="hostc",
    hw=None,
    inventory=None,
    frozen_streams=None,
):
    frame = {
        "type": "event.push", "request_id": 7, "push_secret": secret,
        "satellite_sha": sha, "wire_version": WIRE_VERSION, "host": host,
        "satellite_pid": 4321,
        "events": events, "high_water": hw or {},
    }
    if inventory is not None:
        frame["inventory"] = inventory
    if frozen_streams is not None:
        frame["frozen_streams"] = frozen_streams
    return frame


def _run(coro):
    return asyncio.run(coro)


def test_push_inserts_then_dedups_and_broadcasts_once() -> None:
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            await store.open_session("hostc", "v2-abc", visibility="visible")
            ep, casts, _ = _sink(store)
            ev = _ev("u1")
            first = await ep.handle_push(_push(ep, [ev], hw={"/p.jsonl": 42}))
            assert first["type"] == "event.push.ok" and first["inserted"] == 1
            assert first["high_water"] == {"/p.jsonl": 42}  # echoed for the satellite HWM
            # Replay (satellite restart rescan): accepted, but no re-insert / re-broadcast.
            second = await ep.handle_push(_push(ep, [ev]))
            assert second["inserted"] == 0 and second["accepted"] == 1
            assert len(casts) == 1, "a durable replay must never re-broadcast"
            rows = await store.fetch_session_event_tail("hostc:v2-abc", limit=500)
            assert len(rows) == 1, rows
        finally:
            store.stop()

    _run(_go())


def test_claude_fork_binding_backfills_head_and_suppresses_stale_parent() -> None:
    async def _go() -> None:
        stream_id = "hostc:v2-claude-fork"
        store = Store(":memory:"); store.start()
        try:
            sessions = Sessions(store, local_host="hostc")
            await sessions.open("hostc", "v2-claude-fork", provider="claude")
            ep, casts, _ = _sink(store, sessions=sessions)
            parent = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            fork = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
            await ep.handle_push(_push(ep, [_claude_fork_ev("a1", parent, "A", stream_id=stream_id)]))
            result = await ep.handle_push(_push(ep, [
                _claude_fork_ev("b1", fork, "B head 1", stream_id=stream_id),
                _claude_fork_ev("b2", fork, "B head 2", stream_id=stream_id),
            ]))
            row = await store.fetch_session("hostc", "v2-claude-fork")
            assert result["inserted"] == 2
            assert row["claude_session_id"] == fork
            assert row["claude_session_lineage"] == parent
            assert sessions.get(stream_id)["claude_session_id"] == fork
            assert sessions.get(stream_id)["claude_session_lineage"] == parent
            stale = await ep.handle_push(_push(ep, [_claude_fork_ev("a2", parent, "stale A", stream_id=stream_id)]))
            tail = await store.fetch_session_event_tail(stream_id, limit=500)
            assert stale["inserted"] == 0 and [event["text"] for event in tail] == ["A", "B head 1", "B head 2"]
            assert len(casts) == 3
        finally:
            store.stop()

    _run(_go())

def test_auth_is_enforced_before_any_side_effect() -> None:
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            ep, casts, _ = _sink(store, secret="right")
            bad = await ep.handle_push(_push(ep, [_ev("u1")], secret="wrong"))
            assert bad == {"type": "event.push.error", "request_id": 7, "error": "unauthorized"}
            assert casts == [], "no broadcast on an unauthorized push"
            rows = await store.fetch_session_event_tail("hostc:v2-abc", limit=500)
            assert rows == [], "no insert on an unauthorized push"
        finally:
            store.stop()

    _run(_go())


def test_unconfigured_secret_fails_closed() -> None:
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            ep, _casts, _ = _sink(store, secret=None)  # no secret on hosta
            out = await ep.handle_push(_push(ep, [_ev("u1")], secret="anything"))
            assert out["error"] == "event_push_unconfigured"
        finally:
            store.stop()

    _run(_go())


def test_kill_switch_returns_ingest_disabled() -> None:
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            ep, casts, _ = _sink(store, enabled=False)
            out = await ep.handle_push(_push(ep, [_ev("u1")]))
            assert out["error"] == "ingest_disabled"
            assert casts == [], "disabled ingest must not broadcast"
        finally:
            store.stop()

    _run(_go())


def test_version_gate_mismatch_requires_update_and_alerts() -> None:
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            await store.open_session("hostc", "v2-abc", visibility="visible")
            await store.put("event_push.target_sha", TARGET_SHA)
            ep, casts, _alerts = _sink(store)
            out = await ep.handle_push(_push(ep, [_ev("u1")], sha=SATELLITE_SHA))
            assert out == {
                "type": "event.push.error", "request_id": 7,
                "error": "satellite_version_mismatch",
                "version": {"status": "update_required", "target_sha": TARGET_SHA},
            }
            assert casts == []
            assert await store.fetch_session_event_tail("hostc:v2-abc", limit=500) == []
            runtime = json.loads(await store.get("event_push.runtime.hostc"))
            assert runtime["sha"] == SATELLITE_SHA
            assert runtime["pid"] == 4321
            assert runtime["observed_at"]
            assert runtime["observed_at_epoch"] > 0
        finally:
            store.stop()

    _run(_go())


def test_empty_batch_is_version_checked_without_ingest_side_effects() -> None:
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            await store.open_session("hostc", "v2-abc", visibility="visible")
            await store.put("event_push.target_sha", TARGET_SHA)
            ep, casts, _ = _sink(store)
            out = await ep.handle_push(_push(ep, [], sha=SATELLITE_SHA))
            assert out == {
                "type": "event.push.error", "request_id": 7,
                "error": "satellite_version_mismatch",
                "version": {"status": "update_required", "target_sha": TARGET_SHA},
            }
            assert casts == []
            assert await store.fetch_session_event_tail("hostc:v2-abc", limit=500) == []
        finally:
            store.stop()

    _run(_go())


def test_version_gate_match_is_ok_no_alert() -> None:
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            await store.open_session("hostc", "v2-abc", visibility="visible")
            await store.put("event_push.target_sha", TARGET_SHA)
            ep, _casts, alerts = _sink(store)
            out = await ep.handle_push(_push(ep, [_ev("u1")], sha=TARGET_SHA))
            assert out["version"]["status"] == "ok" and out["stale"] is False
            assert alerts.emitted == []
        finally:
            store.stop()

    _run(_go())


def test_version_verdict_only_accepts_the_exact_configured_pin() -> None:
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            ep, _casts, _alerts = _sink(store)
            assert await ep._version_verdict("hostc", "not-a-sha") == {
                "status": "ok", "target_sha": None,
            }

            await store.put("event_push.target_sha", TARGET_SHA)
            assert (await ep._version_verdict("hostc", TARGET_SHA))["status"] == "ok"
            assert await ep._version_verdict("hostc", SATELLITE_AHEAD_SHA) == {
                "status": "update_required", "target_sha": TARGET_SHA,
            }
        finally:
            store.stop()

    _run(_go())


def test_pin_drift_is_rate_limited_and_never_writes() -> None:
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            ep, _casts, alerts = _sink(store, daemon_sha=TARGET_SHA)
            writes: list[tuple[str, str]] = []
            put = store.put

            async def traced_put(key: str, value: str) -> None:
                writes.append((key, value))
                await put(key, value)

            store.put = traced_put  # type: ignore[method-assign]
            await store.put("event_push.target_sha", TARGET_SHA)
            writes.clear()
            assert await ep.check_pin_drift() is False
            await store.put("event_push.target_sha", SATELLITE_SHA)
            writes.clear()
            assert await ep.check_pin_drift() is True
            assert await ep.check_pin_drift() is True
            assert [kind for kind, _fields in alerts.emitted] == ["pin_drift"]
            assert writes == [], "drift detection is observe-and-alert only"
        finally:
            store.stop()

    _run(_go())


def test_authenticated_stale_satellite_rejects_before_event_validation() -> None:
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            await store.put("event_push.target_sha", TARGET_SHA)
            ep, casts, _alerts = _sink(store)
            rejected = _codex_ev(CURRENT_CODEX_ROLLOUT, 100, stream_id="hostc:v2-codex-current")
            reply = await ep.handle_push(_push(ep, [rejected], sha=SATELLITE_SHA))
            assert reply == {
                "type": "event.push.error", "request_id": 7,
                "error": "satellite_version_mismatch",
                "version": {"status": "update_required", "target_sha": TARGET_SHA},
            }
            assert casts == []

            mixed = [_ev("ordinary-first"), rejected]
            reply = await ep.handle_push(_push(ep, mixed, sha=SATELLITE_SHA))
            assert reply["error"] == "satellite_version_mismatch"
            assert casts == []
            assert await store.fetch_session_event_tail("hostc:v2-abc", limit=500) == []

            unauthorized = await ep.handle_push(_push(ep, [_ev("bad-secret")], secret="wrong", sha=SATELLITE_SHA))
            assert unauthorized["error"] == "unauthorized" and "version" not in unauthorized
        finally:
            store.stop()

    _run(_go())


def test_remote_claude_push_rehydrates_routing_observer_even_on_replay() -> None:
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            await store.open_session("hostc", "v2-claude", provider="claude", visibility="visible")
            routing = _RoutingObserver()
            ep, _casts, _ = _sink(store, routing_integrity=routing)
            event = _ev("claude-assist-1", stream_id="hostc:v2-claude")
            event["kind"] = "ASSIST_TEXT"
            event["raw"] = {
                "jsonl_record_uuid": "claude-assist-1",
                "jsonl_event_index": 1,
                "model": "opus",
                "effort": "high",
            }
            await ep.handle_push(_push(ep, [event]))
            await ep.handle_push(_push(ep, [event]))
            assert len(routing.payloads) == 2
            assert routing.payloads[0]["stream_id"] == "hostc:v2-claude"
        finally:
            store.stop()

    _run(_go())


def test_event_push_replay_updates_real_routing_store_after_observer_restart() -> None:
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            sessions = Sessions(store, local_host="hostc")
            await sessions.open(
                "hostc",
                "v2-claude",
                provider="claude",
                requested_model="claude-opus-4-8",
                requested_effort="high",
                effective_model="claude-opus-4-8",
                effective_effort="high",
                pane_status="pane_alive",
            )
            event = _ev("claude-assist-restart", stream_id="hostc:v2-claude")
            event["kind"] = "ASSIST_TEXT"
            event["raw"] = {
                "jsonl_record_uuid": "claude-assist-restart",
                "jsonl_event_index": 1,
                "model": "opus",
                "effort": "high",
            }
            first_routing = RoutingIntegrity(store, sessions)
            ep, _casts, _ = _sink(store, routing_integrity=first_routing)
            await ep.handle_push(_push(ep, [event]))
            row = await store.fetch_session("hostc", "v2-claude")
            assert row["routing_integrity"] is None

            # A new observer has no in-memory tuple cache. Replaying the same
            # durable satellite event must still rehydrate and stamp it.
            restarted_routing = RoutingIntegrity(store, sessions)
            ep, _casts, _ = _sink(store, routing_integrity=restarted_routing)
            replay = await ep.handle_push(_push(ep, [event]))
            assert replay["inserted"] == 0
            row = await store.fetch_session("hostc", "v2-claude")
            assert row["routing_integrity"] is None
            assert row["effective_model"] == "opus"
            assert row["effective_effort"] == "high"
        finally:
            store.stop()

    _run(_go())


def test_event_push_duplicate_keeps_the_same_claude_drift_episode() -> None:
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            sessions = Sessions(store, local_host="hostc")
            await sessions.open(
                "hostc",
                "v2-claude-evidence",
                provider="claude",
                requested_model="claude-opus-4-8",
                requested_effort="high",
                effective_model="claude-opus-4-8",
                effective_effort="high",
                pane_status="pane_unknown",
            )
            routing = RoutingIntegrity(store, sessions)
            ep, _casts, _ = _sink(store, routing_integrity=routing)
            event = _ev("claude-evidence-1", stream_id="hostc:v2-claude-evidence")
            event["kind"] = "ASSIST_TEXT"
            event["raw"] = {
                "jsonl_record_uuid": "claude-evidence-1",
                "jsonl_event_index": 1,
                "model": "sonnet",
                "effort": "low",
            }

            first = await ep.handle_push(_push(ep, [event]))
            assert first["inserted"] == 1
            evidence = await store.routing_integrity_episode("hostc:v2-claude-evidence")
            assert evidence is not None
            episode_id = evidence["episode_id"]

            await store.update_session(
                "hostc", "v2-claude-evidence", pane_status="pane_alive"
            )
            sessions.apply_durable(
                "hostc:v2-claude-evidence", pane_status="pane_alive"
            )
            replay = await ep.handle_push(_push(ep, [event]))
            assert replay["inserted"] == 0
            current = await store.routing_integrity_episode("hostc:v2-claude-evidence")
            assert current is not None and current["episode_id"] == episode_id
        finally:
            store.stop()

    _run(_go())


def test_bad_and_oversized_batches_are_rejected() -> None:
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            ep, _casts, _ = _sink(store)
            assert (await ep.handle_push(_push(ep, "notalist")))["error"] == "bad_batch"
            big = [_ev(f"u{i}") for i in range(3)]
            from event_push import MAX_BATCH
            over = await ep.handle_push(_push(ep, [big[0]] * (MAX_BATCH + 1)))
            assert over["error"] == "batch_too_large"
        finally:
            store.stop()

    _run(_go())


def test_empty_inventory_never_closes_open_rows() -> None:
    """A present inventory is observation, not durable-close authority."""
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            await sessions.open("hostc", "v2-gone-a", visibility="visible")
            await sessions.open("hostc", "v2-gone-b", visibility="visible")
            await sessions.open("hostc", "v2-terminal", visibility="visible")
            await sessions.open("hostb", "v2-other-host", visibility="visible")
            assert await sessions.mark_closed("hostc", "v2-terminal") is not None
            ep, _casts, _ = _sink(store, sessions=sessions)

            reply = await ep.handle_push(_push(ep, [], inventory=[]))

            assert reply["type"] == "event.push.ok"
            for name in ("v2-gone-a", "v2-gone-b"):
                row = await store.fetch_session("hostc", name)
                assert row is not None
                assert row["status"] == "open"
            terminal = await store.fetch_session("hostc", "v2-terminal")
            assert terminal is not None
            assert (terminal["status"], terminal["close_kind"]) == ("closed", "session_close")
            other = await store.fetch_session("hostb", "v2-other-host")
            assert other is not None and other["status"] == "open"
        finally:
            store.stop()

    _run(_go())


def test_rapid_triple_spawn_inventory_gap_never_closes_rows() -> None:
    """Inventory omission cannot close rows before or after reservation release."""
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            names = [f"v2-rapid-{index}" for index in range(3)]
            for index, name in enumerate(names):
                request_id = f"spawn-rapid-{index}"
                assert await store.reserve_stream_id(
                    "hostc", name, ttl_s=60,
                    request_id=request_id, nonce=f"nonce-{index}",
                    owner_instance_id="daemon-live",
                )
                await store.mark_tmux_created("hostc", name)
                await sessions.open(
                    "hostc", name, fence=request_id,
                    visibility="hidden", pane_pid=str(8100 + index),
                    pane_status="pane_alive",
                )
            ep, _casts, _ = _sink(store, sessions=sessions)

            await ep.handle_push(_push(ep, [], inventory=[]))
            assert all([
                (await store.fetch_session("hostc", name))["status"] == "open"
                for name in names
            ])

            for index, name in enumerate(names):
                request_id = f"spawn-rapid-{index}"
                assert await store.restore_spawn_intent_owner(
                    "hostc", name, request_id=request_id,
                    owner_instance_id="daemon-live", prior_owner="",
                )
                await store.update_session("hostc", name, bootstrap_state="unproven")
                sessions.apply_durable(
                    f"hostc:{name}", bootstrap_state="unproven",
                )
            await ep.handle_push(_push(ep, [], inventory=[]))
            assert all([
                (await store.fetch_session("hostc", name))["closed_at"] is None
                for name in names
            ])

            for index, name in enumerate(names):
                await store.release_stream_id_fenced(
                    "hostc", name, f"spawn-rapid-{index}",
                )
            await ep.handle_push(_push(ep, [], inventory=[]))
            assert all([
                (await store.fetch_session("hostc", name))["status"] == "open"
                for name in names
            ])
        finally:
            store.stop()

    _run(_go())


def test_partial_inventory_never_closes_missing_rows() -> None:
    """A fresh omitted row stays open until the reconciler proves it gone."""
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            await sessions.open("hostc", "v2-present", visibility="visible")
            await sessions.open("hostc", "v2-missing", visibility="visible")
            ep, _casts, _ = _sink(store, sessions=sessions)

            reply = await ep.handle_push(
                _push(ep, [], inventory=["v2-present"]),
            )

            assert reply["type"] == "event.push.ok"
            present = await store.fetch_session("hostc", "v2-present")
            missing = await store.fetch_session("hostc", "v2-missing")
            assert present is not None and present["status"] == "open"
            assert missing is not None
            assert missing["status"] == "open"
            assert missing["close_kind"] is None
        finally:
            store.stop()

    _run(_go())


def test_inventory_absence_with_expired_reservation_stays_open() -> None:
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            request_id = "expired-ghost"
            assert await store.reserve_stream_id(
                "hostc", "v2-ghost", ttl_s=-1, request_id=request_id,
            )
            await sessions.open(
                "hostc", "v2-ghost", fence=request_id, visibility="visible",
            )
            await sessions.open("hostc", "v2-present", visibility="visible",
                                created_at="2026-08-01T00:00:00Z")
            ep, casts, _ = _sink(store, sessions=sessions, emit_inventory=True)
            ep.inventory_emitter.prime()

            reply = await ep.handle_push(_push(
                ep, [_ev("present-event", stream_id="hostc:v2-present")],
                inventory=["v2-present"],
            ))

            row = await store.fetch_session("hostc", "v2-ghost")
            assert reply["type"] == "event.push.ok"
            assert row is not None and row["status"] == "open"
            assert any(
                frame["type"] == "session.inventory"
                and any(item["stream_id"] == "hostc:v2-ghost" for item in frame["sessions"])
                for frame in casts
            )
        finally:
            store.stop()

    _run(_go())


def test_inventory_absence_with_live_reservation_stays_open() -> None:
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            request_id = "live-ghost"
            assert await store.reserve_stream_id(
                "hostc", "v2-ghost", ttl_s=60, request_id=request_id,
            )
            await sessions.open(
                "hostc", "v2-ghost", fence=request_id, visibility="visible",
            )
            ep, _casts, _ = _sink(store, sessions=sessions)

            reply = await ep.handle_push(_push(ep, [], inventory=[]))

            row = await store.fetch_session("hostc", "v2-ghost")
            assert reply["type"] == "event.push.ok"
            assert row is not None and row["status"] == "open"
        finally:
            store.stop()

    _run(_go())


def test_inventory_absence_skips_closed_row_without_close_attempt_or_log(caplog, monkeypatch) -> None:
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            row = await sessions.open("hostc", "v2-closed", visibility="visible")
            closed = await store.mark_closed(
                "hostc", "v2-closed", closed_at="2026-09-01T00:00:00Z",
                pane_status="pane_dead", expected_generation=row["session_generation"],
                close_kind="session_close", reason="test",
            )
            assert closed is not None
            sessions.apply_durable(
                "hostc:v2-closed", status="closed", closed_at=closed["closed_at"],
            )
            mark_closed = AsyncMock(wraps=sessions.mark_closed)
            monkeypatch.setattr(sessions, "mark_closed", mark_closed)
            ep, _casts, _ = _sink(store, sessions=sessions)

            await ep.handle_push(_push(ep, [], inventory=[]))

            current = await store.fetch_session("hostc", "v2-closed")
            assert current is not None and current["status"] == "closed"
            assert mark_closed.await_count == 0
        finally:
            store.stop()

    caplog.set_level(logging.INFO, logger="chat_streamd_v2.sessions")
    _run(_go())
    assert not any(
        "session close mark_closed" in record.getMessage()
        or "mark_closed dropped" in record.getMessage()
        for record in caplog.records
    )


def test_absent_inventory_closes_nothing() -> None:
    """A failed or inconclusive discovery cannot close a durable row."""
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            await sessions.open("hostc", "v2-breaker-open", visibility="visible")
            ep, _casts, _ = _sink(store, sessions=sessions)

            reply = await ep.handle_push(_push(ep, []))

            assert reply["type"] == "event.push.ok"
            row = await store.fetch_session("hostc", "v2-breaker-open")
            assert row is not None and row["status"] == "open"
        finally:
            store.stop()

    _run(_go())


def test_steady_state_inventory_omission_stays_open() -> None:
    """A later inventory omission still cannot close a previously listed row."""
    async def _go() -> None:
        store = Store(":memory:"); store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            await sessions.open("hostc", "v2-killed", visibility="visible")
            ep, _casts, _ = _sink(store, sessions=sessions)

            first = await ep.handle_push(_push(ep, [], inventory=["v2-killed"]))
            assert first["type"] == "event.push.ok"
            before = await store.fetch_session("hostc", "v2-killed")
            assert before is not None and before["status"] == "open"

            second = await ep.handle_push(_push(ep, [], inventory=[]))
            assert second["type"] == "event.push.ok"
            after = await store.fetch_session("hostc", "v2-killed")
            assert after is not None
            assert after["status"] == "open"
            assert after["close_kind"] is None
        finally:
            store.stop()

    _run(_go())
