"""A7 wire history against the real dispatch, Store and frame publisher."""
import hashlib
import json
import shutil

import pytest

import retention
from assistant_composite import AssistantComposite, AssistantCompositeConfig, COMPOSITE_CAPABILITY
from test_connection_logging import connected, daemon, signed_hello
from test_receipt_route_retention import TARGET, append, archive_path, hot_count, move, seed_routes
from test_store_reader import record_calls


HOST, NAME = TARGET.split(":")
BIRTH = "2026-09-01T00:00:00Z"
GEN = "history-original"
WIRE_PAGE = "Store.fetch_wire_history_page"
RESIDUAL_PAGE = "Store.fetch_session_event_page.locals.snapshot"


async def event(store, text, **fields):
    [seq] = await store.append_session_events_lifecycle_cas(
        [{"stream_id": TARGET, "event": {"stream_id": TARGET, "text": text, "kind": "USER", **fields}}],
        limit=100,
    )
    assert type(seq) is int
    return seq


async def open_history(store, *, created_at=BIRTH, generation=GEN):
    await store.open_session(HOST, NAME, provider="claude", pane_pid="123", pane_status="pane_alive",
                             created_at=created_at, session_generation=generation)


async def delivery(store, *, label="fixture-tell", generation=GEN, birth=BIRTH):
    sender = "fixture:sender"
    wire = f"[from {sender}] [tell:{label}]\nmessage 雪"
    seq = await event(store, "message 雪", kind="TELL",
                      raw={"peer_wire_text": wire, "sender": sender, "peer_payload": "message 雪"})
    await store.put_tell_delivery(label, {
        "payload_digest": "proof-digest",
        "delivery": {"tell_id": label, "to_stream_id": TARGET, "text": wire,
                     "proof_generation": generation, "proof_session_created_at": birth,
                     "proof_watermark": 0, "proof_watermark_state": "reachable", "proof_event_id": seq,
                     "request_payload_hash": "proof-digest", "sender_verified": True,
                     "sender_kind": "peer", "from_stream_id": sender, "sender_generation": "sender-gen"},
    })
    return seq


async def authenticate(server, socket, welcome):
    _, hello = signed_hello(server, welcome)
    hello["capabilities"][COMPOSITE_CAPABILITY] = True
    assert (await socket.rpc(hello))["type"] == "ready"


async def dispatch(server, socket, **fields):
    return await server._dispatch(json.dumps({
        "type": "request_stream_events", "request_id": "fixture-history", "stream_id": TARGET,
        "limit": 2, "chunk_limit": 1, **fields,
    }), websocket=socket)


async def publish(server, socket, **fields):
    start = len(socket.sent)
    iterator = await dispatch(server, socket, **fields)
    assert hasattr(iterator, "__aiter__"), iterator
    assert await server._send_direct(socket, iterator, interleave_history=True)
    payloads = socket.sent[start:]
    frames = [json.loads(payload) for payload in payloads]
    assert frames[-1]["type"] == "request_stream_events.ok", frames
    return payloads, frames


def events(frames):
    return [e for frame in frames for e in frame["events"]]


