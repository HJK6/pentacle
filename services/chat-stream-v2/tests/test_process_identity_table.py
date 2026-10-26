"""Close/reap must use numeric kernel SID, never Darwin's degraded ps table."""
import asyncio
from types import SimpleNamespace

import pytest

import prockill


ROW = b"42 1 501 42 42 Mon Jan 1 00:00:00 2026 owned actor\n"
DEGRADED = b"42 1 501 42 Mon Jan 1 00:00:00 2026 owned actor\n"


class Process:
    def __init__(self, rc, output, error=None):
        self.returncode = rc
        self.output = output
        self.error = error
        self.killed = self.waited = False

    async def communicate(self):
        if self.error:
            raise self.error
        return self.output, b""

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        self.waited = True


@pytest.mark.parametrize("fallback", [False, True])
def test_numeric_sid_table_and_linux_fast_path(monkeypatch, fallback):
    calls = []
    processes = [Process(1, DEGRADED), Process(0, ROW)] if fallback else [Process(0, ROW)]

    async def spawn(*args, **kwargs):
        calls.append(args)
        return processes.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    records = asyncio.run(prockill.process_records())
    assert records[42] == {"pid": 42, "ppid": 1, "uid": 501, "pgid": 42,
                           "sid": 42, "start_id": "Mon Jan 1 00:00:00 2026",
                           "command": "owned actor"}
    assert len(calls) == (2 if fallback else 1)
    if fallback:
        assert calls[1][1:] == ("-c", prockill.SESSION_ID_PROBE)


@pytest.mark.parametrize("error", [asyncio.TimeoutError(), asyncio.CancelledError()])
def test_failed_inventory_reaps_probe_and_preserves_cancellation(monkeypatch, error):
    proc = Process(None, b"", error)

    async def spawn(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(prockill.process_records())
    else:
        assert asyncio.run(prockill.process_records()) == {}
    assert proc.killed and proc.waited


def test_failed_fallback_never_parses_partial_stdout(monkeypatch):
    processes = [Process(1, DEGRADED), Process(127, ROW)]

    async def spawn(*args, **kwargs):
        return processes.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert asyncio.run(prockill.process_records()) == {}


def test_portable_probe_inserts_kernel_sid_and_skips_vanished_pid(monkeypatch, capsys):
    import os
    import subprocess

    def run(argv, **kwargs):
        assert "sid=" not in argv[-1]
        assert kwargs["check"] is True and kwargs["timeout"] < 10
        return SimpleNamespace(stdout=DEGRADED.decode() + DEGRADED.decode().replace("42 ", "43 ", 1))

    def getsid(pid):
        if pid == 43:
            raise ProcessLookupError()
        return 7

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(os, "getsid", getsid)
    exec(prockill.SESSION_ID_PROBE, {})
    assert capsys.readouterr().out == "42 1 501 42 7 Mon Jan 1 00:00:00 2026 owned actor\n"


def test_portable_probe_cannot_report_permission_gap_as_death(monkeypatch, capsys):
    import os
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=DEGRADED.decode()))
    def denied(pid):
        raise PermissionError("identity unavailable")
    monkeypatch.setattr(os, "getsid", denied)
    with pytest.raises(PermissionError):
        exec(prockill.SESSION_ID_PROBE, {})
    assert capsys.readouterr().out == ""
