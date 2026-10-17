"""Opt-in, process-owned Python streams with bounded UTF-8 retention.

Raw OS descriptors deliberately remain the service manager's emergency sinks.
"""
from __future__ import annotations

from dataclasses import dataclass
import io
import logging.handlers
import os
from pathlib import Path
import stat
import sys
import time
from typing import Mapping


DEFAULT_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_BACKUPS = 3
STDOUT_ENV = "PENTACLE_DAEMON_STDOUT_PATH"
STDERR_ENV = "PENTACLE_DAEMON_STDERR_PATH"
MAX_BYTES_ENV = "PENTACLE_DAEMON_LOG_MAX_BYTES"
BACKUPS_ENV = "PENTACLE_DAEMON_LOG_BACKUPS"


@dataclass(frozen=True)
class Config:
    stdout: Path
    stderr: Path
    max_bytes: int = DEFAULT_MAX_BYTES
    backups: int = DEFAULT_BACKUPS

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config | None:
        out, err = env.get(STDOUT_ENV), env.get(STDERR_ENV)
        if out is None and err is None:
            return None
        if not out or not err:
            raise ValueError("both managed stdout and stderr paths are required")
        paths = Path(out), Path(err)
        if not all(p.is_absolute() for p in paths):
            raise ValueError("managed log paths must be absolute")
        maximum = int(env.get(MAX_BYTES_ENV, str(DEFAULT_MAX_BYTES)))
        backups = int(env.get(BACKUPS_ENV, str(DEFAULT_BACKUPS)))
        if maximum < 4 or backups < 1:
            raise ValueError("managed logs require max_bytes >= 4 and backups >= 1")
        return cls(*paths, maximum, backups)

    def paths(self, path: Path) -> list[Path]:
        return [path, *(Path(f"{path}.{i}") for i in range(1, self.backups + 1))]

    def validate(self, emergency_streams: tuple[io.TextIOBase, ...]) -> None:
        """Check BOTH namespaces before opening either writer."""
        if self.stdout.parent.resolve() != self.stderr.parent.resolve():
            raise ValueError("managed streams must share an existing owner-controlled directory")
        parent = self.stdout.parent.stat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o022:
            raise ValueError("managed log directory must be owner-controlled")
        reserved: set[Path] = set()
        identities: set[tuple[int, int]] = set()
        for stream in emergency_streams:
            name = getattr(stream, "name", None)
            if isinstance(name, str) and Path(name).is_absolute():
                reserved.add(Path(name).resolve())
            try:
                info = os.fstat(stream.fileno())
                identities.add((info.st_dev, info.st_ino))
            except (OSError, ValueError, AttributeError, io.UnsupportedOperation):
                pass
        for active in (self.stdout, self.stderr):
            expected = set(self.paths(active))
            for candidate in active.parent.glob(active.name + ".[0-9]*"):
                if candidate.name[len(active.name) + 1:].isdigit() and candidate not in expected:
                    raise ValueError("unmanaged older log generation requires explicit disposition")
        for path in self.paths(self.stdout) + self.paths(self.stderr):
            resolved = path.resolve()
            if resolved in reserved:
                raise ValueError("managed rotation namespaces overlap")
            reserved.add(resolved)
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("managed log entries must be regular, unaliased files")
            identity = info.st_dev, info.st_ino
            if identity in identities:
                raise ValueError("managed log namespace aliases another stream")
            identities.add(identity)
            if info.st_size > self.max_bytes:
                raise ValueError("existing managed log exceeds the configured byte cap")


