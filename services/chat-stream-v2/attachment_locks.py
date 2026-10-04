"""Cross-process digest locks shared by managed materialize/publish/GC paths.

Lock order is digest -> database transaction -> filesystem operation. Call only
from an off-loop worker. Lock files are persistent lock identities, not blobs.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import time


@contextmanager
def digest_lock(root: str | Path, sha: str, *, timeout: float = 30.0):
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError("invalid blob digest")
    directory = Path(root) / ".attachment-locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / sha, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
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
