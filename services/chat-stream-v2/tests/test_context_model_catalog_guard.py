"""Context-window table stays consistent with the spawn catalog."""
from __future__ import annotations

import pytest

import context_adapters
from _shared import spawn_profiles
from context_adapters import ContextReading, context_fields


@pytest.mark.parametrize("model", ["claude-fable-5-1", "claude-fable-5-2"])
def test_fable_models_declare_an_explicit_1m_window(model):
    # Explicit rows: the 1,000,000 fallback default would pass without them.
    assert context_adapters._CLAUDE_MODEL_WINDOWS[model] == 1_000_000
    assert context_fields("claude", ContextReading(1, model=model))[1] == 1_000_000


def test_every_declared_claude_window_is_spawnable_or_intentionally_not():
    assert spawn_profiles.unspawnable_gap("claude", context_adapters._CLAUDE_MODEL_WINDOWS) == set()
