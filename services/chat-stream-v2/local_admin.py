"""Daemon-user token for loopback-only reducing-authority recovery."""
from __future__ import annotations
import hmac
import os
from pathlib import Path
import secrets
import stat

DEFAULT_PATH = Path.home() / '.config/pentacle-stream/local-admin.token'


def initialize(path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        read(path)  # An unsafe pre-existing file must fail startup, never repair silently.
        return
    with os.fdopen(fd, 'w') as out:
        out.write(secrets.token_hex(32) + '\n')


def read(path: Path = DEFAULT_PATH) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            raise ValueError('unsafe local admin token file')
        token = source.read(256).strip()
    if len(token) != 64:
        raise ValueError('invalid local admin token file')
    return token


def verify(provided: object, path: Path = DEFAULT_PATH) -> bool:
    if not isinstance(provided, str):
        return False
    try:
        return hmac.compare_digest(read(path), provided)
    except (OSError, ValueError):
        return False
