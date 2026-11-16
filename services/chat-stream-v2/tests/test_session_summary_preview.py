"""A populated inventory must preview conversation without fetching transcripts."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from session_summary import SUMMARY_TAIL_LIMIT
from sessions import Sessions
from store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "session_summary_missing_preview.json"


def test_live_list_sessions_fixture_projects_latest_visible_message():
    fixture = json.loads(FIXTURE.read_text())
    row = fixture["list_sessions"]["active"][0]
    assert "last_text" not in row and "last_kind" not in row
    sessions = Sessions(None, local_host="fixture")
    sessions._inv[row["stream_id"]] = dict(row)
    for event in fixture["events"]:
        sessions.apply_genuine_activity_event(row["stream_id"], event)
    actual = sessions.list_open()[0]
    assert {key: actual.get(key) for key in fixture["expected"]} == fixture["expected"]
    assert actual["last_event_at"] == fixture["events"][-1]["timestamp"]


def test_existing_chat_rebuilds_preview_from_durable_history(tmp_path):
    async def run():
        fixture = json.loads(FIXTURE.read_text())
        row = fixture["list_sessions"]["active"][0]
        database = tmp_path / "sessions.db"
        store = Store(str(database))
        store.start()
        try:
            await store.open_session(row["host"], row["session_name"], created_at=row["created_at"],
                                     session_generation=row["session_generation"], provider="claude")
            for index, event in enumerate(fixture["events"]):
                await store.append_session_event(row["stream_id"], event, identity=f"fixture-{index}", limit=500)
        finally:
            store.stop()
        reopened = Store(str(database))
        reopened.start()
        try:
            sessions = Sessions(reopened, local_host="fixture")
            await sessions.refresh()
            actual = sessions.list_open()[0]
            assert {key: actual.get(key) for key in fixture["expected"]} == fixture["expected"]
        finally:
            reopened.stop()
    asyncio.run(run())


CREATED = "2026-10-26T00:00:00Z"


def registry():
    sessions = Sessions(None, local_host="fixture")
    sessions._inv["fixture:chat"] = {"stream_id": "fixture:chat", "host": "fixture",
        "session_name": "chat", "session_generation": "generation-a", "created_at": CREATED}
    return sessions


def event(text="Answer", kind="ASSIST_TEXT", seq=1, timestamp="2026-10-26T01:00:00Z", **fields):
    return {"stream_id": "fixture:chat", "provider": "claude", "text": text, "kind": kind,
            "daemon_seq": seq, "timestamp": timestamp, **fields}


@pytest.mark.parametrize("kind", ["USER", "ASSIST_TEXT", "ASSIST", "TELL"])
def test_normalized_conversation_kinds_produce_previews(kind):
    sessions = registry()
    sessions.apply_genuine_activity_event("fixture:chat", event(" Visible message ", kind))
    assert sessions.get("fixture:chat")["last_text"] == "Visible message"
    assert sessions.get("fixture:chat")["last_kind"] == kind


@pytest.mark.parametrize("noise", [
    {"kind": "TOOL_USE"}, {"kind": "TOOL_RESULT"}, {"kind": "SYSTEM"},
    {"kind": "THINKING"}, {"kind": "WORKING"}, {"text": " \n"},
    {"raw": {"is_sidechain": True}}, {"raw": {"isSidechain": True}},
    {"kind": "USER", "message_envelope": {"kind": "child_report_ready"}},
    {"kind": "USER", "message_envelope": {"kind": "notice_marker"}},
    {"kind": "USER", "text": "<system-reminder>Internal</system-reminder>"},
])
def test_activity_noise_does_not_replace_conversation_preview(noise):
    sessions = registry()
    sessions.apply_genuine_activity_event("fixture:chat", event())
    sessions.apply_genuine_activity_event("fixture:chat", {**event(seq=2, timestamp="2026-10-26T01:00:01Z"), **noise})
    row = sessions.get("fixture:chat")
    assert (row["last_text"], row["last_kind"]) == ("Answer", "ASSIST_TEXT")
    assert row["last_event_at"] == "2026-10-26T01:00:01Z"


@pytest.mark.parametrize("order", [(10, 11), (11, 10)])
def test_timestamp_ties_use_durable_sequence_in_both_arrival_orders(order):
    sessions = registry()
    for seq in order:
        sessions.apply_genuine_activity_event("fixture:chat", event(str(seq), seq=seq))
    sessions.apply_genuine_activity_event("fixture:chat", event("Older", seq=99, timestamp="2026-10-26T00:30:00Z"))
    assert sessions.get("fixture:chat")["last_text"] == "11"


def test_metadata_refresh_preserves_preview_but_generation_rotation_clears_it():
    sessions = registry()
    original = sessions.get("fixture:chat")
    sessions.apply_genuine_activity_event("fixture:chat", event())
    sessions._cache({**original, "title": "Renamed"})
    assert sessions.get("fixture:chat")["last_text"] == "Answer"
    sessions._cache({**sessions.get("fixture:chat"), "session_generation": "generation-b"})
    assert "last_text" not in sessions.get("fixture:chat")
    assert sessions.apply_genuine_activity_event("fixture:chat", event("Stale", seq=2),
        expected_generation="generation-a") is None
    assert "last_text" not in sessions.get("fixture:chat")
    sessions.apply_genuine_activity_event("fixture:chat", event("New", seq=3),
        expected_generation="generation-b")
    assert sessions.get("fixture:chat")["last_text"] == "New"


def test_pre_birth_and_invalid_timestamp_events_cannot_create_preview():
    sessions = registry()
    for timestamp in ["2026-10-25T23:59:59Z", "invalid", "2026-10-26T01:00:00"]:
        sessions.apply_genuine_activity_event("fixture:chat", event(timestamp=timestamp))
    assert "last_text" not in sessions.get("fixture:chat")


def test_local_jsonl_notice_live_and_restart_match(tmp_path):
    from ingest import Ingest, _StreamIngest, _close_stream
    from message_envelopes import build_message_envelope
    async def run():
        store = Store(":memory:")
        store.start()
        state = None
        try:
            sessions = Sessions(store, local_host="fixture")
            row = await sessions.open("fixture", "chat", provider="claude", created_at=CREATED, pane_pid="1234")
            notice = build_message_envelope("child_report_ready", report_id="fixture-report", ledger_row_id=1,
                child_stream_id="fixture:child", msg_id=1, status="done", summary="Worker done")
            records = [
                {"type": "assistant", "uuid": "answer", "sessionId": "fixture-session", "timestamp": "2026-10-26T01:00:00Z",
                 "message": {"content": [{"type": "text", "text": "Visible answer"}]}},
                {"type": "user", "uuid": "notice", "sessionId": "fixture-session", "timestamp": "2026-10-26T01:00:01Z",
                 "message": {"content": notice}},
            ]
            path = tmp_path / "provider.jsonl"
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            state = _StreamIngest(generation=row["session_generation"], path=str(path))
            ingest = Ingest(store, sessions, None, lambda _: asyncio.sleep(0), local_host="fixture", recent_limit=500)
            assert await ingest._ingest_stream_events(row, state, 500) == 2
            live = sessions.get("fixture:chat")
            assert live["last_text"] == "Visible answer"
            tail = await store.fetch_session_event_tail("fixture:chat", limit=10)
            assert tail[-1]["message_envelope"]["kind"] == "child_report_ready"
            await sessions.refresh()
            rebuilt = sessions.get("fixture:chat")
            for key in ("last_text", "last_kind", "last_event_at", "_last_message_seq"):
                assert rebuilt[key] == live[key]
        finally:
            if state:
                _close_stream(state)
            store.stop()
    asyncio.run(run())


def test_startup_tail_bound_and_same_name_reopen(tmp_path):
    async def run():
        store = Store(str(tmp_path / "sessions.db"))
        store.start()
        try:
            sessions = Sessions(store, local_host="fixture")
            await sessions.open("fixture", "chat", provider="claude", created_at=CREATED)
            await store.append_session_event("fixture:chat", event(), identity="answer", limit=500)
            # The bound applies to the scan, even when durable history is larger.
            for n in range(SUMMARY_TAIL_LIMIT):
                await store.append_session_event("fixture:chat", event("Tool", "TOOL_USE", seq=n + 2),
                    identity=f"tool-{n}", limit=500)
            await sessions.refresh()
            assert "last_text" not in sessions.get("fixture:chat")
            await store.append_session_event("fixture:chat", event("Retained", seq=999), identity="retained", limit=500)
            await sessions.refresh()
            assert sessions.get("fixture:chat")["last_text"] == "Retained"
            await store.mark_closed("fixture", "chat", closed_at="2026-10-26T02:00:00Z", pane_status="gone")
            await sessions.open("fixture", "chat", provider="claude", created_at="2026-10-26T03:00:00Z")
            await sessions.refresh()
            assert "last_text" not in sessions.get("fixture:chat")
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("events_mode", ["summary", "full"])
def test_summary_wire_list_inventory_and_restart(tmp_path, events_mode):
    import websockets
    from inventory import InventoryEmitter
    from server import Server
    from ingest import append_ingested_event

    async def receive(socket, frame_type):
        async with asyncio.timeout(3):
            while True:
                frame = json.loads(await socket.recv())
                if frame["type"] == frame_type:
                    return frame

    async def run():
        database = tmp_path / "wire.db"
        for restart in (False, True):
            store = Store(str(database))
            store.start()
            server = None
            emitter = None
            try:
                sessions = Sessions(store, local_host="fixture")
                if not restart:
                    await sessions.open("fixture", "chat", provider="claude", created_at=CREATED)
                    await sessions.open("fixture", "empty", provider="claude", created_at=CREATED)
                    await store.append_session_event("fixture:chat", event("Initial"), identity="initial", limit=500)
                await sessions.refresh()
                server = Server(port=0, store=store, sessions=sessions, local_host="fixture")
                server.inventory_ready.set()
                port = await server.bind()
                emitter = InventoryEmitter(sessions, server.broadcast, min_interval_s=0)
                async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
                    await socket.send(json.dumps({"type": "hello", "client": "summary-preview-test",
                        "subscribe": {"all": True, "include_subagents": True, "events_mode": events_mode}}))
                    snapshot = await receive(socket, "snapshot")
                    rows = {row["stream_id"]: row for row in snapshot["sessions"]}
                    if events_mode == "summary":
                        assert not snapshot.get("events")
                    assert all(not key.startswith("_last_message") for row in rows.values() for key in row)
                    assert rows["fixture:chat"]["last_text"] == ("Updated" if restart else "Initial")
                    assert "last_text" not in rows["fixture:empty"]
                    await socket.send(json.dumps({"type": "list_sessions", "request_id": "list"}))
                    listed = await receive(socket, "list_sessions.ok")
                    assert all(not key.startswith("_last_message") for row in listed["active"] for key in row)
                    assert next(row for row in listed["active"] if row["stream_id"] == "fixture:chat")["last_text"] == rows["fixture:chat"]["last_text"]
                    if not restart:
                        await append_ingested_event(store, server.broadcast,
                            event("Updated", seq=2, timestamp="2026-10-26T01:00:01Z"), recent_limit=500, sessions=sessions)
                        await emitter.emit_if_changed(immediate=True)
                        inventory = await receive(socket, "session.inventory")
                        assert all(not key.startswith("_last_message") for row in inventory["sessions"] for key in row)
                        assert next(row for row in inventory["sessions"] if row["stream_id"] == "fixture:chat")["last_text"] == "Updated"
            finally:
                if emitter and emitter._flush_task is not None:
                    emitter._flush_task.cancel()
                    await asyncio.gather(emitter._flush_task, return_exceptions=True)
                if server:
                    await server.close()
                store.stop()
    asyncio.run(run())


def test_remote_push_projects_live_preview_before_inventory_and_restart():
    from test_event_push import _sink, _push
    from message_envelopes import build_message_envelope

    async def run():
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, local_host="fixture")
            await sessions.open("fixture", "chat", provider="claude", created_at=CREATED)
            sink, frames, _ = _sink(store, sessions=sessions, emit_inventory=True)
            notice = build_message_envelope("child_report_ready", report_id="remote-report", ledger_row_id=1,
                child_stream_id="fixture:child", msg_id=1, status="done", summary="Worker done")
            payloads = [event("Remote answer", raw={"jsonl_record_uuid": "remote-answer", "jsonl_event_index": 0}),
                event(notice, "USER", seq=2, timestamp="2026-10-26T01:00:01Z",
                      raw={"jsonl_record_uuid": "remote-notice", "jsonl_event_index": 0})]
            reply = await sink.handle_push(_push(sink, payloads, host="fixture"))
            assert reply["inserted"] == 2
            row = sessions.get("fixture:chat")
            assert row["last_text"] == "Remote answer"
            inventory = next(frame for frame in frames if frame["type"] == "session.inventory")
            assert inventory["sessions"][0]["last_text"] == "Remote answer"
            await sessions.refresh()
            assert sessions.get("fixture:chat")["last_text"] == row["last_text"]
            assert sessions.get("fixture:chat")["_last_message_seq"] == row["_last_message_seq"]
        finally:
            store.stop()
    asyncio.run(run())


def test_generation_rotation_keeps_incoming_event_time_but_drops_inherited_time():
    sessions = registry()
    sessions.apply_genuine_activity_event("fixture:chat", event())
    incoming = {"stream_id": "fixture:chat", "host": "fixture", "session_name": "chat",
                "created_at": CREATED, "session_generation": "generation-b", "last_event_at": "2026-10-26T02:00:00Z"}
    sessions._cache(incoming)
    assert sessions.get("fixture:chat")["last_event_at"] == incoming["last_event_at"]
    assert "last_text" not in sessions.get("fixture:chat")
    incoming.pop("last_event_at")
    incoming["session_generation"] = "generation-c"
    sessions._cache(incoming)
    assert "last_event_at" not in sessions.get("fixture:chat")
