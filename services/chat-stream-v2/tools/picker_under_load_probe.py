#!/usr/bin/env python3
"""Assert a catalog reply remains fast while authenticated ``event.push`` fans out.

Run this from the stream host. The probe reads the already-configured bearer
secret from the deployed session store's ``kv.event_push.secret`` row, opened
read-only; it never accepts or prints a secret flag. ``--target-sha`` is the
exact 40-hex deployment SHA: it becomes ``event.push.satellite_sha`` and the
ack must prove the daemon's ``event_push.target_sha`` pin is the same value.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from uuid import uuid4

from websockets.sync.client import connect


DEFAULT_DB = Path("~/.local/share/pentacle-stream/sessions.db").expanduser()
MAX_EVENT_PUSH_BATCH = 2000
WIRE_VERSION = 1
FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")


def _read_event_push_secret(db: Path) -> str:
    """Read only the deployed event.push secret; never expose it in output."""
    try:
        with sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT v FROM kv WHERE k = ?", ("event_push.secret",),
            ).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError(f"cannot read event_push.secret from {db}: {exc}") from exc
    if row is None or not isinstance(row[0], str) or not row[0]:
        raise RuntimeError(f"event_push.secret is not configured in {db}")
    return row[0]


def _recv_json(ws: object, *, timeout: float) -> dict:
    raw = ws.recv(timeout=timeout)  # type: ignore[attr-defined]
    try:
        frame = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("received non-JSON websocket frame") from exc
    if not isinstance(frame, dict):
        raise RuntimeError("received non-object websocket frame")
    return frame


def _recv_correlated(ws: object, request_id: str, *, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f"{request_id} reply timed out")
        frame = _recv_json(ws, timeout=remaining)
        if frame.get("request_id") == request_id:
            return frame


def _expect_welcome(ws: object, *, timeout: float) -> None:
    frame = _recv_json(ws, timeout=timeout)
    if frame.get("type") != "welcome":
        raise RuntimeError(f"expected welcome, got {frame.get('type')!r}")


def _start_observer(ws: object, *, timeout: float) -> None:
    _expect_welcome(ws, timeout=timeout)
    ws.send(json.dumps({  # type: ignore[attr-defined]
        "type": "hello", "request_id": "picker-observer", "client": "pentacle",
        "subscribe": {"include_subagents": True, "events_mode": "summary"},
    }))
    hello = _recv_correlated(ws, "picker-observer", timeout=timeout)
    if hello.get("type") != "hello":
        raise RuntimeError(f"observer hello failed: {hello}")
    snapshot = _recv_json(ws, timeout=timeout)
    if snapshot.get("type") != "snapshot":
        raise RuntimeError(f"expected observer snapshot, got {snapshot.get('type')!r}")


def _burst_events(stream: str, burst: int, marker: str) -> list[dict]:
    host, separator, session_name = stream.partition(":")
    if not separator or not host or not session_name:
        raise ValueError("--stream must be a host:session stream id")
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return [{
        "stream_id": stream,
        "host": host,
        "provider": "claude",
        "kind": "USER",
        "text": f"{marker}:{index}",
        "timestamp": timestamp,
        "raw": {"jsonl_record_uuid": f"{marker}-{index}", "jsonl_event_index": index},
    } for index in range(burst)]


def _event_marker(frame: dict, marker: str) -> str | None:
    if frame.get("type") != "chat.event":
        return None
    event = frame.get("event")
    raw = event.get("raw") if isinstance(event, dict) else None
    record_id = raw.get("jsonl_record_uuid") if isinstance(raw, dict) else None
    if not isinstance(record_id, str) or not record_id.startswith(f"{marker}-"):
        return None
    return record_id


def run_probe(
    url: str,
    stream: str,
    burst: int,
    target_sha: str,
    db: Path,
    *,
    timeout: float = 2.0,
) -> float:
    """Return catalog latency after an authenticated burst reaches the observer."""
    secret = _read_event_push_secret(db)
    marker = f"picker-under-load-{uuid4().hex}"
    events = _burst_events(stream, burst, marker)
    host = stream.partition(":")[0]
    request_id = "picker-event-push"
    catalog_id = "picker-catalog"
    with connect(url, open_timeout=timeout) as observer, connect(url, open_timeout=timeout) as producer:
        _start_observer(observer, timeout=timeout)
        _expect_welcome(producer, timeout=timeout)
        producer.send(json.dumps({
            "type": "event.push", "request_id": request_id, "push_secret": secret,
            "satellite_sha": target_sha, "wire_version": WIRE_VERSION, "host": host,
            "events": events, "high_water": {},
        }))

        started = time.monotonic()
        observer.send(json.dumps({"type": "spawn_catalog_get", "request_id": catalog_id}))
        seen: set[str] = set()
        elapsed: float | None = None
        deadline = started + timeout
        while elapsed is None or len(seen) < burst:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"observer saw {len(seen)}/{burst} burst frames while {catalog_id} was in flight",
                )
            frame = _recv_json(observer, timeout=remaining)
            record_id = _event_marker(frame, marker)
            if record_id is not None:
                seen.add(record_id)
            if frame.get("request_id") == catalog_id:
                if frame.get("type") != "spawn_catalog_get.ok":
                    raise RuntimeError(f"catalog reply failed: {frame}")
                elapsed = time.monotonic() - started

        ack = _recv_correlated(producer, request_id, timeout=timeout)
        version = ack.get("version") if isinstance(ack.get("version"), dict) else {}
        if (
            ack.get("type") != "event.push.ok"
            or ack.get("accepted") != burst
            or ack.get("inserted") != burst
            or version.get("status") != "ok"
            or version.get("target_sha") != target_sha
        ):
            raise RuntimeError(f"event.push batch was not accepted at {target_sha}: {ack}")
        assert elapsed is not None
        return elapsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:7791")
    parser.add_argument("--stream", required=True)
    parser.add_argument("--burst", type=int, required=True)
    parser.add_argument(
        "--target-sha", required=True,
        help="exact deployed 40-hex SHA, verified against kv event_push.target_sha",
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help="deployed sessions.db; reads kv event_push.secret read-only",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.burst <= MAX_EVENT_PUSH_BATCH:
        parser.error(f"--burst must be 1..{MAX_EVENT_PUSH_BATCH}")
    target_sha = args.target_sha.lower()
    if FULL_SHA.fullmatch(target_sha) is None:
        parser.error("--target-sha must be exactly 40 lowercase hexadecimal characters")
    try:
        elapsed = run_probe(args.url, args.stream, args.burst, target_sha, args.db)
    except Exception as exc:
        print(f"FAIL picker under load: {exc}")
        return 2
    if elapsed >= 1.0:
        print(f"FAIL picker under load: catalog reply {elapsed:.3f}s (limit < 1.000s)")
        return 1
    print(f"PASS picker under load: catalog reply {elapsed:.3f}s (burst={args.burst})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
