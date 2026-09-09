from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets

from agent_orch import cli
from agent_orch.config import Config


@dataclass
class MockDaemon:
    responder: Callable[[dict[str, Any]], dict[str, Any] | None]
    requests: list[dict[str, Any]] = field(default_factory=list)
    url: str = ""
    _loop: asyncio.AbstractEventLoop | None = None
    _stop: asyncio.Future[None] | None = None
    _thread: threading.Thread | None = None

    def __enter__(self) -> "MockDaemon":
        ready = threading.Event()

        async def handler(ws):
            async for raw in ws:
                message = json.loads(raw)
                if message.get("type") == "hello":
                    continue
                self.requests.append(message)
                response = self.responder(message)
                if response is not None:
                    await ws.send(json.dumps(response, separators=(",", ":")))

        async def main():
            self._loop = asyncio.get_running_loop()
            self._stop = self._loop.create_future()
            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                self.url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                ready.set()
                await self._stop

        self._thread = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
        self._thread.start()
        assert ready.wait(timeout=5)
        return self

    def __exit__(self, *_exc) -> None:
        if self._loop is not None and self._stop is not None and not self._stop.done():
            self._loop.call_soon_threadsafe(self._stop.set_result, None)
        if self._thread is not None:
            self._thread.join(timeout=5)


def _run_cli_against_mock(monkeypatch, tmp_path: Path, argv: list[str], responder) -> tuple[int, list[dict[str, Any]]]:
    with MockDaemon(responder) as daemon:
        monkeypatch.setattr(cli, "load_config", lambda: Config(daemon.url, "tok", "hosta", tmp_path))
        monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hosta:leader")
        return cli.main(argv), daemon.requests


def _delivered_spawn_response(request: dict[str, Any]) -> dict[str, Any]:
    request_id = str(request["request_id"])
    stream_id = "hosta:codex-child"
    return {
        "type": "spawn.ok",
        "request_id": request_id,
        "session": {"stream_id": stream_id, "session_name": "codex-child"},
        "initial_prompt_delivery": {
            "request_id": request_id,
            "tell_id": f"initial-prompt-{request_id}",
            "ledger_row_id": 3,
            "to_stream_id": stream_id,
            "delivery_status": "delivered",
            "delivery_ack_at": "2026-08-01T00:00:01Z",
        },
    }


def test_mock_daemon_cli_contract_ring(monkeypatch, tmp_path, capsys):
    cases = [
        (
            [
                "tell",
                "hosta:target",
                "hello",
                "--from",
                "hosta:leader",
                "--tell-id",
                "tell-1",
            ],
            {
                "type": "tell",
                "from_stream_id": "hosta:leader",
                "to_stream_id": "hosta:target",
                "tell_id": "tell-1",
                "text": "hello",
                "ttl_seconds": 300,
            },
            lambda request: {
                "type": "tell.ok",
                "request_id": request.get("request_id", "tell-1"),
                "tell_id": request["tell_id"],
                "ledger_row_id": 1,
                "delivery_status": "queued",
            },
        ),
        (
            [
                "report",
                "--from-stream-id",
                "hosta:leader",
                "--msg-id",
                "7",
                "--status",
                "done",
                "--report-id",
                "report-1",
                "--result",
                '{"summary":"ok","findings":[],"next_action":"leader_proceed"}',
            ],
            {
                "type": "report",
                "from_stream_id": "hosta:leader",
                "msg_id": 7,
                "status": "done",
                "summary": "ok",
                "report_id": "report-1",
            },
            lambda request: {
                "type": "report.ok",
                "request_id": request.get("request_id", "report-1"),
                "report_id": request["report_id"],
                "ledger_row_id": 2,
                "ingested": True,
            },
        ),
        (
            [
                "spawn", "--objective", "Exercise the existing spawn contract",
                "--provider",
                "codex",
                "--host",
                "hosta",
                "--parent",
                "hosta:leader",
                "--visibility",
                "hidden",
                "--initial-prompt",
                "qa",
            ],
            {"objective": "Exercise the existing spawn contract",
                "type": "spawn",
                "provider": "codex",
                "host": "hosta",
                "parent_stream_id": "hosta:leader",
                "visibility": "hidden",
                "initial_prompt": "qa",
            },
            _delivered_spawn_response,
        ),
        (
            ["title", "Gate Speed"],
            {
                "type": "rename",
                "host": "hosta",
                "session_name": "leader",
                "display_name": "Gate Speed",
                "source": "agent",
            },
            lambda request: {
                "type": "rename.ok",
                "request_id": request.get("request_id", "rename-1"),
                "session": {"stream_id": "hosta:leader", "display_name": request["display_name"]},
            },
        ),
        (
            [
                "status",
                "--goal",
                "Ship the card",
                "--plan",
                "daemon",
                "--plan",
                "cli",
                "--step-done",
                "1",
                "--update",
                "daemon merged",
                "--handoff-planned",
            ],
            {
                "type": "status_card",
                "host": "hosta",
                "session_name": "leader",
                "from_stream_id": "hosta:leader",
                "goal": "Ship the card",
                "plan": ["daemon", "cli"],
                "step_done": 1,
                "update": "daemon merged",
                "handoff_planned": True,
            },
            lambda request: {
                "type": "status_card.ok",
                "request_id": request.get("request_id", "status-card-1"),
                "session": {"stream_id": "hosta:leader", "status_card": {"goal": request["goal"]}},
            },
        ),
        (
            ["await", "--from", "hosta:worker", "--timeout", "1"],
            {
                "type": "await_report",
                "stream_id": "hosta:worker",
            },
            lambda request: {
                "type": "await_report.ok",
                "request_id": request.get("request_id", "await-1"),
                "ok": True,
                "stream_id": request["stream_id"],
                "report": {"status": "done", "summary": "ok"},
            },
        ),
        (
            ["reconcile", "status", "--host", "hosta", "--json"],
            {
                "type": "reconcile.status",
                "host": "hosta",
            },
            lambda request: {
                "type": "reconcile.status.ok",
                "request_id": request.get("request_id", "reconcile-1"),
                "counts": {
                    "row_open_session_dead": 1,
                    "row_closed_tree_alive": 0,
                    "row_open_host_unreachable": 0,
                    "unmanaged_tree": 0,
                },
                "details": [],
            },
        ),
        (
            ["reconcile", "status", "--host", "hosta"],
            {
                "type": "reconcile.status",
                "host": "hosta",
            },
            lambda request: {
                "type": "reconcile.status.ok",
                "request_id": request.get("request_id", "reconcile-2"),
                "counts": {
                    "row_open_session_dead": 1,
                    "row_closed_tree_alive": 0,
                    "row_open_host_unreachable": 2,
                    "unmanaged_tree": 0,
                },
                "details": [],
            },
        ),
    ]

    for argv, expected, responder in cases:
        exit_code, requests = _run_cli_against_mock(monkeypatch, tmp_path, argv, responder)
        assert exit_code == 0
        request = requests[-1]
        for key, value in expected.items():
            assert request.get(key) == value

    captured = capsys.readouterr()
    assert "tell.ok" in captured.out
    assert "report.ok" in captured.out
    assert "spawn.ok" in captured.out
    assert "rename.ok" in captured.out
    assert "await_report.ok" in captured.out
    assert "reconcile.status.ok" in captured.out
    assert "row_open_host_unreachable=2" in captured.out
