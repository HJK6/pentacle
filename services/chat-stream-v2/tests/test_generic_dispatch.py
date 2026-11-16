"""Removed daemon-v2 verbs use the generic unsupported dispatch path."""

from __future__ import annotations

import asyncio
import json

from server import Server  # noqa: E402
from window_schedule import WindowSchedule  # noqa: E402


def test_removed_verbs_fall_to_generic_dispatch() -> None:
    async def run() -> None:
        server = Server()
        for verb, request_id in (
            ("restore", "restore-removed"),
            ("reconcile.inventory", "inventory-removed"),
        ):
            replies = await server._dispatch(json.dumps({"type": verb, "request_id": request_id}))
            reply = replies[0]
            assert reply["type"] == f"{verb}.error"
            assert reply["error_code"] == "unsupported_in_v2"
            assert reply["verb"] == verb
            assert reply["request_id"] == request_id

    asyncio.run(run())


def test_window_verbs_fall_to_generic_dispatch() -> None:
    async def run() -> None:
        surface = WindowSchedule(None, None, None, None, local_host="hosta")
        verb = "coordination." + "window.request"
        assert verb not in surface.wire_handlers()

        server = Server()
        server.handlers.update(surface.wire_handlers())
        request_id = "window-removed"
        replies = await server._dispatch(json.dumps({
            "type": verb, "request_id": request_id,
        }))
        reply = replies[0]
        assert reply["type"] == f"{verb}.error"
        assert reply["error_code"] == "unsupported_in_v2"
        assert reply["verb"] == verb
        assert reply["request_id"] == request_id

    asyncio.run(run())
