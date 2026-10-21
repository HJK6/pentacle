"""Label operator-authenticated desk sends with `[from <desk>]`.

A small private map names the registry credentials that belong to desks. The
daemon prefixes their sends so a receiver reads them as relayed content, and
removing an entry (or the file) stops the stamp on the next send. This is
attribution and per-credential revocation, not a reduction in privilege.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from report_producer import private_file
from tmux_transport import sanitize_injectable

CONFIG_ENV = "PENTACLE_DESK_STAMP_CONFIG"
CONFIG_MAX_BYTES = 4 * 1024
MAX_ENTRIES = 8
_LABEL = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*-desk")
_BODY_FIELDS = ("text", "message")


def load() -> dict[str, str]:
    """Credential id -> label; an invalid file or entry stamps nothing."""
    raw_path = os.environ.get(CONFIG_ENV)
    if not raw_path:
        return {}
    path = Path(raw_path).expanduser()
    if not private_file(path):
        return {}
    try:
        if path.stat().st_size > CONFIG_MAX_BYTES:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    if not isinstance(value, dict) or len(value) > MAX_ENTRIES:
        return {}
    labels: dict[str, str] = {}
    for key, label in value.items():
        try:
            credential_id = str(uuid.UUID(str(key)))
        except ValueError:
            continue
        if isinstance(label, str) and _LABEL.fullmatch(label) and len(label) <= 32:
            labels[credential_id] = label
    return labels


def stamp_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Return `msg` with its body stamped when it came from a mapped desk credential."""
    auth = msg.get("_auth_context")
    if not isinstance(auth, dict) or auth.get("operator_authenticated") is not True:
        return msg
    principal = str(auth.get("operator_principal") or "")
    if not principal.startswith("operator:"):
        return msg
    try:
        credential_id = str(uuid.UUID(principal[len("operator:"):]))
    except ValueError:
        return msg
    label = load().get(credential_id)
    if not label:
        return msg
    stamped = dict(msg)
    for field in _BODY_FIELDS:
        body = msg.get(field)
        if isinstance(body, str) and body:
            stamped[field] = f"[from {label}]\n{sanitize_injectable(body)}"
    return stamped
