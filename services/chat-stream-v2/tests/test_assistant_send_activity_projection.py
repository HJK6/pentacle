"""Synthetic direct-send and retained activity checks through the real Store worker."""

from __future__ import annotations

import asyncio
from collections import Counter
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys
from threading import Event
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
        self.refresh = ContextVar("observed_refresh", default=None)
        admitted_call = ContextVar("observed_store_call", default=None)
        self.projection_sql = {}
        original_submit = store.submit
        original_project = assistant_activity.project_input
        original_enqueue = store.progress.enqueue

        def enqueue(put, item):
            call = admitted_call.get()
            if call is not None:
                call["queued"] = item[3]  # The Store's own admission timestamp.
            return original_enqueue(put, item)

        def project(*args, **kwargs):
            if self.current is not None:
                self.current["hydrated"] += 1
            return original_project(*args, **kwargs)

        async def submit(fn, **options):
            call = {"callback": fn.__qualname__, "submitted": monotonic(),
                    "refresh": self.refresh.get(), "hydrated": 0, "vm_ticks": 0,
                    "statements": {}, "individual": {}, "rows": {}, "decoded_bytes": {}}
            self.calls.append(call)
            statements = Counter()
            individual = Counter()

            rows = Counter()
            decoded_bytes = Counter()
            family = "other"

            def query(sql):
                nonlocal family
                family = _statement_family(sql)
                statements[family] += 1
                if self.projection(call) and sql.lstrip().upper().startswith(("SELECT", "WITH")):
                    # Keep one executed query per family and shape. Sample after
                    # worker drain so EXPLAIN never changes the measured workload.
                    self.projection_sql.setdefault((family, sql.count("json_each(")), sql)
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
                row_factory = conn.row_factory

                def count_row(cursor, row):
                    rows[family] += 1
                    decoded_bytes[family] += sum(
                        len(value.encode()) if isinstance(value, str)
                        else len(value) if isinstance(value, bytes)
                        else 0 if value is None else 8
                        for value in row
                    )
                    return row_factory(cursor, row)

                if trace:
                    conn.set_trace_callback(query)
                    conn.set_progress_handler(progress, PROGRESS_INTERVAL)
                    if self.projection(call):
                        conn.row_factory = count_row
                self.current = call
                call["started"] = monotonic()
                try:
                    result = fn(conn)
                    call["returned"] = type(result).__name__
                    if self.projection(call):
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
                        conn.row_factory = row_factory
                        conn.set_progress_handler(None, 0)
                        conn.set_trace_callback(None)
                        call["statements"] = dict(statements)
                        call["individual"] = dict(individual)
                        call["rows"] = dict(rows)
                        call["decoded_bytes"] = dict(decoded_bytes)

            token = admitted_call.set(call)
            try:
                return await original_submit(observe, **options)
            finally:
                admitted_call.reset(token)
                call["delivered"] = monotonic()

        monkeypatch.setattr(store.progress, "enqueue", enqueue)
        monkeypatch.setattr(store, "submit", submit)
        monkeypatch.setattr(assistant_activity, "project_input", project)

    def plans(self, conn):
        return [{"family": family, "json_each": json_each, "plan": [tuple(row) for row in conn.execute(
            "EXPLAIN QUERY PLAN " + sql)]} for (family, json_each), sql in self.projection_sql.items()]

    @staticmethod
    def projection(call):
        return any(f".{name}." in call["callback"] for name in (FULL_READER, SUMMARY_READER))


def _receipt(tmp_path, record_property, name, data):
    data = {"python": sys.version, "os": platform.platform(), "sqlite": sqlite3.sqlite_version,
            "progress_interval": PROGRESS_INTERVAL, **data}
    body = json.dumps(data, indent=2, sort_keys=False).encode()
    evidence = os.environ.get("PENTACLE_GATE_EVIDENCE_DIR")
    directory = Path(evidence) / "activity-sidecars" if evidence else tmp_path
    directory.mkdir(exist_ok=True)
    path = directory / f"{name}.json"
    path.write_bytes(body)
    record_property(name, f"{path.name} sha256={hashlib.sha256(body).hexdigest()}")


