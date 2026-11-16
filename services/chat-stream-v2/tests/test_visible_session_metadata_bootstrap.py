"""First operator send gives visible top-level sessions durable metadata."""

from __future__ import annotations

import asyncio

from server import Server
from sessions import Sessions
from store import Store


class _Comms:
    async def send(self, _msg):
        return {"type": "send.result", "delivery": "landed", "submission_confirmed": True}


def test_operator_send_bootstraps_visible_title_and_card() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, local_host="local")
            await sessions.open("local", "visible", provider="claude", visibility="default")
            server = Server(store=store, sessions=sessions, comms=_Comms(), local_host="local")
            reply = await server._on_send({
                "host": "local",
                "session_name": "visible",
                "text": "Diagnose the production synchronization failure and validate the repair.",
                "_auth_context": {"operator_authenticated": True},
            })
            assert reply["delivery"] == "landed"
            row = await store.fetch_session("local", "visible")
            assert row["title"] == "Diagnose the production synchronization failure and validate the repair."
            assert row["status_card"]["goal"] == row["title"]
            assert row["status_card"]["plan"][0]["status"] == "active"
        finally:
            store.stop()

    asyncio.run(run())


def test_non_operator_or_hidden_send_does_not_bootstrap_metadata() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, local_host="local")
            await sessions.open("local", "hidden", provider="claude", visibility="hidden")
            await sessions.open("local", "seat", provider="claude", visibility="default")
            server = Server(store=store, sessions=sessions, comms=_Comms(), local_host="local")
            await server._on_send({
                "host": "local", "session_name": "hidden", "text": "Hidden work",
                "_auth_context": {"operator_authenticated": True},
            })
            await server._on_send({
                "host": "local", "session_name": "seat", "text": "Peer message",
                "_auth_context": {"token_verified": True},
            })
            hidden = await store.fetch_session("local", "hidden")
            seat = await store.fetch_session("local", "seat")
            assert hidden["title"] is None and hidden["status_card"] is None
            assert seat["title"] is None and seat["status_card"] is None
        finally:
            store.stop()

    asyncio.run(run())
