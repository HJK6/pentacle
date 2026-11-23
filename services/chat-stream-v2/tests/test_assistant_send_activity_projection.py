"""Synthetic direct-send and retained activity checks through the real Store worker."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import wraps
import hashlib
import json
import platform
import sqlite3
import sys
from time import monotonic

import pytest

import assistant_activity
from assistant_composite import AssistantComposite, AssistantCompositeConfig
from retention import ROUTE_TABLE
from test_assistant_duplicate_reply_sources import ASSISTANT
from test_send_semantics import FakeTmux, HOST, NAME, _new_comms, _open
from test_voice_context import main_dispatch_subject


TARGET = f"{HOST}:{NAME}"
FULL_READER = "assistant_composite_activity"
SUMMARY_READER = "assistant_composite_activity_summary"
PROGRESS_INTERVAL = 100
STAGE_ORDER = ("admit", "claim", "intent", "recipient", "proof", "landed")
REFRESH_ORDER = ("startup", "admit", "claim", "intent", "landed")
STAMP = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _stamp(seconds):
    return (STAMP + timedelta(seconds=seconds)).isoformat()


def _seed(conn, width, *, stream=ASSISTANT, offset=0):
    """The newest controls and all visible filler stay fixed as width grows."""
    for i in range(offset, offset + width):
        identity = f"seed-{i:05}"
        stamp = _stamp(-i)
        dispatch = f"seed-dispatch-{i:05}"
        body = "Synthetic history " * 16
        conn.execute(
            f"INSERT INTO {ROUTE_TABLE} "
            "(route_id, stream_id, input_identity, payload_digest, input_request_id, body, "
            "routing_state, delivery_state, created_at, updated_at, dispatch_id, route_json) "
            "VALUES (?, ?, ?, 'digest', ?, ?, 'resolved', 'landed', ?, ?, ?, '{}')",
            (f"{stream}:{identity}", stream, identity, identity, body, stamp, stamp, f"{stream}:{dispatch}"),
        )
        if i == 0:
            continue  # Pending independently of padding.
        kind = "question" if i == 1 else "result"
        conn.execute(
            "INSERT INTO v2_assistant_composite_publications "
            "(publication_key, stream_id, payload_digest, canonical_payload_json, dispatch_id, "
            "reply_to_message_id, publish_kind, event_id, created_at) "
            "VALUES (?, ?, 'digest', ?, ?, ?, ?, ?, ?)",
            (f"{stream}:pub-{i}", stream, json.dumps({"message": body}), f"{stream}:{dispatch}", identity, kind, -i, stamp),
        )
        if i == 1:
            continue  # Waiting for the operator independently of padding.
        lane = f"{stream}:lane-{i}"
        conn.execute(
            "INSERT INTO v2_assistant_composite_operations "
            "(operation_id, stream_id, dispatch_id, lane_id, reply_to_message_id, operation, "
            "payload_digest, payload_json, created_at) VALUES (?, ?, ?, ?, ?, 'lane.admit', 'digest', ?, ?)",
            (f"{stream}:op-{i}", stream, f"{stream}:{dispatch}", lane, "different-reply-column",
             json.dumps({"request_message_id": identity, "summary": body}), stamp),
        )
        conn.execute(
            "INSERT INTO v2_assistant_composite_lanes (lane_id, stream_id, phase, created_at, updated_at) "
            "VALUES (?, ?, 'closed', ?, ?)", (lane, stream, stamp, stamp),
        )
    conn.commit()


def _statement_family(sql):
    upper = sql.upper()
    if upper.startswith(("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE")):
        return "transaction"
    if upper.startswith(("ATTACH", "DETACH", "PRAGMA")) or "v2_send_receipt_sequence" in sql or "v2_receipt_route_archive" in sql:
        return "retention_metadata"
    if ROUTE_TABLE in sql:
        return "route"
    for table in ("publications", "operations", "lanes"):
        if f"v2_assistant_composite_{table}" in sql:
            return table
    return "other"


class StoreWork:
    """Observe actual callbacks without changing Store queue or cancellation semantics."""

    def __init__(self, monkeypatch, store, *, trace=True):
        self.calls = []
        self.current = None
        original_submit = store.submit
        original_project = assistant_activity.project_input

        def project(*args, **kwargs):
            if self.current is not None:
                self.current["hydrated"] += 1
            return original_project(*args, **kwargs)

        async def submit(fn, **options):
            call = {"callback": fn.__qualname__, "queued": monotonic(),
                    "hydrated": 0, "vm_ticks": 0, "statements": {}, "individual": {}}
            self.calls.append(call)
            statements = Counter()
            individual = Counter()

            def query(sql):
                family = _statement_family(sql)
                statements[family] += 1
                if family in ("publications", "operations", "lanes") and "SELECT" in sql.upper():
                    single = {
                        "publications": "AND reply_to_message_id=",
                        "operations": "AND json_extract(payload_json,'$.request_message_id')=",
                        "lanes": "AND lane_id=",
                    }
                    if single[family] in sql:
                        individual[family] += 1

            def progress():
                call["vm_ticks"] += 1
                return 0

            @wraps(fn)
            def observe(conn):
                if trace:
                    conn.set_trace_callback(query)
                    conn.set_progress_handler(progress, PROGRESS_INTERVAL)
                self.current = call
                call["started"] = monotonic()
                try:
                    result = fn(conn)
                    call["returned"] = type(result).__name__
                    if FULL_READER in call["callback"] or SUMMARY_READER in call["callback"]:
                        if isinstance(result, dict):
                            call["returned_rows"] = len(result.get("inputs", result))
                    return result
                except BaseException as exc:
                    call["returned"] = type(exc).__name__
                    raise
                finally:
                    call["finished"] = monotonic()
                    self.current = None
                    if trace:
                        conn.set_progress_handler(None, 0)
                        conn.set_trace_callback(None)
                        call["statements"] = dict(statements)
                        call["individual"] = dict(individual)

            try:
                return await original_submit(observe, **options)
            finally:
                call["delivered"] = monotonic()

        monkeypatch.setattr(store, "submit", submit)
        monkeypatch.setattr(assistant_activity, "project_input", project)

    @staticmethod
    def projection(call):
        return any(name in call["callback"] for name in (FULL_READER, SUMMARY_READER))


def _receipt(tmp_path, record_property, name, data):
    data = {"python": sys.version, "os": platform.platform(), "sqlite": sqlite3.sqlite_version,
            "progress_interval": PROGRESS_INTERVAL, **data}
    body = json.dumps(data, indent=2, sort_keys=False).encode()
    path = tmp_path / f"{name}.json"
    path.write_bytes(body)
    record_property(name, f"{path.name} sha256={hashlib.sha256(body).hexdigest()}")


async def _direct_pair(tmp_path, monkeypatch, *, selected, mode="sound", trace=True):
    """Share one no-publication route between the stage and work checks."""
    tmux = FakeTmux()
    comms, store, sessions = _new_comms(tmux, tmp_path)
    composite = None
    try:
        await _open(comms, sessions)
        root = await store.fetch_session(HOST, NAME)
        generation = root["session_generation"]
        config = AssistantCompositeConfig.from_env({
            "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
            "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": ASSISTANT,
            "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": TARGET,
            "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": generation,
        })
        main_dispatch = main_dispatch_subject(comms, store)
        await store.submit(lambda conn: _seed(conn, selected - 2))
        engine = await store.submit(lambda conn: conn.execute("SELECT sqlite_source_id()").fetchone()[0])
        work = StoreWork(monkeypatch, store, trace=trace)
        stages = []
        refreshes = []
        proofs = []
        snapshots = []
        joins = []

        def stage(kind, row):
            stages.append({"stage": kind, "time": monotonic(), **{key: row.get(key) for key in (
                "input_identity", "route_id", "dispatch_id", "route_target_generation", "event_id", "receipt_id")}})

        async def recipient(route):
            stage("recipient", route)
            result = await main_dispatch(route)
            stage("proof", route)
            proofs.append(result)
            return result

        composite = AssistantComposite(store, config=config, dispatch=recipient)
        real_refresh = composite.refresh_activity
        full_reader = store.assistant_composite_activity

        async def refresh(**kwargs):
            kind = stages[-1]["stage"] if stages else "startup"
            start = len(work.calls)
            if mode == "restore_full":
                summary_reader = getattr(store, SUMMARY_READER)
                async def old_projection(*, stream_id):
                    return assistant_activity.summarize_activity(await full_reader(stream_id=stream_id))
                with monkeypatch.context() as patch:
                    patch.setattr(store, SUMMARY_READER, old_projection)
                    await real_refresh(**kwargs)
                assert summary_reader == getattr(store, SUMMARY_READER)
            else:
                await real_refresh(**kwargs)
            refreshes.append({"stage": kind, "begin": start, "end": len(work.calls), "done": monotonic()})
            snapshots.append(composite.activity_snapshot())

        def observe_write(method, kind):
            original = getattr(store, method)

            async def observed(*args, **kwargs):
                result = await original(*args, **kwargs)
                if result is not None:
                    label = kind if kind != "update" else kwargs["delivery_state"]
                    if kind == "update" and label == "landed":
                        assert kwargs["expected_route_target"] == TARGET
                        assert kwargs["expected_route_target_generation"] == generation
                        assert kwargs["expected_dispatch_id"] == result["dispatch_id"]
                    stage(label, result)
                    if label == "landed" and mode == "displace_full":
                        # Schedule a real extra full callback to the same worker.
                        # The pre-recipient window alone accepts this fault.
                        displaced.append(asyncio.create_task(full_reader(stream_id=ASSISTANT)))
                return result

            monkeypatch.setattr(store, method, observed)

        displaced = []
        monkeypatch.setattr(composite, "refresh_activity", refresh)
        observe_write("admit_assistant_composite_input", "admit")
        observe_write("claim_assistant_composite_route", "claim")
        observe_write("update_assistant_composite_route", "update")
        await composite.ensure_projection()
        for identity, body in (("in-a", "pause wmi runs"), ("in-b", "just pause for now")):
            result = await composite.accept_input(
                {"message": body, "msg_id": identity, "request_id": f"rpc-{identity}"},
                operator_principal="operator:fixture",
            )
            worker = composite._worker
            assert worker is not None
            await asyncio.wait_for(asyncio.shield(worker), 10)
            assert not any(not task.done() for task in composite._dispatch_tasks)
            proof = proofs[-1]
            assert proof["submission_confirmed"] is True
            assert proof["delivery"] == "landed"
            assert result["message_id"] == identity and result["event_id"] and result["receipt_id"]
            row = await store.get_assistant_composite_route(stream_id=ASSISTANT, input_identity=identity)
            recipient_receipt = await store.get_send_receipt(TARGET, row["dispatch_id"])
            assert recipient_receipt["submission_confirmed"] is True
            assert recipient_receipt["state"] == "landed"
            assert recipient_receipt["receipt_id"] == proof["receipt_id"]
            assert row["delivery_state"] == "landed"
            assert row["event_id"] == result["event_id"]
            assert row["route_target_generation"] == generation
            joins.append({"accepted": result, "landed": row, "recipient_receipt": recipient_receipt})

        if displaced:
            await asyncio.wait_for(asyncio.gather(*displaced), 10)
        await store.submit(lambda conn: conn.execute("SELECT 1").fetchone()[0])
        end = len(work.calls)
        assert all("finished" in call for call in work.calls[:end])
        assert [entry["stage"] for entry in stages] == list(STAGE_ORDER) * 2
        assert [entry["stage"] for entry in refreshes] == list(REFRESH_ORDER + REFRESH_ORDER[1:])
        assert len(tmux.pastes) == len(proofs) == 2
        assert "<assistant-dispatch-batch-json>" not in tmux.pastes[0]
        assert "<assistant-dispatch-batch-json>" in tmux.pastes[1] and "in-a" in tmux.pastes[1]
        assert joins[1]["landed"]["batched_into_dispatch_id"] == joins[0]["landed"]["dispatch_id"]
        # Compare full values outside the measured worker drain. Do not share the
        # optimized implementation with the retained reader used as oracle.
        legacy = await full_reader(stream_id=ASSISTANT)
        assert len(legacy) == selected
        assert snapshots[-1] == assistant_activity.summarize_activity(legacy)
        assert list(snapshots[-1]["inputs"]) == list(assistant_activity.summarize_activity(legacy)["inputs"])
        assert snapshots[-1]["pending_count"] == 3
        assert snapshots[-1]["waiting_for_operator_count"] == 1
        assert snapshots[-1]["oldest_pending_at"] == _stamp(0)
        assert snapshots[-1]["has_more"] is True
        assert await full_reader(stream_id=ASSISTANT, input_ids=["seed-00000", "not-found", "seed-00000"]) == {
            "seed-00000": legacy["seed-00000"],
        }
        return {"selected": selected, "stages": stages, "refreshes": refreshes, "calls": work.calls[:end],
                "joins": joins, "snapshots": snapshots, "proofs": proofs, "sqlite_source_id": engine, "mode": mode}
    finally:
        if composite is not None:
            await asyncio.wait_for(composite.stop(), 10)
        store.stop()


def _refresh_work_misses(run):
    misses = []
    for refresh in run["refreshes"]:
        window = run["calls"][refresh["begin"]:refresh["end"]]
        calls = [call for call in window if StoreWork.projection(call)]
        assert len(calls) == 1, refresh
        call = calls[0]
        queries = sum(count for key, count in call["statements"].items()
                      if key not in {"retention_metadata", "transaction"})
        if call["individual"] or call["hydrated"] > 100 or queries > 12:
            misses.append({"stage": refresh["stage"], "queries": queries, "hydrated": call["hydrated"],
                           "individual": call["individual"], "vm_ticks": call["vm_ticks"]})
    return misses


def _work_contract(run, *, drain=True):
    calls = run["calls"]
    misses = _refresh_work_misses(run)
    send_misses = [miss for miss in misses if miss["stage"] != "startup"]
    assert not send_misses, ("full activity work in awaited send refresh", run["selected"], send_misses)
    assert not misses, ("full activity work in startup refresh", run["selected"], misses)
    if drain:
        assert not any(FULL_READER in call["callback"] for call in calls), (
            "full activity work through worker drain", run["selected"],
        )
        assert sum(StoreWork.projection(call) for call in calls) == len(run["refreshes"])


def test_direct_send_refresh_stage_work(tmp_path, monkeypatch, record_property):
    run = asyncio.run(_direct_pair(tmp_path, monkeypatch, selected=128))
    _receipt(tmp_path, record_property, "direct-stages", run)
    _work_contract(run)


def _normalized_output(snapshot):
    # The two real route IDs/timestamps are checked against their receipts in
    # each arm. Fixed synthetic history is compared byte-for-byte across sizes.
    return {**snapshot, "inputs": {key: value for key, value in snapshot["inputs"].items()
                                  if key not in {"in-a", "in-b"}}}


def test_fixed_output_work_and_drain(tmp_path, monkeypatch, record_property):
    def drive(width, mode="sound", *, trace=True):
        with monkeypatch.context() as patch:
            return asyncio.run(_direct_pair(tmp_path, patch, selected=width, mode=mode, trace=trace))

    small, large = drive(128), drive(1024)
    _receipt(tmp_path, record_property, "fixed-output", {"arms": [small, large]})
    assert [_normalized_output(slot) for slot in small["snapshots"]] == [
        _normalized_output(slot) for slot in large["snapshots"]
    ]
    misses = {run["selected"]: _refresh_work_misses(run) for run in (small, large)}
    assert not any(miss["stage"] != "startup" for values in misses.values() for miss in values), (
        "full activity work in awaited send refresh", misses,
    )
    _work_contract(small)
    _work_contract(large)
    restored = drive(1024, "restore_full")
    displaced = drive(1024, "displace_full")
    _receipt(tmp_path, record_property, "work-controls", {"arms": [restored, displaced]})
    with pytest.raises(AssertionError, match="full activity work in awaited send refresh"):
        _work_contract(restored)
    _work_contract(displaced, drain=False)
    with pytest.raises(AssertionError, match="full activity work through worker drain"):
        _work_contract(displaced)
    def projection_ticks(run):
        return sum(call["vm_ticks"] for call in run["calls"] if StoreWork.projection(call))
    assert projection_ticks(large) * 2 <= projection_ticks(restored)


async def _check_projection(store, stream=ASSISTANT, composite=None):
    full = await store.assistant_composite_activity(stream_id=stream)
    reference = assistant_activity.summarize_activity(full)
    if composite is not None:
        await composite.refresh_activity()
        actual = composite.activity_snapshot()
    else:
        reader = getattr(store, SUMMARY_READER, None)
        actual = await reader(stream_id=stream) if reader else reference
    assert actual == reference
    assert list(actual["inputs"]) == list(reference["inputs"])
    return full, actual


def test_authenticated_send_fanout_and_proof(tmp_path, record_property):
    import websockets
    from _shared import operator_auth
    from server import Server
    from test_voice_context import CHAT, CONTEXT, FIXTURE, read_recipient_context

    async def run():
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        server = composite = registry = None
        frames, pushes, sent, publications = [], [], [], []
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            await _open(comms, sessions)
            root = await store.fetch_session(HOST, NAME)
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": CHAT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": TARGET,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": root["session_generation"],
            })
            main_dispatch = main_dispatch_subject(comms, store)

            async def broadcast(frame):
                frames.append(frame)
                await server.broadcast(frame)

            async def notify(*args, **kwargs):
                pushes.append((args, kwargs))

            async def recipient(route):
                entered.set()
                await release.wait()
                proof = await main_dispatch(route)
                assert proof["submission_confirmed"] is True
                assert read_recipient_context(tmux.pastes[-1]) == CONTEXT
                publication = {
                    "request_id": "publish:" + route["dispatch_id"], "composite_stream_id": CHAT,
                    "dispatch_id": route["dispatch_id"], "reply_to_message_id": route["input_identity"],
                    "publish_kind": "prose", "response_state": "final", "message": "Synthetic final",
                }
                publications.append(await composite.publish(publication, actor_stream_id=TARGET))
                publications.append(await composite.publish(publication, actor_stream_id=TARGET))
                return proof

            server = Server(host="127.0.0.1", port=0, store=store, sessions=sessions, comms=comms, local_host=HOST)
            composite = AssistantComposite(store, config=config, dispatch=recipient, broadcast=broadcast)
            composite.reply_push = notify
            server.assistant_composite = composite
            await composite.ensure_projection()
            await sessions.refresh()
            auth_dir = tmp_path / "operator-auth"
            auth_dir.mkdir(mode=0o700)
            registry = operator_auth.OperatorCredentialRegistry(auth_dir / "credentials.json")
            credential_id, envelope = registry.issue("pentacle-mobile")
            proof_key = operator_auth.decode_envelope(envelope)["proof_key"]
            server.operator_credential_registry = registry
            port = await server.bind()

            async def read(socket, predicate):
                async with asyncio.timeout(5):
                    while True:
                        frame = json.loads(await socket.recv())
                        if predicate(frame):
                            return frame

            async def hello(socket):
                greeting = json.loads(await socket.recv())
                nonce = greeting["auth"]["operator"]["nonce"]
                await socket.send(json.dumps({
                    "type": "hello", "client": "pentacle-mobile",
                    "capabilities": {"assistant_composite_v1": True},
                    "auth_v2": {"scheme": operator_auth.AUTH_SCHEME, "credential_id": credential_id,
                                "proof": operator_auth.make_proof(proof_key, nonce, credential_id, "pentacle-mobile")},
                }))
                return await read(socket, lambda frame: frame["type"] == "snapshot")

            async def write(socket, request_id, text="Synthetic recording"):
                sent.append({"request_id": request_id, "socket_write": monotonic()})
                await socket.send(json.dumps({"type": "send", "stream_id": CHAT, "text": text,
                                               "msg_id": "take-1", "request_id": request_id, "meta": FIXTURE}))

            url = f"ws://127.0.0.1:{port}"
            async with websockets.connect(url) as socket:
                await hello(socket)
                await write(socket, "rpc-1")
                observed = []

                def admitted(frame):
                    if frame["type"] == "chat.event":
                        observed.append({"received": monotonic(), "frame": frame})
                    return frame.get("request_id") == "rpc-1"

                result = await read(socket, admitted)
                received_ack = monotonic()
                assert result["type"] == "send.result" and result["submission_confirmed"]
                canonical = result["assistant_composite"]
                echo, = [row["frame"]["event"] for row in observed if row["frame"]["event"]["kind"] == "USER"]
                assert echo["daemon_seq"] == canonical["event_id"]
                assert echo["raw"]["assistant_activity"]["response_state"] == "queued"
                assert echo["daemon_seq"] > 0 and canonical["receipt_id"] == result["receipt_id"]
                await asyncio.wait_for(entered.wait(), 5)
                preimage = await store.get_assistant_composite_route(stream_id=CHAT, input_identity="take-1")
                assert preimage["delivery_state"] == "intent" and tmux.pastes == []
                release.set()
                await asyncio.wait_for(asyncio.shield(composite._worker), 5)
                after = await store.get_assistant_composite_route(stream_id=CHAT, input_identity="take-1")
                recipient_receipt = await store.get_send_receipt(TARGET, after["dispatch_id"])
                assert after["delivery_state"] == "landed"
                assert after["route_target_generation"] == root["session_generation"]
                assert recipient_receipt["state"] == "landed" and recipient_receipt["submission_confirmed"] is True
                result_event = await read(socket, lambda frame: frame["type"] == "chat.event"
                                          and frame["event"]["kind"] == "ASSIST_TEXT")
                assert result_event["event"]["daemon_seq"] == publications[0]["event_id"]
                assert [publication["duplicate"] for publication in publications] == [False, True]
                await write(socket, "rpc-2")
                retry = await read(socket, lambda frame: frame.get("request_id") == "rpc-2")
                assert retry["assistant_composite"]["duplicate"]
                await write(socket, "rpc-3", "Changed intent")
                conflict = await read(socket, lambda frame: frame.get("request_id") == "rpc-3")
                assert "idempotency_conflict" in json.dumps(conflict)
            async with websockets.connect(url) as socket:
                snapshot = await hello(socket)
                assert any(row.get("assistant_activity", {}).get("inputs", {}).get("take-1", {})
                           .get("response_state") == "answered" for row in snapshot["sessions"])
            assert len(tmux.pastes) == len(pushes) == 1
            assert [frame["type"] for frame in frames] == [
                "working.state", "chat.event", "working.state", "working.state",
                "chat.event", "working.state", "working.state",
            ]
            assert [frame["assistant_activity"]["inputs"]["take-1"]["response_state"] for frame in frames
                    if frame["type"] == "working.state"] == [
                "queued", "routing", "awaiting_reply", "answered", "answered",
            ]
            await _check_projection(store, CHAT, composite)
            _receipt(tmp_path, record_property, "authenticated", {
                "socket": sent, "received_ack": received_ack, "result": result, "echo_received": observed,
                "frames": frames, "publications": publications, "landed": after, "recipient": recipient_receipt,
            })
        finally:
            release.set()
            if composite is not None:
                await asyncio.wait_for(composite.stop(), 5)
            if server is not None:
                await asyncio.wait_for(server.close(), 5)
            if registry is not None:
                registry.revoke(credential_id)
            store.stop()

    asyncio.run(run())


async def _copy_synthetic_routes(store, archive, selection, *, delete):
    """Place invented historical rows whose unsettled form cannot use the mover."""
    rows = await store.submit(lambda conn: [tuple(row) for row in conn.execute(
        f"SELECT * FROM {ROUTE_TABLE} WHERE {selection}")])
    with sqlite3.connect(archive) as db:
        db.executemany(f"INSERT INTO {ROUTE_TABLE} VALUES ({','.join('?' for _ in rows[0])})", rows)
    if delete:
        def remove(conn):
            conn.execute(f"DELETE FROM {ROUTE_TABLE} WHERE {selection}")
            conn.commit()
        await store.submit(remove)
    return len(rows)


def _edge_rows(conn):
    def change(identity, **fields):
        conn.execute(f"UPDATE {ROUTE_TABLE} SET " + ",".join(f"{key}=?" for key in fields)
                     + " WHERE stream_id=? AND input_identity=?", (*fields.values(), ASSISTANT, identity))

    def remove_result(i):
        conn.execute("DELETE FROM v2_assistant_composite_publications WHERE publication_key=?",
                     (f"{ASSISTANT}:pub-{i}",))

    def publish(i, number, kind, marker):
        conn.execute(
            "INSERT INTO v2_assistant_composite_publications "
            "(publication_key, stream_id, payload_digest, canonical_payload_json, dispatch_id, "
            "reply_to_message_id, publish_kind, event_id, created_at) "
            "VALUES (?, ?, 'digest', ?, '', ?, ?, ?, ?)",
            (f"edge-publication-{number}", ASSISTANT, json.dumps({"response_state": marker}),
             f"seed-{i:05}", kind, number, _stamp(-i)),
        )

    # Tie runs across both cutoffs. The original query's order is the oracle
    # per layout because archive and hot rows can legitimately interleave differently.
    for i in (98, 99, 100, 101):
        change(f"seed-{i:05}", created_at=_stamp(-100))
    for i in (1998, 1999, 2000, 2001):
        change(f"seed-{i:05}", created_at=_stamp(-2000))
    for i in (2040, 2041, 2042, 2043, 2044, 2045, 2046):
        remove_result(i)
    change("seed-02040", routing_state="queued", delivery_state=None)
    publish(2041, 10, "status", "acknowledged")
    change("seed-02042", routing_state="routing_failed")
    change("seed-02043", delivery_state="failed")
    publish(2043, 11, "status", "acknowledged")
    change("seed-02044", route_json=json.dumps({"desk_dispatch": True}))
    change("seed-02045", routing_state="deferred")
    change("seed-02046", delivery_state="uncertain")
    # Conflicting response evidence and embedded/admitted lanes on a visible input.
    change("seed-00002", delivery_state="failed", error_code="delivery_failure",
           route_json=json.dumps({"lane_id": f"{ASSISTANT}:lane-3", "timings": {"routed_at": _stamp(1)}}))
    publish(2, 1, "question", None)
    publish(2, 2, "status", "acknowledged")
    publish(2, 3, "prose", "final")
    conn.commit()


async def _retained_case(path, layout):
    from store import Store

    path.parent.mkdir()
    store = Store(":memory:" if layout == "hot" else str(path))
    store.start()
    cells = []
    try:
        await _check_projection(store)
        await store.submit(lambda conn: _seed(conn, 2060))
        await store.submit(_edge_rows)
        archive = await store.submit(lambda conn: conn.history_archive_path)
        if layout == "archive":
            cold_count = await _copy_synthetic_routes(store, archive, "1", delete=True)
        elif layout == "mixed":
            cold_count = await _copy_synthetic_routes(
                store, archive, "CAST(SUBSTR(input_identity, 6) AS INTEGER) % 2 = 0", delete=True,
            )
        else:
            cold_count = 0
        hot_count = await store.submit(lambda conn: conn.execute(f"SELECT COUNT(*) FROM {ROUTE_TABLE}").fetchone()[0])
        assert (hot_count, cold_count) == {"hot": (2060, 0), "archive": (0, 2060), "mixed": (1030, 1030)}[layout]
        full, live = await _check_projection(store)
        assert len(full) == 2006
        assert live["pending_count"] == 4 and live["waiting_for_operator_count"] == 1
        assert live["oldest_pending_at"] == _stamp(-2044)
        assert live["has_more"] and len(live["inputs"]) == 100
        assert full["seed-02043"]["response_state"] == "failed"  # Older ack branch is independent.
        assert full["seed-02045"]["response_state"] == "waiting_for_dependency"
        assert full["seed-02046"]["response_state"] == "uncertain"
        assert "seed-02042" not in full and "seed-02049" not in full
        assert live["inputs"]["seed-00002"]["lane_ids"] == [f"{ASSISTANT}:lane-2", f"{ASSISTANT}:lane-3"]
        assert live["inputs"]["seed-00002"]["response_state"] == "answered"
        assert live["inputs"]["seed-00002"]["error_code"] is None
        assert live["inputs"]["seed-00002"]["publication_ids"][-3:] == [
            "edge-publication-1", "edge-publication-2", "edge-publication-3",
        ]
        cells.append({"layout": layout, "hot": hot_count, "archive": cold_count,
                      "full": list(full), "live": live})
        keyed = store.assistant_composite_activity
        requested = [f"seed-{i:05}" for i in range(1998)] + ["seed-00001", "absent", "seed-02049", "seed-02050"]
        detail = await keyed(stream_id=ASSISTANT, input_ids=requested)
        assert len(detail) == 1999 and "seed-02049" in detail and "seed-02050" not in detail
        assert "absent" not in detail and await keyed(stream_id=ASSISTANT, input_ids=[]) == {}
        # Caller order is applied to the event list, not to the Store result map.
        composite = AssistantComposite(store, config=AssistantCompositeConfig(enabled=True, stream_id=ASSISTANT))
        events = [{"kind": "USER", "message_id": key, "raw": {"original": index}}
                  for index, key in enumerate(requested)]
        enriched = await composite.enrich_events(events)
        assert [event["message_id"] for event in enriched] == requested
        assert [event["raw"]["original"] for event in enriched] == list(range(len(events)))
        assert enriched[-2]["raw"]["assistant_activity"] == detail["seed-02049"]
        assert enriched[-1] == events[-1]

        assert await store.strict_publication_state(ASSISTANT) == {"enabled": False, "source": "unbound"}
        await store.configure_assistant_mirror(composite_stream_id=ASSISTANT, source_stream_id=TARGET,
                                               source_generation="fixture-1", enabled_default=False,
                                               strict_publication_default=True, name="fixture-strict")
        await store.configure_assistant_mirror(composite_stream_id="other:assistant", source_stream_id=TARGET,
                                               source_generation="fixture-2", enabled_default=False,
                                               strict_publication_default=False, name="fixture-other")
        key = "assistant.strict_publication.fixture-strict.enabled"
        for value, expected, source in [
            (None, True, "env"), ("off", False, "kv"), ("on", True, "kv"),
            ("invalid", False, "kv_invalid"), (None, True, "env"),
        ]:
            def policy(conn):
                if value is None:
                    conn.execute("DELETE FROM kv WHERE k=?", (key,))
                else:
                    conn.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (key, value))
                conn.commit()
            await store.submit(policy)
            assert await store.strict_publication_state(ASSISTANT) == {"enabled": expected, "source": source}
            assert await store.strict_publication_state("other:assistant") == {"enabled": False, "source": "env"}
            _, summary = await _check_projection(store)
            assert summary["pending_count"] == (3 if expected else 4)
            assert summary["oldest_pending_at"] == _stamp(-2041 if expected else -2044)
        return cells
    finally:
        store.stop()


def test_retained_summary_matches_reference(tmp_path, record_property):
    from retention import RetentionError
    from store import Store
    from test_receipt_route_retention import archive_path, hot_count, move

    async def run():
        cells = []
        store = Store(":memory:")
        store.start()
        try:
            previous = 0
            for width in (0, 1, 100, 101, 2000):
                await store.submit(lambda conn: _seed(conn, width - previous, offset=previous))
                full, summary = await _check_projection(store)
                assert len(full) == width and len(summary["inputs"]) == min(100, width)
                assert summary["has_more"] is (width > 100)
                previous = width
        finally:
            store.stop()
        for layout in ("hot", "archive", "mixed"):
            cells.extend(await _retained_case(tmp_path / layout / "hot.db", layout))

        path = tmp_path / "mover.db"
        store = Store(str(path))
        store.start()
        try:
            await store.ensure_assistant_composite_projection(stream_id=ASSISTANT)
            values = dict(stream_id=ASSISTANT, input_identity="moves", input_request_id="move-request", body="old",
                          attachments=[], reply_to_message_id=None, reply_to_question_id=None, actor_stream_id=None)
            first = await store.admit_assistant_composite_input(**values)
            def settle(conn):
                conn.execute(f"UPDATE {ROUTE_TABLE} SET routing_state='resolved',delivery_state='landed',"
                             "created_at=?,updated_at=?", (_stamp(0), _stamp(0)))
                conn.commit()
            await store.submit(settle)
            _, before = await _check_projection(store)
            moved = await move(store, ROUTE_TABLE)
            assert moved["moved"] == 1 and await hot_count(store, ROUTE_TABLE) == 0
            _, after = await _check_projection(store)
            assert after == before
            retry = await store.admit_assistant_composite_input(**dict(values, input_request_id="rotated"))
            assert retry["duplicate"] and retry["route_id"] == first["route_id"]
        finally:
            store.stop()
        store = Store(str(path))
        store.start()
        try:
            _, reopened = await _check_projection(store)
            assert reopened == before
            archive = await archive_path(store)
            await store.submit(lambda conn: _seed(conn, 1))
            assert await _copy_synthetic_routes(store, archive, "input_identity='seed-00000'", delete=False) == 1
            full, _ = await _check_projection(store)
            assert list(full).count("seed-00000") == 1
            with sqlite3.connect(archive) as db:
                db.execute(f"UPDATE {ROUTE_TABLE} SET body='different' WHERE input_identity='seed-00000'")
            for reader in (store.assistant_composite_activity, getattr(store, SUMMARY_READER, None)):
                if reader is not None:
                    with pytest.raises((RetentionError, sqlite3.OperationalError)):
                        await reader(stream_id=ASSISTANT)
            with sqlite3.connect(archive) as db:
                db.execute(f"DELETE FROM {ROUTE_TABLE} WHERE input_identity='seed-00000'")
            await _check_projection(store)
            archive.rename(archive.with_suffix(".unavailable"))
            for reader in (store.assistant_composite_activity, getattr(store, SUMMARY_READER, None)):
                if reader is not None:
                    with pytest.raises(RetentionError, match="path unavailable"):
                        await reader(stream_id=ASSISTANT)
        finally:
            store.stop()
        _receipt(tmp_path, record_property, "retained", {"cells": cells})

    asyncio.run(run())


def test_inflight_refresh_and_restart(tmp_path, monkeypatch, record_property):
    from comms import Comms
    from sessions import Sessions
    from spawnctl import SpawnCtl
    from store import Store

    async def run(barrier):
        path = tmp_path / barrier / "hot.db"
        path.parent.mkdir()
        tmux = FakeTmux()
        store = Store(str(path))
        store.start()
        frames = []
        reached = asyncio.Event()
        hold = asyncio.Event()
        proof = None

        async def broadcast(frame):
            frames.append(frame)

        def dispatcher(store):
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=tmux))
            return comms, sessions, main_dispatch_subject(comms, store)

        comms, sessions, send = dispatcher(store)
        await _open(comms, sessions)
        root = await store.fetch_session(HOST, NAME)
        config = AssistantCompositeConfig.from_env({
            "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
            "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": ASSISTANT,
            "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": TARGET,
            "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": root["session_generation"],
        })

        async def recipient(route):
            nonlocal proof
            if barrier == "post-paste":
                proof = await send(route)
                assert proof["submission_confirmed"] is True
            reached.set()
            await hold.wait()
            raise AssertionError("Owned worker must be stopped while held")

        first = AssistantComposite(store, config=config, dispatch=recipient, broadcast=broadcast)
        try:
            await first.ensure_projection()
            with monkeypatch.context() as patch:
                if barrier == "pre-claim":
                    async def before_claim(**kwargs):
                        reached.set()
                        await hold.wait()
                        raise AssertionError("Owned worker must be stopped while held")
                    patch.setattr(store, "claim_assistant_composite_route", before_claim)
                await first.accept_input({"message": "First", "msg_id": "first", "request_id": "rpc-a"},
                                         operator_principal="operator:fixture")
                await asyncio.wait_for(reached.wait(), 5)
                preimage = await store.get_assistant_composite_route(stream_id=ASSISTANT, input_identity="first")
                assert preimage["delivery_state"] == (None if barrier == "pre-claim" else "intent")
                if barrier != "pre-claim":
                    assert preimage["route_target_generation"] == root["session_generation"]
                    ack = await first.publish({
                        "request_id": "pub-ack", "composite_stream_id": ASSISTANT,
                        "reply_to_message_id": "first", "dispatch_id": preimage["dispatch_id"],
                        "publish_kind": "status", "response_state": "acknowledged", "message": "Seen",
                    }, actor_stream_id=TARGET)
                    assert not ack["duplicate"]
                    assert first.activity_snapshot()["inputs"]["first"]["response_state"] == "acknowledged"

                await first.accept_input({"message": "Second", "msg_id": "second", "request_id": "rpc-b"},
                                         operator_principal="operator:fixture")
                _, queued = await _check_projection(store, composite=first)
                assert queued["inputs"]["second"]["response_state"] == "queued"
                await asyncio.wait_for(first.stop(), 5)
                assert first._worker.done() and not first._dispatch_tasks
                await store.submit(lambda conn: conn.execute("SELECT 1").fetchone())
        finally:
            await asyncio.wait_for(first.stop(), 5)
            store.stop()

        store = Store(str(path))
        store.start()
        _, _, send = dispatcher(store)
        recovered = AssistantComposite(store, config=config, dispatch=send, broadcast=broadcast)
        try:
            await recovered.ensure_projection()
            repair = await recovered.recover()
            assert repair["retained_uncertain"] == int(barrier != "pre-claim")
            await asyncio.wait_for(asyncio.shield(recovered._worker), 5)
            _, fresh = await _check_projection(store, composite=recovered)
            first_row = await store.get_assistant_composite_route(stream_id=ASSISTANT, input_identity="first")
            second_row = await store.get_assistant_composite_route(stream_id=ASSISTANT, input_identity="second")
            assert second_row["delivery_state"] == "landed"
            assert second_row["route_target_generation"] == root["session_generation"]
            assert first_row["delivery_state"] == ("landed" if barrier == "pre-claim" else "uncertain")
            assert fresh["inputs"]["first"]["response_state"] == (
                "awaiting_reply" if barrier == "pre-claim" else "uncertain")
            assert len(tmux.pastes) == (1 if barrier == "held-intent" else 2)
            events = [frame["event"] for frame in frames if frame["type"] == "chat.event"]
            assert [e["message_id"] for e in events if e["kind"] == "USER"] == ["first", "second"]
            if proof is not None:
                receipt = await store.get_send_receipt(TARGET, first_row["dispatch_id"])
                assert receipt["receipt_id"] == proof["receipt_id"] and receipt["submission_confirmed"]
            return {"barrier": barrier, "before": preimage, "after": [first_row, second_row],
                    "proof_before_stop": proof, "frames": frames, "pastes": len(tmux.pastes), "recovery": repair}
        finally:
            await asyncio.wait_for(recovered.stop(), 5)
            store.stop()

    cells = [asyncio.run(run(barrier)) for barrier in ("pre-claim", "held-intent", "post-paste")]
    _receipt(tmp_path, record_property, "restart", {"cells": cells})
