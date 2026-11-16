#!/usr/bin/env python3
"""Install, verify, enable or roll back the per-host usage-state collector on a satellite.

Renders the satellite templates in ``satellite/`` (systemd user unit + timer on Linux,
a launchd plist on macOS), binds them to the pinned release checkout and to absolute
Claude/Codex/tmux paths, and keeps a rollback preimage of anything it replaces. It adds
no service: the schedule is the platform's own timer, 600 s between runs.

Three separately gated steps (nothing activates until ``--enable``):

  install:   python install_usage_collector.py --host amaterasu --release-checkout ~/repos/pentacle-public-runtime
  verify:    python install_usage_collector.py --host amaterasu --release-checkout ... --verify
  enable:    python install_usage_collector.py --host amaterasu --release-checkout ... --enable
  roll back: python install_usage_collector.py --host amaterasu --rollback

``--print`` renders without writing. ``--verify`` runs the rendered command once, in the
rendered environment, and prints the observation tuple, cache refresh evidence and cost.
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
from xml.sax.saxutils import escape

DEPLOY_DIR = Path(__file__).resolve().parent
SATELLITE_DIR = DEPLOY_DIR / "satellite"
LABEL = "com.pentacle.usage-state-collector"
SERVICE = "pentacle-usage-state-collector.service"
TIMER = "pentacle-usage-state-collector.timer"
INTERVAL_SECONDS = 600
COLLECTOR = Path("services/chat-stream-v2/tools/collect_usage_state.py")

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class InstallError(Exception):
    pass


@dataclass(frozen=True)
class Params:
    host: str
    release_checkout: Path
    python: Path
    claude_bin: Path
    codex_bin: Path
    tmux_bin: Path
    usage_cwd: Path
    home: Path

    @property
    def path_value(self) -> str:
        dirs: list[str] = []
        for entry in (self.codex_bin.parent, self.claude_bin.parent, self.tmux_bin.parent,
                      Path("/usr/local/bin"), Path("/usr/bin"), Path("/bin")):
            if str(entry) not in dirs:
                dirs.append(str(entry))
        return ":".join(dirs)

    @property
    def state_path(self) -> Path:
        return self.home / ".local/share/pentacle-stream/usage_state.json"

    @property
    def environment(self) -> dict[str, str]:
        return {
            "PATH": self.path_value, "PENTACLE_HOST_ID": self.host,
            "PENTACLE_CLAUDE_BIN": str(self.claude_bin), "PENTACLE_CODEX_BIN": str(self.codex_bin),
            "PENTACLE_USAGE_TMUX_BIN": str(self.tmux_bin), "PENTACLE_USAGE_CWD": str(self.usage_cwd),
            "PENTACLE_USAGE_CLAUDE_OAUTH": "0",
        }

    @property
    def command(self) -> list[str]:
        return [str(self.python), str(self.release_checkout / COLLECTOR), "--state", str(self.state_path),
                "--shared-scripts", str(self.release_checkout / "scripts")]


def _absolute_executable(label: str, value: Path) -> Path:
    if not value.is_absolute() or not value.is_file() or not os.access(value, os.X_OK):
        raise InstallError(f"{label} must be an absolute path to an executable file: {value}")
    return value


def build_params(args: argparse.Namespace, *, home: Path | None = None) -> Params:
    home = home or Path.home()
    checkout = Path(args.release_checkout).expanduser().resolve() if args.release_checkout else None
    if checkout is None or not (checkout / COLLECTOR).is_file():
        raise InstallError(f"--release-checkout must contain {COLLECTOR}")
    tmux = shutil.which("tmux")
    claude = Path(args.claude_bin or home / ".local/bin/claude")
    codex = Path(args.codex_bin) if args.codex_bin else None
    if codex is None:
        raise InstallError("--codex-bin is required (machines.json codex_bin); PATH is never searched")
    usage_cwd = Path(args.usage_cwd or home)
    if not usage_cwd.is_absolute() or not usage_cwd.is_dir():
        raise InstallError(f"--usage-cwd must be an existing absolute directory: {usage_cwd}")
    return Params(
        host=args.host, release_checkout=checkout,
        python=_absolute_executable("--python", Path(args.python or sys.executable)),
        claude_bin=_absolute_executable("--claude-bin", claude),
        codex_bin=_absolute_executable("--codex-bin", codex),
        tmux_bin=_absolute_executable("--tmux-bin", Path(args.tmux_bin or tmux or "")),
        usage_cwd=usage_cwd, home=home,
    )


def render(template: bytes, params: Params, *, xml: bool = False) -> bytes:
    quote = escape if xml else (lambda value: value)
    values = {
        "__PENTACLE_RELEASE_CHECKOUT__": params.release_checkout, "__PENTACLE_USER_HOME__": params.home,
        "__PENTACLE_PYTHON__": params.python, "__PENTACLE_CLAUDE_BIN__": params.claude_bin,
        "__PENTACLE_CODEX_BIN__": params.codex_bin, "__PENTACLE_TMUX_BIN__": params.tmux_bin,
        "__PENTACLE_USAGE_CWD__": params.usage_cwd, "__PENTACLE_PATH__": params.path_value,
        "__PENTACLE_HOST_ID__": params.host,
    }
    for token, value in values.items():
        template = template.replace(token.encode(), quote(str(value)).encode())
    if b"__PENTACLE_" in template:
        raise InstallError("template has an unbound placeholder")
    return template


def render_systemd(params: Params) -> dict[str, bytes]:
    return {
        SERVICE: render((SATELLITE_DIR / SERVICE).read_bytes(), params),
        TIMER: render((SATELLITE_DIR / TIMER).read_bytes(), params),
    }


def render_launchd(params: Params) -> dict[str, bytes]:
    return {f"{LABEL}.plist": render((SATELLITE_DIR / f"{LABEL}.plist").read_bytes(), params, xml=True)}


def _systemd() -> bool:
    return sys.platform.startswith("linux")


def target_files(params: Params) -> dict[Path, bytes]:
    if _systemd():
        base = params.home / ".config/systemd/user"
        return {base / name: data for name, data in render_systemd(params).items()}
    return {params.home / "Library/LaunchAgents" / name: data for name, data in render_launchd(params).items()}


def preimage_dir(params: Params) -> Path:
    return params.home / ".pentacle/usage-collector-preimage"


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


def install(params: Params, runner: Runner = _run) -> dict:
    """Write the rendered files (keeping preimages) and prepare directories; never activates."""
    files = target_files(params)
    store = preimage_dir(params)
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
    (params.home / ".pentacle").mkdir(parents=True, exist_ok=True)
    params.state_path.parent.mkdir(parents=True, exist_ok=True)
    for path, data in files.items():
        _write_atomic(path, data)
    if _systemd():
        _checked(runner, "systemctl", "--user", "daemon-reload")
    return {"written": sorted(str(path) for path in files), "preimage": str(store)}


def enable(params: Params, runner: Runner = _run) -> None:
    if _systemd():
        _checked(runner, "systemctl", "--user", "enable", "--now", TIMER)
    else:
        domain = f"gui/{os.getuid()}"
        plist = next(iter(target_files(params)))
        runner(("launchctl", "bootout", f"{domain}/{LABEL}"))
        _checked(runner, "launchctl", "bootstrap", domain, str(plist))


def rollback(params: Params, runner: Runner = _run) -> dict:
    manifest_path = preimage_dir(params) / "manifest.json"
    if not manifest_path.exists():
        raise InstallError("no preimage manifest; nothing to roll back")
    if _systemd():
        runner(("systemctl", "--user", "disable", "--now", TIMER))
    else:
        runner(("launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"))
    restored = []
    for path_text, saved in json.loads(manifest_path.read_text()).items():
        path = Path(path_text)
        if saved == "absent":
            path.unlink(missing_ok=True)
        else:
            _write_atomic(path, (preimage_dir(params) / saved).read_bytes())
        restored.append(path_text)
    if _systemd():
        runner(("systemctl", "--user", "daemon-reload"))
    return {"restored": restored}


def _cache_fetched_ms(home: Path):
    try:
        data = json.loads((home / ".claude.json").read_text())
        return (data.get("cachedUsageUtilization") or {}).get("fetchedAtMs")
    except (OSError, ValueError):
        return None


def verify(params: Params) -> dict:
    """Run the rendered collector command once in the rendered environment and report evidence."""
    env = {**{key: value for key, value in os.environ.items() if "TOKEN" not in key.upper()},
           **params.environment}
    before = _cache_fetched_ms(params.home)
    cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    result = subprocess.run(params.command, capture_output=True, text=True, env=env, cwd=str(params.usage_cwd),
                            timeout=200, check=False)
    wall = time.monotonic() - started
    cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    observations = None
    try:
        observations = json.loads(params.state_path.read_text()).get("observations")
    except (OSError, ValueError):
        pass
    return {
        "exit_code": result.returncode, "wall_seconds": round(wall, 3),
        "child_cpu_user_seconds": round(cpu_after.ru_utime - cpu_before.ru_utime, 3),
        "child_cpu_system_seconds": round(cpu_after.ru_stime - cpu_before.ru_stime, 3),
        "claude_cache_fetched_at_ms_before": before, "claude_cache_fetched_at_ms_after": _cache_fetched_ms(params.home),
        "observations": observations, "stderr_tail": (result.stderr or "")[-300:],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True, help="registered machine name (PENTACLE_HOST_ID)")
    parser.add_argument("--release-checkout", help="pinned release checkout the job runs from")
    parser.add_argument("--python"); parser.add_argument("--claude-bin"); parser.add_argument("--codex-bin")
    parser.add_argument("--tmux-bin"); parser.add_argument("--usage-cwd", help="trusted Claude workspace (default: $HOME)")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--print", action="store_true", help="render only")
    action.add_argument("--verify", action="store_true")
    action.add_argument("--enable", action="store_true")
    action.add_argument("--rollback", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.rollback:
            params = Params(args.host, Path(), Path(), Path(), Path(), Path(), Path(), Path.home())
            print(json.dumps(rollback(params)))
            return 0
        params = build_params(args)
        if args.print:
            for path, data in target_files(params).items():
                print(f"# {path}\n{data.decode()}")
        elif args.verify:
            report = verify(params)
            print(json.dumps(report, indent=2))
            return 0 if report["exit_code"] == 0 else 1
        elif args.enable:
            enable(params)
            print(json.dumps({"enabled": True}))
        else:
            print(json.dumps(install(params)))
    except InstallError as exc:
        print(f"install_usage_collector: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
