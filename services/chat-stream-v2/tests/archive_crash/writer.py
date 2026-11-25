"""Child-process retention writer that dies by real process exit at a boundary.

Runs the daemon's own `RetentionJob.run_pass()` against a disposable pair. Two
observers, both installed only in this child and neither touching daemon code:

* ``storage``: a SQLite Unix VFS ``pwrite`` observer. It arms while the Store
  thread is inside ``Connection.commit`` called under ``archive_rows``
  (sessions) or ``archive_events`` (events) and exits at the first write to the
  archive file family (database, ``-wal``, ``-journal``).
* ``stage``: an interpreter profile observer that recognizes a call to
  ``retention.history_checkpoint`` with the named stage and table on the Store
  thread.

Either writes one bounded JSON receipt and calls ``os._exit``. Without a
boundary the pass runs to completion and the receipt holds the pass result.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import ctypes.util
import json
import os
import sys
import threading
import time
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[2]
STORAGE_EXIT = 73
STAGE_EXIT = 74
FAMILIES = {"archive_rows": "sessions", "archive_events": "session_event_tail"}


def _receipt(path: Path, body: dict) -> None:
    text = json.dumps(body, sort_keys=True, default=str)[:16384]
    with open(path, "w", encoding="utf-8") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())


def _fd_path(fd: int) -> str | None:
    try:
        if sys.platform == "darwin":
            import fcntl
            raw = fcntl.fcntl(fd, 50, b"\0" * 1024)  # F_GETPATH
            return os.path.realpath(raw.split(b"\0", 1)[0].decode())
        return os.path.realpath(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return None


def _family(frame) -> str | None:
    retention = str(SERVICE_DIR / "retention.py")
    while frame is not None:
        if frame.f_code.co_filename == retention and frame.f_code.co_name in FAMILIES:
            return FAMILIES[frame.f_code.co_name]
        frame = frame.f_back
    return None


class _Vfs(ctypes.Structure):
    pass


_SYSCALL = ctypes.CFUNCTYPE(None)
_SET = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(_Vfs), ctypes.c_char_p, _SYSCALL)
_GET = ctypes.CFUNCTYPE(_SYSCALL, ctypes.POINTER(_Vfs), ctypes.c_char_p)
_Vfs._fields_ = [
    ("iVersion", ctypes.c_int), ("szOsFile", ctypes.c_int), ("mxPathname", ctypes.c_int),
    ("pNext", ctypes.c_void_p), ("zName", ctypes.c_char_p), ("pAppData", ctypes.c_void_p),
    *[(name, ctypes.c_void_p) for name in (
        "xOpen", "xDelete", "xAccess", "xFullPathname", "xDlOpen", "xDlError", "xDlSym",
        "xDlClose", "xRandomness", "xSleep", "xCurrentTime", "xGetLastError",
        "xCurrentTimeInt64")],
    ("xSetSystemCall", _SET), ("xGetSystemCall", _GET), ("xNextSystemCall", ctypes.c_void_p),
]
_PWRITE = ctypes.CFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t,
                           ctypes.c_int64)


def _sqlite_library():
    import _sqlite3
    # The module's own image: dlopen of a loaded path returns the same handle.
    name = None
    if sys.platform == "darwin":
        import subprocess
        linked = subprocess.run(["otool", "-L", _sqlite3.__file__], capture_output=True,
                                text=True, check=True).stdout.split()
        name = next((item for item in linked if "libsqlite3" in item), None)
    else:
        with open("/proc/self/maps", encoding="utf-8") as maps:
            name = next((line.split()[-1] for line in maps if "libsqlite3" in line), None)
    if name is None:
        raise SystemExit("sqlite3 is not dynamically linked; storage observer unavailable")
    library = ctypes.CDLL(name)
    library.sqlite3_vfs_find.restype = ctypes.POINTER(_Vfs)
    library.sqlite3_vfs_find.argtypes = [ctypes.c_char_p]
    return library


class Observer:
    def __init__(self, args):
        self.args = args
        self.receipt = Path(args.receipt)
        self.archive = os.path.realpath(args.archive)
        self.armed: str | None = None
        self.hits: list[dict] = []
        self._keep = []

    def install_storage(self) -> None:
        library = _sqlite_library()
        vfs = library.sqlite3_vfs_find(None)
        original = vfs.contents.xGetSystemCall(vfs, b"pwrite")
        if not original:
            raise SystemExit("unix VFS has no pwrite system call")
        forward = ctypes.cast(original, _PWRITE)
        family = {self.archive, self.archive + "-wal", self.archive + "-journal"}

        def observe(fd, data, count, offset):
            if self.armed is not None and _fd_path(fd) in family:
                _receipt(self.receipt, {
                    "boundary": "storage", "table": self.armed, "file": _fd_path(fd),
                    "offset": offset, "size": count, "pid": os.getpid(),
                    "thread": threading.current_thread().name, "monotonic": time.monotonic(),
                })
                os._exit(STORAGE_EXIT)
            return forward(fd, data, count, offset)

        hook = _PWRITE(observe)
        self._keep += [library, hook, original]
        if vfs.contents.xSetSystemCall(vfs, b"pwrite", ctypes.cast(hook, _SYSCALL)) != 0:
            raise SystemExit("cannot install the pwrite observer")

    def profile(self, frame, event, arg):
        if threading.current_thread().name != "store":
            return
        args = self.args
        if args.boundary == "storage" and event in ("c_call", "c_return") \
                and getattr(arg, "__name__", None) == "commit":
            import sqlite3
            if isinstance(getattr(arg, "__self__", None), sqlite3.Connection):
                family = _family(frame) if event == "c_call" else None
                self.armed = family if family == args.table else None
        elif args.boundary == "stage" and event == "call" \
                and frame.f_code.co_name == "history_checkpoint" \
                and frame.f_code.co_filename == str(SERVICE_DIR / "retention.py"):
            stage, table = frame.f_locals.get("stage"), frame.f_locals.get("table")
            if (stage, table) == (args.stage, args.table):
                rows = frame.f_locals.get("rows") or []
                _receipt(self.receipt, {
                    "boundary": "stage", "stage": stage, "table": table, "rows": len(rows),
                    "keys": [_key(row) for row in list(rows)[:20]], "pid": os.getpid(),
                    "thread": threading.current_thread().name, "monotonic": time.monotonic(),
                })
                os._exit(STAGE_EXIT)


def _key(row):
    row = dict(row)
    return {name: row.get(name) for name in ("event_id", "event_key", "host", "session_name")
            if name in row}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--boundary", choices=("none", "storage", "stage"), default="none")
    parser.add_argument("--stage")
    parser.add_argument("--table", choices=sorted(FAMILIES.values()))
    parser.add_argument("--tail-keep", type=int, required=True)
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args(argv)
    if args.boundary != "none" and not args.table:
        parser.error("a boundary needs --table")
    sys.path.insert(0, str(SERVICE_DIR))
    observer = Observer(args)
    if args.boundary == "storage":
        observer.install_storage()
    if args.boundary != "none":
        threading.setprofile(observer.profile)

    from retention import RetentionConfig, RetentionJob, default_archive_path
    from store import Store

    if os.path.realpath(default_archive_path(Path(args.db))) != observer.archive:
        parser.error("--archive must be the daemon's default archive for --db")

    async def run():
        store = Store(args.db)
        store.start()
        try:
            config = RetentionConfig(tail_keep=args.tail_keep, batch_size=args.batch_size)
            return await RetentionJob(store, config).run_pass()
        finally:
            store.stop()

    result = asyncio.run(run())
    _receipt(Path(args.receipt), {"boundary": "none", "pid": os.getpid(),
                                  "sessions_moved": result.sessions_moved,
                                  "events_moved": result.events_moved,
                                  "receipts_moved": result.receipts_moved})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
