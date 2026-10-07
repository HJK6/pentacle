#!/usr/bin/env python3
"""Seed dashboard-catalog and report assets into a fixture daemon's asset DB.

Gate-only helper (hermetic web gate and a private dashboard package's e2e).
Writes through the production AssetStore, so the stored form and validation
are exactly what `asset.publish` would produce; the running fixture daemon
reads the same SQLite file. Never point it at a real daemon's database.

  seed_dashboard_assets.py --assets-db DB catalog --spec-id S --stream H:N --file catalog.json
  seed_dashboard_assets.py --assets-db DB reports --spec-id S --stream H:N --rows rows.json
  seed_dashboard_assets.py --assets-db DB delete --spec-id S --asset-id ID
  seed_dashboard_assets.py --assets-db DB raw-catalog-body --spec-id S --file body.txt

`raw-catalog-body` overwrites the stored body of an existing catalog row
WITHOUT validation, so the gate can show clients a malformed catalog that the
daemon would refuse on publish.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services"))

from _shared.assets_store import AssetStore  # noqa: E402


def _report_body(title: str) -> str:
    return json.dumps({
        "schema_version": 1,
        "title": title,
        "sections": [{"id": "summary", "title": "Summary", "status": "reference", "blocks": [
            {"id": "body", "type": "para", "runs": [f"Synthetic report {title}."]}]}],
    })


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-db", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    catalog = sub.add_parser("catalog")
    catalog.add_argument("--spec-id", required=True)
    catalog.add_argument("--stream", required=True)
    catalog.add_argument("--file", required=True)
    reports = sub.add_parser("reports")
    reports.add_argument("--spec-id", required=True)
    reports.add_argument("--stream", required=True)
    reports.add_argument("--rows", required=True, help="JSON list of {asset_id, producer[, title]}")
    raw = sub.add_parser("raw-catalog-body")
    raw.add_argument("--spec-id", required=True)
    raw.add_argument("--file", required=True)
    delete = sub.add_parser("delete")
    delete.add_argument("--spec-id", required=True)
    delete.add_argument("--asset-id", required=True)
    args = parser.parse_args(argv)

    store = AssetStore(args.assets_db)
    try:
        if args.command == "catalog":
            host, _, session = args.stream.partition(":")
            record = store.publish_asset(
                host=host, session_name=session, stream_id=args.stream,
                asset_id="dashboard-catalog", title="Dashboard catalog",
                content_type="dashboard-catalog",
                body=Path(args.file).read_text(encoding="utf-8"), spec_id=args.spec_id)
            print(json.dumps({"asset_id": record["asset_id"], "stream_id": record["stream_id"]}))
        elif args.command == "reports":
            host, _, session = args.stream.partition(":")
            for row in json.loads(Path(args.rows).read_text(encoding="utf-8")):
                title = row.get("title") or row["asset_id"]
                store.publish_asset(
                    host=host, session_name=session, stream_id=args.stream,
                    asset_id=row["asset_id"], title=title, content_type="report",
                    body=_report_body(title), producer=row["producer"], spec_id=args.spec_id)
            print(json.dumps({"seeded": True}))
        elif args.command == "raw-catalog-body":
            import sqlite3
            with sqlite3.connect(args.assets_db) as conn:
                changed = conn.execute(
                    "UPDATE assets SET body = ? WHERE spec_id = ? AND asset_id = 'dashboard-catalog'",
                    (Path(args.file).read_text(encoding="utf-8"), args.spec_id)).rowcount
            if changed != 1:
                raise SystemExit(f"expected one catalog row, updated {changed}")
            print(json.dumps({"raw_catalog_body": True}))
            return 0
        else:
            for record in store.find_assets_by_id(args.asset_id):
                if record.get("spec_id") == args.spec_id:
                    store.delete_asset(host=record["host"], session_name=record["session_name"],
                                       asset_id=record["asset_id"])
            print(json.dumps({"deleted": args.asset_id}))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
