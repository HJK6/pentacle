"""Post-admission spawn deadline over the real client path (restart continuity, cycle 3).

A stub daemon (real websockets server) sits behind a TCP proxy that can stop
forwarding a connection, all in a background thread. The client side runs
`wsclient.spawn_once` unchanged inside its own `asyncio.run`, so the bound is
measured through actual loop teardown, and a CLI subprocess probe measures it
through process exit. Spec: spec_pentacle__daemon_restart_continuity_2026_10.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import websockets

from agent_orch import wsclient
from agent_orch.config import Config

STREAM = "testhost:v2-dl"
DEADLINE_S = 0.6
SLACK_S = 0.05  # test slack for the controlled probes, not an inner budget


@dataclass
class Plan:
    """What the stub does on each connection after the spawn RPC (connection 1)."""
    inventory: str = "starting"         # starting | ready | failed | silent | drop
    outcome: str = "starting"           # starting | ready | silent
    connect_stall: bool = False         # the inventory connection never completes its handshake
    refuse_after_admission: bool = False  # every later connection is refused (daemon down)
    freeze_inventory_before_deadline: bool = False  # stop forwarding, so the close handshake stalls
    terminal_in_snapshot: str | None = None  # ready | failed: the snapshot itself is the proof
    stall_close_after_proof: bool = False   # once a proof frame is out, the client's next bytes (its close) stall


@dataclass
class Stub:
    plan: Plan
    admitted_at: float | None = None
    spawn_requests: list[dict] = field(default_factory=list)
    outcome_requests: list[dict] = field(default_factory=list)
    connections: int = 0
    loop: asyncio.AbstractEventLoop | None = None
    port: int = 0
    frozen: set = field(default_factory=set)
    proof_sent: bool = False
    dropped: bool = False  # the inventory connection was closed (daemon restart)
    durable_answered: asyncio.Event | None = None  # the pre-drop durable readback has been answered
    received: list = field(default_factory=list)  # (monotonic, message type) of every client request
    thread: threading.Thread | None = None


def _closed(session: dict) -> dict:
    return {**session, "state": "failed", "status": "closed", "closed_at": "2026-10-07T00:00:00Z",
            "error_code": "spawn_failed"}


async def _send_proof(stub: Stub, ws, frame: dict) -> None:
    await ws.send(json.dumps(frame))
    stub.proof_sent = True


async def _handler(stub: Stub, ws) -> None:
    hello = json.loads(await ws.recv())
    if (hello.get("subscribe") or {}).get("mode") == "rpc":
        async for raw in ws:
            request = json.loads(raw)
            stub.received.append((time.monotonic(), request.get("type")))
            if request.get("type") == "spawn":
                stub.spawn_requests.append(request)
                stub.admitted_at = time.monotonic()
                await ws.send(json.dumps({
                    "type": "spawn.ok", "ok": True, "request_id": request["request_id"], "stream_id": STREAM,
                    "state": "starting", "session": {"stream_id": STREAM, "state": "starting", "status": "open"},
                    "initial_prompt_delivery": {"state": "staged"}}))
            elif request.get("type") == "await_spawn":
                stub.outcome_requests.append(request)
                outcome = stub.plan.outcome
                if stub.plan.inventory == "drop" and not stub.dropped:
                    # The pre-drop durable readback (current rules: not proof).
                    await ws.send(json.dumps({"type": "await_spawn.ok", "ok": True, "request_id": request["request_id"],
                                              "state": "starting",
                                              "session": {"stream_id": STREAM, "state": "starting", "status": "open"}}))
                    stub.durable_answered.set()
                    continue
                if outcome == "silent":
                    await asyncio.Event().wait()
                if outcome == "error":
                    await _send_proof(stub, ws, {"type": "await_spawn.error", "ok": False,
                                                 "request_id": request["request_id"], "error_code": "spawn_failed",
                                                 "error": "fixture failure",
                                                 "session": _closed({"stream_id": STREAM})})
                    continue
                stream = "testhost:v2-other" if outcome == "ready-other-stream" else STREAM
                state = "ready" if outcome.startswith("ready") else outcome
                frame = {"type": "await_spawn.ok", "ok": True, "request_id": request["request_id"], "state": state,
                         "session": {"stream_id": stream, "state": state, "status": "open"}}
                if outcome == "ready":
                    await _send_proof(stub, ws, frame)
                else:
                    await ws.send(json.dumps(frame))
        return
    if stub.plan.inventory == "silent":
        await asyncio.Event().wait()
    starting = {"stream_id": STREAM, "state": "starting", "status": "open"}
    if stub.plan.terminal_in_snapshot:
        terminal = _closed(starting) if stub.plan.terminal_in_snapshot == "failed" else {**starting, "state": "ready"}
        await _send_proof(stub, ws, {"type": "snapshot", "sessions": [terminal]})
        await asyncio.Event().wait()
    await ws.send(json.dumps({"type": "snapshot", "sessions": [starting]}))
    if stub.plan.inventory == "drop":
        await stub.durable_answered.wait()
        stub.dropped = True
        await ws.close(1001, "restart")
        return
    if stub.plan.inventory in ("ready", "failed", "late-ready"):
        if stub.plan.inventory == "late-ready":  # first established after the client's deadline
            await asyncio.sleep(max(0.0, stub.admitted_at + DEADLINE_S + 0.05 - time.monotonic()))
        else:
            await asyncio.sleep(0.05)
        terminal = _closed(starting) if stub.plan.inventory == "failed" else {**starting, "state": "ready"}
        await _send_proof(stub, ws, {"type": "sessions", "sessions": [terminal]})
    await asyncio.Event().wait()


async def _pipe(reader, writer, conn_id: int, stub: Stub, client_side: bool = False) -> None:
    try:
        while data := await reader.read(65536):
            if client_side and stub.plan.stall_close_after_proof and stub.proof_sent:
                stub.frozen.add(conn_id)  # the client's first bytes after the proof: its close frame
            if conn_id in stub.frozen:
                await asyncio.Event().wait()  # hold the bytes; the connection stays open
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        if conn_id not in stub.frozen:
            writer.close()


async def _proxy_client(stub: Stub, upstream_port: int, reader, writer) -> None:
    stub.connections += 1
    conn_id = stub.connections
    plan = stub.plan
    if conn_id > 1 and plan.refuse_after_admission:
        writer.transport.abort()
        return
    if conn_id == 2 and plan.connect_stall:
        await asyncio.Event().wait()  # accept TCP, forward nothing: the WebSocket handshake stalls
    up_reader, up_writer = await asyncio.open_connection("127.0.0.1", upstream_port)
    if conn_id == 2 and plan.freeze_inventory_before_deadline:
        async def freeze() -> None:
            while stub.admitted_at is None:
                await asyncio.sleep(0.01)
            await asyncio.sleep(max(0.0, stub.admitted_at + DEADLINE_S - 0.1 - time.monotonic()))
            stub.frozen.add(conn_id)
        asyncio.ensure_future(freeze())
    await asyncio.gather(_pipe(reader, up_writer, conn_id, stub, client_side=True),
                         _pipe(up_reader, writer, conn_id, stub))


def _start_stub(plan: Plan) -> Stub:
    stub = Stub(plan)
    ready = threading.Event()

    def run() -> None:
        loop = asyncio.new_event_loop()
        stub.loop = loop
        asyncio.set_event_loop(loop)

        async def main() -> None:
            stub.durable_answered = asyncio.Event()
            server = await websockets.serve(lambda ws: _handler(stub, ws), "127.0.0.1", 0)
            upstream = server.sockets[0].getsockname()[1]
            proxy = await asyncio.start_server(lambda r, w: _proxy_client(stub, upstream, r, w), "127.0.0.1", 0)
            stub.port = proxy.sockets[0].getsockname()[1]
            ready.set()
            await asyncio.Event().wait()

        try:
            loop.run_until_complete(main())
        except (RuntimeError, asyncio.CancelledError):
            pass
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.wait(pending, timeout=1))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    stub.thread = threading.Thread(target=run, daemon=True)
    stub.thread.start()
    assert ready.wait(5)
    return stub


def _stop_stub(stub: Stub) -> None:
    if stub.loop is None:
        return
    stub.loop.call_soon_threadsafe(lambda: [t.cancel() for t in asyncio.all_tasks(stub.loop)])
    stub.thread.join(5)


def _run_client(stub: Stub, tmp_path: Path) -> tuple[dict, float, float]:
    """spawn_once in its own asyncio.run; returns (result, return_s, run_exit_s)
    measured from the stub's admission reply."""
    config = Config(f"ws://127.0.0.1:{stub.port}", "", "testhost", tmp_path)
    returned: dict = {}

    async def go() -> dict:
        result = await wsclient.spawn_once(config, {"provider": "claude", "host": "testhost",
                                                    "request_id": "spawn-dl", "idempotency_key": "spawn-dl"},
                                           timeout=DEADLINE_S)
        returned["at"] = time.monotonic()
        return result

    result = asyncio.run(go())
    exited = time.monotonic()
    assert stub.admitted_at is not None
    return result, returned["at"] - stub.admitted_at, exited - stub.admitted_at


