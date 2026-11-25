"""Private environment and ownership for the real-process contract tier."""

import os
import shutil
import signal
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tests.contract.harness import Contract
from tests.soak.harness import TmuxNamespace
from tests.soak.restart_matrix import RestartDaemon, slugify_cwd


@pytest.fixture
def _isolated_satellite_held_spans():
    """The parent fixture imports and patches a daemon module in-process."""


@pytest.fixture
def _isolated_chat_checkpoint():
    """The subprocess has its own HOME and state directory."""


@pytest.fixture
def chat():
    # The usual Ctrl-C handler unwinds pytest fixtures. Do the same for a direct
    # TERM to this runner (or xdist worker).
    previous = signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        with TemporaryDirectory(prefix="chat-contract-") as temporary, ExitStack() as stack:
            root = Path(temporary).resolve()
            home = root / "home"
            control = root / "control"
            control.mkdir()
            (root / "tmp").mkdir()
            tmux = shutil.which("tmux")
            assert tmux, "tmux is required for chat contracts"
            env = {
                "HOME": str(home),
                "PATH": os.pathsep.join((str(Path(sys.executable).parent),
                                        str(Path(tmux).parent), os.defpath,
                                        "/usr/sbin", "/sbin")),
                "LANG": "C.UTF-8",
                "PYTHONUTF8": "1",
                "PYTHONUSERBASE": str(home / ".local"),
                "TMPDIR": str(root / "tmp"),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "XDG_STATE_HOME": str(home / ".local" / "state"),
                "RESTART_STUB_CONTROL": str(control),
                "RESTART_STUB_TRANSCRIPT_DIR": str(
                    root / ".claude" / "projects" / slugify_cwd(str(root / "seats"))
                ),
            }
            tmux_namespace = TmuxNamespace(root, env=env)
            stack.callback(tmux_namespace.kill_server)
            daemon = RestartDaemon(root, tmux_namespace, tmux_namespace.wrapper)
            daemon.extra_env["PYTHONUSERBASE"] = env["PYTHONUSERBASE"]
            stack.callback(daemon.kill_quiet)
            tmux_namespace.start()
            try:
                yield Contract(daemon, tmux_namespace, control, stack)
            finally:
                if daemon.logpath.exists():
                    # Captured by pytest on failure. Keep the fixture log private
                    # and bounded, including when the test is interrupted.
                    with daemon.logpath.open("rb") as log:
                        log.seek(max(0, daemon.logpath.stat().st_size - 8192))
                        print(log.read().decode(errors="replace"))
    finally:
        signal.signal(signal.SIGTERM, previous)
