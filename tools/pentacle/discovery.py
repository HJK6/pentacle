"""Read local installation facts. Raw process/configuration data stays internal."""
from __future__ import annotations

import ctypes
import ipaddress
import json
import os
import plistlib
import re
import shlex
import socket
import ssl
import struct
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


def run(argv, *, timeout=20, cwd=None, env=None):
    try:
        return subprocess.run([str(v) for v in argv], capture_output=True, text=True,
                              timeout=timeout, cwd=cwd, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return subprocess.CompletedProcess(argv, 127, "", "")


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None


def loopback(url: str) -> bool:
    try:
        host = urllib.parse.urlsplit(url).hostname
        return host == "localhost" or ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def endpoint_port(url: str):
    try:
        parsed = urllib.parse.urlsplit(url)
        return parsed.port or (443 if parsed.scheme in {"wss", "https"} else 80)
    except ValueError:
        return None


@dataclass
class Process:
    pid: int
    argv: list[str]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict, repr=False)
    start: str = ""

    def option(self, name, default=None):
        for index, value in enumerate(self.argv):
            if value == name and index + 1 < len(self.argv):
                return self.argv[index + 1]
            if value.startswith(name + "="):
                return value[len(name) + 1:]
        return default

    def checkout(self, unit: str) -> Path | None:
        for arg in self.argv[1:]:
            path = Path(arg)
            if not path.is_absolute():
                path = self.cwd / path
            if unit == "daemon" and path.name == "main.py" and path.parent.name == "chat-stream-v2":
                return path.resolve().parents[2]
            if unit == "web" and (path.name == "server" or path.as_posix().endswith("server/index.js")):
                return path.resolve().parent if path.name == "server" else path.resolve().parents[1]
        return None


def _mac_args(pid: int):
    """KERN_PROCARGS2 preserves spaces and argument boundaries that ps loses."""
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, pid)
    buffer = ctypes.create_string_buffer(1024 * 1024)
    size = ctypes.c_size_t(len(buffer))
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        raise OSError
    raw = buffer.raw[:size.value]
    argc = struct.unpack_from("i", raw)[0]
    cursor = raw.index(b"\0", 4) + 1  # executable path, followed by padding
    while cursor < len(raw) and raw[cursor] == 0:
        cursor += 1
    argv = []
    for _ in range(argc):
        end = raw.index(b"\0", cursor)
        argv.append(os.fsdecode(raw[cursor:end]))
        cursor = end + 1
    env = {}
    for chunk in raw[cursor:].split(b"\0"):
        if b"=" in chunk:
            key, value = os.fsdecode(chunk).split("=", 1)
            env[key] = value
    return argv, env


def _mac_invocation(argv, env):
    """The framework launcher re-execs Python but retains the venv entry path."""
    launcher = env.get("__PYVENV_LAUNCHER__")
    if argv and "/Resources/Python.app/" in argv[0] and launcher:
        path = Path(launcher)
        framework = Path(argv[0].split("/Resources/Python.app/", 1)[0])
        try:
            if path.is_absolute() and os.access(path, os.X_OK) and path.resolve(strict=True).is_relative_to(framework):
                return [str(path), *argv[1:]]
        except OSError:
            pass
    return argv


def process(pid: int) -> Process | None:
    try:
        if sys.platform.startswith("linux"):
            root = Path("/proc") / str(pid)
            if root.stat().st_uid != os.getuid():
                return None
            raw_argv = (root / "cmdline").read_bytes()
            argv = [os.fsdecode(v) for v in raw_argv.split(b"\0")[:-1]]
            env = dict(os.fsdecode(v).split("=", 1) for v in (root / "environ").read_bytes().split(b"\0") if b"=" in v)
            start = (root / "stat").read_text().rsplit(")", 1)[1].split()[19]
            return Process(pid, argv, (root / "cwd").resolve(strict=True), env, start)
        if sys.platform == "darwin":
            owner = run(["ps", "-p", pid, "-o", "uid=", "-o", "lstart="])
            fields = owner.stdout.strip().split(maxsplit=1)
            if owner.returncode or int(fields[0]) != os.getuid():
                return None
            argv, env = _mac_args(pid)
            argv = _mac_invocation(argv, env)
            cwd = run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"])
            directory = next(line[1:] for line in cwd.stdout.splitlines() if line.startswith("n"))
            return Process(pid, argv, Path(directory), env, fields[1])
    except (OSError, ValueError, IndexError, StopIteration):
        pass
    return None


