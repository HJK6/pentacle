"""Durable, latest-row receipt projection regressions."""

from __future__ import annotations

import asyncio
import json

from event_push import EventPush, WIRE_VERSION
from ingest import append_ingested_event
from server import Server
from store import Store


TARGET = "hostc:receipt-target"
REQUEST = "send-receipt-projection"


def _store() -> Store:
    store = Store(":memory:")
    store.start()
    return store


async def _append(store: Store, state: str, *, request_id: str = REQUEST) -> dict:
    return await store.append_send_receipt(
        to_stream_id=TARGET,
        request_id=request_id,
        receipt_id="receipt-1",
        state=state,
        wire_text="hello",
        display_text="hello",
        attachments=[],
        delivery=state,
        submission_confirmed=state == "landed",
    )


def test_receipt_query_projects_latest_accepted_to_landed_row() -> None:
    async def go() -> None:
        store = _store()
        try:
            accepted = await _append(store, "accepted")
            landed = await _append(store, "landed")
            projected = await store.get_send_receipt(TARGET, REQUEST)
            assert projected is not None
            assert projected["state"] == "landed"
            assert landed["receipt_rowid"] > accepted["receipt_rowid"]
            assert "receipt_rowid" not in projected
        finally:
            store.stop()

    asyncio.run(go())


def test_stamped_identical_user_events_prefer_explicit_request_id() -> None:
    """A replayed stamped event must not cross-correlate prompt bodies."""
    async def go() -> None:
        store = _store()
        try:
            first_request = "send-r53-first"
            second_request = "send-r53-second"
            await _append(store, "accepted", request_id=first_request)
            await _append(store, "accepted", request_id=second_request)

            stamped = await store.stamp_event_with_send_receipt({
                "kind": "USER",
                "stream_id": TARGET,
                "request_id": first_request,
                "text": "hello",
            })

            assert stamped["request_id"] == first_request
            assert stamped["receipt_state"] == "accepted"
            first = await store.get_send_receipt(TARGET, first_request)
            second = await store.get_send_receipt(TARGET, second_request)
            assert first is not None and first["state"] == "accepted"
            assert second is not None and second["state"] == "accepted"
            receipt_rows = await store.submit(
                lambda conn: conn.execute("SELECT COUNT(*) FROM v2_send_receipts").fetchone()[0],
            )
            assert receipt_rows == 2
        finally:
            store.stop()

    asyncio.run(go())


def test_receipt_query_projects_accepted_to_not_landed_row() -> None:
    async def go() -> None:
        store = _store()
        try:
            await _append(store, "accepted")
            terminal = await _append(store, "not_landed")
            projected = await store.get_send_receipt(TARGET, REQUEST)
            assert projected is not None
            assert projected["state"] == "not_landed"
            assert terminal["receipt_rowid"] > 0
        finally:
            store.stop()

    asyncio.run(go())


def test_retry_after_terminal_supersedes_with_latest_attempt() -> None:
    async def go() -> None:
        store = _store()
        try:
            await _append(store, "accepted")
            await _append(store, "not_landed")
            retry_accept = await _append(store, "accepted")
            retry_landed = await _append(store, "landed")
            projected = await store.get_send_receipt(TARGET, REQUEST)
            assert projected is not None
            assert projected["state"] == "landed"
            assert retry_landed["receipt_rowid"] > retry_accept["receipt_rowid"]
        finally:
            store.stop()

    asyncio.run(go())


class _VisibleSessions:
    @staticmethod
    def get(stream_id: str) -> dict[str, str] | None:
        return {"stream_id": stream_id, "visibility": "visible"} if stream_id == TARGET else None


def test_query_verb_never_returns_two_rows_for_one_key() -> None:
    async def go() -> None:
        store = _store()
        try:
            await _append(store, "accepted")
            await _append(store, "not_landed")
            await _append(store, "accepted")
            await _append(store, "landed")
            server = Server(store=store, sessions=_VisibleSessions())
            (reply,) = await server._dispatch(json.dumps({
                "type": "send.receipt.get", "request_id": REQUEST, "to_stream_id": TARGET,
            }))
            assert reply["type"] == "send.receipt.get.ok"
            assert reply["found"] is True
            assert len(reply["receipts"]) == 1
            assert reply["receipts"][0]["state"] == "landed"
        finally:
            store.stop()

    asyncio.run(go())


