"""Read the blocking runner's manual quarantine selectors without executing tests."""
from __future__ import annotations

import base64
from datetime import date
import json
from pathlib import Path
import subprocess


QUARANTINE_PATH = "scripts/quarantine-v2.json"


def effective_tests(checkout):
    root = Path(checkout)
    if (root / ".git").exists():
        # A lagging checkout may still contain only the legacy list.
        from .reliability_hosted import download
        try:
            response = json.loads(download("HJK6/pentacle", f"contents/{QUARANTINE_PATH}?ref=main"))
        except subprocess.TimeoutExpired as exc:
            raise ValueError("effective quarantine GET timed out") from exc
        if (not isinstance(response, dict) or response.get("encoding") != "base64"
                or not isinstance(response.get("content"), str)):
            raise ValueError("invalid effective quarantine response")
        data = json.loads(base64.b64decode("".join(response["content"].split()), validate=True))
    else:
        try:
            data = json.loads((root / QUARANTINE_PATH).read_text())
        except FileNotFoundError as exc:
            raise ValueError("effective quarantine list unavailable") from exc
    return validate_quarantine(data)


def validate_quarantine(data):
    """Validate the same v2 identities as the candidate selector.

    Expired rows remain visible as open repair signals; the gate rejects them.
    """
    tests = data.get("nonblocking_tests") if isinstance(data, dict) else None
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 2 or not isinstance(tests, list):
        raise ValueError("invalid effective quarantine list")
    seen = set()
    for row in tests:
        if (not isinstance(row, dict) or set(row) != {"id", "owner", "reason", "expires"}
                or any(not isinstance(row[k], str) or not row[k].strip() or row[k] != row[k].strip() for k in row)):
            raise ValueError("invalid effective quarantine row")
        identity = row["id"]
        prefix, _, rest = identity.partition("::")
        file, separator, node = rest.partition("::")
        if (prefix not in ("daemon", "cli", "shared") or not separator or not file.endswith(".py")
                or not node or file.startswith("/") or ".." in Path(file).parts or identity in seen):
            raise ValueError("invalid effective quarantine ID")
        if date.fromisoformat(row["expires"]).isoformat() != row["expires"]:
            raise ValueError("invalid effective quarantine expiry")
        seen.add(identity)
    return seen
