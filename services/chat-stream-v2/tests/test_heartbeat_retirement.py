"""Regression guard for the retired heartbeat family."""

from importlib.util import find_spec

import pytest


@pytest.mark.parametrize(
    "module", ("example_runtime.heartbeat_contract", "public_tools.heartbeat", "heartbeat_supervisor")
)
def test_retired_heartbeat_modules_are_not_importable(module: str) -> None:
    try:
        found = find_spec(module)
    except ModuleNotFoundError:
        found = None
    assert found is None