@pytest.fixture(autouse=True)
def _client_env(monkeypatch):
    for name in ("AGENT_ORCH_STREAM_ID", "AGENT_ORCH_STREAM_TOKEN", "AGENT_ORCH_STREAM_TOKEN_FILE",
                 "PENTACLE_STREAM_ID", "AGENT_ORCH_RPC_RETRY_MAX_ATTEMPTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_ORCH_RPC_RETRY_BACKOFF_BASE_S", "0.02")
    monkeypatch.setenv("AGENT_ORCH_RPC_RETRY_BACKOFF_CAP_S", "0.05")
    monkeypatch.setenv("AGENT_ORCH_RPC_RETRY_DEADLINE_S", "30")


STALLS = {
    "connect": Plan(connect_stall=True),
    "hello-snapshot": Plan(inventory="silent"),
    "inventory": Plan(inventory="starting", outcome="starting"),
    "durable-readback": Plan(inventory="starting", outcome="silent"),
    "reconnect-backoff": Plan(inventory="drop", refuse_after_admission=False, outcome="silent"),
    "daemon-down-after-admission": Plan(refuse_after_admission=True),
    "close-and-cancellation-cleanup": Plan(inventory="starting", outcome="silent",
                                           freeze_inventory_before_deadline=True),
}


@pytest.mark.parametrize("stall", sorted(STALLS))
def test_admitted_spawn_wait_holds_one_deadline_through_loop_exit(tmp_path, stall):
    """Whatever stalls after admission, the client returns a typed
    spawn.indeterminate (admitted identity kept) within the deadline, and its
    asyncio.run exits within the same bound: nothing it started outlives it."""
    stub = _start_stub(STALLS[stall])
    try:
        result, returned_s, exited_s = _run_client(stub, tmp_path)
    finally:
        _stop_stub(stub)
    assert result["type"] == "spawn.indeterminate", result
    assert result["request_id"] == "spawn-dl" and result["stream_id"] == STREAM
    assert result["initial_prompt_delivery"]["state"] == "indeterminate"
    assert returned_s <= DEADLINE_S + SLACK_S, returned_s
    assert exited_s <= DEADLINE_S + SLACK_S, exited_s
    # Same-key durable admission: one spawn request, never re-spawned; every
    # outcome readback names the admitted request.
    assert len(stub.spawn_requests) == 1
    assert all(r.get("spawn_request_id") == "spawn-dl" for r in stub.outcome_requests)
    assert not _sent_after_deadline(stub)


