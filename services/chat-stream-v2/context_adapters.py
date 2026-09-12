"""Pure provider adapters for routing integrity and context usage.

These helpers validate provider-native readings and classify context pressure.
They perform no I/O or persistence; malformed input remains inconclusive.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
import re
from typing import Any
from v2_runtime import env_number


_CLAUDE_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

_CLAUDE_MODEL_WINDOWS = {
    "claude-opus-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-opus-4-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-haiku-4-5": 200_000,
}

_DEFAULT_CLAUDE_WINDOW = 1_000_000
_CODEX_ADVISORY_PCT = 0.50
_CODEX_HANDOFF_PCT = 0.75


@dataclass(frozen=True)
class ContextReading:
    """One valid provider-native measurement of current context use."""

    tokens: int
    model: str | None = None
    model_context_window: int | None = None


def _claude_window(model: str | None) -> int:
    normalized = str(model or "").strip()
    exact = _CLAUDE_MODEL_WINDOWS.get(normalized)
    if exact is not None:
        return exact
    for known, window in _CLAUDE_MODEL_WINDOWS.items():
        if normalized.startswith(known):
            return window
    return env_number(
        os.environ, "PENTACLE_CONTEXT_DEFAULT_WINDOW", _DEFAULT_CLAUDE_WINDOW,
        lambda raw: int(float(raw)),
    )


def _context_level(tokens: int, advisory: int, handoff: int) -> str:
    if tokens >= handoff:
        return "handoff"
    if tokens >= advisory:
        return "advisory"
    return "none"


def context_fields(provider: str, reading: ContextReading) -> tuple[int, int, str]:
    """Translate one reading into the four persisted v2 context dimensions.

    Thresholds deliberately match the public threshold configuration. Codex
    uses its rollout-reported window at 50/75%; Claude uses its model window
    and the capped 70/85% thresholds.
    """
    tokens = int(reading.tokens)
    if provider == "codex":
        window = int(reading.model_context_window or 0)
        if window <= 0:
            raise ValueError("Codex context reading lacks a positive window")
        return tokens, window, _context_level(
            tokens,
            round(_CODEX_ADVISORY_PCT * window),
            round(_CODEX_HANDOFF_PCT * window),
        )
    if provider == "claude":
        window = _claude_window(reading.model)
        advisory = min(
            env_number(
                os.environ, "PENTACLE_CONTEXT_ADVISORY_ABS", 400_000,
                lambda raw: int(float(raw)),
            ),
            round(env_number(os.environ, "PENTACLE_CONTEXT_ADVISORY_PCT", 0.70, float) * window),
        )
        handoff = min(
            env_number(
                os.environ, "PENTACLE_CONTEXT_HANDOFF_ABS", 600_000,
                lambda raw: int(float(raw)),
            ),
            round(env_number(os.environ, "PENTACLE_CONTEXT_HANDOFF_PCT", 0.85, float) * window),
        )
        return tokens, window, _context_level(tokens, advisory, handoff)
    raise ValueError(f"unsupported context provider: {provider}")


def parse_claude_context(raw: object) -> ContextReading | None:
    """Read the current-context sum from one normalized main Claude record."""
    if not isinstance(raw, dict) or raw.get("is_sidechain"):
        return None
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return None
    total = 0
    saw_usage = False
    for field in _CLAUDE_USAGE_FIELDS:
        if field not in usage:
            continue
        saw_usage = True
        value = usage[field]
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            return None
        total += int(value)
    if not saw_usage:
        return None
    model = raw.get("model")
    return ContextReading(tokens=total, model=model if isinstance(model, str) else None)


def parse_codex_context_reading(record: object) -> ContextReading | None:
    """Read the current-context token_count from one Codex provider record."""
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    last_usage = info.get("last_token_usage") if isinstance(info, dict) else None
    if not isinstance(last_usage, dict):
        return None
    tokens = last_usage.get("total_tokens")
    window = info.get("model_context_window")
    if (
        not isinstance(tokens, (int, float))
        or isinstance(tokens, bool)
        or not math.isfinite(tokens)
        or not isinstance(window, (int, float))
        or isinstance(window, bool)
        or not math.isfinite(window)
        or int(window) <= 0
        or int(window) != window
    ):
        return None
    return ContextReading(tokens=int(tokens), model_context_window=int(window))

