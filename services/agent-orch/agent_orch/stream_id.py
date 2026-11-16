from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

from .config import Config
from .wsclient import fetch_snapshot


LOG = logging.getLogger(__name__)


# Granular discovery source for the caller stream id.
class LeaderSource:
    FLAG = "flag"
    ENV_PENTACLE_STREAM_ID = "env_pentacle_stream_id"
    ENV_AGENT_ORCH_STREAM_ID = "env_agent_orch_stream_id"
    SHELL_FALLBACK = "shell_fallback"


def discover_leader_stream_id(
    config: Config,
    snapshot: dict[str, Any] | None = None,
    as_override: str | None = None,
) -> tuple[str | None, str]:
    """Resolve the local leader stream id and report which precedence rule won.

    Precedence (highest first):
      1. ``as_override`` (explicit caller override by a command).
      2. ``PENTACLE_STREAM_ID`` env var.
      3. ``AGENT_ORCH_STREAM_ID`` env var.
      4. tmux self-discovery via ``tmux display-message -p '#{session_name}'``
         + snapshot lookup.

    Returns ``(stream_id | None, source)`` where ``source`` is one of the
    ``LeaderSource`` constants. The shell-fallback source is reported even
    when the snapshot lookup returns ``None`` so callers can distinguish
    "no env, no flag, tmux not present" from "explicit binding miss".
    """
    if as_override:
        return as_override, LeaderSource.FLAG
    pentacle = os.environ.get("PENTACLE_STREAM_ID")
    if pentacle:
        return pentacle, LeaderSource.ENV_PENTACLE_STREAM_ID
    agent_orch = os.environ.get("AGENT_ORCH_STREAM_ID")
    if agent_orch:
        return agent_orch, LeaderSource.ENV_AGENT_ORCH_STREAM_ID

    tmux_name = _tmux_session_name()
    if tmux_name:
        if snapshot is None:
            snapshot = fetch_snapshot(config, events_mode="summary")
        matches = [
            session
            for session in snapshot.get("sessions", [])
            if isinstance(session, dict)
            and session.get("session_name") == tmux_name
            and session.get("host") == config.host_id
            and isinstance(session.get("stream_id"), str)
        ]
        if len(matches) == 1:
            return matches[0]["stream_id"], LeaderSource.SHELL_FALLBACK

    LOG.warning("agent-orch leader is untracked")
    return None, LeaderSource.SHELL_FALLBACK


def discover_leader_stream_id_short(config: Config) -> str | None:
    """Return just the leader stream id, applying the same precedence rules.

    Used by callers that don't need to display or persist the discovery source
    (spawn parent inference, INBOX `from` field fallback, tell `--from` default).
    """
    pentacle = os.environ.get("PENTACLE_STREAM_ID")
    if pentacle:
        return pentacle
    agent_orch = os.environ.get("AGENT_ORCH_STREAM_ID")
    if agent_orch:
        return agent_orch
    tmux_name = _tmux_session_name()
    if not tmux_name:
        LOG.warning("agent-orch leader is untracked")
        return None
    snapshot = fetch_snapshot(config, events_mode="summary")
    stream_id, _source = discover_leader_stream_id(config, snapshot)
    return stream_id


def env_stream_id() -> str | None:
    """This process's OWN stream id from the environment only — never snapshot
    discovery. It is the authenticated-caller identity a report/close stamps in
    `caller_stream_id`, distinct from an operator's `--from-stream-id` override:
    the daemon rejects a `from_stream_id` that does not match this caller. Returns
    None when unset (a tokenless/untracked caller has no env identity to assert)."""
    return os.environ.get("PENTACLE_STREAM_ID") or os.environ.get("AGENT_ORCH_STREAM_ID")


def _tmux_session_name() -> str | None:
    if not os.environ.get("TMUX"):
        return None
    try:
        # Intentional G7 exemption: current-pane self-discovery must use the
        # operator-inherited TMUX socket, otherwise tmux cannot identify this
        # attached session.
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#{session_name}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    name = result.stdout.strip()
    return name or None