async def _direct_pair(tmp_path, monkeypatch, *, selected, mode="sound", trace=True, backlog=False, prepare=None):
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
        if prepare is not None:
            await prepare(store)
        engine = await store.submit(lambda conn: conn.execute("SELECT sqlite_source_id()").fetchone()[0])
        work = StoreWork(monkeypatch, store, trace=trace)
        stages = []
        refreshes = []
        proofs = []
        snapshots = []
        joins = []
        load = []

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
            refresh_index = len(refreshes)
            pending = []
            if backlog and kind == "admit":
                loop = asyncio.get_running_loop()
                started = asyncio.Event()
                release = Event()

                def wait_on_worker(conn):
                    loop.call_soon_threadsafe(started.set)
                    if not release.wait(5):
                        raise AssertionError("Backlog release did not run")

                def cheap_read(conn):
                    return conn.execute("SELECT 1").fetchone()[0]

                def queue_independent_read():
                    pending.append(asyncio.create_task(store.submit(cheap_read)))

                pending.append(asyncio.create_task(store.submit(wait_on_worker)))
                await asyncio.wait_for(started.wait(), 5)
                loop.call_soon(queue_independent_read)
                loop.call_later(0.02, release.set)
                load.append({"refresh": refresh_index, "target_wait_ms": 20})

            token = work.refresh.set(refresh_index)
            try:
                if mode == "restore_full":
                    async def old_projection(*, stream_id):
                        return assistant_activity.summarize_activity(await full_reader(stream_id=stream_id))
                    with monkeypatch.context() as patch:
                        patch.setattr(store, SUMMARY_READER, old_projection)
                        await real_refresh(**kwargs)
                else:
                    await real_refresh(**kwargs)
            finally:
                work.refresh.reset(token)
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending), 5)
            refreshes.append({"stage": kind, "index": refresh_index, "begin": start, "end": len(work.calls),
                              "done": monotonic()})
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
            canonical_receipt = await store.get_send_receipt(ASSISTANT, f"rpc-{identity}")
            assert canonical_receipt["receipt_id"] == result["receipt_id"] and canonical_receipt["submission_confirmed"]
            joins.append({"accepted": result, "canonical_receipt": canonical_receipt, "landed": row,
                          "recipient_receipt": recipient_receipt})

        if displaced:
            await asyncio.wait_for(asyncio.gather(*displaced), 10)
        await store.submit(lambda conn: conn.execute("SELECT 1").fetchone()[0])
        end = len(work.calls)
        assert all(call["queued"] <= call["started"] <= call["finished"] <= call["delivered"]
                   for call in work.calls[:end])
        assert [entry["stage"] for entry in stages] == list(STAGE_ORDER) * 2
        assert [entry["stage"] for entry in refreshes] == list(REFRESH_ORDER + REFRESH_ORDER[1:])
        assert len(tmux.pastes) == len(proofs) == 2
        assert "<assistant-dispatch-batch-json>" not in tmux.pastes[0]
        assert "<assistant-dispatch-batch-json>" in tmux.pastes[1] and "in-a" in tmux.pastes[1]
        assert joins[1]["landed"]["batched_into_dispatch_id"] == joins[0]["landed"]["dispatch_id"]
        # Compare full values outside the measured worker drain. Do not share the
        # optimized implementation with the retained reader used as oracle.
        plans = await store.submit(work.plans) if trace else []
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
                "joins": joins, "snapshots": snapshots, "proofs": proofs, "sqlite_source_id": engine,
                "mode": mode, "plans": plans, "controlled_backlog": load}
    finally:
        if composite is not None:
            await asyncio.wait_for(composite.stop(), 10)
        store.stop()


def _refresh_work_misses(run):
    misses = []
    for refresh in run["refreshes"]:
        window = run["calls"][refresh["begin"]:refresh["end"]]
        calls = [call for call in window if StoreWork.projection(call) and call["refresh"] == refresh["index"]]
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
        assert not any(f".{FULL_READER}." in call["callback"] for call in calls), (
            "full activity work through worker drain", run["selected"],
        )
        assert sum(StoreWork.projection(call) for call in calls) == len(run["refreshes"])


