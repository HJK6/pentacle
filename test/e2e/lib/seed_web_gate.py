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
import base64
from datetime import datetime, timedelta, timezone
import json
import hashlib
import os
from pathlib import Path
import sys

# Import the daemon's own store/ingest so the seed goes through the exact code
# paths the daemon reads back (session_created_at linkage, event identity).
_SERVICE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "services", "chat-stream-v2"
)
_SERVICE_DIR = os.path.abspath(_SERVICE_DIR)
sys.path.insert(0, _SERVICE_DIR)
sys.path.insert(0, os.path.dirname(_SERVICE_DIR))

from store import Store  # noqa: E402
from ingest import _identity_key  # noqa: E402
import recurrence  # noqa: E402

# A fixed, human-readable transcript so assertions can match exact strings.
TRANSCRIPT = [
    ("USER", "hello from the web gate fixture"),
    ("ASSIST_TEXT", "fixture assistant reply for the web gate"),
]

# Latest message of the second-host fixture: its sidebar preview must read as
# plain text (sidebar-preview-plain-text scenario) while the chat keeps Markdown.
MARKDOWN_PREVIEW = [
    ("ASSIST_TEXT", "## Result\n- **Gate** passed with *italics*, `inline code` and a [link](https://example.com/docs)"),
]


SCHEDULE_FIXTURE = {
    "recurring_id": "5ced0000-0000-4000-8000-000000000001",
    "name": "Gate nightly retro",
    "oneshot_id": "sched-web-gate-oneshot",
    "oneshot_prompt": "web gate one-shot reminder",
}
_SPEC = "spec_gate__scheduled_section"
_PROVENANCE = json.dumps([{"spec_id": _SPEC, "provenance": "spawn_explicit",
                           "granting_principal": "operator", "granted_at": "2026-10-20T00:00:00Z"}])


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


async def seed_schedules(store: Store) -> None:
    """One active weekly rule and one pending one-shot, both days from firing.

    The weekly day is today + 3 in the rule's zone, so the scenario's pause,
    resume and time edit can never put an occurrence inside the run.
    """
    now = datetime.now(timezone.utc)
    zone = recurrence.zone("America/Chicago")
    day = recurrence.DAYS[(now.astimezone(zone).weekday() + 3) % 7]
    rule = {"every": "weekly", "at_time": "02:00", "days": [day], "tz": "America/Chicago"}
    timestamp = _iso(now)
    prompt = b"web gate seeded rule"
    template = dict(
        target_host="local", requested_provider="claude", requested_model="", requested_effort="",
        resolved_provider="claude", resolved_model="claude-sonnet-5-5", resolved_effort="high",
        prompt_sha256=hashlib.sha256(prompt).hexdigest(), prompt_b64=base64.b64encode(prompt).decode(),
        owner_spec_ids_json=json.dumps([_SPEC]), owner_spec_provenance_json=_PROVENANCE,
        objective="web gate fixture", objective_source="explicit", created_at=timestamp, updated_at=timestamp,
    )

    def write(conn) -> None:
        rule_row = dict(
            recurring_id=SCHEDULE_FIXTURE["recurring_id"], name=SCHEDULE_FIXTURE["name"], state="active",
            recurrence_json=json.dumps(rule), next_fire_at_utc=_iso(recurrence.next_occurrence(rule, now)),
            parent_mode="none", generation=1, **template,
        )
        conn.execute(
            f"INSERT INTO v2_recurring_schedules ({','.join(rule_row)}) VALUES ({','.join('?' for _ in rule_row)})",
            tuple(rule_row.values()),
        )
        shot = dict(
            schedule_id=SCHEDULE_FIXTURE["oneshot_id"], request_id="5ced0000-0000-4000-8000-0000000000a1",
            owner_service_actor="recurring", fires_at_utc=_iso(now + timedelta(days=30)),
            state="pending", generation=1, **template,
        )
        shot["prompt_b64"] = base64.b64encode(SCHEDULE_FIXTURE["oneshot_prompt"].encode()).decode()
        shot["prompt_sha256"] = hashlib.sha256(SCHEDULE_FIXTURE["oneshot_prompt"].encode()).hexdigest()
        conn.execute(
            f"INSERT INTO v2_schedules ({','.join(shot)}) VALUES ({','.join('?' for _ in shot)})",
            tuple(shot.values()),
        )
        conn.commit()

    await store.submit(write)


def _event(stream_id: str, kind: str, text: str, index: int) -> dict:
    return {
        "stream_id": stream_id,
        "provider": "claude",
        "kind": kind,
        "text": text,
        "timestamp": "2026-08-05T00:00:00Z",
        "raw": {"jsonl_record_uuid": f"web-gate-seed-{index}", "jsonl_event_index": index},
    }


async def seed(db: str, host: str, session: str, objective: str, token_file: str | None = None, blob_root: str | None = None) -> dict:
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
            if host == 'local' and session == 'web-gate-1':
                # A separate hidden, bound producer lets voice answers exercise
                # real composite admission without changing ordinary fixtures.
                producer = await store.open_session(
                    host, 'web-gate-voice-producer', visibility='hidden',
                    role='assistant', provider='claude', pane_status='pane_alive',
                    objective='Synthetic voice-answer question producer',
                )
                # One stream per token hash: derive a distinct producer
                # credential from the scratch token instead of sharing it.
                producer_token = hashlib.sha256(f'voice-producer:{token}'.encode()).hexdigest()
                assert await store.grant_stream_token(
                    host, 'web-gate-voice-producer', hashlib.sha256(producer_token.encode()).hexdigest(),
                    'sha256:v1',
                ) == 'ok'
                # Identity only. The producer credential is derived from the scratch
                # token the gate already tracks/removes. Never put it in a page.
                manifest = {'stream_id': 'local:web-gate-assistant',
                            'producer_stream_id': 'local:web-gate-voice-producer',
                            'producer_generation': producer['session_generation']}
                Path(db).parent.joinpath('voice-answers-fixture.json').write_text(json.dumps(manifest))
        appended = 0
        transcript = TRANSCRIPT + (MARKDOWN_PREVIEW if session == 'web-gate-second-host' else [])
        for index, (kind, text) in enumerate(transcript):
            event = _event(stream_id, kind, text, index)
            if index >= len(TRANSCRIPT):
                # The summary only takes messages from this session's lifecycle.
                event["timestamp"] = _iso(datetime.now(timezone.utc))
            result = await store.append_session_event(
                stream_id, event, identity=_identity_key(event), limit=500,
            )
            if result:
                appended += 1
        if session == 'web-gate-1':
            await seed_schedules(store)
        if blob_root and session == 'web-gate-1':
            fixtures = [
                ('web-gate-file.pdf', 'application/pdf', b'%PDF synthetic web download', True),
                ('web-gate-file.zip', 'application/zip', b'PK\x03\x04 synthetic web download', True),
                ('web-gate-expired.pdf', 'application/pdf', b'%PDF synthetic missing download', False),
            ]
            for index, (filename, mime, body, present) in enumerate(fixtures, start=100):
                digest = hashlib.sha256(body).hexdigest()
                if present:
                    path = Path(blob_root) / digest[:2] / digest
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(body)
                event = _event(stream_id, 'ASSIST_TEXT', '', index)
                event['attachments'] = [dict(key=digest, mime=mime, size=len(body), filename=filename)]
                if await store.append_session_event(stream_id, event, identity=_identity_key(event), limit=500):
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
    parser.add_argument("--blob-root")
    args = parser.parse_args()
    result = asyncio.run(seed(args.db, args.host, args.session, args.objective, args.token_file, args.blob_root))
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
