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
    thread: threading.Thread | None = None


async def _handler(stub: Stub, ws) -> None:
    hello = json.loads(await ws.recv())
    if (hello.get("subscribe") or {}).get("mode") == "rpc":
        async for raw in ws:
            request = json.loads(raw)
            if request.get("type") == "spawn":
                stub.spawn_requests.append(request)
                stub.admitted_at = time.monotonic()
                await ws.send(json.dumps({
                    "type": "spawn.ok", "ok": True, "request_id": request["request_id"], "stream_id": STREAM,
                    "state": "starting", "session": {"stream_id": STREAM, "state": "starting", "status": "open"},
                    "initial_prompt_delivery": {"state": "staged"}}))
            elif request.get("type") == "await_spawn":
                stub.outcome_requests.append(request)
                if stub.plan.outcome == "silent":
                    await asyncio.Event().wait()
                state = stub.plan.outcome
                await ws.send(json.dumps({
                    "type": "await_spawn.ok", "ok": True, "request_id": request["request_id"], "state": state,
                    "session": {"stream_id": STREAM, "state": state, "status": "open"}}))
        return
    if stub.plan.inventory == "silent":
        await asyncio.Event().wait()
    starting = {"stream_id": STREAM, "state": "starting", "status": "open"}
    await ws.send(json.dumps({"type": "snapshot", "sessions": [starting]}))
    if stub.plan.inventory == "drop":
        await ws.close(1001, "restart")
        return
    if stub.plan.inventory in ("ready", "failed"):
        await asyncio.sleep(0.05)
        terminal = {**starting, "state": stub.plan.inventory}
        if stub.plan.inventory == "failed":
            terminal.update(status="closed", closed_at="2026-10-07T00:00:00Z", error_code="spawn_failed")
        await ws.send(json.dumps({"type": "sessions", "sessions": [terminal]}))
    await asyncio.Event().wait()


async def _pipe(reader, writer, conn_id: int, stub: Stub) -> None:
    try:
        while data := await reader.read(65536):
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
    await asyncio.gather(_pipe(reader, up_writer, conn_id, stub), _pipe(up_reader, writer, conn_id, stub))


def _start_stub(plan: Plan) -> Stub:
    stub = Stub(plan)
    ready = threading.Event()

    def run() -> None:
        loop = asyncio.new_event_loop()
        stub.loop = loop
        asyncio.set_event_loop(loop)

        async def main() -> None:
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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("stall", ["inventory", "close-and-cancellation-cleanup"])
def test_spawn_cli_process_exits_within_the_deadline(tmp_path, stall):
    """The actual CLI process, under an outer watchdog, prints the typed
    indeterminate and exits within the spawn deadline plus interpreter exit."""
    stub = _start_stub(STALLS[stall])
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_ORCH_STREAM", "PENTACLE_STREAM"))}
    env.update({"AGENT_ORCH_WS_URL": f"ws://127.0.0.1:{stub.port}", "AGENT_ORCH_HOST_ID": "testhost",
                "AGENT_ORCH_RPC_RETRY_BACKOFF_BASE_S": "0.02", "AGENT_ORCH_RPC_RETRY_BACKOFF_CAP_S": "0.05",
                "PYTHONPATH": str(Path(wsclient.__file__).resolve().parents[1])})
    code = ("import sys, json, asyncio\n"
            "from agent_orch import wsclient\n"
            "from agent_orch.config import load_config\n"
            "r = asyncio.run(wsclient.spawn_once(load_config(), {'provider': 'claude', 'host': 'testhost',"
            " 'request_id': 'spawn-dl', 'idempotency_key': 'spawn-dl'}, timeout=float(sys.argv[1])))\n"
            "print(json.dumps(r))\n")
    try:
        proc = subprocess.Popen([sys.executable, "-c", code, str(DEADLINE_S)], env=env, cwd=str(tmp_path),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = proc.communicate(timeout=20)  # the outer watchdog
        exited = time.monotonic()
    finally:
        _stop_stub(stub)
    assert proc.returncode == 0, err[-2000:]
    result = json.loads(out.strip().splitlines()[-1])
    assert result["type"] == "spawn.indeterminate" and result["request_id"] == "spawn-dl"
    # Interpreter shutdown after the loop is outside the client's await budget.
    assert exited - stub.admitted_at <= DEADLINE_S + SLACK_S + 0.25, exited - stub.admitted_at
