"""Roles delegate shared rules; validate that chain against configured memory."""
from __future__ import annotations

import re
from pathlib import Path

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


def test_effective_inspect_and_fresh_qa_when_report_is_missing(instructions):
    _, shared, orchestration = instructions
    assert "inspect an actual anomaly before recovery" in shared
    waiting = " ".join(
        orchestration.split("## Waiting for worker completion\n", 1)[1]
        .split("\n## ", 1)[0]
        .lower()
        .split()
    )
    for required in (
        "child_idle_unreported",
        "inspect the child once",
        "a missing notification does not mean the typed report is absent",
        "if the durable report exists",
        "read and use that report once",
        "agent-orch inspect",
        "agent-orch await",
        "if no terminal report exists",
        "parented seat",
        "agent-orch spawn",
        "--parent",
        "--provider",
        "--model",
        "--effort",
        "--role qa",
        "--phase qa",
        "--spec-id",
        "--objective",
        "--no-self-close-on-completion",
        "the parent reads the new typed report once",
        "then closes that seat",
        "`await-spawn` reconciles spawn creation and prompt delivery, not completion",
    ):
        assert required in waiting
    assert "use `recover` only" not in waiting


def test_tracked_process_copy_describes_the_supported_report_path():
    process_doc = (
        Path(__file__).resolve().parents[3]
        / "process/docs/config/agent_orchestration.md"
    ).read_text(encoding="utf-8")
    start = process_doc.index("A `child_idle_unreported`")
    section = " ".join(
        process_doc[start:].split("\n\nAsk for user input", 1)[0].lower().split()
    )
    for required in (
        "inspect the child once",
        "a missing notification does not mean the typed report is absent",
        "if a durable report exists",
        "agent-orch inspect",
        "agent-orch await",
        "if no terminal report exists",
        "parented seat",
        "agent-orch spawn",
        "--parent",
        "--provider",
        "--model",
        "--effort",
        "--role qa --phase qa",
        "--spec-id",
        "--objective",
        "--no-self-close-on-completion",
        "the parent reads the typed report and closes the seat",
        "await-spawn",
    ):
        assert required in section
    assert "before recovering a missing report" not in section


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
