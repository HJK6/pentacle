#!/usr/local/libexec/triforce-memory-python/bin/python -I
"""Root-installed report reader; no general agent-orch command dispatch."""
from __future__ import annotations

import json
import grp
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
TOKEN = "PENTACLE_WORK_REPORT_READER_TOKEN"
URL = "AGENT_ORCH_WS_URL"


def _root_path(path: Path) -> None:
    for item in (path, *path.parents):
        info = item.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("unsafe reader installation")


def reader_environment() -> dict[str, str]:
    """Called by the fixed publisher launcher before its environment reset.

    The ACL is deliberately a named-user read, never publisher-primary-group
    access: that primary group can be staff on Darwin.
    """
    if os.geteuid() != pwd.getpwnam("_triforce_memory").pw_uid:
        raise ValueError("reader configuration requires publisher identity")
    prefix = Path(__file__).resolve().parents[2]
    _root_path(prefix)
    config = prefix / "reader.json"
    fd = os.open(config, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_gid != grp.getgrnam("wheel").gr_gid
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 16384):
            raise ValueError("unsafe reader configuration")
        acl = subprocess.run(["/bin/ls", "-lde", str(config)], check=True,
                             capture_output=True, text=True, timeout=2)
        entries = [line.strip() for line in acl.stdout.splitlines()
                   if re.match(r"\s*\d+:", line)]
        if entries != ["0: user:_triforce_memory allow read"]:
            raise ValueError("reader configuration requires only publisher read ACL")
        with os.fdopen(fd, encoding="utf-8") as handle:
            fd = -1
            value = json.load(handle)
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(value, dict) or set(value) != {"token", "ws_url"}:
        raise ValueError("invalid reader configuration")
    token, url = value["token"], value["ws_url"]
    if (not isinstance(token, str) or not token.strip() or len(token) > 4096
            or not isinstance(url, str) or not url or len(url) > 2048):
        raise ValueError("invalid reader configuration")
    parsed = urlsplit(url)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("invalid reader URL")
    return {TOKEN: token, URL: url, "AGENT_ORCH_TOKEN": ""}


def _load_reader():
    release = Path(__file__).resolve().parent
    # These directories are extracted from the copied, verified release archive.
    # No site file, editable install or release runtime under a private home is used.
    sys.path[:0] = [str(release / "app/services/agent-orch"),
                   str(release / "app/services"),
                   str(release / "app/runtime-deps/websockets")]
    from agent_orch.work_report_cli import main
    return main


def main(argv: list[str]) -> int:
    if argv[:2] != ["report", "get"]:
        print("consumer permits report get only", file=sys.stderr)
        return 2
    if not os.environ.get(TOKEN, "").strip() or not os.environ.get(URL, "").strip():
        print(json.dumps({"type": "work.report.get.error", "error_code": "unavailable"}))
        return 1
    for key in list(os.environ):
        if key.startswith(("AGENT_ORCH_", "PENTACLE_")) and key not in {TOKEN, URL}:
            os.environ.pop(key)
    os.environ["AGENT_ORCH_TOKEN"] = ""
    return _load_reader()(argv[2:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
