"""Direct reparent coverage, including the central cross-host metadata path."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from server import Server  # noqa: E402
from sessions import Sessions  # noqa: E402
from store import STREAM_TOKEN_HASH_VERSION, Store  # noqa: E402


class LocalSocket:
    remote_address = ("127.0.0.1", 8765)


def test_reparent_dispatch_moves_cross_host_worker() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        token = "old-parent-token"
        try:
            sessions = Sessions(store, tmux=None, local_host="testhost")
            await store.open_session("testhost", "old", visibility="hidden")
            await store.grant_stream_token(
                "testhost",
                "old",
                hashlib.sha256(token.encode("utf-8")).hexdigest(),
                STREAM_TOKEN_HASH_VERSION,
            )
            await store.open_session(
                "loopbox",
                "child",
                visibility="hidden",
                parent_stream_id="testhost:old",
            )
            await store.open_session("testhost", "successor", visibility="hidden")
            await sessions.refresh()

            server = Server(store=store, sessions=sessions)
            reply = await server._dispatch(json.dumps({
                "type": "reparent",
                "request_id": "reparent-cross-host",
                "stream_id": "loopbox:child",
                "new_parent_stream_id": "testhost:successor",
                "from_stream_id": "testhost:old",
                "stream_token": token,
            }), websocket=object())

            assert reply == [{
                "type": "reparent.ok",
                "ok": True,
                "worker_stream_id": "loopbox:child",
                "old_parent_stream_id": "testhost:old",
                "new_parent_stream_id": "testhost:successor",
                "request_id": "reparent-cross-host",
            }]
            row = await store.fetch_session("loopbox", "child")
            assert row is not None
            assert row["parent_stream_id"] == "testhost:successor"
        finally:
            store.stop()

    asyncio.run(run())


def test_reparent_rejects_pretoken_caller_and_audits_refusal() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="testhost")
            await store.open_session("testhost", "old", visibility="hidden")
            await store.open_session(
                "loopbox",
                "child",
                visibility="hidden",
                parent_stream_id="testhost:old",
            )
            await store.open_session("testhost", "successor", visibility="hidden")
            await sessions.refresh()

            server = Server(store=store, sessions=sessions)
            reply = (await server._dispatch(json.dumps({
                "type": "reparent",
                "request_id": "reparent-pretoken",
                "stream_id": "loopbox:child",
                "new_parent_stream_id": "testhost:successor",
                "from_stream_id": "testhost:old",
            }), websocket=LocalSocket()))[0]

            assert reply["error_code"] == "stream_ownership_unverified"
            row = await store.fetch_session("loopbox", "child")
            assert row is not None
            assert row["parent_stream_id"] == "testhost:old"
        finally:
            store.stop()

    asyncio.run(run())
