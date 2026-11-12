"""Restart no-op work must preserve the original intake path's results."""
import asyncio
from copy import deepcopy
import json

import pytest

from ingest import Ingest, IngestConfig, _close_stream
from routing_integrity import RoutingIntegrity
from sessions import Sessions
from store import Store


STAMP = "2026-10-01T00:00:00Z"


def records(provider, count=6):
    result = []
    if provider == "codex":
        result.append({"type": "session_meta", "payload": {"id": "native"}})
    for i in range(count):
        if provider == "claude":
            result.append({"type": "assistant", "uuid": f"record-{i}",
                           "sessionId": "native", "timestamp": STAMP,
                           "message": {"role": "assistant", "content": [
                               {"type": "text", "text": f"output {i}"}]}})
        else:
            result.append({"type": "response_item", "timestamp": STAMP,
                           "payload": {"type": "message", "id": f"record-{i}",
                                       "role": "assistant", "content": [
                                           {"type": "output_text", "text": f"output {i}"}]}})
    return result


async def fixture(path, provider):
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, local_host="fixture")
    await sessions.open("fixture", "replay", provider=provider, jsonl_path=str(path),
                        pane_pid="4321", claude_session_id="native",
                        session_generation="fixed-generation")
    return store, sessions


def pump_for(store, sessions, frames, *, baseline=False, limit=500):
    async def broadcast(frame):
        frames.append(deepcopy(frame))
    if baseline:
        # The unchanged effect-producing path is the differential reference.
        # This seam is absent before the optimization; the baseline RED still
        # runs through the exact original Ingest implementation.
        async def no_skip(*args, **kwargs):
            return 0
        store.skip_stored_replay = no_skip
    routing = RoutingIntegrity(store, sessions, broadcast=broadcast)
    return Ingest(store, sessions, None, broadcast, local_host="fixture",
                  recent_limit=500, config=IngestConfig(max_events_per_pass=limit),
                  routing_integrity=routing)


async def drain(pump, path):
    for _ in range(100):
        await pump.run_pass()
        state = pump._streams["fixture:replay"]
        assert not state.failures
        if state.offset == path.stat().st_size:
            return
    pytest.fail("finite transcript failed to drain")


def close_pump(pump):
    for state in pump._streams.values():
        _close_stream(state)


def write(path, items):
    path.write_text("".join(json.dumps(record) + "\n" for record in items))


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_stored_replay_avoids_per_event_store_work(tmp_path, provider):
    async def run():
        path = tmp_path / "transcript.jsonl"
        write(path, records(provider, 30))
        store, sessions = await fixture(path, provider)
        frames = []
        first = pump_for(store, sessions, frames)
        replay = None
        try:
            await drain(first, path)
            before = await store.fetch_session_event_tail("fixture:replay", limit=500)
            close_pump(first)
            frames.clear()
            calls = []
            store.call_diag.record = lambda name, *args: calls.append(name)
            replay = pump_for(store, sessions, frames)
            await drain(replay, path)
            after = await store.fetch_session_event_tail("fixture:replay", limit=500)
            assert before == after
            assert not [f for f in frames if f["type"] == "chat.event"]
            assert not [name for name in calls if "prepare_native_input_events" in name]
            assert not [name for name in calls if "bind_send_occurrence" in name]
        finally:
            close_pump(first)
            if replay is not None:
                close_pump(replay)
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("missing", [(), (0,), (3,), (5,), (0, 3, 5)])
def test_mixed_replay_matches_original_rows_messages_and_state(tmp_path, monkeypatch, provider, missing):
    import store as store_module
    import store_usage
    monkeypatch.setattr(store_module.time, "time", lambda: 1790812800.0)
    monkeypatch.setattr(store_module, "iso_now", lambda: STAMP)
    monkeypatch.setattr(store_usage, "iso_now", lambda: STAMP)

    async def run(baseline):
        path = tmp_path / "transcript.jsonl"
        write(path, records(provider))
        store, sessions = await fixture(path, provider)
        frames = []
        seed = pump_for(store, sessions, frames)
        replay = None
        try:
            await drain(seed, path)
            close_pump(seed)
            def remove(conn):
                rows = conn.execute("SELECT event_id FROM session_event_tail "
                                    "WHERE stream_id=? ORDER BY event_id", ("fixture:replay",)).fetchall()
                maximum = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='session_event_tail'").fetchone()[0]
                for i in missing:
                    conn.execute("DELETE FROM session_event_tail WHERE event_id=?", (rows[i][0],))
                conn.commit()
                return maximum
            maximum = await store.submit(remove)
            frames.clear()
            replay = pump_for(store, sessions, frames, baseline=baseline, limit=2)
            await drain(replay, path)
            rows = await store.fetch_session_event_tail("fixture:replay", limit=500)
            new_ids = [r["daemon_seq"] for r in rows if r["daemon_seq"] > maximum]
            assert len(new_ids) == len(missing)
            assert new_ids == sorted(set(new_ids))
            ranks = {r["daemon_seq"]: i for i, r in enumerate(rows)}
            def normalize(value):
                if isinstance(value, dict):
                    return {k: ranks.get(v, v) if k == "daemon_seq" else normalize(v)
                            for k, v in value.items()}
                if isinstance(value, list):
                    return [normalize(v) for v in value]
                return value
            state = replay._streams["fixture:replay"]
            seat = sessions.get("fixture:replay")
            # Fixture construction has independent transport/ownership IDs;
            # compare every intake-owned projection, not unrelated bootstrap IDs.
            fields = ("usage", "context_tokens", "context_level", "last_text", "last_kind",
                      "last_event_at", "last_activity", "routing_integrity", "capacity_retry")
            return normalize(rows), normalize(frames), {k: seat.get(k) for k in fields}, \
                (state.offset, state.session_id, state.provenance_state, state.cursor_norm_state,
                 replay.routing_integrity._claude_tuples)
        finally:
            close_pump(seed)
            if replay is not None:
                close_pump(replay)
            store.stop()
    assert asyncio.run(run(True)) == asyncio.run(run(False))


