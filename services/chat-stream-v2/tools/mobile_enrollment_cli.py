#!/usr/bin/env python3
"""Create a short-lived, single-use mobile enrollment link on the daemon host."""

from __future__ import annotations

import argparse
import json
import secrets
import string
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

SERVICE_DIR = Path(__file__).resolve().parents[1]
for directory in (SERVICE_DIR.parent, SERVICE_DIR):
    sys.path.insert(0, str(directory))

from _shared import operator_auth
from enrollment import EnrollmentRegistry
from v2_runtime import iso_now


def issue_link(ws_url: str, *, label: str = "phone", ttl_seconds: int = 600,
               codes_path: Path | None = None) -> dict[str, object]:
    endpoint = urlsplit(ws_url)
    if (endpoint.scheme not in {"ws", "wss"} or not endpoint.hostname
            or endpoint.username is not None or endpoint.password is not None
            or endpoint.fragment or any(c.isspace() for c in ws_url)):
        raise ValueError("--ws-url must be a ws:// or wss:// endpoint without credentials or a fragment")
    _ = endpoint.port  # Reject malformed ports before writing a code.
    if not 1 <= ttl_seconds <= 3600:
        raise ValueError("--ttl-seconds must be between 1 and 3600")
    registry = EnrollmentRegistry(codes_path=codes_path)
    path = registry.codes_path
    with operator_auth.file_lock(path.with_suffix(path.suffix + ".lock")):
        codes = registry._load_codes()
        alphabet = string.ascii_uppercase + "23456789"
        while True:
            code = "".join(secrets.choice(alphabet) for _ in range(8))
            if code not in codes:
                break
        expires_at = time.time() + ttl_seconds
        codes[code] = {
            "created_at": iso_now(), "expires_at": expires_at, "used_at": "",
            "label": label, "protocol_version": 2,
            "scheme": operator_auth.AUTH_SCHEME, "client_kind": "pentacle-mobile",
            "replaces_credential_id": None,
        }
        operator_auth.atomic_write_json(path, codes)
    return {
        "url": "pentacle://enroll?" + urlencode({"code": code, "ws": ws_url}),
        "expires_at": expires_at,
        "label": label,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ws-url", required=True, help="daemon endpoint reachable by the phone or simulator")
    parser.add_argument("--label", default="phone")
    parser.add_argument("--ttl-seconds", type=int, default=600)
    args = parser.parse_args()
    try:
        result = issue_link(args.ws_url, label=args.label, ttl_seconds=args.ttl_seconds)
    except (ValueError, operator_auth.OperatorRegistryUnavailable) as exc:
        parser.exit(1, f"Enrollment link creation failed: {exc}\n")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
