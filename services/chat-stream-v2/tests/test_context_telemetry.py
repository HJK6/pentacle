"""Regression coverage for v2 all-provider context telemetry."""

from __future__ import annotations

import asyncio
import json

from ingest import Ingest
from routing_integrity import RoutingIntegrity
from server import Server
from sessions import Sessions
from store import Store


HOST = "hostb"


class _Tmux:
    async def capture(self, _name: str) -> str:
        return "› Work\n\n  gpt-5.6-terra xhigh · ~/workspace\n\n"


async def _surfaces(server: Server, stream_id: str) -> tuple[dict, dict, dict]:
    server.inventory_ready.set()
    listed = await server._on_list_sessions({})
    inspect = await server._on_inspect_stream({"stream_id": stream_id, "event_tail": 0})
    status = await server._on_status_card({"stream_id": stream_id, "goal": "telemetry"})
    return listed, inspect, status


async def _assert_null_context_surfaces(server: Server, stream_id: str) -> None:
    listed, inspect, status = await _surfaces(server, stream_id)
    listed_session = next(
        session for session in listed["active"] if session["stream_id"] == stream_id
    )
    for surface in (listed_session, inspect["session"], status["session"]):
        assert surface["context_tokens"] is None
        assert surface["model_context_window"] is None
        assert surface["context_level"] is None
        assert surface["context_updated_at"] is None


def test_claude_events_populate_status_list_and_inspect_context_fields(tmp_path) -> None:
    async def run() -> None:
        store = Store(str(tmp_path / "claude.db"))
        store.start()
        sessions = Sessions(store, local_host=HOST)
        observer = RoutingIntegrity(store, sessions)
        server = Server(store=store, sessions=sessions)
        stream_id = f"{HOST}:claude-context"
        try:
            await sessions.open(
                HOST,
                "claude-context",
                created_at="2026-08-14T00:00:00Z",
                provider="claude",
                requested_model="claude-fable-5",
                requested_effort="high",
                pane_status="pane_alive",
            )

            await observer.observe_claude_event(
                {
                    "stream_id": stream_id,
                    "provider": "claude",
                    "kind": "ASSIST_TEXT",
                    "timestamp": "2026-08-14T12:25:10Z",
                    "raw": {
                        "model": "claude-fable-5",
                        "effort": "high",
                        "source_session_identity": "claude-main-1",
                        "usage": {
                            "input_tokens": 120_000,
                            "output_tokens": 10_000,
                            "cache_read_input_tokens": 20_000,
                            "cache_creation_input_tokens": 5_000,
                        },
                    },
                }
            )

            await observer.observe_claude_event(
                {
                    "stream_id": stream_id,
                    "provider": "claude",
                    "kind": "ASSIST_TEXT",
                    "timestamp": "2026-08-14T12:26:10Z",
                    "raw": {
                        "model": "claude-fable-5",
                        "effort": "high",
                        "source_session_identity": "claude-main-1",
                        "usage": {
                            "input_tokens": 220_000,
                            "output_tokens": 20_000,
                            "cache_read_input_tokens": 20_000,
                            "cache_creation_input_tokens": 5_000,
                        },
                    },
                }
            )

            listed, inspect, status = await _surfaces(server, stream_id)
            for surface in (listed["active"][0], inspect["session"], status["session"]):
                assert surface["context_tokens"] == 265_000
                assert surface["model_context_window"] == 1_000_000
                assert surface["context_level"] == "none"
                assert surface["context_updated_at"] == "2026-08-14T12:26:10Z"
        finally:
            store.stop()

    asyncio.run(run())

def test_fd_bound_codex_token_count_populates_context_without_event_tail_row(tmp_path) -> None:
    """A real Codex bookkeeping record updates its own stream, not the event tail."""
    async def run() -> None:
        rollout_path = tmp_path / "rollout.jsonl"
        rollout_path.write_text(
            "\n".join(
                (
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {"id": "01a000b9-60a7-7a51-9e9a-bfed1711c4fb"},
                        }
                    ),
                    json.dumps(
                        {
                            "timestamp": "2026-08-14T14:43:00Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "last_token_usage": {"total_tokens": 111_838},
                                    "model_context_window": 258_400,
                                },
                            },
                        }
                    ),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        store = Store(str(tmp_path / "fd-bound.db"))
        store.start()
        sessions = Sessions(store, local_host=HOST)
        observer = RoutingIntegrity(store, sessions)
        server = Server(store=store, sessions=sessions)
        stream_id = f"{HOST}:codex-fd-context"
        try:
            await sessions.open(
                HOST,
                "codex-fd-context",
                created_at="2026-08-14T00:00:00Z",
                provider="codex",
                requested_model="gpt-5.6-terra",
                requested_effort="xhigh",
                pane_status="pane_alive",
                jsonl_path=str(rollout_path),
            )
            ingest = Ingest(
                store,
                sessions,
                _Tmux(),
                lambda _event: asyncio.sleep(0),
                local_host=HOST,
                recent_limit=20,
                routing_integrity=observer,
            )

            assert await ingest.run_pass() == 0

            listed, inspect, status = await _surfaces(server, stream_id)
            for surface in (listed["active"][0], inspect["session"], status["session"]):
                assert surface["context_tokens"] == 111_838
                assert surface["model_context_window"] == 258_400
                assert surface["context_level"] == "none"
                assert surface["context_updated_at"] == "2026-08-14T14:43:00Z"
        finally:
            store.stop()

    asyncio.run(run())


def test_fd_bound_codex_token_count_without_session_identity_stays_null(tmp_path) -> None:
    async def run() -> None:
        rollout_path = tmp_path / "unbound-rollout.jsonl"
        rollout_path.write_text(
            json.dumps(
                {
                    "timestamp": "2026-08-14T14:43:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {"total_tokens": 111_838},
                            "model_context_window": 258_400,
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        store = Store(str(tmp_path / "unbound-fd.db"))
        store.start()
        sessions = Sessions(store, local_host=HOST)
        observer = RoutingIntegrity(store, sessions)
        server = Server(store=store, sessions=sessions)
        stream_id = f"{HOST}:codex-unbound-context"
        try:
            await sessions.open(
                HOST,
                "codex-unbound-context",
                provider="codex",
                requested_model="gpt-5.6-terra",
                requested_effort="xhigh",
                pane_status="pane_alive",
                jsonl_path=str(rollout_path),
            )
            ingest = Ingest(
                store,
                sessions,
                _Tmux(),
                lambda _event: asyncio.sleep(0),
                local_host=HOST,
                recent_limit=20,
                routing_integrity=observer,
            )

            assert await ingest.run_pass() == 0
            await _assert_null_context_surfaces(server, stream_id)
        finally:
            store.stop()

    asyncio.run(run())
