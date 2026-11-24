#!/usr/bin/env python3
"""Print the candidate daemon release, schema, and protocol identity."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import daemon_identity


def main() -> int:
    result = daemon_identity.identity(daemon_identity.release_sha())
    print(json.dumps(result, sort_keys=True))
    return 0 if result["release_sha"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
