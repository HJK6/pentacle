"""Small shared runtime parsing and timestamp helpers for daemon-v2."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any, TypeVar


T = TypeVar("T")
log = logging.getLogger("chat_streamd_v2.runtime")


def env_number(
    values: Mapping[str, Any],
    name: str,
    default: T,
    cast: Callable[[Any], T],
    *,
    prefix: str = "",
    positive: bool = False,
) -> T:
    raw = values.get(f"{prefix}{name}")
    if raw is None:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        log.warning("ignoring invalid numeric setting %s%s=%r", prefix, name, raw)
        return default
    if positive and not value > 0:  # type: ignore[operator]
        return default
    return value


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
