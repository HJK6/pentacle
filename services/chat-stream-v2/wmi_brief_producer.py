"""One privately bound daily-brief principal; no configurable capabilities."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from report_producer import private_file
from tmux_transport import sanitize_injectable

CONFIG_ENV = "PENTACLE_WMI_BRIEF_PRODUCER_CONFIG"
CONFIG_MAX_BYTES = 16 * 1024
BODY_MAX_BYTES = 128 * 1024
RATE_ATTEMPTS = 6
RATE_WINDOW_SECONDS = 60.0
_STREAM_ID = re.compile(r"[A-Za-z0-9._-]{1,64}:[A-Za-z0-9._-]{1,64}")
_ACTOR = re.compile(r"[A-Za-z0-9._-]{1,64}:wmi-daily-brief")
_ID = re.compile(r"[!-~]{1,120}")
_CONFIG_FIELDS = {"stream_id", "token_file", "composite_stream_id", "ops_stream_id"}
TELL_FIELDS = {"type", "request_id", "tell_id", "from_stream_id", "stream_token",
               "to_stream_id", "text", "ttl_seconds", "sanitize"}


def is_actor(value: object) -> bool:
    """Reserve the fixed session name even when private configuration is absent."""
    return isinstance(value, str) and _ACTOR.fullmatch(value) is not None


@dataclass(frozen=True)
class BriefProducer:
    stream_id: str
    token_file: str
    composite_stream_id: str
    ops_stream_id: str

    def valid_tell(self, msg: dict) -> bool:
        if set(msg) != TELL_FIELDS:
            return False
        body = msg.get("text")
        if (
            msg.get("from_stream_id") != self.stream_id
            or msg.get("to_stream_id") not in (self.composite_stream_id, self.ops_stream_id)
            or any(not isinstance(msg.get(key), str) or not _ID.fullmatch(msg[key])
                   for key in ("request_id", "tell_id"))
            or type(msg.get("ttl_seconds")) is not int or msg["ttl_seconds"] != 3600
            or msg.get("sanitize") is not True
            or not isinstance(body, str) or not body.startswith("GATE ")
        ):
            return False
        try:
            if len(body.encode("utf-8")) > BODY_MAX_BYTES:
                return False
        except UnicodeError:
            return False
        clean = sanitize_injectable(body)
        return clean.startswith("GATE ") and bool(clean[5:].strip())


def load() -> BriefProducer | None:
    raw_path = os.environ.get(CONFIG_ENV)
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if not private_file(path):
        return None
    try:
        if path.stat().st_size > CONFIG_MAX_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(value, dict) or set(value) != _CONFIG_FIELDS:
        return None
    if not all(isinstance(item, str) for item in value.values()):
        return None
    if (
        not is_actor(value["stream_id"])
        or not Path(value["token_file"]).expanduser().is_absolute()
        or any(not _STREAM_ID.fullmatch(value[key])
               for key in ("composite_stream_id", "ops_stream_id"))
        or len({value[key] for key in ("stream_id", "composite_stream_id", "ops_stream_id")}) != 3
        or any(is_actor(value[key]) for key in ("composite_stream_id", "ops_stream_id"))
    ):
        return None
    return BriefProducer(**value)
