"""Bounded classification for agent and non-agent session names."""

from __future__ import annotations

from typing import Iterable


# Names minted by the public examples or carried by a read-only viewer.
AGENT_SESSION_PREFIXES: tuple[str, ...] = (
    "v2-",
    "claude-",
    "codex-",
    "viewer-",
    "viewer-codex-",
)

# Short-lived provider probes are never adopted or surfaced as chat sessions.
EPHEMERAL_PROBE_PREFIXES: tuple[str, ...] = ("usage-check-",)


def is_ephemeral_probe_session(session_name: str | None) -> bool:
    """Return true for a synthetic provider-probe session name."""
    name = str(session_name or "")
    return any(name.startswith(prefix) for prefix in EPHEMERAL_PROBE_PREFIXES)


def is_agent_session_name(session_name: str | None) -> bool:
    """Return true when a name has a recognized agent/viewer prefix."""
    name = str(session_name or "")
    if is_ephemeral_probe_session(name):
        return False
    return any(name.startswith(prefix) for prefix in AGENT_SESSION_PREFIXES)


def is_surfaceable_session(
    session_name: str | None,
    known: Iterable[str] | None = None,
) -> bool:
    """Surface recognized names or caller-supplied custom names.

    Probe names remain hidden even if a caller accidentally includes one in the
    known set.
    """
    name = str(session_name or "")
    if is_ephemeral_probe_session(name):
        return False
    if is_agent_session_name(name):
        return True
    return known is not None and name in set(known)
