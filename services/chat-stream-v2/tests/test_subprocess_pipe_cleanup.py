"""Bounded cleanup owns its root and pipe handles, not descendants."""
import asyncio
import os
import sys
import time

import pytest

import hosts
from prockill import terminate_and_reap


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("root_exits", [False, True])
def test_inherited_pipe_does_not_extend_cleanup(monkeypatch, tmp_path, cancel, root_exits):
    finished = tmp_path / "descendant_finished"
    child_code = (
        "import pathlib,time; time.sleep(2); "
        f"pathlib.Path({str(finished)!r}).touch()"
    )
    root_code = (
        "import subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        f"print(child.pid,flush=True); time.sleep({0 if root_exits else 30})"
    )

    async def exercise():
        real_exec = asyncio.create_subprocess_exec
        ready = asyncio.Event()
        captured = {}

        async def capture(*args, **kwargs):
            proc = await real_exec(*args, **kwargs)
            captured["proc"] = proc
            captured["child_pid"] = int(await proc.stdout.readline())
            ready.set()
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
        task = asyncio.create_task(hosts._bounded_exec(
            sys.executable, "-c", root_code, timeout=30 if cancel else 0.05,
        ))
        await asyncio.wait_for(ready.wait(), 5)
        started = time.monotonic()
        try:
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                assert await task == (1, "", True)
            assert time.monotonic() - started < 1
            proc = captured["proc"]
            assert proc.returncode is not None
            with pytest.raises(ProcessLookupError):
                os.kill(proc.pid, 0)
            os.kill(captured["child_pid"], 0)
            assert not finished.exists(), "cleanup waited for descendant EOF"
            assert proc._transport.is_closing()
        finally:
            # The disposable descendant exits itself; never broaden root cleanup.
            deadline = time.monotonic() + 4
            while not finished.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert finished.exists(), "cleanup must not kill the descendant"

    asyncio.run(exercise())


def test_success_preserves_output_and_exit_status():
    result = asyncio.run(hosts._bounded_exec(
        sys.executable, "-c", "print('owned output'); raise SystemExit(7)", timeout=5,
    ))
    assert result == (7, "owned output\n", False)


def test_transport_close_error_does_not_skip_root_reap():
    calls = []

    class BrokenTransport:
        def close(self):
            calls.append("close")
            raise OSError("already closed")

    class Process:
        returncode = None
        _transport = BrokenTransport()

        def kill(self):
            calls.append("kill")

        async def wait(self):
            calls.append("wait")

    asyncio.run(terminate_and_reap(Process()))
    assert calls == ["kill", "close", "wait"]
