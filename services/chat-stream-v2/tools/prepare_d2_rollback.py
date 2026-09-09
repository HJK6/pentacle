#!/usr/bin/env python3
"""Cancel unsupported D2 work with candidate consumers stopped before rollback.

Only the sessions DB is modified. The separate question expiry/clear marker
must remain in notifications.db, including when rolling back to a prior binary.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(1, str(Path(__file__).resolve().parents[2]))
from store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error("--db must name the existing isolated or stopped deployment sessions database")
    store = Store(str(args.db))
    store.start()
    try:
        result = asyncio.run(store.cancel_watch_wake_for_rollback())
        print(json.dumps({"db": str(args.db.resolve()), **result}, sort_keys=True))
    finally:
        store.stop()


if __name__ == "__main__":
    main()