def test_late_existing_user_event_is_stamped_from_durable_attachment_receipt() -> None:
    wire = "Image at /tmp/public-staged"

    async def go() -> tuple[dict, dict, int, Store]:
        store = _store()
        casts: list[dict] = []

        async def broadcast(frame: dict) -> None:
            casts.append(frame)

        try:
            await store.open_session("hostc", "receipt-target", visibility="visible")
            await store.append_send_receipt(
                to_stream_id=TARGET,
                request_id=REQUEST,
                receipt_id="receipt-image",
                state="accepted",
                wire_text="",
                display_text="",
                attachments=[{"key": "a" * 64, "mime": "image/jpeg", "bytes": 876_000}],
                delivery="accepted",
                submission_confirmed=False,
                optimistic_id="optimistic-image",
            )
            await store.append_send_receipt(
                to_stream_id=TARGET,
                request_id=REQUEST,
                receipt_id="receipt-image",
                state="landed",
                wire_text=wire,
                display_text="",
                attachments=[{"key": "a" * 64, "mime": "image/jpeg", "bytes": 876_000}],
                delivery="landed",
                submission_confirmed=True,
                optimistic_id="optimistic-image",
            )
            event = {"objective": "Exercise the existing spawn contract",
                "stream_id": TARGET, "provider": "claude", "kind": "USER", "text": wire,
                "request_id": REQUEST,
                "timestamp": "2026-08-20T15:01:00Z",
                "raw": {"jsonl_record_uuid": "late-user", "jsonl_event_index": 0},
            }
            assert await append_ingested_event(store, broadcast, event, recent_limit=50) is not None
            rows = await store.fetch_session_event_tail(TARGET, limit=50)
            receipt_rows = await store.submit(
                lambda conn: conn.execute("SELECT COUNT(*) FROM v2_send_receipts").fetchone()[0],
            )
            return casts[0]["event"], rows[0], receipt_rows, store
        except BaseException:
            store.stop()
            raise

    frame, row, receipt_rows, store = asyncio.run(go())
    try:
        for event in (frame, row):
            assert event["text"] == ""
            assert wire not in json.dumps(event)
            assert event["receipt_id"] == "receipt-image"
            assert event["request_id"] == REQUEST
            assert event["receipt_state"] == "landed"
            assert event["receipt_delivery"] == "landed"
            assert event["optimistic_id"] == "optimistic-image"
            assert event["attachment_count"] == 1
            assert event["attachments"][0]["mime"] == "image/jpeg"
        assert receipt_rows == 2
    finally:
        store.stop()


def test_peer_existing_event_push_is_stamped_without_a_receipt_channel() -> None:
    async def go() -> None:
        store = _store()
        broadcasts: list[dict] = []

        async def broadcast(frame: dict) -> None:
            broadcasts.append(frame)

        class Alerts:
            def emit(self, *_args, **_kwargs) -> None:
                return None

        try:
            await store.open_session("hostc", "receipt-target", visibility="visible")
            await store.append_send_receipt(
                to_stream_id=TARGET,
                request_id=REQUEST,
                receipt_id="receipt-peer",
                state="landed",
                wire_text="Image at /tmp/public-staged",
                display_text="",
                attachments=[{"key": "b" * 64, "mime": "image/jpeg", "bytes": 876_000}],
                delivery="landed",
                submission_confirmed=True,
            )
            wire = "Image at /tmp/public-staged"
            event = {
                "host": "hostc", "stream_id": TARGET, "provider": "claude", "kind": "USER",
                "text": wire, "timestamp": "2026-08-20T15:02:00Z",
                "raw": {"jsonl_record_uuid": "peer-late", "jsonl_event_index": 0},
            }
            sink = EventPush(store, broadcast, Alerts(), recent_limit=50)
            async def placeholder() -> str:
                return "placeholder"
            sink._secret = placeholder  # type: ignore[method-assign]
            reply = await sink.handle_push({
                "type": "event.push", "request_id": "push-1", "push_secret": "placeholder",
                "wire_version": WIRE_VERSION, "host": "hostc",
                "events": [event], "high_water": {"/tmp/receipt.jsonl": 1},
            })
            assert reply.get("inserted") == 1, reply
            assert len(broadcasts) == 1
            stamped = broadcasts[0]["event"]
            assert stamped["receipt_id"] == "receipt-peer"
            assert stamped["receipt_state"] == "landed"
            assert stamped["request_id"] == REQUEST
            assert stamped["text"] == ""
            assert wire not in json.dumps(stamped)
            assert stamped["attachments"][0]["mime"] == "image/jpeg"
        finally:
            store.stop()

    asyncio.run(go())


def test_receipt_log_survives_restart_and_keeps_attachment_paths_out_of_columns(tmp_path) -> None:
    async def go() -> tuple[list[str], str]:
        receipt_db = tmp_path / "receipts.db"
        store = Store(str(receipt_db))
        store.start()
        try:
            staged_path = "/tmp/public-staged"
            await store.append_send_receipt(
                to_stream_id=TARGET,
                request_id=REQUEST,
                receipt_id="receipt-path-free",
                state="accepted",
                wire_text=staged_path,
                display_text="",
                attachments=[],
                delivery="accepted",
                submission_confirmed=False,
            )

        finally:
            store.stop()

        restarted = Store(str(receipt_db))
        restarted.start()
        try:
            projected = await restarted.get_send_receipt(TARGET, REQUEST)
            assert projected is not None and projected["state"] == "accepted"

            def read(conn):
                columns = [str(row[1]) for row in conn.execute("PRAGMA table_info(v2_send_receipts)")]
                digest = str(conn.execute("SELECT wire_digest FROM v2_send_receipts").fetchone()[0])
                return columns, digest

            return await restarted.submit(read)
        finally:
            restarted.stop()

    columns, digest = asyncio.run(go())
    assert "wire_text" not in columns
    assert "wire_digest" in columns
    assert len(digest) == 64
