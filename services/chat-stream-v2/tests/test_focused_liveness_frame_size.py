"""Focused-liveness stays responsive when inventory frames are large.

The synthetic cases exercise bounded summary projections and ensure that a
small liveness frame is not delayed by a large inventory projection.

The contract is per-subscription and reuses the existing reductions:
  - summary-mode hello snapshot caps notifications at
    SUMMARY_SNAPSHOT_NOTIFICATIONS_LIMIT (client backfills via notification.list);
    full-mode (desktop) keeps HELLO_SNAPSHOT_NOTIFICATIONS_LIMIT.
  - broadcast session.inventory is summary-reduced for summary-mode clients, the
    same reduction the hello snapshot already applied.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import notify as notify_mod
from notify import (
    HELLO_SNAPSHOT_NOTIFICATIONS_LIMIT,
    SUMMARY_SNAPSHOT_NOTIFICATIONS_LIMIT,
    Notify,
)
from server import Server


def test_complete_roster_frame_budget_0_12_100_500():
    import json
    from agents_roster import project

    measurements = {}
    for count in (0, 12, 100, 500):
        parent = {"stream_id": "hosta:parent", "host": "hosta", "status": "open"}
        children = [{"stream_id": f"hosta:child-{index:04}", "session_generation": f"{index:032x}",
                     "parent_stream_id": "hosta:parent", "status": "open", "visibility": "hidden",
                     "objective": "🌈" * 120, "role": "worker", "requested_model": "gpt-5.6-luna",
                     "created_at": "2026-09-08T00:00:00Z", "working": index % 2 == 0}
                    for index in range(count)]
        projected = Server._summary_snapshot_sessions(project([parent, *children], {}))[0]
        # Production websocket JSON preserves Unicode; budget wire bytes, not Python's ASCII escape default.
        from server import _encode_frame
        encoded = lambda value: len(_encode_frame({"type": "session.inventory", "sessions": [value]}).encode())
        delta = encoded(projected) - encoded(Server._summary_snapshot_sessions([parent])[0])
        assert len(projected.get("agents", [])) == count
        assert delta <= 1024 * count
        if count == 500:
            assert delta <= 512 * 1024
        measurements[count] = delta
    print("roster added serialized bytes:", json.dumps(measurements))


def test_unicode_inventory_and_snapshot_encoding_preserves_dedupe_and_other_frames():
    import json
    from server import _encode_frame

    async def run():
        daemon = Server()
        ws = object()
        daemon._clients.add(ws)
        daemon._client_include_subagents[ws] = True
        seen = []
        def enqueue(websocket, frame_type, encoded):
            seen.append(encoded)
            key = daemon._coalesce_frame_key(frame_type, encoded)
            daemon._client_last_sent_digest.setdefault(websocket, {})[key] = hash(encoded)
            return True
        daemon._enqueue = enqueue
        daemon._has_different_pending_coalescible = lambda *_args: False
        row = {"stream_id": "hosta:parent", "objective": "Check a synthetic sample text"}
        frame = {"type": "session.inventory", "sessions": [row]}
        await daemon.broadcast(frame)
        await daemon.broadcast(frame)
        assert len(seen) == 1 and json.loads(seen[0]) == frame
        assert "sample text" in seen[0]
        row["objective"] += " again"
        await daemon.broadcast(frame)
        assert len(seen) == 2
        snapshot = {**frame, "type": "snapshot"}
        assert json.loads(_encode_frame(snapshot)) == snapshot
        other = {"type": "chat.event", "text": "sample text"}
        assert _encode_frame(other) == json.dumps(other)
    asyncio.run(run())


def test_summary_snapshot_notifications_uses_the_small_cap(tmp_path: Path) -> None:
    async def go() -> None:
        n = Notify(db_path=str(tmp_path / "notifications.db"))
        await n.start()
        seen: list = []
        orig = n._db.call

        async def spy(op, **kwargs):
            if op == "list_notifications":
                seen.append(kwargs.get("limit"))
            return await orig(op, **kwargs)

        n._db.call = spy  # type: ignore[method-assign]
        try:
            await n.snapshot_notifications(summary=True)
            await n.snapshot_notifications(summary=False)
        finally:
            await n.stop()
        assert seen == [SUMMARY_SNAPSHOT_NOTIFICATIONS_LIMIT, HELLO_SNAPSHOT_NOTIFICATIONS_LIMIT]

    asyncio.run(go())
    assert SUMMARY_SNAPSHOT_NOTIFICATIONS_LIMIT < HELLO_SNAPSHOT_NOTIFICATIONS_LIMIT


def test_broadcast_session_inventory_is_summary_reduced_for_summary_clients() -> None:
    daemon = Server()
    ws_summary = object()
    ws_full = object()
    for ws, mode in ((ws_summary, "summary"), (ws_full, "full")):
        daemon._clients.add(ws)
        daemon._client_events_mode[ws] = mode
        daemon._client_include_subagents[ws] = True
    # A full inventory row carries a persistence-only column (token_hash) that the
    # summary reduction drops; display_name is a summary field that survives.
    row = {
        "stream_id": "hosta:v2-abc", "host": "hosta", "display_name": "d",
        "online": True, "token_hash": "TEST", "jsonl_path": "/x/y.jsonl",
    }
    payload = {"type": "session.inventory", "sessions": [row]}

    out_summary = daemon._frame_for_client(ws_summary, "session.inventory", payload)
    out_full = daemon._frame_for_client(ws_full, "session.inventory", payload)

    assert out_summary is not None and out_full is not None
    srow = out_summary["sessions"][0]
    frow = out_full["sessions"][0]
    # summary client: persistence-only fields dropped, visible fields kept
    assert "token_hash" not in srow and "jsonl_path" not in srow
    assert srow["stream_id"] == "hosta:v2-abc" and srow["display_name"] == "d"
    # desktop (full) client: full row preserved
    assert frow.get("token_hash") == "TEST"


def test_broadcast_projects_summary_and_full_clients_in_separate_groups() -> None:
    # A summary and a full client that share every OTHER subscription dimension
    # must NOT share one rendered projection: broadcast() groups by events_mode
    # too, so each gets the right session.inventory. (Regression: QA r1 reject —
    # a mixed group projected once from clients[0] and gave one client the wrong
    # frame.)
    daemon = Server()
    ws_summary = object()
    ws_full = object()
    for ws, mode in ((ws_summary, "summary"), (ws_full, "full")):
        daemon._clients.add(ws)
        daemon._client_events_mode[ws] = mode
        daemon._client_include_subagents[ws] = False  # identical other dimensions

    captured: dict = {}

    def _spy_enqueue(ws, _ftype, frame):
        captured[ws] = frame
        return True

    daemon._enqueue = _spy_enqueue  # type: ignore[method-assign]
    row = {
        "stream_id": "hosta:v2-abc", "host": "hosta", "display_name": "d",
        "online": True, "token_hash": "TEST",
    }
    asyncio.run(daemon.broadcast({"type": "session.inventory", "sessions": [row]}))

    import json as _json
    assert ws_summary in captured and ws_full in captured
    assert "token_hash" not in _json.loads(captured[ws_summary])["sessions"][0]
    assert _json.loads(captured[ws_full])["sessions"][0]["token_hash"] == "TEST"


def test_notify_module_exposes_distinct_caps() -> None:
    # Guard the per-subscription contract: the two caps are distinct constants.
    assert notify_mod.SUMMARY_SNAPSHOT_NOTIFICATIONS_LIMIT == 20
    assert notify_mod.HELLO_SNAPSHOT_NOTIFICATIONS_LIMIT == 100
