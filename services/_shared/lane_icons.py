"""Lane icon keys and ordered title fallback from the shared chat-core table."""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

TABLE = json.loads((Path(__file__).resolve().parents[2] / "pentacle-chat-core/lane_icons.json").read_text())
LANE_ICON_KEYS = frozenset(item["key"] for item in TABLE["icons"])
log = logging.getLogger(__name__)


def validate_icon(value: str | None) -> str | None:
    if value is not None and (not isinstance(value, str) or value not in LANE_ICON_KEYS):
        raise ValueError("work_lane_icon_invalid")
    return value


@lru_cache(maxsize=128)
def _warn_unknown(value: str) -> None:
    log.warning("unknown legacy work lane icon; projecting null")


def resolve_icon(value: str | None, title: str | None) -> str | None:
    if value is not None:
        if value in LANE_ICON_KEYS:
            return value
        _warn_unknown(str(value))
        return None
    title = (title or "").lower()
    return next((item["key"] for item in TABLE["icons"]
                 if any(word in title for word in item["keywords"])), None)
