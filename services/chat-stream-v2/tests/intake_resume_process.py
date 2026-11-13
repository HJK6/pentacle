"""Real-process, file-backed intake driver; stdin commands delimit actual passes.

Only process discovery is replaced by the legacy launch-recorded fixture path.
The normalizers, Store, routing and provenance sink are production objects.
"""
import asyncio
from collections import Counter
from copy import deepcopy
import json
import logging
import inspect
import os
from pathlib import Path
import sqlite3
import signal
import threading
import sys
import time

from ingest import Ingest, IngestConfig, _close_stream
from routing_integrity import RoutingIntegrity
from sessions import Sessions
from store import Store
from usage_provenance import ProvenanceSink, HistoryLog
from intake_sqlite_guard import install
import store_capacity_retry
import store as store_module
import store_usage

STAMP = "2026-10-01T00:00:00Z"


SID = "fixture:replay"


async def main():
    logging.basicConfig(level=logging.INFO)
    # Fix only daemon wall-clock metadata; duration measurements stay monotonic,
    # and the provider-supplied timestamps in the transcript are untouched.
    store_module.iso_now = lambda: STAMP
    store_usage.iso_now = lambda: STAMP
    root, transcript, provider = Path(sys.argv[1]).resolve(), Path(sys.argv[2]), sys.argv[3]
    root.mkdir(parents=True, exist_ok=True)
    # SQLite reports reading the PRIMARY KEY during its creation. Create the
    # empty fixture schema before the strict guard; never read its contents.
    with sqlite3.connect(root / "sessions.db") as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    guard = install(root)
    store_capacity_retry.load = lambda conn, sid: None
    store = Store(str(root / "sessions.db"))
    store.start()
    sessions = Sessions(store, local_host="fixture")
    await sessions.refresh()
    if await store.fetch_session("fixture", "replay") is None:
        await sessions.open("fixture", "replay", provider=provider, jsonl_path=str(transcript),
                            pane_pid="4321", claude_session_id="native",
                            session_generation="fixed-generation")
    frames = []

    async def broadcast(frame):
        frames.append(deepcopy(frame))

    routing = RoutingIntegrity(store, sessions, broadcast=broadcast)
    provenance = ProvenanceSink(store.record_provenance, HistoryLog(root / "usage.jsonl"), now=lambda: STAMP)
    pump = Ingest(store, sessions, None, broadcast, local_host="fixture", recent_limit=10000,
                  routing_integrity=routing, provenance=provenance,
                  config=IngestConfig(max_events_per_pass=4096))
    calls = Counter()
    injected = []
    writes = []
    if hasattr(store, "save_ingest_position"):
        original_save = store.save_ingest_position

        async def measure_save(row, changes):
            writes.append({"columns": sorted(changes), "bytes": sum(len(v.encode()) for v in changes.values())})
            return await original_save(row, changes)
        store.save_ingest_position = measure_save
    store.call_diag.record = lambda name, *args: calls.update([name])
    try:
        for line in sys.stdin:
            command = json.loads(line)
            op = command["op"]
            if op == "close":
                break
            if op == "guidelines":
                from assistant_guidelines import AssistantGuidelines
                from test_assistant_guidelines import Store as GuidelineStore, Composite, Rulings
                mode = command.get("mode", "failure")
                memory = root / "guideline-memory"
                (memory / "agents").mkdir(parents=True, exist_ok=True)
                (memory / "agents/assistant_baseline.md").write_text("Fixture role baseline\n")
                os.environ["AGENT_ORCH_MEMORY_REPO"] = str(memory)
                composite = Composite()
                composite.current = {"stream_id": SID if mode != "non_target" else "fixture:other",
                                     "generation": "fixed-generation"}

                class GuidelineComms:
                    async def tell(self, message):
                        injected.append("guideline_tell")
                        if mode == "failure":
                            raise RuntimeError("injected guideline delivery failure")
                        return {"delivery_status": "delivered"}

                # Guideline metadata alone uses the synthetic KV adapter. The
                # real routing, intake and checkpoint Store stay under the
                # permanent no-KV-read guard; no live application data is read.
                service = AssistantGuidelines(GuidelineStore(), composite, Rulings(), GuidelineComms(),
                    env={"PENTACLE_ASSISTANT_GUIDELINES_ENABLED": "0" if mode == "disabled" else "1"})
                await service._write_record("front_desk", {
                    "stream_id": SID, "generation": "fixed-generation", "identity": service.load("front_desk")[0],
                    "delivered_at": time.time() if mode == "cooldown" else 0,
                    "delivery_status": "committed_pending_proof" if mode == "pending" else "delivered",
                    "pending": False})
                routing.guidelines = service
                print('{}', flush=True)
                continue
            if op == "unavailable_history":
                provenance.history = None
                print('{}', flush=True)
                continue
            if op == "notification_failure":
                await store.update_session("fixture", "replay", requested_model="claude-opus-5-5", requested_effort="high")
                await sessions.refresh()

                class FailedNotify:
                    async def create_internal_notification(self, **kwargs):
                        injected.append("notification")
                        raise RuntimeError("injected notification failure")
                routing.notify = FailedNotify()
                print('{}', flush=True)
                continue
            if op == "edit_position":
                column = command["column"]
                assert column in {"position_json", "provenance_json", "routing_json"}
                def change(conn):
                    with conn:
                        conn.execute(f"UPDATE v2_ingest_positions SET {column}=? WHERE stream_id=?", (command["value"], SID))
                await store.submit(change)
                print('{}', flush=True)
                continue
            if op in {"close_seat", "reopen_seat"}:
                await store.mark_closed("fixture", "replay", closed_at="2026-10-01T00:00:01Z", pane_status="pane_dead")
                await sessions.refresh()
                if op == "reopen_seat":
                    await sessions.open("fixture", "replay", provider=provider, jsonl_path=str(transcript),
                                        pane_pid="4321", claude_session_id="native", session_generation="new-generation")
                print('{}', flush=True)
                continue
            if op == "seed_tell":
                from comms import Comms
                import comms as comms_module
                from spawnctl import SpawnCtl
                from test_tell_immediate_delivery import StoreTailProofCodex
                comms_module.SUBMISSION_EVIDENCE_TIMEOUT_S = 0.01
                comms_module.SUBMISSION_EVIDENCE_POLL_S = 0.001
                tmux = StoreTailProofCodex(store, SID, append_on_paste=False)
                sessions.tmux = tmux
                comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=tmux))
                reply = await comms.tell({"stream_id": SID, "tell_id": "restart-tell", "message": "same input",
                                         "from_stream_id": "fixture:sender", "_auth_context": {
                                             "stream_id": "fixture:sender", "token_verified": True}})
                print(json.dumps({"reply": reply, "wire": tmux.pastes[0]}), flush=True)
                continue
            if op == "kill_at":
                stage = command["stage"]

                def kill():
                    (root / "kill-reached").write_text(stage)
                    print(json.dumps({"kill_ready": stage}), flush=True)
                    # The parent sends SIGKILL while this boundary cannot move.
                    threading.Event().wait()

                if stage == "during_position":
                    armed = False

                    def trace(sql):
                        nonlocal armed
                        if sql.startswith(("UPDATE v2_ingest_positions", "INSERT INTO v2_ingest_positions")):
                            armed = True
                        elif armed and sql == "COMMIT":
                            kill()
                    await store.submit(lambda conn: conn.set_trace_callback(trace))
                else:
                    method = {"after_append": "_reconcile_ingested_tell",
                              "after_binding": "promote_tell_delivery", "before_position": "save_ingest_position"}[stage]

                    async def die(*args, **kwargs):
                        kill()
                    setattr(store, method, die)
                print('{}', flush=True)
                continue
            if op == "tell_state":
                print(json.dumps(await store.get_tell_delivery("restart-tell")), flush=True)
                continue
            if op == "position":
                print(json.dumps(await store.load_ingest_position(SID)), flush=True)
                continue
            if op == "client_history":
                from server import Server
                server = Server(store=store, sessions=sessions, comms=None, local_host="fixture")
                response = await server._on_request_stream_events({"stream_id": SID, "limit": 500})
                events = []
                async for frame in response:
                    events.extend(json.loads(frame.payload)["events"])
                assert guard["kv_read_attempts"] == guard["outside_path_attempts"] == 0
                print(json.dumps(events), flush=True)
                continue
            if op == "rebuild_projections":
                await sessions.refresh()
                sessions.restore_genuine_activity(SID, await store.fetch_session_event_tail(SID, limit=64))
                print('{}', flush=True)
                continue
            if op == "projection":
                seat = sessions.get(SID)
                fields = ("usage", "context_tokens", "context_level", "last_text", "last_kind",
                          "last_event_at", "last_activity", "routing_integrity", "routing_integrity_event_id")
                print(json.dumps({key: seat.get(key) for key in fields}), flush=True)
                continue
            if op == "fail_once":
                owner = {"provenance": provenance, "routing": routing, "store": store}[command["owner"]]
                name = command["method"]
                original = getattr(owner, name)
                return_none = command.get("return_none", False)

                async def fail(*args, **kwargs):
                    setattr(owner, name, original)
                    injected.append(name)
                    if return_none:
                        return None
                    raise RuntimeError("injected completed-frontier failure")
                setattr(owner, name, fail)
                print('{}', flush=True)
                continue
            if op == "pass":
                calls.clear()
                frames.clear()
                writes.clear()
                pump.config.max_events_per_pass = command.get("limit", 4096)
                started = time.monotonic()
                appended = await pump.run_pass()
                elapsed = time.monotonic() - started
                state = pump._streams.get(SID)
                result = {"appended": appended, "elapsed_s": elapsed, "offset": state.offset if state else None,
                          "failures": state.failures if state else 0, "calls": dict(calls), "injected": list(injected),
                          "resume_reason": getattr(state, "resume_reason", "legacy"),
                          "checkpoint_hold": getattr(state, "checkpoint_hold", ""),
                          "source": inspect.getfile(Ingest), "writes": list(writes)}
            elif op == "snapshot":
                rows = await store.fetch_session_event_tail(SID, limit=10000)
                state = pump._streams[SID]
                tables = ("v2_usage_state", "v2_usage_records", "v2_usage_provenance", "v2_usage_identity",
                          "v2_usage_codex_responses", "v2_usage_codex_thread_proof", "v2_usage_codex_thread_flags")

                def accounting(conn):
                    return {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1,2,3")]
                            for table in tables}
                # Inventory delivery has its own restart-reset throttle. Compare
                # its rebuilt projection separately, and ordered event pushes here.
                result = {"events": rows, "frames": [f for f in frames if f["type"] == "chat.event"],
                          "accounting": await store.submit(accounting),
                          "continuation": {"offset": state.offset, "session_id": state.session_id,
                                           "provenance": state.provenance_state,
                                           "routing_tuple": routing._claude_tuples.get(SID)}}
            else:
                raise AssertionError(op)
            assert guard["kv_read_attempts"] == guard["outside_path_attempts"] == 0
            print(json.dumps(result), flush=True)
    finally:
        for state in pump._streams.values():
            _close_stream(state)
        store.stop()


if __name__ == "__main__":
    asyncio.run(main())
