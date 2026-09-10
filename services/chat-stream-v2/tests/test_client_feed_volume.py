"""Regression coverage for the daemon v2 client-feed volume bounds.

Each test isolates one removal.  None makes a claim about the mobile
chats-list freeze; that attribution belongs to its separate lane.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from context_adapters import ContextReading
from inventory import InventoryEmitter
from mirror import Mirror, MirrorConfig
from presence import RemotePresence
from routing_integrity import RoutingIntegrity
from server import Server
from sessions import Sessions
from store import Store


class _Rows:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = {str(row["stream_id"]): dict(row) for row in rows}

    def list_open(self) -> list[dict]:
        return [dict(row) for row in self.rows.values()]

    def get(self, stream_id: str) -> dict | None:
        row = self.rows.get(stream_id)
        return dict(row) if row is not None else None

    def apply_live(self, stream_id: str, **fields: object) -> dict | None:
        row = self.rows.get(stream_id)
        if row is None:
            return None
        row.update(fields)
        return dict(row)


class LocalSocket:
    """Explicit local bootstrap transport for projection-only tests."""
    remote_address = ("127.0.0.1", 8765)


def _attach(server: Server, websocket: object) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    server._clients.add(websocket)
    server._client_send_queues[websocket] = queue
    return queue


def _rows() -> list[dict]:
    return [
        {
            "stream_id": "host:lead",
            "host": "host",
            "session_name": "lead",
            "visibility": "visible",
            "opened_by_host_id": "mobile",
            "created_at": "2026-08-15T00:00:00Z",
        },
        {
            "stream_id": "host:worker",
            "host": "host",
            "session_name": "worker",
            "visibility": "hidden",
            "opened_by_host_id": "mobile",
        },
        {
            "stream_id": "host:other",
            "host": "host",
            "session_name": "other",
            "visibility": "visible",
            "opened_by_host_id": "desktop",
        },
    ]


def test_projection_filters_hidden_workers_but_keeps_the_opted_in_positive_control() -> None:
    async def run() -> None:
        server = Server(sessions=_Rows(_rows()))
        prehello_client, default_client, opted_in_client = LocalSocket(), LocalSocket(), LocalSocket()
        prehello_queue = _attach(server, prehello_client)
        default_queue = _attach(server, default_client)
        opted_in_queue = _attach(server, opted_in_client)

        await server._dispatch(
            json.dumps({"type": "hello", "subscribe": {"opened_by_host_ids": ["mobile"]}}),
            websocket=default_client,
        )
        await server._dispatch(
            json.dumps({
                "type": "hello",
                "subscribe": {"include_subagents": True, "opened_by_host_ids": ["mobile"]},
            }),
            websocket=opted_in_client,
        )
        await server.broadcast({"type": "session.inventory", "sessions": _rows()})

        prehello_frame = json.loads(prehello_queue.get_nowait()[1])
        default_frame = json.loads(default_queue.get_nowait()[1])
        opted_in_frame = json.loads(opted_in_queue.get_nowait()[1])
        assert [row["stream_id"] for row in prehello_frame["sessions"]] == ["host:lead", "host:other"]
        assert [row["stream_id"] for row in default_frame["sessions"]] == ["host:lead"]
        assert [row["stream_id"] for row in opted_in_frame["sessions"]] == ["host:lead", "host:worker"]

        await server.broadcast({"type": "working.state", "stream_id": "host:worker"})
        assert prehello_queue.empty(), "the pre-hello frame stream received a hidden worker"
        assert default_queue.empty(), "the non-opted-in frame stream received a hidden worker"
        opted_in_working = json.loads(opted_in_queue.get_nowait()[1])
        assert opted_in_working == {"type": "working.state", "stream_id": "host:worker"}

        await server.broadcast({"type": "session.died", "stream_id": "host:worker"})
        assert prehello_queue.empty(), "the pre-hello frame stream received a hidden worker death"
        assert default_queue.empty(), "the non-opted-in frame stream received a hidden worker death"
        opted_in_death = json.loads(opted_in_queue.get_nowait()[1])
        assert opted_in_death == {"type": "session.died", "stream_id": "host:worker"}

    asyncio.run(run())


def test_subscribe_rpc_mode_suppresses_the_unsolicited_snapshot_and_excludes_pushes() -> None:
    async def run() -> None:
        server = Server(sessions=_Rows(_rows()))
        client = LocalSocket()
        queue = _attach(server, client)
        replies = await server._dispatch(
            json.dumps({
                "type": "hello",
                "request_id": "rpc-only",
                "subscribe": {
                    "events_mode": "summary",
                    "snapshot": False,
                    "mode": "rpc",
                    "exclude_event_types": ["working.state"],
                },
            }),
            websocket=client,
        )
        assert replies == [{
            "type": "ready", "snapshot": False, "events_mode": "summary", "request_id": "rpc-only",
        }]
        assert server._client_events_mode[client] == "summary"

        await server.broadcast({"type": "working.state", "stream_id": "host:lead"})
        assert queue.empty(), "exclude_event_types was not honored for this client"

        # The two snapshot controls remain independently effective: a client
        # cannot accidentally receive a bootstrap snapshot by changing only one.
        snapshot_false_client = LocalSocket()
        _attach(server, snapshot_false_client)
        snapshot_false = await server._dispatch(
            json.dumps({"type": "hello", "subscribe": {"snapshot": False}}),
            websocket=snapshot_false_client,
        )
        assert snapshot_false[0]["type"] == "ready"
        assert snapshot_false[0]["snapshot"] is False

        rpc_client = LocalSocket()
        _attach(server, rpc_client)
        rpc_only = await server._dispatch(
            json.dumps({"type": "hello", "subscribe": {"mode": "rpc"}}),
            websocket=rpc_client,
        )
        assert rpc_only[0]["type"] == "ready"
        assert rpc_only[0]["snapshot"] is False

        # `events_mode` changes the snapshot itself, not merely an internal map
        # or an echo in the ready reply. The full payload contains persistence
        # fields; summary mode keeps only the client-facing session projection.
        full_client, summary_client = LocalSocket(), LocalSocket()
        _attach(server, full_client)
        _attach(server, summary_client)
        full = await server._dispatch(
            json.dumps({"type": "hello", "subscribe": {"events_mode": "full"}}),
            websocket=full_client,
        )
        summary = await server._dispatch(
            json.dumps({"type": "hello", "subscribe": {"events_mode": "summary"}}),
            websocket=summary_client,
        )
        full_snapshot = next(frame for frame in full if frame["type"] == "snapshot")
        summary_snapshot = next(frame for frame in summary if frame["type"] == "snapshot")
        assert full_snapshot["events_mode"] == "full"
        assert summary_snapshot["events_mode"] == "summary"
        assert full_snapshot["sessions"] != summary_snapshot["sessions"]
        assert "created_at" in full_snapshot["sessions"][0]
        assert "created_at" not in summary_snapshot["sessions"][0]

    asyncio.run(run())


def test_broadcast_serializes_once_per_projection_group() -> None:
    async def run() -> None:
        server = Server(sessions=_Rows(_rows()))
        default_a, default_b, opted_in = LocalSocket(), LocalSocket(), LocalSocket()
        for websocket in (default_a, default_b, opted_in):
            _attach(server, websocket)

        for websocket, subscribe in (
            (default_a, {"opened_by_host_ids": ["mobile"]}),
            (default_b, {"opened_by_host_ids": ["mobile"]}),
            (opted_in, {"opened_by_host_ids": ["mobile"], "include_subagents": True}),
        ):
            await server._dispatch(
                json.dumps({"type": "hello", "subscribe": subscribe}), websocket=websocket,
            )

        with patch("server.json.dumps", wraps=json.dumps) as encode:
            await server.broadcast({"type": "session.inventory", "sessions": _rows()})

        assert encode.call_count == 2

    asyncio.run(run())


def test_terminal_working_edge_flushes_without_spending_client_delivery_budget() -> None:
    async def run() -> None:
        rows = _Rows([{"stream_id": "host:terminal", "working": True}])
        frames: list[dict] = []

        async def broadcast(frame: dict) -> None:
            frames.append(frame)

        emitter = InventoryEmitter(rows, broadcast, min_interval_s=0.05)
        presence = RemotePresence(rows, hosts=object(), broadcast=broadcast,
                                  inventory_emitter=emitter)
        await emitter.emit_if_changed()
        # Ordinary churn already has a delayed task; terminal idle must not
        # wait for it or create another queue/emit later a duplicate snapshot.
        rows.apply_live("host:terminal", preview="changed")
        await emitter.emit_if_changed()
        pending = emitter._flush_task
        assert pending is not None
        rows.apply_live("host:terminal", working=False)
        presence._inventory_dirty = True
        try:
            await presence._flush_broadcasts()
            assert len(frames) == 2
            assert frames[-1]["sessions"][0]["working"] is False
            await pending
            assert len(frames) == 2
        finally:
            if emitter._flush_task:
                emitter._flush_task.cancel()
                await asyncio.gather(emitter._flush_task, return_exceptions=True)

    asyncio.run(run())


def test_nonterminal_working_omission_and_null_keep_existing_churn_throttle() -> None:
    async def run() -> None:
        for mode in ("null", "missing", "preview"):
            rows = _Rows([{"stream_id": "host:active", "working": True}])
            frames: list[dict] = []

            async def broadcast(frame: dict) -> None:
                frames.append(frame)

            emitter = InventoryEmitter(rows, broadcast, min_interval_s=10)
            await emitter.emit_if_changed()
            if mode == "null":
                rows.apply_live("host:active", working=None)
            elif mode == "missing":
                rows.rows["host:active"].pop("working")
            else:
                rows.apply_live("host:active", preview="changed")
            try:
                assert await emitter.emit_if_changed() is False
                pending = emitter._flush_task
                assert pending is not None
                rows.apply_live("host:active", preview="more ordinary churn")
                assert await emitter.emit_if_changed() is False
                assert emitter._flush_task is pending
                assert len(frames) == 1
            finally:
                emitter._flush_task.cancel()
                await asyncio.gather(emitter._flush_task, return_exceptions=True)

    asyncio.run(run())


def test_presence_inventory_emitter_dedups_volatile_activity_churn() -> None:
    async def run() -> None:
        rows = _Rows([{
            "stream_id": "host:remote",
            "host": "host",
            "session_name": "remote",
            "visibility": "visible",
            "last_activity": "first",
            "genuine_activity_at": 1.0,
        }])
        emitted: list[dict] = []

        async def broadcast(frame: dict) -> None:
            emitted.append(frame)

        presence = RemotePresence(rows, hosts=object(), broadcast=broadcast)
        presence._inventory_dirty = True
        await presence._flush_broadcasts()
        rows.rows["host:remote"]["last_activity"] = "second"
        rows.rows["host:remote"]["genuine_activity_at"] = 2.0
        presence._inventory_dirty = True
        await presence._flush_broadcasts()

        assert [frame["type"] for frame in emitted] == ["session.inventory"]

    asyncio.run(run())


def test_inventory_signature_excludes_volatile_activity_fields() -> None:
    async def run() -> None:
        rows = _Rows([{
            "stream_id": "host:local",
            "host": "host",
            "session_name": "local",
            "visibility": "visible",
            "last_activity": "volatile",
            "genuine_activity_at": 123.0,
        }])

        async def broadcast(_frame: dict) -> None:
            return None

        mirror = Mirror(
            None, rows, None, broadcast, local_host="host",
            config=MirrorConfig(inventory_min_interval_s=2.0),
        )
        await mirror._maybe_emit_inventory()
        signature = mirror._last_inv_signature
        assert signature is not None
        assert all("last_activity" not in row for row in signature)
        assert all("genuine_activity_at" not in row for row in signature)

    asyncio.run(run())


def test_prehello_list_sessions_defaults_to_restrictive_projection() -> None:
    async def scenario() -> None:
        rows = [row for row in _rows() if row["stream_id"] != "host:other"]
        server = Server(sessions=_Rows(rows))
        websocket = LocalSocket()
        _attach(server, websocket)

        prehello_frames = await server._dispatch(
            json.dumps({"type": "list_sessions"}), websocket=websocket,
        )
        prehello_frame = prehello_frames[0]
        assert [row["stream_id"] for row in prehello_frame["active"]] == ["host:lead"]

        await server._dispatch(
            json.dumps({
                "type": "hello",
                "subscribe": {
                    "include_subagents": True,
                    "opened_by_host_ids": ["mobile"],
                },
            }),
            websocket=websocket,
        )
        opted_in_frames = await server._dispatch(
            json.dumps({"type": "list_sessions"}), websocket=websocket,
        )
        opted_in_frame = opted_in_frames[0]
        assert [row["stream_id"] for row in opted_in_frame["active"]] == [
            "host:lead", "host:worker",
        ]

    asyncio.run(scenario())


def test_context_only_update_flushes_to_an_already_connected_client() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        sessions = Sessions(store, local_host="host")
        server = Server(store=store, sessions=sessions)
        client = LocalSocket()
        queue = _attach(server, client)

        observer = RoutingIntegrity(store, sessions, broadcast=server.broadcast)
        assert observer.inventory_emitter is not None
        # This is a two-observation, five-second source cadence.  Disable the
        # emitter's separate rate floor so timestamp-only churn cannot hide
        # behind throttling; only a semantic context change may emit twice.
        observer.inventory_emitter.min_interval_s = 0.0
        try:
            await sessions.open("host", "context", provider="codex", visibility="visible")
            persisted = await observer.observe_context(
                "host",
                "context",
                provider="codex",
                reading=ContextReading(tokens=10, model_context_window=100),
                observed_at="2026-08-15T00:00:00Z",
            )
            assert persisted is not None
            frame = json.loads(queue.get_nowait()[1])
            assert frame["type"] == "session.inventory"
            assert frame["sessions"][0]["context_tokens"] == 10

            # Two identical context readings five seconds apart do not turn into
            # two inventory broadcasts.  context_updated_at is observational,
            # not a client-visible context change that warrants a full frame.
            unchanged = await observer.observe_context(
                "host",
                "context",
                provider="codex",
                reading=ContextReading(tokens=10, model_context_window=100),
                observed_at="2026-08-15T00:00:05Z",
            )
            assert unchanged is not None
            assert queue.empty()
        finally:
            store.stop()

    asyncio.run(run())
