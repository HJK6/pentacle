"""Public test bootstrap for the pure chat-stream examples."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


SERVICE_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
SERVICES_ROOT = SERVICE_DIR.parent
AGENT_ORCH_DIR = SERVICES_ROOT / "agent-orch"
TOOLS_DIR = SERVICE_DIR / "tools"
pytest_plugins = ["capabilities"]

PUBLIC_SHARED_DIR = Path(__file__).resolve().parents[2] / "_shared"

for _path in reversed((SERVICE_DIR, TESTS_DIR, SERVICES_ROOT, AGENT_ORCH_DIR, TOOLS_DIR, PUBLIC_SHARED_DIR)):
    _text = str(_path)
    if _text not in sys.path:
        sys.path.insert(0, _text)

# Some tools resolve host identity at import. Pin an invented identity before
# collection so results never depend on the operator's machine config.
os.environ["PENTACLE_HOST_ID"] = "test-host"

# The admission/auth tests construct Server() and assert its DEFAULT behavior
# (opt-ins OFF). Server() falls back to these env vars whenever the matching
# constructor arg is None, so an operator shell that exports any of them would
# silently flip a default and make the suite non-hermetic. PR #53 QA finding 1:
# the strict-default seat-token test depended on PENTACLE_SEAT_OPERATOR_AUTHORITY
# being unset, which conftest did not guarantee. Scrub every Server-constructor
# opt-in/identity env default before collection; a test that wants a flag on
# passes it explicitly to Server(), which overrides the (now-absent) env.
for _server_opt_in_env in (
    "PENTACLE_SEAT_OPERATOR_AUTHORITY",
    "PENTACLE_DOT_PRINCIPAL_STREAM_IDS",
    "PENTACLE_DOT_READ_ENABLED",
    "PENTACLE_DOT_TLS_PORT",
    "PENTACLE_DOT_TLS_CERT",
    "PENTACLE_DOT_TLS_KEY",
    "PENTACLE_DOT_TLS_BINDS",
    "PENTACLE_SYSTEM_PRODUCER_STREAM_ID",
    "PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN",
    "PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN_FILE",
):
    os.environ.pop(_server_opt_in_env, None)


@pytest.fixture(scope="session", autouse=True)
def _public_test_environment():
    """Keep the fixture namespace explicit without installing runtime hooks."""
    yield


def pytest_configure(config) -> None:
    del config


@pytest.fixture(autouse=True)
def _isolated_satellite_held_spans(tmp_path_factory, monkeypatch):
    """Keep satellite held-span state off the real ~/.local/state path.

    A `Satellite` built without `held_span_path` uses the production default;
    on a satellite host that file is live state a v2 satellite would deliver.
    """
    try:
        import usage_hold
    except ImportError:  # pragma: no cover - module absent on older trees
        yield
        return
    real_init = usage_hold.HeldSpans.__init__
    default = Path(os.path.expanduser(usage_hold.DEFAULT_PATH))
    base = tmp_path_factory.mktemp("held-spans")

    def init(self, path=usage_hold.DEFAULT_PATH, **kwargs):
        if Path(os.path.expanduser(str(path))) == default:
            path = base / "unfenced_usage.json"
        real_init(self, path, **kwargs)

    monkeypatch.setattr(usage_hold.HeldSpans, "__init__", init)
    yield


@pytest.fixture()
def isolated_tmux_env(tmp_path):
    """Keep bare tmux calls and child daemons on one owned test socket."""
    import subprocess
    from tmux_isolation import isolate_tmux
    # An independent context tears down after test-owned monkeypatch fixtures,
    # so injected subprocess failures cannot replace the cleanup transport.
    with pytest.MonkeyPatch.context() as isolation:
        wrapper, socket = isolate_tmux(tmp_path, isolation)
        try:
            yield wrapper
        finally:
            subprocess.run([wrapper, "kill-server"], check=False, capture_output=True, timeout=5)
            socket.unlink(missing_ok=True)