@pytest.mark.parametrize("stored", [0, 1, 3])
@pytest.mark.parametrize("changed", [False, True])
def test_partial_segments_and_changed_delivery_use_original_split(stored, changed):
    from ingest import _identity_key, append_ingested_event
    from test_native_input_lookup import seat, source, proof, put, TARGET, WIRE_A, WIRE_B

    async def run(baseline):
        store = Store(":memory:")
        store.start()
        frames = []
        event = source("operator\n" + WIRE_A + "\n" + WIRE_B)
        async def broadcast(frame):
            frames.append(frame)
        try:
            await store.submit(lambda c: (seat(c), c.execute(
                "UPDATE sessions SET pane_pid='4321'"), proof(c, WIRE_A, "a"),
                proof(c, WIRE_B, "b"), c.commit()))
            parts = await store.prepare_native_input_events([event])
            assert len(parts) >= 3
            await store.submit(lambda c: ([put(c, part) for part in parts[:stored]], c.commit()))
            if changed:
                # A new overlapping proof makes the original splitter flag
                # ambiguity even though retained cuts existed before the crash.
                await store.submit(lambda c: (proof(c, "operator\n" + WIRE_A, "changed"), c.commit()))
            lifecycle = await store.fetch_open_session_lifecycle(TARGET, pane_pid="4321")
            if not baseline:
                assert await store.skip_stored_replay([event], lifecycle) == 0
            await append_ingested_event(store, broadcast, event, recent_limit=500, lifecycle=lifecycle)
            rows = await store.fetch_session_event_tail(TARGET, limit=500)
            return rows, frames
        finally:
            store.stop()
    assert asyncio.run(run(True)) == asyncio.run(run(False))


def test_plain_stored_input_becomes_ambiguous_after_delivery_change():
    from ingest import append_ingested_event
    from test_native_input_lookup import seat, source, proof, TARGET, WIRE_A
    async def run():
        store = Store(":memory:")
        store.start()
        event = source(WIRE_A + "\n" + WIRE_A)
        async def broadcast(frame):
            pass
        try:
            await store.submit(lambda c: (seat(c), c.execute("UPDATE sessions SET pane_pid='4321'"), c.commit()))
            lifecycle = await store.fetch_open_session_lifecycle(TARGET, pane_pid="4321")
            await append_ingested_event(store, broadcast, event, recent_limit=500, lifecycle=lifecycle)
            await store.submit(lambda c: (proof(c, WIRE_A, "new-proof"), c.commit()))
            assert await store.skip_stored_replay([event], lifecycle) == 0
            prepared = await store.prepare_native_input_events([event])
            assert prepared[0]["raw"]["bug_ref"] == "ambiguous_delivery_span"
        finally:
            store.stop()
    asyncio.run(run())


