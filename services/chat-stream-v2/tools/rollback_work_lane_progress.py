#!/usr/bin/env python3
"""Reverse only the work-lane progress event CHECK in an owned offline DB.

The default is a read-only plan. Apply after stopping the owning daemon and
retaining its SQLite backup; never replace the current DB with an old backup.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from urllib.parse import quote

SERVICE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SERVICE), str(SERVICE.parent)]
from store_work_lanes import WORK_LANE_EVENTS_DDL
from work_lane_migration import rollback_progress

PREVIOUS_DDL = WORK_LANE_EVENTS_DDL.replace(
    "'lead_handoff','set_members','item_change'", "'lead_handoff'")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-offline", action="store_true")
    args = parser.parse_args(argv)
    if args.apply and not args.confirm_offline:
        parser.error("--apply requires --confirm-offline and an independently retained SQLite backup")
    mode = "rw" if args.apply else "ro"
    conn = sqlite3.connect("file:" + quote(str(args.db.resolve())) + "?mode=" + mode, uri=True)
    try:
        if args.apply:
            conn.execute("BEGIN IMMEDIATE")
            try:
                count = rollback_progress(conn, PREVIOUS_DDL)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        else:
            count = conn.execute("SELECT count(*) FROM v2_work_lane_events WHERE operation IN ('set_members','item_change')").fetchone()[0]
        print(json.dumps({"applied": args.apply, "archived_events": count,
                          "preserved": ["current unrelated rows", "publication links", "confirmation indexes",
                                        "member and observation snapshots", "archived replay receipts"]}))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
