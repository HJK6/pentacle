import asyncio
import json
import sys

from tmux_transport import Tmux


def test_local_tmux_child_omits_daemon_settings(monkeypatch):
    blocked = (
        "PENTACLE_DAEMON_PROGRESS_PATH",
        "PENTACLE_DAEMON_INSTANCE_ID",
        "PENTACLE_DAEMON_STDOUT_PATH",
        "PENTACLE_DAEMON_STDERR_PATH",
        "PENTACLE_DOT_TLS_PORT",
        "PENTACLE_DOT_TLS_BINDS",
        "PENTACLE_DOT_TLS_CERT",
        "PENTACLE_DOT_TLS_KEY",
        "PENTACLE_ASSISTANT_AUTO_RESTORE",
    )
    unrelated = "PENTACLE_DAEMON_TEST_UNRELATED"
    for key in (*blocked, unrelated):
        monkeypatch.setenv(key, "fixture")

    # Use a Python child in place of the tmux executable to observe its environment.
    probe = (
        "import json, os, sys\n"
        "print(json.dumps({key: os.environ[key] for key in sys.argv[1:] if key in os.environ}))"
    )
    code, output = asyncio.run(Tmux(bin_path=sys.executable).run("-c", probe, *blocked, unrelated))

    assert code == 0
    assert json.loads(output) == {unrelated: "fixture"}
