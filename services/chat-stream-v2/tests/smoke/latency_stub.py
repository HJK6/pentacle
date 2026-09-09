#!/usr/bin/env python3
"""Deterministic provider stand-in for a bounded send-and-observe harness.

Each submitted line is delivery-confirmed by an echo, then holds an actual
provider-style spinner long enough for the normal observation path to
take over before returning to an idle capture. It is a dedicated test target,
never an interactive user pane.
"""

from __future__ import annotations

import sys
import threading
import time


def _run_turn() -> None:
    # Default Mirror cadence is one second. Advance the reported elapsed label
    # so the unmodified baseline emits a positive working.state before the
    # bounded tail returns to idle.
    # Hold past the five-second tracker heartbeat: the unfixed daemon emits an
    # initial 0 ms frame from its first capture, then needs that heartbeat for
    # the first positive frame the desktop is allowed to anchor.
    for elapsed in range(1, 7):
        time.sleep(1)
        print(f"Working ({elapsed}s · thinking)", flush=True)
    for _ in range(9):
        print("idle-line", flush=True)


def main() -> int:
    print("READY", flush=True)
    print("⏵⏵ bypass permissions on (latency stub)", flush=True)
    print("❯ ", flush=True)
    for raw in sys.stdin:
        line = raw.rstrip("\r\n")
        if not line:
            continue
        print(f"ECHO {line}", flush=True)
        print("Working (0s · thinking)", flush=True)
        threading.Thread(target=_run_turn, daemon=True).start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
