"""Versioned daemon identity shared by the daemon, CLI, and web build."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

_IDENTITY = json.loads(Path(__file__).with_name("daemon_identity.json").read_text(encoding="utf-8"))
SCHEMA_VERSION = _IDENTITY["schema_version"]
PROTOCOL_VERSION = _IDENTITY["protocol_version"]
MIN_PROTOCOL_VERSION = _IDENTITY["min_protocol_version"]
MAX_PROTOCOL_VERSION = _IDENTITY["max_protocol_version"]

if (type(SCHEMA_VERSION) is not int or type(PROTOCOL_VERSION) is not int
        or type(MIN_PROTOCOL_VERSION) is not int or type(MAX_PROTOCOL_VERSION) is not int
        or not MIN_PROTOCOL_VERSION <= PROTOCOL_VERSION <= MAX_PROTOCOL_VERSION):
    raise RuntimeError("invalid daemon identity manifest")


def release_sha(root: str | Path | None = None) -> str:
    """Resolve checkout, deploy-stamp, or shipped VERSION identity in that order."""
    checkout = Path(root).resolve() if root is not None else Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is not None and result.returncode == 0:
        value = result.stdout.strip()
        if len(value) == 40 and all(char in "0123456789abcdef" for char in value):
            return value

    stamp_path = os.environ.get("PENTACLE_DEPLOY_STAMP_PATH", "")
    if stamp_path:
        try:
            stamp = json.loads(Path(stamp_path).read_text(encoding="utf-8"))
            value = stamp.get("sha", "") if isinstance(stamp, dict) else ""
            if isinstance(value, str) and len(value) == 40 and all(c in "0123456789abcdef" for c in value):
                return value
        except (OSError, ValueError):
            pass

    try:
        value = (checkout / "VERSION").read_text(encoding="utf-8").strip().splitlines()[0]
    except (OSError, IndexError):
        return ""
    return value if len(value) == 40 and all(c in "0123456789abcdef" for c in value) else ""


def identity(release: str | None = None) -> dict[str, int | str]:
    return {
        "release_sha": release if release is not None else release_sha(),
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "min_protocol_version": MIN_PROTOCOL_VERSION,
        "max_protocol_version": MAX_PROTOCOL_VERSION,
    }