def test_direct_send_refresh_stage_work(tmp_path, monkeypatch, record_property):
    run = asyncio.run(_direct_pair(tmp_path, monkeypatch, selected=128))
    _receipt(tmp_path, record_property, "direct-stages", run)
    _work_contract(run)


def _normalized_output(snapshot):
    inputs = {}
    for key, item in snapshot["inputs"].items():
        if key not in {"in-a", "in-b"}:
            inputs[key] = item
            continue
        assert item["message_id"] == key and item["accepted_at"]
        assert item["publication_ids"] == item["lane_ids"] == []
        assert item["reply_latency_ms"] is item["final_latency_ms"] is None
        assert item["queue_latency_ms"] == assistant_activity._elapsed(item["accepted_at"], item["routing_started_at"])
        assert item["routing_latency_ms"] == assistant_activity._elapsed(item["routing_started_at"], item["routed_at"])
        inputs[key] = {field: bool(value) if field in {
            "dispatch_id", "accepted_at", "routing_started_at", "routed_at", "delivery_recorded_at",
        } else value is not None if field in {"queue_latency_ms", "routing_latency_ms"} else value
                       for field, value in item.items()}
    return {**snapshot, "inputs": inputs}


async def _measure_retained_summary(path, monkeypatch, *, layout, width, other_stream_width):
    from store import Store

    path.parent.mkdir(parents=True)
    store = Store(":memory:" if layout == "hot" else str(path))
    store.start()
    try:
        reader = getattr(store, SUMMARY_READER)
        await reader(stream_id=ASSISTANT)  # initialize the synthetic pair
        await store.submit(lambda conn: _seed(conn, width))
        await store.submit(lambda conn: _seed(conn, other_stream_width, stream="padding:other"))
        archive = await store.submit(lambda conn: conn.history_archive_path)
        cold = 0
        if layout != "hot":
            selection = "stream_id=" + repr(ASSISTANT)
            if layout == "mixed":
                selection += " AND CAST(SUBSTR(input_identity, 6) AS INTEGER) % 2 = 0"
            cold = await _copy_synthetic_routes(store, archive, selection, delete=True)
        work = StoreWork(monkeypatch, store)
        actual = await reader(stream_id=ASSISTANT)
        call, = work.calls
        plans = await store.submit(work.plans)
        assert call["hydrated"] == len(actual["inputs"]) == 100
        assert not call["individual"] and call["vm_ticks"] > 0
        assert actual["pending_count"] == actual["waiting_for_operator_count"] == 1
        assert actual["has_more"]
        assert cold == {"hot": 0, "archive": width, "mixed": (width + 1) // 2}[layout]
        return {"layout": layout, "width": width, "hot": width - cold, "archive": cold,
                "other_stream_width": other_stream_width, "result": actual, "call": call, "plans": plans}
    finally:
        store.stop()


def _record_hosted_work(record_property, sound, restored, displaced):
    """Preserve the measured stage and fault counters in the uploaded JUnit."""
    arms = []
    for run, observed in [
        *((arm, "sound_pass") for arm in sound),
        (restored, "awaited_send_work_rejected"),
        (displaced, "drain_work_rejected"),
    ]:
        calls = run["calls"]
        projections = [call for call in calls if StoreWork.projection(call)]
        full = [call for call in projections if f".{FULL_READER}." in call["callback"]]
        stages = []
        for refresh in run["refreshes"]:
            index = refresh["index"]
            call, = [entry for entry in projections if entry["refresh"] == index]
            statements = call["statements"]
            metadata = statements.get("retention_metadata", 0)
            transactions = statements.get("transaction", 0)
            stages.append([
                0 if index == 0 else (index - 1) // 4 + 1, refresh["stage"], call["hydrated"],
                sum(statements.values()) - metadata - transactions, metadata, transactions,
                *[call["individual"].get(family, 0) for family in ("publications", "operations", "lanes")],
                call["vm_ticks"],
            ])
        arms.append({
            "scenario": run["mode"], "width": run["selected"], "observed_predicate": observed, "stages": stages,
            "confirmed_recipient_receipts": sum(join["recipient_receipt"]["submission_confirmed"] for join in run["joins"]),
            "fenced_landings": sum(join["landed"]["delivery_state"] == "landed" for join in run["joins"]),
            "worker_drain": {
                "callbacks": len(calls), "projection_callbacks": len(projections), "full_detail_callbacks": len(full),
                "detached_full_detail_callbacks": sum(call["refresh"] is None for call in full),
                "vm_ticks": sum(call["vm_ticks"] for call in calls),
            },
        })
    record_property("activity_work_v1", json.dumps({
        "stage_fields": [
            "send_ordinal", "stage", "hydrated", "data_statements", "retention_statements", "transaction_statements",
            "individual_publication", "individual_operation", "individual_lane", "vm_ticks",
        ],
        "sqlite_version": sqlite3.sqlite_version,
        "progress_interval": PROGRESS_INTERVAL,
        "arms": arms,
    }, separators=(",", ":")))


def test_fixed_output_work_and_drain(tmp_path, monkeypatch, record_property):
    def drive(width, mode="sound", *, trace=True, backlog=False):
        with monkeypatch.context() as patch:
            return asyncio.run(_direct_pair(tmp_path, patch, selected=width, mode=mode, trace=trace, backlog=backlog))

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
    with pytest.raises(AssertionError, match="full activity work in awaited send refresh") as restored_failure:
        _work_contract(restored)
    _work_contract(displaced, drain=False)
    with pytest.raises(AssertionError, match="full activity work through worker drain") as displaced_failure:
        _work_contract(displaced)
    _receipt(tmp_path, record_property, "control-verdicts", {
        "restored_loop": str(restored_failure.value), "post_landing_displacement": str(displaced_failure.value),
    })
    def projection_ticks(run):
        return sum(call["vm_ticks"] for call in run["calls"] if StoreWork.projection(call))
    assert projection_ticks(large) * 2 <= projection_ticks(restored)
    assert sum(call["vm_ticks"] for call in displaced["calls"]) > 2 * sum(call["vm_ticks"] for call in large["calls"])
    loaded = drive(1024, trace=True, backlog=True)
    _receipt(tmp_path, record_property, "controlled-backlog", loaded)
    _work_contract(loaded)
    assert len(loaded["controlled_backlog"]) == 2
    assert len([call for call in loaded["calls"] if ".cheap_read" in call["callback"]]) == 2
    retained = []
    for layout in ("hot", "archive", "mixed"):
        arms = []
        for width, padding in ((2000, 0), (4096, 2048)):
            with monkeypatch.context() as patch:
                arms.append(asyncio.run(_measure_retained_summary(
                    tmp_path / f"{layout}-{width}" / "hot.db", patch, layout=layout,
                    width=width, other_stream_width=padding,
                )))
        retained.extend(arms)
        small_arm, large_arm = arms
        assert small_arm["result"] == large_arm["result"]
        assert list(small_arm["result"]["inputs"]) == list(large_arm["result"]["inputs"])
        assert large_arm["call"]["vm_ticks"] < 3 * small_arm["call"]["vm_ticks"], arms
    _receipt(tmp_path, record_property, "retained-work", {"arms": retained})
    _record_hosted_work(record_property, (small, large), restored, displaced)


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


def test_authenticated_send_fanout_and_proof(tmp_path, monkeypatch, record_property):
    import websockets
    from _shared import operator_auth
    from server import Server
    from test_voice_context import CHAT, CONTEXT, FIXTURE, read_recipient_context

    async def run():
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        server = composite = registry = None
        frames, pushes, sent, publications, refreshes = [], [], [], [], []
        phase = "setup"
        fail_next_fanout = False
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
                nonlocal fail_next_fanout
                frames.append(frame)
                if fail_next_fanout:
                    fail_next_fanout = False
                    raise RuntimeError("controlled fanout interruption")
                await server.broadcast(frame)

            async def notify(*args, **kwargs):
                pushes.append((args, kwargs))

            async def recipient(route):
                nonlocal phase
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
                for phase in ("recipient_publication", "recipient_publication_replay"):
                    publications.append(await composite.publish(publication, actor_stream_id=TARGET))
                phase = "route"
                return proof

            server = Server(host="127.0.0.1", port=0, store=store, sessions=sessions, comms=comms, local_host=HOST)
            composite = AssistantComposite(store, config=config, dispatch=recipient, broadcast=broadcast)
            composite.reply_push = notify
            server.assistant_composite = composite
            work = StoreWork(monkeypatch, store, trace=False)
            original_refresh = composite.refresh_activity

            async def track_refresh(**kwargs):
                await original_refresh(**kwargs)
                refreshes.append({"phase": phase, "finished": monotonic()})

            monkeypatch.setattr(composite, "refresh_activity", track_refresh)
            await composite.ensure_projection()
            phase = "route"
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
                phase = "ingress_replay"
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
            assert Counter(refresh["phase"] for refresh in refreshes) == {
                "setup": 1, "route": 4, "recipient_publication": 1, "recipient_publication_replay": 1,
                "ingress_replay": 1,
            }
            if hasattr(store, SUMMARY_READER):
                assert sum(f".{FULL_READER}." in call["callback"] for call in work.calls) == 1  # USER enrichment.
            canonical_receipt = await store.get_send_receipt(CHAT, "rpc-1")
            assert canonical_receipt["receipt_id"] == canonical["receipt_id"]
            assert canonical_receipt["submission_confirmed"] is True

            proactive = {
                "request_id": "proactive-replay", "composite_stream_id": CHAT, "publish_kind": "status",
                "note_type": "result", "ref": "message:take-1", "message": "Synthetic proactive status",
                "_auth_context": {"token_verified": True, "stream_id": TARGET,
                                  "session_generation": root["session_generation"]},
            }
            phase = "proactive"
            fail_next_fanout = True
            with pytest.raises(RuntimeError, match="controlled fanout interruption"):
                await composite.publish(proactive, actor_stream_id=TARGET)
            replay = await composite.publish(proactive, actor_stream_id=TARGET)
            assert replay["duplicate"]
            assert [frame["type"] for frame in frames[-2:]] == ["chat.event", "chat.event"]
            assert [frame["event"]["daemon_seq"] for frame in frames[-2:]] == [replay["event_id"]] * 2
            assert len(pushes) == 1
            phase = "oracle"
            await _check_projection(store, CHAT, composite)
            _receipt(tmp_path, record_property, "authenticated", {
                "socket": sent, "received_ack": received_ack, "result": result, "echo_received": observed,
                "frames": frames, "publications": publications, "landed": after, "recipient": recipient_receipt,
                "canonical_receipt": canonical_receipt, "refreshes": refreshes, "worker": work.calls,
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
    for i in (190, 191, 192, 193):
        remove_result(i)
        publish(i, i * 2, "status", "acknowledged")
        publish(i, i * 2 + 1, "question", None)
    conn.execute("DELETE FROM v2_assistant_composite_lanes WHERE lane_id=?", (f"{ASSISTANT}:lane-190",))
    conn.execute("UPDATE v2_assistant_composite_lanes SET pending_question_id='question-192' WHERE lane_id=?",
                 (f"{ASSISTANT}:lane-192",))
    conn.execute("UPDATE v2_assistant_composite_lanes SET phase='cancelled' WHERE lane_id=?",
                 (f"{ASSISTANT}:lane-193",))
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
        assert live["pending_count"] == 5 and live["waiting_for_operator_count"] == 3
        assert [full[f"seed-{i:05}"]["response_state"] for i in (190, 191, 192, 193)] == [
            "waiting_for_operator", "acknowledged", "waiting_for_operator", "cancelled",
        ]
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
        if layout != "mixed":
            return cells
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
            assert summary["pending_count"] == (4 if expected else 5)
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
            readers = [store.assistant_composite_activity]
            if hasattr(store, SUMMARY_READER):
                readers.append(getattr(store, SUMMARY_READER))
            errors = []
            for reader in readers:
                with pytest.raises((RetentionError, sqlite3.OperationalError)) as error:
                    await reader(stream_id=ASSISTANT)
                errors.append(type(error.value))
            assert len(set(errors)) == 1
            with sqlite3.connect(archive) as db:
                db.execute(f"DELETE FROM {ROUTE_TABLE} WHERE input_identity='seed-00000'")
            await _check_projection(store)
            with sqlite3.connect(archive) as db:
                original_pair = db.execute("SELECT pair_id FROM v2_receipt_route_archive").fetchone()[0]
                db.execute("UPDATE v2_receipt_route_archive SET pair_id='different-pair'")
            for reader in readers:
                with pytest.raises(RetentionError, match="pair mismatch"):
                    await reader(stream_id=ASSISTANT)
            with sqlite3.connect(archive) as db:
                db.execute("UPDATE v2_receipt_route_archive SET pair_id=?", (original_pair,))
            archive.rename(archive.with_suffix(".unavailable"))
            for reader in readers:
                with pytest.raises(RetentionError, match="path unavailable"):
                    await reader(stream_id=ASSISTANT)
            with sqlite3.connect(archive):
                pass
            for reader in readers:
                with pytest.raises(RetentionError, match="archive replaced"):
                    await reader(stream_id=ASSISTANT)
        finally:
            store.stop()
        _receipt(tmp_path, record_property, "retained", {"cells": cells})

    asyncio.run(run())


async def _transfer_during_receipt(tmp_path):
    from test_assistant_transfer_route_continuity import (
        CHAT, OLD, committed_transfer, fixture, route_of, wire_production_close_hook,
    )

    async with fixture(tmp_path, authority=True) as (store, server, composite, gateway, _, __):
        wire_production_close_hook(server, composite)
        entered, release = asyncio.Event(), asyncio.Event()
        attempts = []

        async def backend(route):
            attempts.append((route["dispatch_id"], route["route_target"], route["route_target_generation"]))
            if route["route_target"] == OLD:
                entered.set()
                await release.wait()
            return {"delivery": "landed"}  # Controlled late-transport receipt, not physical proof.

        composite.dispatch = backend
        await composite.accept_input({"message": "In flight during move", "msg_id": "in-flight", "request_id": "rpc"},
                                     operator_principal="operator:" + gateway.ceremony.cid)
        try:
            await asyncio.wait_for(entered.wait(), 5)
            original = await route_of(store, "in-flight")
            await _check_projection(store, CHAT, composite)
            transfer, _, successor, old_generation = await committed_transfer(store, server, gateway, composite)
            carried = await route_of(store, "in-flight")
            assert carried["dispatch_id"] == original["dispatch_id"]
            assert carried["route_target_generation"] == successor["target_generation"] != old_generation
            assert carried["delivery_state"] == "intent"
            await _check_projection(store, CHAT, composite)
        finally:
            release.set()
        await asyncio.wait_for(asyncio.shield(composite._worker), 5)
        assert await route_of(store, "in-flight") == carried
        await server.host_transfer.project_and_cleanup(transfer)
        landed = await route_of(store, "in-flight")
        assert landed["delivery_state"] == "landed"
        assert landed["route_target_generation"] == successor["target_generation"]
        assert [attempt for attempt in attempts if attempt[0] == carried["dispatch_id"]] == [
            (carried["dispatch_id"], OLD, old_generation),
            (carried["dispatch_id"], successor["target"], successor["target_generation"]),
        ]
        _, summary = await _check_projection(store, CHAT, composite)
        return {"old": original, "carried": carried, "landed": landed, "attempts": attempts, "summary": summary}


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
                        "request_id": f"publish:{preimage['dispatch_id']}:ack", "composite_stream_id": ASSISTANT,
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
    transfer = asyncio.run(_transfer_during_receipt(tmp_path))
    _receipt(tmp_path, record_property, "restart", {"cells": cells, "transfer": transfer})
