from __future__ import annotations

import re

import pytest

from agent_orch.config import load_config
from agent_orch.role_baseline import resolve_memory_repo_path


BASELINE_FILES = (
    "qa_baseline.md",
    "documentation_baseline.md",
    "doc_qa_baseline.md",
    "nexus_baseline.md",
    "lead_baseline.md",
    "evaluator_baseline.md",
)


def _baseline_dir():
    """Resolve the live memory-repo agents/ directory; skip if unavailable.

    Production loader path. The test runs against the same path the running
    agent-orch wrapper would auto-load from. CI environments without a
    configured memory_repo_path skip cleanly.
    """
    config = load_config()
    memory_root = resolve_memory_repo_path(config)
    if memory_root is None:
        pytest.skip("no memory_repo_path resolvable in this environment")
    return memory_root / "agents"


def test_role_baselines_use_report_contract() -> None:
    baseline_dir = _baseline_dir()
    for filename in BASELINE_FILES:
        text = (baseline_dir / filename).read_text(encoding="utf-8")
        assert "agent-orch report --msg-id" in text, (
            f"{filename} missing required `agent-orch report --msg-id` instruction"
        )


def test_role_baselines_do_not_reference_retired_completion_markers() -> None:
    baseline_dir = _baseline_dir()
    retired_literals = (
        "DONE <msg_id>",
        "<OUTBOX_V1",
        "</OUTBOX_V1>",
    )
    for filename in BASELINE_FILES:
        text = (baseline_dir / filename).read_text(encoding="utf-8")
        for retired_literal in retired_literals:
            assert retired_literal not in text, (
                f"{filename} still references retired marker `{retired_literal}`"
            )
        assert re.search(r"end your reply with.*DONE", text, re.IGNORECASE | re.DOTALL) is None, (
            f"{filename} still tells agents to end-reply with DONE"
        )


def test_role_baselines_document_peer_comms_handoff_capabilities() -> None:
    """Spec 2 Stage D: each baseline documents the new tell / --terminate / [from <stream>] primitives.

    Capability-only documentation; no when-to-use prescriptions (workflow lives in
    development_process.md, which is canonical for that scope).
    """
    baseline_dir = _baseline_dir()
    required_substrings = ("agent-orch tell", "[from ")
    for filename in BASELINE_FILES:
        text = (baseline_dir / filename).read_text(encoding="utf-8")
        for required in required_substrings:
            assert required in text, (
                f"{filename} missing required capability documentation `{required}`"
            )
        assert "agent-orch report" in text and "--terminate" in text, (
            f"{filename} missing conditional `agent-orch report --terminate` capability"
        )


def test_role_baselines_document_inspect_recover_capabilities() -> None:
    """Spec sanitizer recovery Stage C: each baseline documents inspect/recover primitives."""
    baseline_dir = _baseline_dir()
    required_substrings = (
        "agent-orch inspect",
        "agent-orch recover",
    )
    for filename in BASELINE_FILES:
        text = (baseline_dir / filename).read_text(encoding="utf-8")
        for required in required_substrings:
            assert required in text, (
                f"{filename} missing required capability documentation `{required}`"
            )


def test_role_baselines_document_self_terminate_natural_language_mapping() -> None:
    """Each baseline must spell out the natural-language → command mapping for self-close.

    The universal self-close is
    `agent-orch close --operator-confirm $AGENT_ORCH_STREAM_ID` — the same
    chat_streamd `close` RPC the Pentacle trash button uses, allowed for a caller
    closing its own stream. `agent-orch stop` is now only a non-error no-op stub,
    so the durable baseline contract is the positive self-close command.
    """
    baseline_dir = _baseline_dir()
    required_substrings = (
        # Natural-language phrase the agent is most likely to hear.
        '"terminate yourself"',
        # The universal self-close command (works for top-level AND workers).
        "agent-orch close --operator-confirm",
        # Closing one's own stream — the daemon allows caller==target self-close.
        "$AGENT_ORCH_STREAM_ID",
    )
    for filename in BASELINE_FILES:
        text = (baseline_dir / filename).read_text(encoding="utf-8")
        for required in required_substrings:
            assert required in text, (
                f"{filename} missing self-terminate mapping substring `{required}`"
            )


def test_shared_agents_md_documents_self_terminate_natural_language_mapping() -> None:
    """The memory-repo AGENTS.md must also carry the natural-language → command mapping.

    Role baselines only reach `--role`-injected sub-agents. Leaders (and any agent that
    reads the shared AGENTS.md via @-include from its workspace AGENTS.md / CLAUDE.md)
    need the mapping too, since they receive natural-language self-close prompts the
    same way workers do. This test pins the mapping into AGENTS.md's Agent
    Orchestration section so future edits don't drop the leader-facing coverage.
    """
    baseline_dir = _baseline_dir()
    # AGENTS.md lives at the memory-repo root, one level above agents/.
    agents_md = baseline_dir.parent / "AGENTS.md"
    if not agents_md.exists():
        pytest.skip(f"AGENTS.md not found at {agents_md} in this memory repo")
    text = agents_md.read_text(encoding="utf-8")
    required_substrings = (
        '"terminate yourself"',
        "agent-orch close --operator-confirm",
        "$AGENT_ORCH_STREAM_ID",
    )
    for required in required_substrings:
        assert required in text, (
            f"AGENTS.md missing self-terminate mapping substring `{required}`"
        )