def listener(url: str) -> Process | None:
    if not loopback(url) or endpoint_port(url) is None:
        return None
    port = endpoint_port(url)
    pids = set()
    if sys.platform == "darwin":
        result = run(["lsof", "-nP", "-a", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"])
        pids = {int(v[1:]) for v in result.stdout.splitlines() if re.fullmatch(r"p[0-9]+", v)}
    elif sys.platform.startswith("linux"):
        inodes = set()
        for table in ("tcp", "tcp6"):
            try:
                for line in (Path("/proc/net") / table).read_text().splitlines()[1:]:
                    values = line.split()
                    if int(values[1].split(":")[1], 16) == port and values[3] == "0A" and int(values[7]) == os.getuid():
                        inodes.add(values[9])
            except (OSError, ValueError, IndexError):
                continue
        for path in Path("/proc").iterdir():
            try:
                if not path.name.isdigit() or path.stat().st_uid != os.getuid():
                    continue
                for fd in (path / "fd").iterdir():
                    try:
                        target = os.readlink(fd)
                        if target.startswith("socket:[") and target[8:-1] in inodes:
                            pids.add(int(path.name))
                    except OSError:
                        continue
            except OSError:
                continue
    candidates = [value for pid in pids if (value := process(pid)) is not None]
    return candidates[0] if len(candidates) == 1 else None


def open_stores(proc: Process | None) -> list[Path]:
    if proc is None:
        return []
    names = []
    if sys.platform.startswith("linux"):
        try:
            for fd in (Path("/proc") / str(proc.pid) / "fd").iterdir():
                try:
                    names.append(Path(os.readlink(fd)))
                except OSError:
                    continue
        except OSError:
            return []
    elif sys.platform == "darwin":
        result = run(["lsof", "-a", "-p", proc.pid, "-Fn"])
        names = [Path(v[1:]) for v in result.stdout.splitlines() if v.startswith("n/")]
    stores = set()
    for name in names:
        try:
            if name.is_file():
                with name.open("rb") as source:
                    if source.read(16) == b"SQLite format 3\0":
                        stores.add(name.resolve())
        except OSError:
            continue
    return sorted(stores)


CONFIG_SCRIPT = r"""
const {loadConfig} = require(process.argv[1]);
try {
  const loaded=loadConfig(process.argv[2]);
  const stream=loaded.config.chatStream || {};
  process.stdout.write('\nPENTACLE_DISCOVERY:'+JSON.stringify({path:loaded.path,
    daemon_url:stream.url || null, token_path:stream.tokenPath || null,
    warnings:loaded.warnings.map(w=>w.code)})+'\n');
} catch (_) {process.exitCode=2;}
"""


def config(root: Path, path: Path) -> dict | None:
    result = run(["node", "-e", CONFIG_SCRIPT, root / "config-loader.js", root],
                 cwd=root, env={**os.environ, "PENTACLE_CONFIG": str(path)})
    if result.returncode:
        return None
    try:
        value = json.loads(result.stdout.rsplit("PENTACLE_DISCOVERY:", 1)[1].strip())
        if not isinstance(value, dict) or any(value.get(key) is not None and not isinstance(value[key], str) for key in ("path", "daemon_url", "token_path")):
            return None
        return value
    except (ValueError, IndexError):
        return None


def health(url: str, timeout=10) -> dict | None:
    try:
        parsed = urllib.parse.urlsplit(url)
        endpoint = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + "/api/health", "", ""))
        # Never follow a redirect to another endpoint or inherit proxy settings.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(endpoint, timeout=timeout) as response:
            value = json.loads(response.read(1024 * 1024))
        return value if isinstance(value, dict) and value.get("ok") is True else None
    except (OSError, ValueError):
        return None


