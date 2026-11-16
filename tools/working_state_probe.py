#!/usr/bin/env python3
"""Probe the v2 mobile working-state wire contract against a running daemon.

The target stream should be idle when the probe connects. Start one bounded
turn after the listening line; the probe asserts the hello snapshot map and
then a named stream's working-state edge within the deadline. A sustained
remote pane may already be active, in which case --allow-initial-working
asserts its live snapshot state instead of waiting for a second edge.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any

import websockets


async def _recv_json(ws: Any, deadline: float) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("probe timeout expired")
    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
    frame = json.loads(raw)
    if frame.get("type") == "ping":
        await ws.send(json.dumps({"type": "pong"}, separators=(",", ":")))
        return await _recv_json(ws, deadline)
    return frame


async def _recv_until(ws: Any, predicate, deadline: float) -> dict[str, Any]:
    while True:
        frame = await _recv_json(ws, deadline)
        if predicate(frame):
            return frame


def _session(frame: dict[str, Any], stream_id: str) -> dict[str, Any] | None:
    for row in frame.get("sessions") or frame.get("active") or []:
        if isinstance(row, dict) and str(row.get("stream_id") or "") == stream_id:
            return row
    return None


def _active_snapshot_state(working_states: dict[str, Any], stream_id: str) -> dict[str, Any]:
    state = working_states.get(stream_id)
    if not isinstance(state, dict):
        raise AssertionError(f"active stream missing from snapshot working_states: {stream_id}")
    phase = str(state.get("tokens_phase") or state.get("phase") or "")
    try:
        elapsed_ms = float(state.get("elapsed_ms") or 0)
    except (TypeError, ValueError):
        elapsed_ms = 0
    if phase not in {"down", "up", "working"} and elapsed_ms <= 0:
        raise AssertionError(f"snapshot working state was not active: {stream_id}")
    return state


async def _run(args: argparse.Namespace) -> int:
    deadline = time.monotonic() + args.timeout
    async with websockets.connect(args.url, max_size=16 * 1024 * 1024, open_timeout=min(10.0, args.timeout)) as ws:
        welcome = await _recv_until(ws, lambda frame: frame.get("type") == "welcome", deadline)
        await ws.send(json.dumps({
            "type": "hello",
            "request_id": "working-state-probe-hello",
            "client": "pentacle-mobile",
            "subscribe": {
                "all": True,
                "include_subagents": True,
                "events_mode": "summary",
            },
        }, separators=(",", ":")))
        await _recv_until(
            ws,
            lambda frame: frame.get("type") == "hello"
            and frame.get("request_id") == "working-state-probe-hello",
            deadline,
        )
        snapshot = await _recv_until(ws, lambda frame: frame.get("type") == "snapshot", deadline)
        working_states = snapshot.get("working_states")
        if not isinstance(working_states, dict):
            raise AssertionError("snapshot is missing object working_states")
        initial = _session(snapshot, args.stream_id)
        if initial is None:
            request_id = "working-state-probe-list"
            await ws.send(json.dumps({"type": "list_sessions", "request_id": request_id}, separators=(",", ":")))
            listed = await _recv_until(
                ws,
                lambda frame: frame.get("type") == "list_sessions.ok"
                and frame.get("request_id") == request_id,
                deadline,
            )
            initial = _session(listed, args.stream_id)
        if initial is None:
            raise AssertionError(f"named stream not present in snapshot/list_sessions: {args.stream_id}")
        initial_working = bool(initial.get("working"))
        if initial_working and not args.allow_initial_working:
            raise AssertionError(f"named stream was already working: {args.stream_id}")
        if initial_working and not initial.get("working_label"):
            raise AssertionError(f"already-working stream lacked a working label: {args.stream_id}")

        print(json.dumps({
            "status": "listening",
            "url": args.url,
            "stream_id": args.stream_id,
            "snapshot_working_states": len(working_states),
            "initial_working": initial_working,
            "welcome_auth_required": bool(welcome.get("auth_required")),
            "timeout_s": args.timeout,
        }, sort_keys=True, separators=(",", ":")), flush=True)

        if initial_working:
            snapshot_state = _active_snapshot_state(working_states, args.stream_id)
            print(json.dumps({
                "status": "pass",
                "stream_id": args.stream_id,
                "tokens_phase": (
                    str(snapshot_state.get("tokens_phase") or "")
                    if isinstance(snapshot_state, dict) else ""
                ),
                "working": True,
                "working_label": str(initial["working_label"]),
                "frame_type": "snapshot",
            }, sort_keys=True, separators=(",", ":")), flush=True)
            return 0

        while True:
            frame = await _recv_json(ws, deadline)
            if frame.get("type") != "working.state" or frame.get("stream_id") != args.stream_id:
                continue
            phase = str(frame.get("tokens_phase") or frame.get("phase") or "")
            if phase not in {"down", "up", "working"}:
                continue
            request_id = "working-state-probe-list-after-edge"
            await ws.send(json.dumps({"type": "list_sessions", "request_id": request_id}, separators=(",", ":")))
            listed = await _recv_until(
                ws,
                lambda item: item.get("type") == "list_sessions.ok"
                and item.get("request_id") == request_id,
                deadline,
            )
            target = _session(listed, args.stream_id)
            if not target or not target.get("working") or not target.get("working_label"):
                raise AssertionError(f"working edge lacked live session working label: {args.stream_id}")
            print(json.dumps({
                "status": "pass",
                "stream_id": args.stream_id,
                "tokens_phase": phase,
                "working": True,
                "working_label": str(target["working_label"]),
                "frame_type": "working.state",
            }, sort_keys=True, separators=(",", ":")), flush=True)
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:7791")
    parser.add_argument("--stream-id", required=True)
    parser.add_argument("--timeout", type=float, default=65.0)
    parser.add_argument(
        "--allow-initial-working",
        action="store_true",
        help="accept an already-working target and prove its snapshot state",
    )
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
