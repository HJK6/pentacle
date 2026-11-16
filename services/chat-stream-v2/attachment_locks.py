"""Cross-process digest locks shared by managed materialize/publish/GC paths.

Operation owners retain sorted locks around short Store commits and worker file
operations. Store GC probes without waiting. Lock files are persistent identities.
"""
import asyncio
from contextlib import contextmanager, ExitStack, suppress
from functools import partial
import fcntl
import os
from pathlib import Path
import re
import time


MAX_OPERATIONS = 8
MAX_DIGESTS = 16
LOCK_BUDGET_SECONDS = 30.0


async def settle_future(future):
    """Join owned work before propagating repeated caller cancellation.

    Store and executor callers must pass their private raw result Future, not
    a cancellable task wrapping a bare await of that Future. A task is suitable
    only when its own in-flight work and cleanup obey this settlement contract.
    """
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break
    if cancelled:
        with suppress(BaseException):
            future.result()
        raise asyncio.CancelledError
    return future.result()


async def attachment_io(fn, *args, **kwargs):
    # A raw executor Future survives shutdown cancelling every asyncio Task.
    future = asyncio.get_running_loop().run_in_executor(None, partial(fn, *args, **kwargs))
    return await settle_future(future)


async def with_digest_locks(store, root, digests, operation):
    """Bound loop-owned operations; retain every acquired lock through commit."""
    keys = sorted(set(digests))
    if len(keys) > MAX_DIGESTS:
        raise ValueError('too many attachment digests')

    async def owner():
        active = getattr(store, '_attachment_operations', 0)
        if active >= MAX_OPERATIONS:
            raise RuntimeError('attachment operations busy')
        store._attachment_operations = active + 1
        stack = ExitStack()  # survives cancellation during partial acquisition
        deadline = time.monotonic() + LOCK_BUDGET_SECONDS

        def acquire():
            for sha in keys:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('attachment digest lock busy')
                stack.enter_context(digest_lock(root, sha, timeout=remaining))

        try:
            await attachment_io(acquire)
            return await operation(deadline)
        finally:
            try:
                await attachment_io(stack.close)
            finally:
                store._attachment_operations -= 1

    return await settle_future(asyncio.create_task(owner()))


def check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError('attachment operation expired before commit')


@contextmanager
def digest_lock(root: str | Path, sha: str, *, timeout: float = 30.0, directory_fd: int | None = None):
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError("invalid blob digest")
    directory = Path(root) / ".attachment-locks"
    if directory_fd is None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / sha if directory_fd is None else sha,
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600,
                 dir_fd=directory_fd)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("attachment digest lock busy")
                time.sleep(0.01)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
