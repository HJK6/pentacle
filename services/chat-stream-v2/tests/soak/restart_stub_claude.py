#!/usr/bin/env python3
"""Stub Claude CLI for the daemon restart-continuity matrix.

Launched through the real tuple path (`--claude-bin`), so it receives the same
argv as Claude (`--session-id <id>` ...) inside a real tmux pane. It renders
the bypass-mode composer chrome the readiness predicate expects, accepts one
bracketed paste + Enter per submission, and appends a USER record to the
transcript the daemon recorded for this seat (held open, as Claude does).

Control is file based so the harness can drive each stage deterministically:

  $RESTART_STUB_CONTROL/mode       normal | hold_ready | withhold_user
  $RESTART_STUB_CONTROL/release    created by the harness to end a hold
  $RESTART_STUB_CONTROL/<session>.booting   written while holding readiness
  $RESTART_STUB_CONTROL/<session>.pasted    written when a submission is held
  $RESTART_STUB_CONTROL/<session>.inputs    one JSON line per received submission
"""
from __future__ import annotations

import json
import os
import re
import sys
import termios
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

CONTROL = Path(os.environ["RESTART_STUB_CONTROL"])
TRANSCRIPT_DIR = Path(os.environ["RESTART_STUB_TRANSCRIPT_DIR"])
SESSION = os.environ.get("PENTACLE_TMUX_SESSION") or os.environ.get("PENTACLE_STREAM_ID", "unknown").split(":")[-1]
_ESC = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _mode() -> str:
    try:
        return (CONTROL / "mode").read_text().strip() or "normal"
    except OSError:
        return "normal"


def _wait_release() -> None:
    while not (CONTROL / "release").exists():
        time.sleep(0.05)


def _chrome() -> None:
    sys.stdout.write("\n" + "─" * 40 + "\n❯\n" + "─" * 40 + "\n  ⏵⏵ bypass permissions on\n")
    sys.stdout.flush()


def main() -> int:
    argv = sys.argv[1:]
    session_id = argv[argv.index("--session-id") + 1] if "--session-id" in argv else uuid.uuid4().hex
    try:
        attrs = termios.tcgetattr(0)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(0, termios.TCSANOW, attrs)
    except termios.error:
        pass
    mode = _mode()
    if mode == "hold_ready":
        (CONTROL / f"{SESSION}.booting").write_text(str(os.getpid()))
        sys.stdout.write("Claude stub booting...\n")
        sys.stdout.flush()
        _wait_release()
    _chrome()
    transcript = None
    for raw in sys.stdin:
        text = _ESC.sub("", raw).replace("\r", "").strip()
        if not text:
            continue
        with open(CONTROL / f"{SESSION}.inputs", "a", encoding="utf-8") as log:
            log.write(json.dumps({"at": time.time(), "pid": os.getpid(), "text": text}) + "\n")
        if mode == "withhold_user":
            (CONTROL / f"{SESSION}.pasted").write_text(text)
            _wait_release()
        if transcript is None:
            TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
            transcript = open(TRANSCRIPT_DIR / f"{session_id}.jsonl", "a", encoding="utf-8")
        transcript.write(json.dumps({
            "type": "user", "sessionId": session_id, "uuid": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "cwd": os.getcwd(), "message": {"role": "user", "content": text},
        }) + "\n")
        transcript.flush()
        sys.stdout.write(f"> {text[:60]}\n")
        _chrome()
    while True:  # keep the seat alive like an idle TUI after stdin closes
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main())
