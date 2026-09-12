#!/usr/bin/env python3
"""Seed a hermetic chat-stream-v2 sessions DB for the web-mode E2E gate.

Run this with the daemon STOPPED, against the same `--db` path the daemon will
open. At boot the daemon rebuilds its inventory purely from this DB
(`main.py` → `sessions.refresh()`), so a seeded `open` session appears in the
sidebar and its seeded transcript loads over `requestStreamEvents` — with no
live provider, tmux pane, or network. This is what makes the gate deterministic.

Usage:
    python3 seed_web_gate.py --db <sessions.db> [--host local] \
        [--session web-gate-1] [--objective "web gate fixture"]

Prints one JSON line: {"stream_id": "...", "events": N}.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import hashlib
import os
import sys

# Import the daemon's own store/ingest so the seed goes through the exact code
# paths the daemon reads back (session_created_at linkage, event identity).
_SERVICE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "services", "chat-stream-v2"
)
sys.path.insert(0, os.path.abspath(_SERVICE_DIR))

from store import Store  # noqa: E402
from ingest import _identity_key  # noqa: E402

# A fixed, human-readable transcript so assertions can match exact strings.
TRANSCRIPT = [
    ("USER", "hello from the web gate fixture"),
    ("ASSIST_TEXT", "fixture assistant reply for the web gate"),
]


def _event(stream_id: str, kind: str, text: str, index: int) -> dict:
    return {
        "stream_id": stream_id,
        "provider": "claude",
        "kind": kind,
        "text": text,
        "timestamp": "2026-08-05T00:00:00Z",
        "raw": {"jsonl_record_uuid": f"web-gate-seed-{index}", "jsonl_event_index": index},
    }


async def seed(db: str, host: str, session: str, objective: str, token_file: str | None = None) -> dict:
    store = Store(db)
    store.start()
    try:
        stream_id = f"{host}:{session}"
        # A top-level session with visibility="default": that is the exact value
        # the renderer's sidebar admits (renderer/sidebar_filter.js
        # filterSidebarSessions "fail closed on visibility" -> only 'default'),
        # and it is delivered to a loopback client's inventory. No parent =>
        # not a subagent, so it is not filtered out.
        # provider is required: the renderer's transcript selector reads
        # session.provider.toUpperCase() (pentacle-chat-core
        # selectSessionDetailFromStreamEvents), so a null provider throws.
        await store.open_session(
            host, session, visibility="default", role="worker",
            provider="claude", objective=objective,
        )
        if token_file:
            with open(token_file) as source:
                token = source.read().strip()
            assert await store.grant_stream_token(host, session, hashlib.sha256(token.encode()).hexdigest(), "sha256:v1") == "ok"
        appended = 0
        for index, (kind, text) in enumerate(TRANSCRIPT):
            event = _event(stream_id, kind, text, index)
            result = await store.append_session_event(
                stream_id, event, identity=_identity_key(event), limit=500,
            )
            if result:
                appended += 1
        return {"stream_id": stream_id, "events": appended}
    finally:
        store.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--host", default="local")
    parser.add_argument("--session", default="web-gate-1")
    parser.add_argument("--objective", default="web gate fixture")
    parser.add_argument("--token-file")
    args = parser.parse_args()
    result = asyncio.run(seed(args.db, args.host, args.session, args.objective, args.token_file))
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
