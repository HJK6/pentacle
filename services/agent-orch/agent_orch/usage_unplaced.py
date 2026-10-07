"""`agent-orch usage --unplaced`: read-only view of v2_usage_unplaced.

Unplaced usage is never counted as measured, placed or priced. Record
refusals are listed per stream with their reasons; held-span loss rows are
summed by ``records_lost`` per reason; ``loss_conflict`` rows are listed as
unresolved with both payloads and contribute no count.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

DATA_DIR = "~/.local/share/pentacle-stream"


def summarize(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    out: dict[str, Any] = {"streams": {}, "losses": {}, "conflicts": []}
    if "v2_usage_unplaced" not in tables:
        return out
    for host, provider, native, record_key, stream_id, reason, detail in conn.execute(
        "SELECT host, provider, native_session_id, record_key, stream_id, reason, detail "
        "FROM v2_usage_unplaced ORDER BY host, record_key"
    ):
        payload = json.loads(detail) if detail else None
        if record_key.startswith("loss_conflict:"):
            out["conflicts"].append({
                "host": host, "loss_id": record_key.removeprefix("loss_conflict:"),
                "stored": (payload or {}).get("stored"), "pending": (payload or {}).get("pending"),
            })
        elif record_key.startswith("loss:"):
            records = int((payload or {}).get("records_lost") or 0)
            out["losses"][reason] = out["losses"].get(reason, 0) + records
        else:
            stream = out["streams"].setdefault(stream_id or f"{host}:?", {"count": 0, "reasons": {}})
            stream["count"] += 1
            stream["reasons"][reason] = stream["reasons"].get(reason, 0) + 1
    return out


def read(data_dir: str = DATA_DIR) -> dict[str, Any]:
    path = Path(data_dir).expanduser() / "sessions.db"
    if not path.is_file():
        raise FileNotFoundError(f"no daemon database at {path} (run on the daemon host)")
    conn = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        return summarize(conn)
    finally:
        conn.close()


def render(summary: dict[str, Any]) -> str:
    lines = ["unplaced records (never counted):"]
    if not summary["streams"]:
        lines.append("  none")
    for stream_id, stream in sorted(summary["streams"].items()):
        reasons = ", ".join(f"{reason}={count}" for reason, count in sorted(stream["reasons"].items()))
        lines.append(f"  {stream_id}: {stream['count']} ({reasons})")
    lines.append("held-span losses (records_lost by reason):")
    if not summary["losses"]:
        lines.append("  none")
    for reason, count in sorted(summary["losses"].items()):
        lines.append(f"  {reason}: {count}")
    lines.append(f"unresolved loss conflicts: {len(summary['conflicts'])}")
    for conflict in summary["conflicts"]:
        stored = (conflict["stored"] or {}).get("records_lost")
        pending = (conflict["pending"] or {}).get("records_lost")
        lines.append(f"  {conflict['host']} {conflict['loss_id']}: stored records_lost={stored} "
                     f"pending records_lost={pending} (no recovered count)")
    return "\n".join(lines)