def _sent_after_deadline(stub: Stub) -> list:
    return [(round(t - stub.admitted_at, 3), kind) for t, kind in stub.received if t > stub.admitted_at + DEADLINE_S]


PROVEN = {
    "snapshot-ready": (Plan(terminal_in_snapshot="ready"), ("spawn.ok", "ready")),
    "snapshot-error": (Plan(terminal_in_snapshot="failed"), ("spawn.error", None)),
    "inventory-ready": (Plan(inventory="ready"), ("spawn.ok", "ready")),
    "inventory-error": (Plan(inventory="failed"), ("spawn.error", None)),
    "recovery-ready": (Plan(inventory="drop", outcome="ready"), ("spawn.ok", "ready")),
    "recovery-error": (Plan(inventory="drop", outcome="error"), ("spawn.error", None)),
}


@pytest.mark.parametrize("case", sorted(PROVEN))
def test_proof_established_in_time_survives_a_stalled_close(tmp_path, case):
    """B4-TERMINAL-CLOSE: once ready/error is proven (snapshot, inventory, or
    the recovery readback), the client's close handshake is frozen; the
    deadline then expires mid-cleanup, and the proven result is still the
    one returned, within the same bound through loop exit."""
    plan, expected = PROVEN[case]
    plan.stall_close_after_proof = True
    stub = _start_stub(plan)
    try:
        result, returned_s, exited_s = _run_client(stub, tmp_path)
    finally:
        _stop_stub(stub)
    assert stub.proof_sent and stub.frozen, "the race fixture must have frozen the close after the proof"
    assert result["type"] == expected[0], result
    if expected[1]:
        assert result["state"] == expected[1]
    assert result["request_id"] == "spawn-dl" and result["stream_id"] == STREAM
    assert returned_s <= DEADLINE_S + SLACK_S and exited_s <= DEADLINE_S + SLACK_S, (returned_s, exited_s)
    assert len(stub.spawn_requests) == 1 and not _sent_after_deadline(stub)


