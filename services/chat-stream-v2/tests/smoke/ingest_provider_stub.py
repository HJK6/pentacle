#!/usr/bin/env python3
"""Self-contained provider tuple-launch actor for public smoke tests."""

import datetime
import json
import os
from pathlib import Path
import sys
import uuid


def _argument_value(name: str) -> str:
    try:
        index = sys.argv.index(name) + 1
        return sys.argv[index]
    except (ValueError, IndexError):
        raise SystemExit(f"missing required argument: {name}") from None


native_id = _argument_value("--session-id")
root = os.environ.get("CHAT_STREAM_STUB_ROOT")
if not root:
    raise SystemExit("CHAT_STREAM_STUB_ROOT is required")

workspace = os.path.realpath(os.getcwd())
slug = workspace.replace("/", "-").replace("_", "-").replace(".", "-")
safe_id = "".join(character if character.isalnum() or character in "-_" else "_" for character in native_id)
if not safe_id:
    raise SystemExit("session id must contain a filename-safe character")

path = Path(root) / slug / (safe_id + ".jsonl")
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("a", buffering=1) as transcript:
    print("READY\n⏵⏵ bypass permissions on (public fixture)\n❯ ", flush=True)
    for line in sys.stdin:
        line = line.rstrip("\r\n")
        if line:
            transcript.write(json.dumps({
                "type": "user",
                "sessionId": native_id,
                "uuid": str(uuid.uuid4()),
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "message": {"role": "user", "content": line},
            }) + "\n")
            response = f"Public fixture assistant: {line}"
            transcript.write(json.dumps({
                "type": "assistant", "sessionId": native_id, "uuid": str(uuid.uuid4()),
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "message": {"role": "assistant", "content": [{"type": "text", "text": response}]},
            }) + "\n")
            print(f"ECHO {line}\n{response}\n⏵⏵ bypass permissions on (public fixture)\n❯ ", flush=True)
