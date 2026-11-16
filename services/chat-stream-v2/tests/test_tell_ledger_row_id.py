"""`tell.ok` carries v1's `ledger_row_id`, and a replay reproduces it (QA #10).

v1's tell reply includes `ledger_row_id` (the tell row's id); v2 omitted it.
The store now assigns the id from the delivery row's rowid and stamps it into
the stored reply, so the fresh reply and any idempotent replay both carry the
same value. `delivery_status` stays "delivered" — v2 injects synchronously and
confirms the echo before replying, so unlike v1's pre-delivery "queued" it is
the honest terminal state, and it is in v1's recognized vocabulary.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from comms import Comms  # noqa: E402
from sessions import Sessions  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from store import Store  # noqa: E402

HOST = "hosta"
NAME = "v2-target"


class EchoTmux:
    """A live pane that echoes whatever was pasted, so the receipt observes it."""

    def __init__(self) -> None:
        self._pasted = ""

    async def has_session(self, name: str) -> bool:
        return True

    async def paste(self, name: str, text: str) -> None:
        self._pasted = text

    async def capture(self, name: str) -> str:
        return self._pasted

    async def run(self, *args: str, **kwargs: object) -> tuple[int, str]:
        return (0, "")


def test_tell_ok_carries_ledger_row_id_and_replay_reproduces_it() -> None:
    async def _go() -> tuple[dict, dict]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = EchoTmux()
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            spawnctl = SpawnCtl(store, sessions, tmux=tmux)
            comms = Comms(store, sessions, spawnctl)
            await sessions.open(HOST, NAME, created_at="2026-08-01T00:00:00Z")
            msg = {"stream_id": f"{HOST}:{NAME}", "message": "hello world", "tell_id": "t-1"}
            first = await comms.tell(dict(msg))
            replay = await comms.tell(dict(msg))
            return first, replay
        finally:
            store.stop()

    first, replay = asyncio.run(_go())

    assert first["type"] == "tell.ok"
    assert isinstance(first["ledger_row_id"], int), "tell.ok must carry ledger_row_id (QA #10)"
    assert first["delivery_status"] == "delivered"

    assert replay.get("duplicate") is True
    assert replay["ledger_row_id"] == first["ledger_row_id"], "a replay must reproduce the id"


def test_delivered_answer_tell_lookup_fails_closed_on_partial_or_foreign_records() -> None:
    async def _go() -> list[str]:
        store = Store(":memory:")
        store.start()
        try:
            async def put(tell_id: str, delivery_status: str) -> None:
                delivery = {"tell_id": tell_id, "delivery_status": delivery_status}
                reply = {"tell_id": tell_id, "delivery_status": delivery_status}
                await store.put_tell_delivery(
                    tell_id,
                    {"payload_digest": "test", "reply": reply, "delivery": delivery},
                )

            await put("notification-answer-delivered", "delivered")
            await put("notification-answer-partial", "pasted_unsubmitted")
            await put("v1-notification-answer-foreign", "delivered")
            return await store.list_delivered_tell_ids(
                prefix="notification-answer-", limit=100
            )
        finally:
            store.stop()

    assert asyncio.run(_go()) == ["notification-answer-delivered"]