def welcome(url: str, timeout=10) -> dict | None:
    """Read the unauthenticated welcome using only the standard library."""
    import base64
    import hashlib
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            return None
        sock = socket.create_connection((parsed.hostname, endpoint_port(url)), timeout)
        with sock:
            if parsed.scheme == "wss":
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
            with sock:
                nonce = base64.b64encode(os.urandom(16)).decode()
                target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                request = (f"GET {target} HTTP/1.1\r\nHost: {parsed.netloc}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {nonce}\r\nSec-WebSocket-Version: 13\r\n\r\n")
                sock.sendall(request.encode())
                with sock.makefile("rb") as reader:
                    status = reader.readline(4096)
                    headers = {}
                    total = len(status)
                    while True:
                        line = reader.readline(4096)
                        total += len(line)
                        if total > 32768 or not line:
                            return None
                        if line == b"\r\n":
                            break
                        key, value = line.decode().split(":", 1)
                        headers[key.lower()] = value.strip()
                    expected = base64.b64encode(hashlib.sha1((nonce + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                    if status.split()[1] != b"101" or headers.get("sec-websocket-accept") != expected:
                        return None
                    frame = reader.read(2)
                    if len(frame) != 2 or frame[0] != 0x81 or frame[1] & 0x80:
                        return None
                    size = frame[1] & 127
                    if size == 126:
                        size = struct.unpack("!H", reader.read(2))[0]
                    elif size == 127:
                        size = struct.unpack("!Q", reader.read(8))[0]
                    if size > 1024 * 1024:
                        return None
                    value = json.loads(reader.read(size))
                    return value if isinstance(value, dict) and value.get("type") == "welcome" else None
    except (OSError, ValueError, IndexError, struct.error):
        return None


def read_unit(path: Path) -> dict | None:
    try:
        raw = path.read_bytes()
        if path.suffix == ".plist":
            data = plistlib.loads(raw)
            if b"Pentacle managed service" not in raw and data.get("Label") != "com.pentacle.chat-streamd-v2":
                return None
            return {"label": data["Label"], "path": str(path), "argv": data["ProgramArguments"],
                    "cwd": data["WorkingDirectory"], "env": data.get("EnvironmentVariables", {})}
        text = raw.decode()
        if "Pentacle managed service" not in text and path.name != "pentacle-chat-streamd-v2.service":
            return None
        values, environment = {}, {}
        for line in text.splitlines():
            if line.startswith("Environment="):
                for entry in shlex.split(line.split("=", 1)[1]):
                    key, value = entry.split("=", 1)
                    environment[key] = value.replace("%%", "%")
            elif "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value
        argv = [v.replace("%%", "%").replace("$$", "$") for v in shlex.split(values["ExecStart"])]
        working = values["WorkingDirectory"]
        if working.startswith(('"', "'")):
            working = shlex.split(working)[0]
        return {"label": path.name, "path": str(path), "argv": argv,
                "cwd": working.replace("%%", "%"), "env": environment}
    except (OSError, ValueError, KeyError, UnicodeError, plistlib.InvalidFileException):
        return None


def unit_for(proc: Process | None, kind: str) -> dict | None:
    if proc is None:
        return None
    home = Path.home()
    if kind == "daemon":
        canonical = home / ("Library/LaunchAgents/com.pentacle.chat-streamd-v2.plist" if sys.platform == "darwin"
                            else ".config/systemd/user/pentacle-chat-streamd-v2.service")
        if canonical.exists():
            # An installed unit can point elsewhere even when the observed
            # process is hand-started. A malformed installed unit fails too.
            return read_unit(canonical) or {"argv": [], "cwd": str(home)}
    if sys.platform == "darwin":
        listing = run(["launchctl", "list"])
        labels = {parts[2] for line in listing.stdout.splitlines()
                  if len(parts := line.split()) == 3 and parts[0] == str(proc.pid)}
        for label in labels:
            path = home / "Library/LaunchAgents" / (label + ".plist")
            unit = read_unit(path)
            if unit and unit["label"] == label and unit["argv"] == proc.argv:
                return {**unit, "pid": proc.pid}
    elif sys.platform.startswith("linux"):
        folder = home / ".config/systemd/user"
        for path in folder.glob("*.service"):
            try:
                unit = read_unit(path)
                if not unit:
                    continue
                pid = run(["systemctl", "--user", "show", "--property=MainPID", "--value", path.name])
                if pid.returncode or pid.stdout.strip() != str(proc.pid):
                    continue
                if unit["argv"] == proc.argv:
                    return {**unit, "pid": proc.pid}
            except (OSError, ValueError, KeyError):
                continue
    return None
