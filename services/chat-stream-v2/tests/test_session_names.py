"""Session-name classification for a public sidebar.

Only agent-shaped or explicitly registered sessions are surfaceable; probe
sessions remain hidden.
"""
from __future__ import annotations
from pathlib import Path

from session_names import (  # noqa: E402
    is_agent_session_name,
    is_ephemeral_probe_session,
    is_surfaceable_session,
)


def test_agent_prefixes_recognised():
    for name in ("v2-7efef61a", "claude-hosta-abcd", "codex-1234", "viewer-xyz", "viewer-codex-1"):
        assert is_agent_session_name(name), name


def test_non_agent_names_rejected():
    for name in ("sleep", "python3", "pytest", "bash", "gate-42", "", None):
        assert not is_agent_session_name(name), name


def test_probe_sessions_classified_and_never_agent():
    assert is_ephemeral_probe_session("usage-check-1234567890")
    assert not is_ephemeral_probe_session("v2-abcd")
    # A probe pane is never an agent session, even though it is a daemon pane.
    assert not is_agent_session_name("usage-check-1234567890")


def test_surfaceable_agent_or_known_but_never_probe():
    # agent-named: surfaceable with no known set.
    assert is_surfaceable_session("v2-abcd")
    # custom caller-supplied spawn name: surfaceable only when registry-known.
    assert not is_surfaceable_session("my-custom-lane")
    assert is_surfaceable_session("my-custom-lane", {"my-custom-lane"})
    # probe pane: never surfaceable, even if (wrongly) registry-known.
    assert not is_surfaceable_session("usage-check-1", {"usage-check-1"})
    # a raw non-agent, non-known pane never surfaces.
    assert not is_surfaceable_session("sleep", {"v2-abcd"})
