#!/usr/bin/env python3
"""One-shot bounce-A open-question clear (D3, epic_example_2026_01).

The Nexus runs this inside the coordinated bounce-A window, against the deployed
``notifications.db``, BEFORE the daemon at ``A_SHA`` begins admitting new asks.
It expires every stored OPEN ``agent_question`` (including NULL producers and
legacy ack) and its paired notification through the store's shared terminalization
path, then stamps the marker ``daemon_updates_A_question_clear_v1`` in the SAME
transaction. It is idempotent: once the marker exists, re-running is a no-op and
post-clear questions are never swept. Terminal answers/history are preserved.

Usage:
  # Dry run: count what WOULD clear; writes nothing, stamps no marker.
  python3 question_clear_bounce_a.py --db <notifications.db> --dry-run

  # Execute the one-shot clear (idempotent).
  python3 question_clear_bounce_a.py --db <notifications.db>

Exit code is 0 on success (including an idempotent no-op) and 1 on error. The
result is printed as one JSON object; keep it as the bounce-A count receipt.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SERVICES_ROOT = Path(__file__).resolve().parents[2]
if str(SERVICES_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICES_ROOT))

from _shared.notifications_store import (  # noqa: E402
    QUESTION_CLEAR_MARKER_A,
    open_store,
)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounce-A one-shot open-question clear")
    parser.add_argument("--db", required=True, help="path to the deployed notifications.db")
    parser.add_argument("--marker", default=QUESTION_CLEAR_MARKER_A,
                        help="one-shot marker (default: the bounce-A marker)")
    parser.add_argument("--cutoff", default=None,
                        help="ISO-8601 cutoff; only questions created at/before it clear "
                             "(default: now)")
    parser.add_argument("--dry-run", action="store_true",
                        help="count what would clear; write nothing and stamp no marker")
    args = parser.parse_args(argv)

    store = open_store(args.db)
    try:
        result = store.clear_open_agent_questions_once(
            args.marker, cutoff=args.cutoff, dry_run=bool(args.dry_run),
        )
    finally:
        store.close()
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except Exception as exc:  # noqa: BLE001 - surface a clean non-zero to the operator
        print(json.dumps({"error": str(exc)}, separators=(",", ":")), file=sys.stderr)
        raise SystemExit(1)
