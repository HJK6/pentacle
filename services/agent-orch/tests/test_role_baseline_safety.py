"""Safety tests for role baselines.

Public role templates may discuss the self-close option, but must not grant it
unconditionally inside copy-and-paste spawn instructions.  The detector below
keeps that distinction explicit and works without a configured memory repo.
"""

from __future__ import annotations

import re

import pytest

from agent_orch.config import Config
from agent_orch.role_baseline import resolve_memory_repo_path


_FORBIDDEN_PATTERNS = (
    "--self-close-on-completion",
    "self_close_on_completion: true",
    "self_close_on_completion=true",
    '"self_close_on_completion": true',
)

_CODE_BLOCK_RE = re.compile(r"```[^\n]*\n.*?\n```", re.DOTALL)


def _extract_code_blocks(text: str) -> list[str]:
    return _CODE_BLOCK_RE.findall(text)


def test_no_role_baseline_unconditionally_grants_self_close_on_completion(tmp_path):
    config = Config(
        ws_url="ws://unused",
        token="",
        host_id="test",
        runtime_dir=tmp_path / "runtime",
        memory_repo_path=None,
    )
    memory_repo = resolve_memory_repo_path(config)
    if memory_repo is None:
        pytest.skip("memory_repo_path is not configured in this environment")

    agents_dir = memory_repo / "agents"
    if not agents_dir.is_dir():
        pytest.skip(f"agents/ directory missing under {memory_repo}")

    violations: list[tuple[str, str, str]] = []
    for baseline_path in sorted(agents_dir.glob("*_baseline.md")):
        text = baseline_path.read_text(encoding="utf-8")
        for block in _extract_code_blocks(text):
            block_lc = block.lower()
            for pattern in _FORBIDDEN_PATTERNS:
                if pattern.lower() in block_lc:
                    snippet = block.strip()
                    if len(snippet) > 200:
                        snippet = snippet[:197] + "..."
                    violations.append((str(baseline_path), pattern, snippet))

    assert not violations, (
        "Role baselines must not grant self_close_on_completion in spawn templates: "
        f"{violations}"
    )


def test_pattern_detector_distinguishes_prose_from_code():
    prose_only = """
# QA Baseline

Workers MUST NOT pass `--self-close-on-completion` unless the lead explicitly
authorized it at spawn time.  This mention is prose, not a template.
"""
    code_block_violation = """
# QA Baseline

Spawn workers with this exact command:

```bash
agent-orch spawn --provider codex --role qa --self-close-on-completion --initial-prompt-file /tmp/brief.md
```
"""

    def has_violation(blocks: list[str]) -> bool:
        return any(
            pattern.lower() in block.lower()
            for block in blocks
            for pattern in _FORBIDDEN_PATTERNS
        )

    assert not has_violation(_extract_code_blocks(prose_only))
    assert has_violation(_extract_code_blocks(code_block_violation))