class _ByteRotatingHandler(logging.handlers.RotatingFileHandler):
    """Keep stdlib rotation; account for encoded bytes and oversized writes."""

    def __init__(self, path: Path, config: Config, emergency: io.TextIOBase):
        super().__init__(path, maxBytes=config.max_bytes, backupCount=config.backups,
                         encoding="utf-8", errors="backslashreplace")
        self.emergency = emergency
        self.failed_writes = 0
        self.last_error: str | None = None
        self._last_notice = float("-inf")

    def _open(self):
        # Disable platform newline expansion: the cap counts the bytes written.
        return self._builtin_open(self.baseFilename, self.mode, encoding=self.encoding,
                                  errors=self.errors, newline="")

    def write_text(self, text: str) -> None:
        data = text.encode("utf-8", errors="backslashreplace")
        self.acquire()
        try:
            position = 0
            while position < len(data):
                end = min(position + self.maxBytes, len(data))
                # Never cut the multi-byte character at the next segment's start.
                while end < len(data) and data[end] & 0xC0 == 0x80:
                    end -= 1
                chunk = data[position:end].decode("utf-8")
                if self.stream is None:
                    self.stream = self._open()
                self.stream.seek(0, os.SEEK_END)
                if self.stream.tell() + end - position > self.maxBytes:
                    self.doRollover()
                remaining = chunk
                while remaining:
                    written = self.stream.write(remaining)
                    if written is None or written <= 0 or written > len(remaining):
                        raise OSError("managed log write made invalid progress")
                    remaining = remaining[written:]
                self.stream.flush()
                position = end
        except (OSError, ValueError) as exc:
            # No record payload and no recursive logging/fallback stream growth.
            self.failed_writes += 1
            self.last_error = type(exc).__name__
            now = time.monotonic()
            if now - self._last_notice >= 60:
                self._last_notice = now
                try:
                    self.emergency.write("managed_log_error subsystem=logging bug_ref=daemon_log_retention\n")
                    self.emergency.flush()
                except Exception:
                    pass
        finally:
            self.release()


class ManagedStream(io.TextIOBase):
    def __init__(self, path: Path, config: Config, original: io.TextIOBase,
                 emergency: io.TextIOBase):
        self.handler = _ByteRotatingHandler(path, config, emergency)
        self.original = original

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "backslashreplace"

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        # Explicit descriptor consumers still use the emergency descriptor.
        return self.original.fileno()

    def write(self, text: str) -> int:
        if self.closed:
            raise ValueError("write to closed managed stream")
        if not isinstance(text, str):
            raise TypeError("managed stream requires text")
        self.handler.write_text(text)
        return len(text)

    def flush(self) -> None:
        # Writes already flush inside the same serialization lock. In particular,
        # interpreter shutdown must not reopen a handler closed by logging.
        pass

    def close(self) -> None:
        handler = getattr(self, "handler", None)
        if handler is not None:
            handler.close()
        super().close()


_streams: tuple[ManagedStream, ManagedStream] | None = None


def install(env: Mapping[str, str] | None = None) -> None:
    global _streams
    if _streams is not None:
        return
    config = Config.from_env(os.environ if env is None else env)
    if config is None:
        return
    originals = sys.stdout, sys.stderr
    config.validate(originals)
    stdout = ManagedStream(config.stdout, config, originals[0], originals[1])
    try:
        stderr = ManagedStream(config.stderr, config, originals[1], originals[1])
    except BaseException:
        stdout.close()
        raise
    _streams = stdout, stderr
    sys.stdout, sys.stderr = _streams


def status() -> dict[str, dict[str, int | str | None]] | None:
    if _streams is None:
        return None
    return {name: {"failed_writes": stream.handler.failed_writes,
                   "last_error": stream.handler.last_error}
            for name, stream in zip(("stdout", "stderr"), _streams)}


def retained_paths(path: Path, inode: int, device: int, backups: int) -> list[Path]:
    """Locate a captured active inode, then visit all its newer generations."""
    candidates = [path, *(Path(f"{path}.{i}") for i in range(1, backups + 1))]
    matches = []
    for index, candidate in enumerate(candidates):
        try:
            info = candidate.stat()
        except FileNotFoundError:
            continue
        if (info.st_ino, info.st_dev) == (inode, device):
            matches.append(index)
    if len(matches) != 1:
        raise ValueError("managed log start generation expired or is ambiguous")
    result = list(reversed(candidates[:matches[0] + 1]))
    if not all(candidate.is_file() for candidate in result):
        raise ValueError("managed log rotation is incomplete")
    return result
