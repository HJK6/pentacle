"""Explicit stopped-process database copies and restore for UPGRADE.md."""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pentacle import discovery as d
else:
    from . import discovery as d


class Refused(ValueError):
    pass


def validate(plan):
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise Refused("The upgrade plan is unreadable. Repeat prepare.")
    if not isinstance(plan.get("stores"), list) or not plan["stores"] or any(not isinstance(v, str) or not Path(v).is_absolute() for v in plan["stores"]):
        raise Refused("The upgrade plan has no valid database paths.")
    for kind in ("daemon", "web"):
        proc = plan.get(kind)
        if (not isinstance(proc, dict) or type(proc.get("pid")) is not int or proc["pid"] <= 0
                or not isinstance(proc.get("start"), str) or not proc["start"]
                or not isinstance(proc.get("argv"), list) or not proc["argv"]
                or any(not isinstance(v, str) for v in proc["argv"])
                or not isinstance(proc.get("cwd"), str)):
            raise Refused("The upgrade plan has no valid process identities.")
    return plan


def load_plan(root):
    deploy = (root / ".pentacle-deploy").resolve()
    try:
        directory = Path((deploy / "upgrade-current").read_text().strip()).resolve(strict=True)
        if directory.parent != deploy or not directory.name.startswith("upgrade-"):
            raise ValueError
    except (OSError, ValueError):
        raise Refused("No current upgrade plan exists. Repeat prepare.") from None
    return directory, validate(d.read_json(directory / "plan.json"))


def assert_stopped(plan):
    # The old PID detects the original process. A process scan detects one
    # restarted by the saved launcher, even before it starts listening.
    for kind in ("daemon", "web"):
        saved = plan[kind]
        try:
            os.kill(saved["pid"], 0)
        except ProcessLookupError:
            pass
        else:
            current = d.process(saved["pid"])
            if current is None or current.start == saved["start"]:
                raise Refused("Stop both the daemon and web host before copying or restoring databases.")
    if sys.platform.startswith("linux"):
        pids = [int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit()]
    elif sys.platform == "darwin":
        result = d.run(["ps", "-U", os.getuid(), "-o", "pid="])
        if result.returncode:
            raise Refused("Running process identities could not be checked.")
        pids = [int(line) for line in result.stdout.splitlines() if line.strip().isdigit()]
    else:
        raise Refused("Process checks require macOS or Linux.")
    for pid in pids:
        if sys.platform == "darwin":
            try:
                argv, _ = d._mac_args(pid)
            except (OSError, ValueError, IndexError):
                continue
            if not any(value.endswith(("/main.py", "/server/index.js")) or value in {"main.py", "server", "server/index.js"} for value in argv):
                continue
        current = d.process(pid)
        if current is None:
            continue
        for kind in ("daemon", "web"):
            saved = plan[kind]
            previous = d.Process(0, saved["argv"], Path(saved["cwd"]))
            if (current.cwd == previous.cwd and current.argv == previous.argv
                    or current.checkout(kind) is not None and current.checkout(kind) == previous.checkout(kind)):
                raise Refused("Stop both the daemon and web host before copying or restoring databases.")


def verified(path):
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            return db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    except (sqlite3.Error, OSError):
        return False


def backup(directory, plan):
    assert_stopped(plan)
    for index, name in enumerate(plan["stores"]):
        destination = directory / f"store-{index}.db"
        if destination.exists():
            if not verified(destination):
                raise Refused("An existing database copy failed its integrity check; it was not overwritten.")
            continue
        temporary = destination.with_suffix(".db.tmp")
        temporary.unlink(missing_ok=True)
        try:
            with closing(sqlite3.connect(Path(name).resolve().as_uri() + "?mode=ro", uri=True)) as source:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
                with closing(sqlite3.connect(temporary)) as target:
                    source.backup(target)
            if not verified(temporary):
                raise Refused("A database copy failed its integrity check.")
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            # Publish without replacing a concurrently completed first copy.
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if not verified(destination):
                    raise Refused("An existing database copy failed its integrity check.")
        except sqlite3.Error:
            raise Refused("A database could not be copied.") from None
        finally:
            temporary.unlink(missing_ok=True)
    (directory / "backup-complete").touch(mode=0o600)


def restore(directory):
    plan = validate(d.read_json(directory / "plan.json"))
    assert_stopped(plan)
    copies = [directory / f"store-{index}.db" for index in range(len(plan["stores"]))]
    if not (directory / "backup-complete").is_file() or not all(verified(path) for path in copies):
        raise Refused("Complete verified database copies are required before restore.")
    for name, source in zip(plan["stores"], copies):
        destination = Path(name)
        # Restore is explicit and deliberately discards writes since the copy.
        temporary = destination.with_name(destination.name + ".pentacle-restore-tmp")
        try:
            shutil.copyfile(source, temporary)
            temporary.chmod(0o600)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            temporary.replace(destination)
            for suffix in ("-wal", "-shm"):
                Path(str(destination) + suffix).unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        restore(Path(sys.argv[1]))
    except (Refused, OSError, ValueError, IndexError) as error:
        print(str(error) if isinstance(error, Refused) else "Database restore could not complete.", file=sys.stderr)
        raise SystemExit(2)
