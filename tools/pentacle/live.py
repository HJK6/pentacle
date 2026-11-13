"""Run the opt-in functional probe in the installation's own interpreter."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def probe(snapshot):
    from .discovery import run
    providers = [("claude", "--claude-bin"), ("codex", "--codex-bin"),
                 ("gemini", "--gemini-bin"), ("cursor", "--cursor-agent-bin")]
    proc = snapshot.daemon_process
    if proc is None:
        return False
    provider = next((name for name, option in providers if proc.option(option)), None)
    if provider is None:
        return False
    result = run([snapshot.python, Path(__file__).resolve(), provider], timeout=155,
                 cwd=snapshot.root, env=snapshot.cli_environment())
    try:
        value = json.loads(result.stdout)
        return result.returncode == 0 and value == {"replied": True, "closed": True}
    except ValueError:
        return False


def _main(provider):
    import asyncio
    import uuid
    from agent_orch.config import load_config
    from agent_orch.wsclient import close_once, inspect_stream_once, spawn_catalog_get_once, spawn_once

    config = load_config()
    deadline = time.monotonic() + 120
    stream_id = None
    replied, closed = False, False
    request_id = "doctor-" + uuid.uuid4().hex
    try:
        catalog = asyncio.run(spawn_catalog_get_once(config, timeout=10))
        profile = catalog.get("profiles", {}).get("agent_orch", {}).get(provider)
        payload = {"host": config.host_id, "provider": provider, "parent_stream_id": None,
                   "role": None, "phase": None, "visibility": "hidden", "request_id": request_id,
                   "idempotency_key": request_id, "initial_prompt": "Reply with exactly PENTACLE_DOCTOR_OK.",
                   "objective": "Pentacle functional health check"}
        if isinstance(profile, list) and len(profile) == 2:
            payload.update(model=profile[0], effort=profile[1])
        response = asyncio.run(spawn_once(config, payload, timeout=min(50, max(1, deadline - time.monotonic()))))
        stream_id = response.get("stream_id") or response.get("session", {}).get("stream_id")
        if not stream_id:
            return replied, closed
        while time.monotonic() < deadline:
            value = asyncio.run(inspect_stream_once(config, stream_id, event_tail=30,
                              timeout=min(10, max(0.1, deadline - time.monotonic()))))
            for event in value.get("recent_events", []):
                if event.get("kind") == "ASSIST_TEXT" and "PENTACLE_DOCTOR_OK" in str(event.get("text") or ""):
                    replied = True
                    break
            if replied:
                break
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    finally:
        if stream_id:
            try:
                response = asyncio.run(close_once(config, stream_id, reason="pentacle_doctor_cleanup",
                                                   operator_confirm=True, force=True, timeout=20))
                closed = response.get("type") == "close.ok"
            except Exception:
                closed = False
        print(json.dumps({"replied": replied, "closed": closed}))
    return replied, closed


if __name__ == "__main__":
    try:
        replied, closed = _main(sys.argv[1])
        raise SystemExit(0 if replied and closed else 1)
    except Exception:
        # No provider text, session contents or connection details are forwarded.
        raise SystemExit(1)