@pytest.mark.asyncio
async def test_wire_history_parity_for_archived_and_current_generations(daemon, monkeypatch, caplog):
    store = daemon.store
    await open_history(store)
    await append(store, meta_json='{"voice":{"duration_s":1.5}}')
    voice = await event(store, "body", request_id="send-one", receipt_id="receipt-send-one",
                        message_id="input-0")
    await seed_routes(store)
    assert (await move(store))["moved"] == 1
    assert (await move(store, retention.ROUTE_TABLE))["moved"] == 1
    assert await hot_count(store) == await hot_count(store, retention.ROUTE_TABLE) == 0
    [binding] = await store.bind_chat_checkpoints(HOST, [{
        "stream_id": TARGET, "session_created_at": BIRTH, "generation": GEN, "pane_pid": "123",
        "native_session_id": "fixture-native", "file_digest": "a" * 64, "provider": "claude",
        "start_offset": 0, "next_offset": 0,
    }])
    assert binding["status"] == "bound"

    # Keep the current birth explicit in the peer delivery proof.
    current_birth = "2026-09-02T00:00:00Z"
    await open_history(store, created_at=current_birth, generation="history-current")
    notice_id = "status-fixture"
    body = f"[pentacle-notice:{notice_id}]\nstatus update"
    notice = await event(store, body)
    binding = {"body_sha256": hashlib.sha256(body.encode()).hexdigest(), "kind": "status_card",
               "notice_id": notice_id, "pre_input_watermark": 0, "proof_event_id": notice,
               "recipient_stream_id": TARGET, "session_generation": "history-current"}
    def bind_notice(conn):
        with conn:
            conn.execute(
                "INSERT INTO v2_outbound_notices(notice_id,kind,dedupe_key,recipient_stream_id,"
                "tell_id,body,payload_digest,created_at,proof_binding,delivered_at) "
                "VALUES(?,'status_card',?,?,?,?,?,'now',?,'now')",
                (notice_id, notice_id, TARGET, notice_id, body, "digest", json.dumps(binding)),
            )
    await store.submit(bind_notice)
    # An ordinary current USER event with an archived route activity binding.
    route = await event(store, "archived route", message_id="input-0")
    tell = await delivery(store, generation="history-current", birth=current_birth)
    excluded = await event(store, "excluded by exclusive cursor")
    daemon.assistant_composite = AssistantComposite(
        store, config=AssistantCompositeConfig(enabled=True, stream_id=TARGET))
    await daemon.sessions.refresh()

    seen = record_calls(monkeypatch)
    original = store.fetch_session_event_page

    async def writer_page(*args, **kwargs):
        kwargs.pop("history", None)
        return await original(*args, **kwargs)

    async with connected(daemon) as (socket, welcome, _):
        await authenticate(daemon, socket, welcome)
        requests = ({"generation": GEN, "before_daemon_seq": notice},
                    {"before_daemon_seq": excluded, "limit": 3})
        with monkeypatch.context() as writer:
            writer.setattr(store, "fetch_session_event_page", writer_page)
            controls = [await publish(daemon, socket, **fields) for fields in requests]
        caplog.clear()
        seen.clear()
        actual = [await publish(daemon, socket, **fields) for fields in requests]

    assert actual == controls
    [historical] = events(actual[0][1])
    assert historical["daemon_seq"] == voice
    assert historical["meta"] == {"voice": {"duration_s": 1.5}}
    assert "assistant_activity" in historical["raw"]
    assert [e["daemon_seq"] for e in events(actual[1][1])] == [tell, route, notice]
    assert events(actual[1][1])[0]["raw"]["input_provenance"]["kind"] == "peer"
    assert events(actual[1][1])[-1]["raw"]["daemon_notice"]["event_id"] == notice
    assert seen[store.history_read_diag] == [WIRE_PAGE] * 4
    assert WIRE_PAGE not in seen[store.call_diag]
    assert any("chat_history_readable" in name for name in seen[store.call_diag])
    assert any("assistant_composite_activity" in name for name in seen[store.call_diag])
    assert await original(TARGET, before_daemon_seq=excluded, limit=2)
    await store.submit(lambda conn: None)
    assert RESIDUAL_PAGE in seen[store.call_diag]
    summaries = [json.loads(rec.getMessage().removeprefix("wire_history_timing ")) for rec in caplog.records
                 if rec.getMessage().startswith("wire_history_timing ")]
    assert len(summaries) == 2
    for timing, (raw, frames), pages in zip(summaries, actual, (1, 3)):
        assert timing["request_hash"] == hashlib.sha256(b'"fixture-history"').hexdigest()[:16]
        assert (timing["client_kind"], timing["pages"], timing["events"]) == ("desktop", pages, len(events(frames)))
        assert timing["bytes"] == sum(len(payload.encode("utf-8")) for payload in raw)
        assert timing["store_calls"] == pages
        assert all(timing[key] >= 0 for key in ("store_queue_ms", "store_execution_ms", "frame_ms"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", [False, True], ids=["one-build", "updated-proof"])
async def test_wire_history_builds_deliveries_again_only_after_commit(daemon, monkeypatch, mutate):
    store = daemon.store
    await open_history(store)
    await append(store)
    tell = await delivery(store)
    newest = await event(store, "second page lookahead")
    await daemon.sessions.refresh()
    statements = []
    await store.read_history(lambda conn: conn.set_trace_callback(statements.append))
    seen = record_calls(monkeypatch)
    async with connected(daemon) as (socket, welcome, _):
        await authenticate(daemon, socket, welcome)
        frames = await dispatch(daemon, socket)
        try:
            first = json.loads((await anext(frames)).payload)
            assert first["events"][0]["daemon_seq"] == newest
            if mutate:
                def remove_delivery(conn):
                    with conn:
                        conn.execute("DELETE FROM v2_tell_deliveries WHERE tell_id='fixture-tell'")
                await store.submit(remove_delivery)
            second = json.loads((await anext(frames)).payload)
            assert second["events"][0]["daemon_seq"] == tell
            assert second["events"][0]["raw"]["input_provenance"]["kind"] == ("unknown" if mutate else "peer")
            assert [json.loads(frame.payload)["type"] async for frame in frames] == ["request_stream_events.ok"]
        finally:
            await frames.aclose()
    builds = [sql for sql in statements if sql.startswith("SELECT tell_id,reply,json_extract")]
    assert len(builds) == (2 if mutate else 1), builds
    assert seen[store.history_read_diag] == [WIRE_PAGE, WIRE_PAGE]


@pytest.mark.asyncio
async def test_wire_history_bootstraps_on_writer_then_follows_archive_rebind(daemon, monkeypatch):
    store = daemon.store
    await open_history(store)

    # Normal ingest also checks proof, which would bootstrap the pair before
    # the wire request. Stage only the durable event for this first-use case.
    def seed_event(conn):
        with conn:
            return conn.execute(
                "INSERT INTO session_event_tail(stream_id,session_created_at,event_key,event_json,recorded_at) "
                "VALUES (?,?,'bootstrap',?,0)",
                (TARGET, BIRTH, json.dumps({"stream_id": TARGET, "kind": "USER", "text": "new file input"})),
            ).lastrowid

    await daemon.sessions.refresh()
    seen = record_calls(monkeypatch)
    async with connected(daemon) as (socket, welcome, _):
        await authenticate(daemon, socket, welcome)
        seq = await store.submit(seed_event)
        path = await archive_path(store)
        assert not path.exists()
        _, initial = await publish(daemon, socket)
        assert [e["daemon_seq"] for e in events(initial)] == [seq]
        assert path.exists()
        await store.submit(lambda conn: None)
        assert seen[store.call_diag].count(WIRE_PAGE) == 1
        assert seen[store.history_read_diag] == []
        assert (await publish(daemon, socket))[1] == initial
        moved = path.with_suffix(".moved")
        shutil.copyfile(path, moved)
        await store.submit(lambda conn: retention.bind_history_archive(conn, moved))
        path.unlink()
        assert (await publish(daemon, socket))[1] == initial
    assert seen[store.history_read_diag] == [WIRE_PAGE, WIRE_PAGE]
    assert seen[store.call_diag].count(WIRE_PAGE) == 1
