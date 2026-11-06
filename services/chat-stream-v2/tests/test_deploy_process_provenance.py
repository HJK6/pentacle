"""Process census parsing, including a read-only check of the real host table."""
import os
import subprocess

import pytest

from deploy import coordinated as c
from deploy.coordinated_runtime import Runtime


def runtime(tmp_path, runner):
    return Runtime(None, None, tmp_path, runner, stream="fixture:deployer", boot_wait_seconds=1)


def test_process_rows_accepts_signed_uid(tmp_path):
    output = "  42 1 -2 /usr/sbin/example agent\n  43 0 501 /usr/bin/example worker\n"
    def runner(argv, cwd):
        assert argv == ("ps", "-ax", "-o", "pid=,ppid=,uid=,command=")
        return subprocess.CompletedProcess(argv, 0, output, "")
    assert runtime(tmp_path, runner).process_rows() == [
        (42, 1, -2, "/usr/sbin/example agent"),
        (43, 0, 501, "/usr/bin/example worker"),
    ]


@pytest.mark.parametrize("line", [
    "42 1 -2", "garbled", "-42 1 501 command", "42 -1 501 command",
    "42 1 --2 command", "42 1 uid command", "42 1 1.5 command", "42 x 501 command",
])
def test_process_rows_refuses_malformed_rows(tmp_path, line):
    def runner(argv, cwd):
        return subprocess.CompletedProcess(argv, 0, line + "\n", "")
    with pytest.raises(c.Invalid, match="process provenance is unreadable"):
        runtime(tmp_path, runner).process_rows()


def test_process_rows_real_host_read_only(tmp_path):
    # Exercise the actual parser against the host ps, without launchd, stop,
    # signals, registry access, or a substituted process table.
    calls = []
    def runner(argv, cwd):
        assert argv == ("ps", "-ax", "-o", "pid=,ppid=,uid=,command=")
        calls.append(argv)
        return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False)
    rows = runtime(tmp_path, runner).process_rows()
    assert len(calls) == 1
    assert any(pid == os.getpid() and uid == os.getuid() for pid, ppid, uid, command in rows)
    assert all(pid >= 0 and ppid >= 0 and command for pid, ppid, uid, command in rows)
