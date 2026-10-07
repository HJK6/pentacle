#!/usr/bin/env python3
"""Backfill usage provenance for the ledger host's own transcripts (Thoth).

Satellites backfill with ``satellite.py --backfill`` over ``event.push``. The
ledger host has no satellite, so this tool feeds the same ``ProvenanceSink``
directly against ``sessions.db`` (one short ``BEGIN IMMEDIATE`` transaction per
batch, safe beside the running daemon). It never creates or migrates tables:
the deployed daemon must already have opened the database.

    python3 tools/backfill_usage_provenance.py --db ~/.local/share/pentacle-stream/sessions.db \
        --host thoth --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from store_usage import record_provenance_conn  # noqa: E402
from usage_history import HISTORY_FILENAME, HistoryLog  # noqa: E402
from usage_provenance import (  # noqa: E402
    BACKFILL_BATCH, DEFAULT_CLAUDE_ROOT, DEFAULT_CODEX_ROOT, PAYLOAD_VERSION,
    BackfillCursor, ProvenanceSink, run_backfill,
)

REQUIRED_TABLES = frozenset({"v2_usage_records", "v2_usage_provenance", "v2_usage_identity", "v2_usage_codex_responses",
                             "v2_usage_codex_thread_proof", "v2_usage_codex_thread_flags"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--host", required=True, help="the ledger host name these transcripts belong to")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--provider", choices=("all", "claude", "codex"), default="all")
    parser.add_argument("--claude-root", default=DEFAULT_CLAUDE_ROOT)
    parser.add_argument("--codex-root", default=DEFAULT_CODEX_ROOT)
    parser.add_argument("--cursor", default="~/.local/state/pentacle-stream/provenance_backfill.json")
    parser.add_argument("--no-cursor", action="store_true")
    parser.add_argument("--batch", type=int, default=BACKFILL_BATCH)
    args = parser.parse_args(argv)

    db = args.db.expanduser()
    if not db.exists():
        print(f"no database at {db}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(db.absolute().as_uri() + "?mode=rw", uri=True, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not REQUIRED_TABLES <= tables:
        print("provenance tables absent: deploy the daemon first", file=sys.stderr)
        return 2

    async def record(host, items, *, dry_run=False, version=1):
        return record_provenance_conn(conn, host, items, dry_run=dry_run, version=version)

    sink = ProvenanceSink(record, HistoryLog(db.with_name(HISTORY_FILENAME)))

    async def push(items, dry_run):
        return await sink.admit(args.host, {"version": PAYLOAD_VERSION, "items": items, "dry_run": dry_run})

    roots = {}
    if args.provider in ("all", "claude"):
        roots["claude"] = args.claude_root
    if args.provider in ("all", "codex"):
        roots["codex"] = args.codex_root
    try:
        summary = asyncio.run(run_backfill(
            push, roots=roots, cursor=BackfillCursor(None if args.no_cursor else args.cursor),
            dry_run=args.dry_run, batch=args.batch,
        ))
    finally:
        conn.close()
    summary["host"] = args.host
    print(json.dumps(summary, sort_keys=True))
    return 1 if any(p.get("batches_failed") for p in summary["providers"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
