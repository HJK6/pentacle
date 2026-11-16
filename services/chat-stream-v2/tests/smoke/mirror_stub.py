"""Provider stand-in that also drives the capture-derived working state.

It prints a boot marker and echoes submitted lines (so the
send receipt path still works), but it additionally reacts to two control
words so the mirror pump has a real pane-spinner to observe:

  "work" -> echo it, then print the "Working (Ns)" spinner line a provider
            shows during an active turn. `mirror._extract_live_state` reads that
            as working=True.
  "idle" -> echo it, then print enough plain lines to push the spinner out of
            the capture window, so the pump reads working=False.

A stub cannot reproduce a full TUI, but the "Working (Ns)" line is the exact
literal `_extract_live_state` keys on, so the working-state PATH is exercised
end to end (capture -> parse -> tracker -> working.state frame).
"""

from __future__ import annotations

import sys


def main() -> int:
    print("READY", flush=True)
    for raw in sys.stdin:
        line = raw.rstrip("\r\n")
        if not line:
            continue
        print(f"ECHO {line}", flush=True)  # delivery receipt, always
        if line == "work":
            print("Working (3s · thinking)", flush=True)
        elif line == "idle":
            for _ in range(9):
                print("idle-line", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
