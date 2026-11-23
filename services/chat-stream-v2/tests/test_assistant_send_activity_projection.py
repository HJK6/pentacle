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


def _work_contract(run, *, drain=True):
    calls = run["calls"]
    for refresh in run["refreshes"]:
        window = calls[refresh["begin"]:refresh["end"]]
        calls_in_refresh = [call for call in window if StoreWork.projection(call)]
        assert len(calls_in_refresh) == 1, refresh
        call = calls_in_refresh[0]
        assert not call["individual"] and call["hydrated"] <= 100, (
            "full activity work in awaited refresh", run["selected"], refresh["stage"], call,
        )
        assert sum(count for key, count in call["statements"].items()
                   if key not in {"retention_metadata", "transaction"}) <= 12, call
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
    _work_contract(small)
    _work_contract(large)
    restored = drive(1024, "restore_full")
    displaced = drive(1024, "displace_full")
    _receipt(tmp_path, record_property, "work-controls", {"arms": [restored, displaced]})
    with pytest.raises(AssertionError, match="full activity work in awaited refresh"):
        _work_contract(restored)
    _work_contract(displaced, drain=False)
    with pytest.raises(AssertionError, match="full activity work through worker drain"):
        _work_contract(displaced)
    def projection_ticks(run):
        return sum(call["vm_ticks"] for call in run["calls"] if StoreWork.projection(call))
    assert projection_ticks(large) * 2 <= projection_ticks(restored)
