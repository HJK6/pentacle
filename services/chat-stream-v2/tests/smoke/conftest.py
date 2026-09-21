"""All smoke callers and daemons share a per-test owned tmux namespace."""
import pytest


@pytest.fixture(autouse=True)
def _smoke_tmux_namespace(isolated_tmux_env):
    yield isolated_tmux_env