@pytest.mark.parametrize("plan", [Plan(inventory="late-ready"), Plan(inventory="drop", outcome="ready-other-stream")],
                         ids=["proof-after-expiry", "other-stream-identity"])
def test_late_or_foreign_proof_never_becomes_the_result(tmp_path, plan):
    stub = _start_stub(plan)
    try:
        result, returned_s, exited_s = _run_client(stub, tmp_path)
    finally:
        _stop_stub(stub)
    assert result["type"] == "spawn.indeterminate" and result["stream_id"] == STREAM, result
    assert returned_s <= DEADLINE_S + SLACK_S and exited_s <= DEADLINE_S + SLACK_S
    assert len(stub.spawn_requests) == 1 and not _sent_after_deadline(stub)


@pytest.mark.parametrize("plan,expected", [
    (Plan(inventory="ready"), ("spawn.ok", "ready")),
    (Plan(inventory="failed"), ("spawn.error", None)),
    (Plan(inventory="drop", outcome="ready"), ("spawn.ok", "ready")),
], ids=["terminal-ready", "terminal-error", "transport-recovery"])
def test_terminal_proof_in_time_keeps_its_truthful_result(tmp_path, plan, expected):
    stub = _start_stub(plan)
    try:
        result, returned_s, exited_s = _run_client(stub, tmp_path)
    finally:
        _stop_stub(stub)
    assert result["type"] == expected[0], result
    if expected[1]:
        assert result["state"] == expected[1]
    assert result["request_id"] == "spawn-dl" and result["stream_id"] == STREAM
    assert returned_s < DEADLINE_S and exited_s <= DEADLINE_S + SLACK_S
    assert len(stub.spawn_requests) == 1


CLI_CASES = {
    "no-proof": (STALLS["inventory"], "spawn.indeterminate", 3),
    "proven-error-stalled-close": (Plan(terminal_in_snapshot="failed", stall_close_after_proof=True), "spawn.error", 1),
}


@pytest.mark.parametrize("case", sorted(CLI_CASES))
def test_agent_orch_spawn_cli_exits_within_the_deadline(tmp_path, case):
    """The ACTUAL `agent-orch spawn` entry point (python -m agent_orch.cli) in a
    disposable process against the stub (never a live daemon), under an outer
    watchdog: it prints the right typed result and the process exits within
    the spawn deadline plus interpreter exit."""
    plan, expected, rc = CLI_CASES[case]
    stub = _start_stub(plan)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_ORCH_STREAM", "PENTACLE_STREAM"))}
    env.update({"AGENT_ORCH_WS_URL": f"ws://127.0.0.1:{stub.port}", "AGENT_ORCH_HOST_ID": "testhost",
                "HOME": str(tmp_path), "PYTHONPATH": str(Path(wsclient.__file__).resolve().parents[1])})
    argv = [sys.executable, "-m", "agent_orch.cli", "spawn", "--provider", "claude", "--host", "testhost",
            "--top-level", "--timeout", str(DEADLINE_S), "--objective", "deadline probe", "--initial-prompt", "hello",
            "--request-id", "spawn-dl", "--idempotency-key", "spawn-dl"]
    try:
        proc = subprocess.run(argv, env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=20)  # watchdog
        exited = time.monotonic()
    finally:
        _stop_stub(stub)
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["type"] == expected and result["request_id"] == "spawn-dl", (proc.stdout[-500:], proc.stderr[-500:])
    assert proc.returncode == rc, proc.stderr[-500:]
    assert len(stub.spawn_requests) == 1 and not _sent_after_deadline(stub)
    # Interpreter shutdown after asyncio.run is outside the client's await budget.
    assert exited - stub.admitted_at <= DEADLINE_S + SLACK_S + 0.25, exited - stub.admitted_at
