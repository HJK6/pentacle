#!/usr/bin/env python3
"""tmux `has-session` barrier for the pre-intent SIGTERM cell.

Invoked by the cell's tmux wrapper for the target seat's `has-session` while
`control/hold_has_session` names that seat. It holds only once the seat's own
empty reservation exists (the call inside the protected spawn body, after the
claim and before intent submission); an earlier liveness probe passes through.
Holding acknowledges with this process's pid and request id, then blocks on
`signal.pause()` until the daemon's cancellation kills it. Any other call
execs the real tmux unchanged.
Usage: hold_has_session.py <db> <control-dir> <real-tmux> <socket> <tmux args...>
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
from pathlib import Path


def main() -> None:
    db, control, real_tmux, socket, *args = sys.argv[1:]
    control_dir = Path(control)
    target = (control_dir / "hold_has_session").read_text().strip() if (control_dir / "hold_has_session").exists() else ""
    ack = control_dir / "has_session_held"
    row = None
    if target and args[-1] == f"={target}:" and not ack.exists():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute("SELECT request_id, payload FROM v2_stream_reservations "
                               "WHERE session_name=?", (target,)).fetchone()
        except sqlite3.OperationalError:
            row = None
        finally:
            conn.close()
    if row is not None and row[1] is None:
        tmp = ack.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "request_id": row[0], "args": args}))
        tmp.replace(ack)
        while True:
            signal.pause()
    os.execv(real_tmux, [real_tmux, "-S", socket, *args])


if __name__ == "__main__":
    main()
