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
    release = tmp_path / "descendant_release"
    # The descendant holds the inherited pipe until the test releases it (or a
    # 60 s stuck-detector fallback), so cleanup provably returned first.
    child_code = (
        "import pathlib,time\n"
        f"release=pathlib.Path({str(release)!r}); deadline=time.monotonic()+60\n"
        "while not release.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
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
        try:
            # Race readiness against the task, so a setup failure surfaces as itself.
            readiness = asyncio.ensure_future(ready.wait())
            await asyncio.wait({readiness, task}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
            readiness.cancel()
            if not ready.is_set():
                result = task.result() if task.done() else "still running"  # re-raises a setup error
                raise AssertionError(f"root never reported its descendant: {result!r}")
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                assert await task == (1, "", True)
            proc = captured["proc"]
            assert proc.returncode is not None
            with pytest.raises(ProcessLookupError):
                os.kill(proc.pid, 0)
            os.kill(captured["child_pid"], 0)
            assert not finished.exists(), "cleanup waited for descendant EOF"
            assert proc._transport.is_closing()
        finally:
            # The disposable descendant exits itself; never broaden root cleanup.
            release.touch()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            proc = captured.get("proc")
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            if "child_pid" in captured:  # only a created descendant can be checked
                deadline = time.monotonic() + 30
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
