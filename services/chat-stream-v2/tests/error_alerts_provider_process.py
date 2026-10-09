#!/usr/bin/env python3
"""Deterministic terminal counterpart. No model, network, or daemon imports."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import termios
import tty
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--native-id", required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    transcript = root / ".codex" / "sessions" / (args.native_id + ".jsonl")
    transcript.parent.mkdir(parents=True, exist_ok=True)
    previous = termios.tcgetattr(sys.stdin.fileno())
    tty.setraw(sys.stdin.fileno())
    prompt = "\r\nOpenAI Codex\r\n─────────\r\n› \r\n  gpt-6-astra high\r\n"
    try:
        with transcript.open("a", buffering=1) as events, (
            root / "provider-input.jsonl"
        ).open("a", buffering=1) as received:
            events.write(
                json.dumps({"type": "session_meta", "payload": {"id": args.native_id}})
                + "\n"
            )
            print("\x1b[?2004h" + prompt, end="", flush=True)
            buffer = bytearray()
            pasted = False
            while True:
                byte = os.read(sys.stdin.fileno(), 1)
                if not byte:
                    return
                buffer.extend(byte)
                if buffer.endswith(b"\x1b[200~"):
                    del buffer[-6:]
                    pasted = True
                elif buffer.endswith(b"\x1b[201~"):
                    del buffer[-6:]
                    pasted = False
                elif byte in (b"\r", b"\n") and not pasted:
                    text = buffer.decode("utf-8", errors="strict").strip("\r\n")
                    buffer.clear()
                    if not text:
                        continue
                    stamp = datetime.now(timezone.utc).isoformat()
                    identity = str(uuid.uuid4())
                    received.write(
                        json.dumps({"id": identity, "at": stamp, "text": text}) + "\n"
                    )
                    events.write(
                        json.dumps(
                            {
                                "type": "response_item",
                                "timestamp": stamp,
                                "payload": {
                                    "type": "message",
                                    "id": identity,
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": text}],
                                },
                            }
                        )
                        + "\n"
                    )
                    events.flush()
                    os.fsync(events.fileno())
                    received.flush()
                    os.fsync(received.fileno())
                    print(
                        "\r\n" + text.replace("\n", "\r\n") + prompt, end="", flush=True
                    )
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, previous)


if __name__ == "__main__":
    main()
