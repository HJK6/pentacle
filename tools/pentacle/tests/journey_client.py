"""Private fixture conversation client, run in the installed interpreter."""
import asyncio
import json
import sys
import time
import uuid

from agent_orch.config import load_config
from agent_orch.wsclient import _operator_envelope_rpc, inspect_stream_once, spawn_catalog_get_once, spawn_once


async def run(mode, stream, marker):
    config = load_config()
    if mode == "spawn":
        catalog = await spawn_catalog_get_once(config, timeout=10)
        profile = catalog.get("profiles", {}).get("agent_orch", {}).get("claude")
        payload = {"host": "local", "provider": "claude", "parent_stream_id": None, "role": None,
                   "visibility": "hidden", "initial_prompt": marker, "request_id": "fixture-" + uuid.uuid4().hex}
        if isinstance(profile, list) and len(profile) == 2:
            payload.update(model=profile[0], effort=profile[1])
        result = await spawn_once(config, payload, timeout=60)
        stream = result.get("stream_id") or result.get("session", {}).get("stream_id")
        if not stream:
            raise RuntimeError("Fixture spawn did not return a stream")
    elif mode in {"send", "close"}:
        host, session = stream.split(":", 1)
        payload = {"type": mode, "host": host, "session_name": session,
                   "request_id": "fixture-" + uuid.uuid4().hex}
        if mode == "close":
            payload.update(operator_confirm=True, force=True, reason="fixture_cleanup")
        else:
            payload["text"] = marker
        result = await _operator_envelope_rpc(config, payload, caller=None, prefix=mode, timeout=30)
        if result.get("type") != mode + ".ok":
            raise RuntimeError("Fixture operation was refused: " + str(result.get("error_code")))
        if mode == "close":
            return {"stream": stream, "closed": True}
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        value = await inspect_stream_once(config, stream, event_tail=100, timeout=10)
        texts = [event.get("text", "") for event in value.get("recent_events", []) if event.get("kind") == "ASSIST_TEXT"]
        if any(marker in text for text in texts):
            return {"stream": stream, "replied": True, "marker": marker}
        await asyncio.sleep(0.5)
    raise RuntimeError("Fixture reply or retained history was not observed")


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run(*sys.argv[1:]))))
