"""Roles delegate shared rules; validate that chain against configured memory."""
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
ORCHESTRATION_PATH = "docs/config/agent_orchestration.md"


@pytest.fixture(scope="module")
def instructions():
    """Use the production loader's explicit memory root, with three fixed homes."""
    root = resolve_memory_repo_path(load_config())
    if root is None:
        pytest.skip("no memory_repo_path resolvable in this environment")
    # A configured but incomplete instruction tree is a failure, not a skip.
    roles = {name: (root / "agents" / name).read_text(encoding="utf-8")
             for name in BASELINE_FILES}
    return roles, (root / "AGENTS.md").read_text(encoding="utf-8"), (
        root / ORCHESTRATION_PATH
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize("filename", BASELINE_FILES)
def test_role_baseline_delegates_to_shared_instructions(instructions, filename):
    roles, _, _ = instructions
    assert "(../AGENTS.md)" in roles[filename], f"{filename} lacks shared startup/communications link"
    if filename == "doc_qa_baseline.md":
        assert "(documentation_baseline.md)" in roles[filename]


def test_shared_delegation_targets_canonical_sections(instructions):
    _, shared, orchestration = instructions
    # These explicit anchors bind the delegated rules without resolving arbitrary Markdown.
    for anchor, heading in (
        ("completion-reports", "Completion Reports"),
        ("operator-questions", "Operator questions"),
    ):
        assert f"({ORCHESTRATION_PATH}#{anchor})" in shared
        assert f"## {heading}" in orchestration
    assert "## Communications" in shared


def test_effective_report_and_lifecycle_authority(instructions):
    _, _, orchestration = instructions
    for required in (
        "agent-orch report --msg-id", '"summary"', '"findings"', '"next_action"',
        "The parent owns worker lifecycle by default.",
        "only then use `agent-orch report --terminate`",
        "--self-close-on-completion",
    ):
        assert required in orchestration, f"canonical report contract missing {required!r}"


def test_effective_peer_communications(instructions):
    _, shared, orchestration = instructions
    for required in ("agent-orch tell", "send <stream> <msg-id>", "START / GATE / BLOCKER / END"):
        assert required in shared
    for required in ("## Send vs Tell", "[from <stream_id>]", "child_report_ready"):
        assert required in orchestration


def test_effective_inspect_before_recover(instructions):
    _, shared, orchestration = instructions
    assert "inspect an actual anomaly before recovery" in shared
    waiting = orchestration.split("## Waiting for worker completion\n", 1)[1].split("\n## ", 1)[0]
    for required in ("agent-orch inspect", "inspect once", "use `recover` only", "did not report"):
        assert required in waiting


def test_effective_explicit_self_close_mapping(instructions):
    _, _, orchestration = instructions
    for required in (
        "terminate yourself", "agent-orch close --operator-confirm $AGENT_ORCH_STREAM_ID",
        "If a report is expected, file it first", "otherwise the parent closes the seat",
    ):
        assert required.lower() in orchestration.lower()


def test_role_baselines_do_not_reference_retired_completion_markers(instructions):
    roles, _, _ = instructions
    for filename, text in roles.items():
        for marker in ("DONE <msg_id>", "<OUTBOX_V1", "</OUTBOX_V1>"):
            assert marker not in text, f"{filename} still references retired marker {marker!r}"
        assert re.search(r"end your reply with.*DONE", text, re.IGNORECASE | re.DOTALL) is None
