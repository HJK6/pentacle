#!/usr/bin/env python3
"""Read one daemon.stats response from a running chat_streamd v2."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

import websockets

DAEMON_WS_MAX_SIZE = 4 * 1024 * 1024


async def read_daemon_stats(*, ws_url: str, timeout_s: float) -> dict:
    request_id = f"daemon-stats-{uuid.uuid4().hex}"
    async with websockets.connect(
        ws_url,
        open_timeout=timeout_s,
        max_size=DAEMON_WS_MAX_SIZE,
    ) as websocket:
        await websocket.send(json.dumps({"type": "daemon.stats", "request_id": request_id}))
        while True:
            frame = json.loads(await asyncio.wait_for(websocket.recv(), timeout=timeout_s))
            if frame.get("type") == "daemon.stats.ok" and frame.get("request_id") == request_id:
                return frame


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read chat_streamd v2 daemon.stats once.")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:7791")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)
    print(json.dumps(asyncio.run(read_daemon_stats(ws_url=args.ws_url, timeout_s=args.timeout)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
