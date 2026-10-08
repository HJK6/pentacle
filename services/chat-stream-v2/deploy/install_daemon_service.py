#!/usr/bin/env python3
"""Install, enable or roll back a login service for the chat_streamd v2 daemon.

Renders the templates in ``daemon/`` (a launchd agent on macOS, a systemd user unit
on Linux) so the daemon starts at login and restarts if it exits. Both units run the
same command, built once here. Nothing starts until ``--enable``:

  print:     python install_daemon_service.py --release-checkout ~/repos/pentacle --print
  install:   python install_daemon_service.py --release-checkout ~/repos/pentacle
  enable:    python install_daemon_service.py --release-checkout ~/repos/pentacle --enable
  roll back: python install_daemon_service.py --rollback

Rollback does not stop the service. It restores the earlier unit file only when the
service manager explicitly reports the service stopped, and otherwise changes nothing.

An existing unit with different content is never overwritten unless ``--replace`` is
given; the first replaced file is kept as the rollback preimage.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
from xml.sax.saxutils import escape

DEPLOY_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = DEPLOY_DIR / "daemon"
LABEL = "com.pentacle.chat-streamd-v2"
SERVICE = "pentacle-chat-streamd-v2.service"
DAEMON = Path("services/chat-stream-v2/main.py")
VENV_PYTHON = Path("services/chat-stream-v2/.venv/bin/python")
DEFAULT_PORT = 7791
# The only answers that prove the service is stopped: launchd's "could not find
# service" exit status, and these systemd ActiveState values.
LAUNCHCTL_SERVICE_NOT_FOUND = 113
SYSTEMD_STOPPED_STATES = frozenset({"inactive", "failed"})
# Both unit formats quote differently; refusing these keeps one command valid in each.
UNSAFE_PATH_CHARACTERS = frozenset(" \t\n\"'\\%$;")

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class InstallError(Exception):
    pass


@dataclass(frozen=True)
class Params:
    release_checkout: Path
    python: Path
    tmux_bin: Path
    state_dir: Path
    spawn_cwd: Path
    home: Path
    port: int = DEFAULT_PORT
    local_host: str | None = None

    @property
    def path_value(self) -> str:
        dirs: list[str] = []
        for entry in (self.tmux_bin.parent, self.python.parent, self.home / ".local/bin",
                      Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/usr/bin"),
                      Path("/bin"), Path("/usr/sbin"), Path("/sbin")):
            if str(entry) not in dirs:
                dirs.append(str(entry))
        return ":".join(dirs)

    @property
    def log_dir(self) -> Path:
        return self.home / "Library/Logs/pentacle/chat-streamd-v2"

    @property
    def command(self) -> list[str]:
        """The one daemon command both units run."""
        argv = [
            str(self.python), str(self.release_checkout / DAEMON),
            "--host", "127.0.0.1", "--port", str(self.port),
            "--db", str(self.state_dir / "sessions.db"),
            "--notifications-db", str(self.state_dir / "notifications.db"),
            "--assets-db", str(self.state_dir / "assets.db"),
            "--blob-root", str(self.state_dir / "blobs"),
            "--spawn-cwd", str(self.spawn_cwd),
        ]
        if self.local_host:
            argv += ["--local-host", self.local_host]
        return argv


def _absolute_executable(label: str, value: Path) -> Path:
    if not value.is_absolute() or not value.is_file() or not os.access(value, os.X_OK):
        raise InstallError(f"{label} must be an absolute path to an executable file: {value}")
    return value


def _absolute_directory(label: str, value: Path, *, must_exist: bool) -> Path:
    if not value.is_absolute() or (must_exist and not value.is_dir()):
        raise InstallError(f"{label} must be an {'existing ' if must_exist else ''}absolute directory: {value}")
    return value


def build_params(args: argparse.Namespace, *, home: Path | None = None) -> Params:
    home = home or Path.home()
    checkout = Path(args.release_checkout).expanduser().resolve() if args.release_checkout else None
    if checkout is None or not (checkout / DAEMON).is_file():
        raise InstallError(f"--release-checkout must contain {DAEMON}")
    if not 1 <= int(args.port) <= 65535:
        raise InstallError(f"--port must be between 1 and 65535: {args.port}")
    params = Params(
        release_checkout=checkout,
        python=_absolute_executable("--python", Path(args.python).expanduser() if args.python else checkout / VENV_PYTHON),
        tmux_bin=_absolute_executable("--tmux-bin", Path(args.tmux_bin or shutil.which("tmux") or "")),
        state_dir=_absolute_directory(
            "--state-dir",
            Path(args.state_dir).expanduser() if args.state_dir else home / ".local/share/pentacle-stream",
            must_exist=False,
        ),
        spawn_cwd=_absolute_directory(
            "--spawn-cwd", Path(args.spawn_cwd).expanduser() if args.spawn_cwd else home, must_exist=True,
        ),
        home=home, port=int(args.port), local_host=args.local_host or None,
    )
    for value in (*params.command, params.path_value, str(params.log_dir)):
        if UNSAFE_PATH_CHARACTERS & set(value):
            raise InstallError(f"paths with spaces, quotes, backslashes, %, $ or ; are not supported: {value}")
    return params


def render(template: bytes, params: Params, *, xml: bool = False) -> bytes:
    quote = escape if xml else (lambda value: value)
    values = {
        "__PENTACLE_PROGRAM_ARGUMENTS__": "\n".join(
            f"    <string>{escape(argument)}</string>" for argument in params.command
        ),
        "__PENTACLE_EXEC_START__": shlex.join(params.command),
        "__PENTACLE_RELEASE_CHECKOUT__": quote(str(params.release_checkout)),
        "__PENTACLE_PATH__": quote(params.path_value),
        "__PENTACLE_LOG_DIR__": quote(str(params.log_dir)),
    }
    for token, value in values.items():
        template = template.replace(token.encode(), value.encode())
    if b"__PENTACLE_" in template:
        raise InstallError("template has an unbound placeholder")
    return template


def render_launchd(params: Params) -> dict[str, bytes]:
    return {f"{LABEL}.plist": render((TEMPLATE_DIR / f"{LABEL}.plist").read_bytes(), params, xml=True)}


def render_systemd(params: Params) -> dict[str, bytes]:
    return {SERVICE: render((TEMPLATE_DIR / SERVICE).read_bytes(), params)}


def _systemd() -> bool:
    return sys.platform.startswith("linux")


def unit_dir(home: Path) -> Path:
    return home / (".config/systemd/user" if _systemd() else "Library/LaunchAgents")


def target_files(params: Params) -> dict[Path, bytes]:
    rendered = render_systemd(params) if _systemd() else render_launchd(params)
    return {unit_dir(params.home) / name: data for name, data in rendered.items()}


def preimage_dir(home: Path) -> Path:
    return home / ".pentacle/daemon-service-preimage"


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=60)


def _checked(runner: Runner, *argv: str) -> None:
    result = runner(argv)
    if result.returncode:
        raise InstallError(f"{' '.join(argv)} failed: {(result.stderr or result.stdout).strip()}")


def install(params: Params, runner: Runner = _run, *, replace: bool = False) -> dict:
    """Write the rendered unit (keeping a preimage) and prepare directories; never starts it."""
    files = target_files(params)
    for path, data in files.items():
        if path.exists() and path.read_bytes() != data and not replace:
            raise InstallError(f"{path} exists with different content; pass --replace to overwrite it")
    store = preimage_dir(params.home)
    store.mkdir(parents=True, exist_ok=True)
    manifest_path = store / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for path in files:  # the first install's preimage is the one rollback restores
        if str(path) in manifest:
            continue
        if path.exists():
            shutil.copy2(path, store / path.name)
            manifest[str(path)] = path.name
        else:
            manifest[str(path)] = "absent"
    _write_atomic(manifest_path, json.dumps(manifest, indent=2).encode())
    params.state_dir.mkdir(parents=True, exist_ok=True)
    if not _systemd():
        params.log_dir.mkdir(parents=True, exist_ok=True)
    for path, data in files.items():
        _write_atomic(path, data)
    if _systemd():
        _checked(runner, "systemctl", "--user", "daemon-reload")
    return {"written": sorted(str(path) for path in files), "preimage": str(store)}


def enable(home: Path, runner: Runner = _run) -> None:
    if _systemd():
        _checked(runner, "systemctl", "--user", "enable", "--now", SERVICE)
        return
    plist = unit_dir(home) / f"{LABEL}.plist"
    if not plist.is_file():
        raise InstallError(f"{plist} is not installed; run the install step first")
    domain = f"gui/{os.getuid()}"
    runner(("launchctl", "bootout", f"{domain}/{LABEL}"))
    _checked(runner, "launchctl", "bootstrap", domain, str(plist))


def rollback(home: Path, runner: Runner = _run) -> dict:
    manifest_path = preimage_dir(home) / "manifest.json"
    if not manifest_path.exists():
        raise InstallError("no preimage manifest; nothing to roll back")
    # Rollback never stops the service itself, and it touches no file unless the
    # service manager explicitly says the service is stopped. A failed or unclear
    # query is not that answer.
    if _systemd():
        stop_command = f"systemctl --user disable --now {SERVICE}"
        state = runner(("systemctl", "--user", "show", "--property=ActiveState", "--value", SERVICE))
        stopped = state.returncode == 0 and (state.stdout or "").strip() in SYSTEMD_STOPPED_STATES
    else:
        target = f"gui/{os.getuid()}/{LABEL}"
        stop_command = f"launchctl bootout {target}"
        stopped = runner(("launchctl", "print", target)).returncode == LAUNCHCTL_SERVICE_NOT_FOUND
    if not stopped:
        raise InstallError(
            "the service is not confirmed stopped, so nothing was restored or removed; "
            f"stop it with `{stop_command}` and run --rollback again"
        )
    restored = []
    for path_text, saved in json.loads(manifest_path.read_text()).items():
        path = Path(path_text)
        if saved == "absent":
            path.unlink(missing_ok=True)
        else:
            _write_atomic(path, (preimage_dir(home) / saved).read_bytes())
        restored.append(path_text)
    manifest_path.unlink()
    if _systemd():
        runner(("systemctl", "--user", "daemon-reload"))
    return {"restored": restored}


def main(argv: list[str] | None = None, *, home: Path | None = None, runner: Runner = _run) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release-checkout", help="checkout the daemon runs from")
    parser.add_argument("--python", help=f"daemon interpreter (default: <checkout>/{VENV_PYTHON})")
    parser.add_argument("--tmux-bin", help="tmux binary; its directory leads the unit's PATH (default: from PATH)")
    parser.add_argument("--state-dir", help="directory for the daemon stores (default: ~/.local/share/pentacle-stream)")
    parser.add_argument("--spawn-cwd", help="directory new agent sessions start in (default: $HOME)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--local-host", help="this machine's name in your machines file (default: hostname)")
    parser.add_argument("--replace", action="store_true", help="overwrite an existing, different unit file")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--print", action="store_true", help="render only; write nothing")
    action.add_argument("--enable", action="store_true", help="start the installed unit now and at login")
    action.add_argument("--rollback", action="store_true", help="restore what was there before; the service must already be stopped")
    args = parser.parse_args(argv)
    home = home or Path.home()
    try:
        if args.rollback:
            print(json.dumps(rollback(home, runner)))
        elif args.enable:
            enable(home, runner)
            print(json.dumps({"enabled": True}))
        else:
            params = build_params(args, home=home)
            if args.print:
                for path, data in target_files(params).items():
                    print(f"# {path}\n{data.decode()}")
            else:
                print(json.dumps(install(params, runner, replace=args.replace)))
    except InstallError as exc:
        print(f"install_daemon_service: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
