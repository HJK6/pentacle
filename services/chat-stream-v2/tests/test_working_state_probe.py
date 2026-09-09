from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_PROBE_PATH = Path(__file__).parents[3] / "tools" / "working_state_probe.py"
_SPEC = importlib.util.spec_from_file_location("working_state_probe", _PROBE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_PROBE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_PROBE)


@pytest.mark.parametrize(
    "working_states",
    [{}, {"peer:active": {"tokens_phase": "idle", "elapsed_ms": 0}}],
)
def test_active_snapshot_requires_named_active_tracker_state(working_states: dict) -> None:
    with pytest.raises(AssertionError, match="snapshot"):
        _PROBE._active_snapshot_state(working_states, "peer:active")


def test_active_snapshot_accepts_named_non_idle_tracker_state() -> None:
    state = _PROBE._active_snapshot_state(
        {"peer:active": {"tokens_phase": "down", "elapsed_ms": 0}},
        "peer:active",
    )
    assert state["tokens_phase"] == "down"
