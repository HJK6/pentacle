"""Read-only delivered-frame and durable tell-record contracts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from comms import Comms  # noqa: E402
from ledger import Ledger  # noqa: E402
from server import Server  # noqa: E402
from sessions import Sessions  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from store import Store  # noqa: E402


HOST = "hosta"
RECIPIENT = f"{HOST}:recipient"
SENDER = f"{HOST}:sender"


class EchoTmux:
    """A provider-less pane that records every immediate paste."""

    def __init__(self) -> None:
        self.pastes: list[str] = []

    async def has_session(self, name: str) -> bool:
        return True

    async def capture(self, name: str) -> str:
        return self.pastes[-1] if self.pastes else ""

    async def paste(self, name: str, text: str) -> None:
        self.pastes.append(text)

    async def run(self, *args: str, **kwargs: object) -> tuple[int, str]:
        return (0, "")


async def _state() -> tuple[Store, EchoTmux, Comms, Server]:
    store = Store(":memory:")
    store.start()
    tmux = EchoTmux()
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    spawnctl = SpawnCtl(store, sessions, tmux=tmux)
    comms = Comms(store, sessions, spawnctl)
    await sessions.open(HOST, "recipient", provider="stub")
    ledger = Ledger(store, sessions=sessions, comms=comms)
    server = Server(
        store=store,
        sessions=sessions,
        spawnctl=spawnctl,
        comms=comms,
        ledger=ledger,
        local_host=HOST,
    )
    return store, tmux, comms, server


def test_delivered_text_binds_sender_recipient_and_ledger_row() -> None:
    async def _go() -> None:
        store, tmux, comms, server = await _state()
        try:
            body = "exact first line\nexact second line — no queue"
            reply = await comms.tell(
                {
                    "tell_id": "audit-tell-1",
                    "from_stream_id": SENDER,
                    "to_stream_id": RECIPIENT,
                    "message": body,
                }
            )

            assert reply["type"] == "tell.ok"
            assert reply["delivery_status"] == "delivered"
            assert isinstance(reply["ledger_row_id"], int)
            assert tmux.pastes == [body]

            by_tell = (await server._dispatch(json.dumps({
                "type": "ledger_get",
                "request_id": "audit-get-tell",
                "tell_id": "audit-tell-1",
            })))[0]
            by_row = (await server._dispatch(json.dumps({
                "type": "ledger_get",
                "request_id": "audit-get-row",
                "ledger_row_id": reply["ledger_row_id"],
            })))[0]
            inbound = (await server._dispatch(json.dumps({
                "type": "inbound_audit",
                "request_id": "audit-inbound",
                "stream_id": RECIPIENT,
                "limit": 10,
            })))[0]

            assert by_tell["type"] == "ledger_get.ok"
            assert by_row["type"] == "ledger_get.ok"
            assert by_tell["tell"] == by_row["tell"]
            record = by_tell["tell"]
            assert record["ledger_row_id"] == reply["ledger_row_id"]
            assert record["tell_id"] == "audit-tell-1"
            assert record["from_stream_id"] == SENDER
            assert record["to_stream_id"] == RECIPIENT
            assert record["text"] == body
            assert record["text_available"] is True
            assert record["delivery_status"] == "delivered"
            assert record["delivery_attempts"] == 1
            assert record["request_payload_hash"]

            assert inbound["type"] == "inbound_audit.ok"
            assert inbound["read_only"] is True
            assert inbound["source"] == "v2_tell_deliveries"
            assert inbound["frames"] == [record]
            assert inbound["frames"][0]["text"] == record["text"]
            assert inbound["frames"][0]["from_stream_id"] != RECIPIENT
        finally:
            store.stop()

    asyncio.run(_go())


def test_audit_is_read_only_and_cannot_reintroduce_hold_or_inbox_semantics() -> None:
    async def _go() -> None:
        store, tmux, comms, server = await _state()
        try:
            await comms.tell({
                "tell_id": "audit-tell-2",
                "from_stream_id": SENDER,
                "to_stream_id": RECIPIENT,
                "message": "already delivered",
            })
            before = await store.get_tell_delivery("audit-tell-2")
            count_before = await store.submit(
                lambda conn: conn.execute(
                    "SELECT COUNT(*) FROM v2_tell_deliveries"
                ).fetchone()[0]
            )

            rejected = (await server._dispatch(json.dumps({
                "type": "inbound_audit",
                "request_id": "audit-hold",
                "stream_id": RECIPIENT,
                "hold": True,
            })))[0]
            zero = (await server._dispatch(json.dumps({
                "type": "inbound_audit",
                "request_id": "audit-zero",
                "stream_id": RECIPIENT,
                "limit": 0,
            })))[0]
            after = await store.get_tell_delivery("audit-tell-2")
            count_after = await store.submit(
                lambda conn: conn.execute(
                    "SELECT COUNT(*) FROM v2_tell_deliveries"
                ).fetchone()[0]
            )

            assert rejected["type"] == "inbound_audit.error"
            assert rejected["error_code"] == "invalid_request"
            assert "cannot be deferred" in rejected["error"]
            assert zero["type"] == "inbound_audit.ok"
            assert zero["frames"] == []
            assert zero["complete"] is True
            assert before == after
            assert count_before == count_after == 1
            assert "inbox" not in server.handlers
            assert "hold" not in server.handlers
            assert "defer" not in server.handlers
            assert tmux.pastes == ["already delivered"]
        finally:
            store.stop()

    asyncio.run(_go())


def test_missing_tell_is_typed_without_creating_delivery_state() -> None:
    async def _go() -> None:
        store, _tmux, _comms, server = await _state()
        try:
            reply = (await server._dispatch(json.dumps({
                "type": "ledger_get",
                "request_id": "audit-missing",
                "tell_id": "does-not-exist",
            })))[0]
            assert reply["type"] == "ledger_get.error"
            assert reply["error_code"] == "tell_not_found"
        finally:
            store.stop()

    asyncio.run(_go())
