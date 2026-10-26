"""Read accepted report metadata, optionally as the isolated publisher."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid

from .config import load_config
from . import wsclient
from .send_boundary import reader_identity_ready, send_frame

READER = "work-report-reader"
TOKEN_ENV = "PENTACLE_WORK_REPORT_READER_TOKEN"


async def get_report(config, report_id: str, *, timeout: float = 20.0) -> dict:
    """One attempt, including connect and cleanup, bounded to twenty seconds.

    A launcher-provided reader credential replaces every inferred seat and
    operator credential. It is never attached to the lookup payload.
    """
    timeout = min(20.0, max(0.01, timeout))
    deadline = time.monotonic() + timeout
    request_id = f"work-report-{uuid.uuid4()}"
    try:
        async with asyncio.timeout(timeout):
            explicit = READER if TOKEN_ENV in os.environ else None
            identity = reader_identity_ready(explicit)
            hello = wsclient._rpc_hello(config, from_stream_id=explicit)
            async with wsclient.websockets.connect(config.ws_url, open_timeout=timeout, close_timeout=0,
                                                   max_size=wsclient.WEBSOCKET_MAX_SIZE) as ws:
                await send_frame(ws, hello, identity, config=config, explicit_from_stream_id=explicit, compact=False)
                await send_frame(ws, {"type": "work.report.get", "request_id": request_id,
                                      "report_id": report_id}, identity, config=config,
                                 explicit_from_stream_id=explicit, compact=False)
                return await wsclient._read_rpc_response(ws, request_id, prefix="work.report.get", deadline=deadline)
    except Exception:
        # Transport/auth details can contain URLs or secrets; keep the public
        # failure typed and bounded. No retry and no fall back to a seat token.
        return {"type": "work.report.get.error", "error_code": "unavailable", "request_id": request_id}


def main(argv) -> int:
    parser = argparse.ArgumentParser(prog="agent-orch report get")
    parser.add_argument("report_id")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    response = asyncio.run(get_report(load_config(), args.report_id, timeout=args.timeout))
    print(json.dumps(response, separators=(",", ":")))
    return 0 if response.get("type") == "work.report.get.ok" else 1
