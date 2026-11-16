#!/usr/bin/env python3
"""Install (or uninstall) the orphan-reaper launchd job from the release checkout.

Mirrors deploy.py's plist path: render `__PENTACLE_RELEASE_CHECKOUT__`, keep a
rollback copy of any existing plist, write atomically, then bootout+bootstrap.
Separate from the daemon deploy so this lane touches zero daemon runtime.

  install (default `--act` production form; only after the operator confirmed a dry-run card):
      python deploy/install_orphan_reaper.py --release-checkout ~/repos/pentacle-v2
  arm dry-run instead (side-effect-free):
      python deploy/install_orphan_reaper.py --dry-run
  roll back to the saved copy / remove:
      python deploy/install_orphan_reaper.py --rollback
      python deploy/install_orphan_reaper.py --uninstall
"""
from __future__ import annotations

import argparse
from xml.sax.saxutils import escape
import os
import subprocess
import sys
import time
from pathlib import Path

LABEL = "com.pentacle.orphan-reaper"
TEMPLATE = Path(__file__).resolve().parent / "com.pentacle.orphan-reaper.plist"
TOKEN = b"__PENTACLE_RELEASE_CHECKOUT__"
DEFAULT_RELEASE_CHECKOUT = Path.home() / "repos" / "pentacle-v2"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _backup_path() -> Path:
    return _plist_path().with_suffix(".plist.bak")


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_bytes(data)
    tmp.replace(path)


def _render(repo: Path, dry_run: bool) -> bytes:
    src = TEMPLATE.read_bytes()
    if TOKEN not in src:
        raise SystemExit("template missing release-checkout placeholder")
    out = src.replace(TOKEN, escape(str(repo)).encode()).replace(
        b"__PENTACLE_USER_HOME__", escape(str(Path.home())).encode()
    )
    if dry_run:
        # Drop the `--act` argument line to arm the side-effect-free form.
        out = out.replace(b"    <string>--act</string>\n", b"")
    return out


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], text=True, capture_output=True, check=False)


def _reload(path: Path) -> None:
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LABEL}")
    time.sleep(0.3)
    res = _launchctl("bootstrap", domain, str(path))
    if res.returncode != 0:
        raise SystemExit(f"bootstrap failed: {(res.stderr or res.stdout).strip()}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--release-checkout", type=Path, default=DEFAULT_RELEASE_CHECKOUT)
    ap.add_argument("--dry-run", action="store_true", help="Arm the reaper in dry-run (no --act).")
    ap.add_argument("--rollback", action="store_true", help="Restore the saved .plist.bak and reload.")
    ap.add_argument("--uninstall", action="store_true", help="Bootout and remove the job.")
    args = ap.parse_args(argv)
    dst = _plist_path()

    if args.uninstall:
        _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
        if dst.exists():
            dst.unlink()
        print(f"uninstalled {LABEL}")
        return 0
    if args.rollback:
        bak = _backup_path()
        if not bak.exists():
            raise SystemExit("no rollback copy present")
        _write_atomic(dst, bak.read_bytes())
        _reload(dst)
        print(f"rolled back {LABEL} from {bak}")
        return 0

    # Keep a rollback copy of any existing plist before overwriting.
    if dst.exists():
        _write_atomic(_backup_path(), dst.read_bytes())
    _write_atomic(dst, _render(args.release_checkout, args.dry_run))
    _reload(dst)
    mode = "dry-run" if args.dry_run else "act"
    print(f"installed {LABEL} ({mode}, 6h) -> {dst}"
          + (f"  (rollback copy: {_backup_path()})" if _backup_path().exists() else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