def test_stored_tell_repairs_promotion_after_crash(monkeypatch):
    import comms as comms_module
    from codex_rollout_norm import normalize_codex_rollout_record
    from ingest import _identity_key, append_ingested_event
    from test_tell_late_proof_controls import setup
    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_POLL_S", 0.001)
    async def run():
        store, sessions, comms, tmux, request = await setup()
        try:
            request.update(from_stream_id="localhost:v2-sender", _auth_context={
                "stream_id": "localhost:v2-sender", "token_verified": True})
            await comms.tell(request)
            event = normalize_codex_rollout_record({"type": "response_item", "timestamp": STAMP,
                "payload": {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": tmux.pastes[0]}]}},
                host="localhost", session_name="v2-late-proof", session_id="native")[0]
            # Simulate the exact crash boundary: row committed, reconciliation
            # did not execute. Restore the real method before replay.
            reconcile = store._reconcile_ingested_tell
            async def crash_gap(event):
                pass
            store._reconcile_ingested_tell = crash_gap
            seq = await store.append_session_event(request["stream_id"], event,
                                                    identity=_identity_key(event), limit=500)
            store._reconcile_ingested_tell = reconcile
            before = await store.get_tell_delivery(request["tell_id"])
            assert before["delivery"]["delivery_status"] == "committed_pending_proof"
            row = await store.fetch_session("localhost", "v2-late-proof")
            await store.submit(lambda c: (c.execute("UPDATE sessions SET pane_pid='4321' "
                "WHERE host='localhost' AND session_name='v2-late-proof'"), c.commit()))
            lifecycle = await store.fetch_open_session_lifecycle(request["stream_id"], pane_pid="4321")
            assert await store.skip_stored_replay([event], lifecycle) == 0
            frames = []
            async def broadcast(frame):
                frames.append(frame)
            await append_ingested_event(store, broadcast, event, recent_limit=500, lifecycle=lifecycle)
            after = await store.get_tell_delivery(request["tell_id"])
            assert after["delivery"]["delivery_status"] == "delivered"
            assert after["delivery"]["proof_event_id"] == seq
            assert frames == []
        finally:
            store.stop()
    asyncio.run(run())


def test_first_replay_boundary_log_and_steady_state(tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="chat_streamd_v2.ingest")
    async def run():
        path = tmp_path / "transcript.jsonl"
        items = records("claude", 2)
        write(path, items)
        boundary = path.stat().st_size
        with path.open("a") as f:
            f.write('{"partial":')
        store, sessions = await fixture(path, "claude")
        frames = []
        pump = pump_for(store, sessions, frames)
        try:
            await pump.run_pass()
            state = pump._streams["fixture:replay"]
            assert state.replay_complete and state.offset == boundary
            assert not pump._replay_backlog()
            calls = []
            store.call_diag.record = lambda name, *args: calls.append(name)
            # Replace the unfinished trailing line with a retained duplicate.
            write(path, items + [items[-1]])
            await drain(pump, path)
            assert any("prepare_native_input_events" in name for name in calls)
            logs = [json.loads(r.message.split("ingest_replay_complete ", 1)[1])
                    for r in caplog.records if r.message.startswith("ingest_replay_complete ")]
            assert len(logs) == 1
            assert logs[0]["boundary"] == logs[0]["offset"] == boundary
            assert logs[0]["disposition"] == "consumed"
        finally:
            close_pump(pump)
            store.stop()
    asyncio.run(run())


def test_stored_send_repairs_unbound_receipt_after_crash(tmp_path):
    from ingest import _identity_key, append_ingested_event
    from test_send_semantics import FakeTmux, _new_comms, _open, HOST, NAME
    async def run():
        comms, store, sessions = _new_comms(FakeTmux(), tmp_path)
        target = f"{HOST}:{NAME}"
        try:
            await _open(comms, sessions)
            await comms.send({"stream_id": target, "text": "receipt crash gap",
                              "request_id": "send-replay-crash"})
            event = {"kind": "USER", "stream_id": target, "provider": "claude",
                     "text": "receipt crash gap", "request_id": "send-replay-crash",
                     "timestamp": STAMP, "raw": {"jsonl_record_uuid": "receipt-gap",
                                                  "jsonl_event_index": 0}}
            reconcile = store._reconcile_ingested_tell
            async def crash_gap(event):
                pass
            store._reconcile_ingested_tell = crash_gap
            seq = await store.append_session_event(target, event, identity=_identity_key(event), limit=500)
            store._reconcile_ingested_tell = reconcile
            await store.submit(lambda c: (c.execute("UPDATE sessions SET pane_pid='4321' "
                "WHERE host=? AND session_name=?", (HOST, NAME)), c.commit()))
            lifecycle = await store.fetch_open_session_lifecycle(target, pane_pid="4321")
            assert await store.skip_stored_replay([event], lifecycle) == 0
            frames = []
            async def broadcast(frame):
                frames.append(frame)
            assert await append_ingested_event(store, broadcast, event, recent_limit=500,
                                               lifecycle=lifecycle) is None
            rows = await store.submit(lambda c: [dict(row) for row in c.execute(
                "SELECT meta_json FROM v2_send_receipts WHERE to_stream_id=? "
                "AND request_id=? ORDER BY rowid", (target, "send-replay-crash"))])
            assert json.loads(rows[-1]["meta_json"])["_input_proof"]["proof_event_id"] == seq
            assert frames == []
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["generation", "rotate", "truncate"])
def test_recreated_or_replaced_transcript_matches_original(tmp_path, monkeypatch, mutation):
    import store as store_module
    monkeypatch.setattr(store_module.time, "time", lambda: 1790812800.0)
    monkeypatch.setattr(store_module, "iso_now", lambda: STAMP)
    async def run(baseline):
        path = tmp_path / "replace.jsonl"
        items = records("claude")
        write(path, items)
        store, sessions = await fixture(path, "claude")
        frames = []
        seed = pump_for(store, sessions, frames)
        replay = None
        try:
            await drain(seed, path)
            close_pump(seed)
            frames.clear()
            replay = pump_for(store, sessions, frames, baseline=baseline)
            replay.config.max_read_bytes = len((json.dumps(items[0]) + "\n").encode())
            await replay.run_pass()
            if mutation == "generation":
                await store.submit(lambda c: (c.execute("UPDATE sessions SET created_at=?",
                    ("2026-10-02T00:00:00Z",)), c.execute(
                    "UPDATE v2_session_generations SET generation='replacement'"), c.commit()))
                sessions._inv["fixture:replay"] = await store.fetch_session("fixture", "replay")
            else:
                replacement = records("claude", 1)
                replacement[0]["uuid"] = "replacement"
                replacement[0]["message"]["content"][0]["text"] = "new"
                if mutation == "rotate":
                    other = tmp_path / "incoming.jsonl"
                    write(other, replacement)
                    other.replace(path)
                else:
                    write(path, replacement)
            replay.config.max_read_bytes = 4 * 1024 * 1024
            await drain(replay, path)
            rows = await store.fetch_session_event_tail("fixture:replay", limit=500)
            ranks = {r["daemon_seq"]: i for i, r in enumerate(rows)}
            def normalized(values):
                return [{**v, "daemon_seq": ranks[v["daemon_seq"]]} for v in values]
            events = [f["event"] for f in frames if f["type"] == "chat.event"]
            return normalized(rows), normalized(events), sessions.get("fixture:replay").get("last_text")
        finally:
            close_pump(seed)
            if replay is not None:
                close_pump(replay)
            store.stop()
    assert asyncio.run(run(True)) == asyncio.run(run(False))


@pytest.mark.parametrize("settlement", ["same-receipt", "other-receipt", "changed-wire", "duplicate-key"])
def test_receipt_noop_requires_same_settled_receipt(settlement):
    from ingest import _identity_key
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            await store.open_session("fixture", "replay", provider="claude", pane_pid="4321")
            lifecycle = await store.fetch_open_session_lifecycle("fixture:replay", pane_pid="4321")
            event = {"stream_id": "fixture:replay", "provider": "claude", "kind": "ASSIST_TEXT",
                     "text": "quotes pending wire", "timestamp": STAMP,
                     "raw": {"jsonl_record_uuid": "quoted", "jsonl_event_index": 0}}
            await store.append_session_event("fixture:replay", event, identity=_identity_key(event), limit=500)
            async def receipt(key, wire, bound):
                proof = {"wire_text": wire, "proof_event_id": bound}
                meta = json.dumps({"_input_proof": proof})
                if settlement == "duplicate-key" and bound is not None:
                    meta = '{"_input_proof":{"wire_text":"pending wire","proof_event_id":7,"proof_event_id":null}}'
                await store.append_send_receipt(to_stream_id="fixture:replay", request_id="send-quote",
                    receipt_id=key, state="landed", wire_text=wire, display_text=wire, attachments=[],
                    delivery="landed", submission_confirmed=True, meta_json=meta)
            await receipt("receipt-a", "pending wire", None)
            assert await store.skip_stored_replay([event], lifecycle) == 0
            await receipt("receipt-b" if settlement == "other-receipt" else "receipt-a",
                          "different wire" if settlement == "changed-wire" else "pending wire", 7)
            assert await store.skip_stored_replay([event], lifecycle) == (1 if settlement == "same-receipt" else 0)
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("unclean", [False, True])
def test_replay_split_reuses_existing_lookup_without_changing_output(unclean):
    from test_native_input_lookup import seat, source, proof, TARGET, WIRE_A, WIRE_B
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            await store.submit(lambda c: (seat(c), proof(c, WIRE_A, "a"), proof(c, WIRE_B, "b"), c.commit()))
            event = source("operator\n" + WIRE_A + "\n" + WIRE_B)
            for step in range(3):
                if step:
                    wire = (WIRE_A + "\x00") if unclean else ("operator\n" + WIRE_A)
                    await store.submit(lambda c: (proof(c, wire, f"change-{step}"), c.commit()))
                expected = await store.prepare_native_input_events([event])
                actual = await store.prepare_native_input_events([event], replay=True)
                assert actual == expected
        finally:
            store.stop()
    asyncio.run(run())
