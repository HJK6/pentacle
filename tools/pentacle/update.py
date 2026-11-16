"""Release checks and checklist preparation; never operate services or code."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

from . import discovery as d
from .doctor import Snapshot
from .install import atomic_json, commit, identity, release_manifest
from .releases import ancestor
from .stores import Refused, backup, load_plan


def selection(root, remote):
    manifest = release_manifest(root, remote)
    entries = (manifest.get("data") or {}).get("releases", [])
    if not entries:
        raise Refused("No stable release is available. Run pentacle version to inspect the release source.")
    head = commit(root)
    if head is None:
        raise Refused("The install must be a Git checkout.")
    target = entries[-1]
    installed = next((entry for entry in entries if entry["commit"] == head), None)
    names = {head, installed["version"] if installed else None}
    tested = bool(names.intersection(target["tested_update_from"]))
    between = [entry for entry in entries if entry["commit"] != head
               and ancestor(root, head, entry["commit"])
               and ancestor(root, entry["commit"], target["commit"])]
    return manifest, head, installed, target, tested, between


def notes(root, entry):
    shown = d.run(["git", "-C", root, "show", entry["commit"] + ":CHANGELOG.md"])
    if shown.returncode:
        raise Refused("Release notes could not be read.")
    sections, heading = {}, None
    for line in shown.stdout.splitlines():
        if line.startswith("## "):
            heading = line[3:]
            sections[heading] = [line]
        elif heading:
            sections[heading].append(line)
    if any(key not in sections for key in entry["changelog_sections"]):
        raise Refused("A release notes section is missing.")
    return "\n".join("\n".join(sections[key]).strip() for key in entry["changelog_sections"])


def check(args, root):
    from .cli import version_text
    manifest, head, installed, target, tested, between = selection(root, args.release_remote)
    # A release check needs neither a running install nor its dependencies.
    value = identity(root, manifest, None, None)
    value["tested_start"] = tested
    if args.json:
        print(json.dumps(value))
    else:
        version_text(value)
        print("Directly tested starting point: " + ("yes" if tested else "no"))
        if not ancestor(root, head, target["commit"]):
            print("The installed commit is not an ancestor of the latest release.")
        for entry in between:
            print("\nRelease " + entry["version"])
            print(notes(root, entry))
            for action in entry["required_actions"]:
                print("Required action: " + action)
    return 0


def private_text(path, content, mode=0o600):
    # The directory is private before any environment values are written.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())


def carried_environment(proc):
    extra = os.environ.get("PENTACLE_KEEP_ENV", "").split(",")
    extra = {key.strip() for key in extra if key.strip()}
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in extra):
        raise Refused("PENTACLE_KEEP_ENV must contain comma-separated environment variable names.")
    return {key: value for key, value in proc.env.items()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
            and (key in {"PATH", "HOME"} or key.startswith("PENTACLE_") or key in extra)}


def prepare(args, root):
    deploy = root / ".pentacle-deploy"
    pointer = deploy / "upgrade-current"
    # Invalidate a prior plan BEFORE all checks, including release selection.
    pointer.unlink(missing_ok=True)
    manifest, head, installed, target, tested, between = selection(root, args.release_remote)
    dirty = d.run(["git", "-C", root, "status", "--porcelain", "--untracked-files=no"])
    if dirty.returncode or dirty.stdout:
        raise Refused("Tracked files are modified. Preserve those changes before preparing an upgrade.")
    helper = Path(__file__).resolve().parents[2]
    try:
        helper_commit = (helper / "COMMIT").read_text().strip()
    except OSError:
        helper_commit = commit(helper)
    if helper_commit != target["commit"]:
        raise Refused("The helper does not match the selected release. Repeat step 1 of UPGRADE.md.")
    if not ancestor(root, head, target["commit"]):
        raise Refused("The installed commit is not an ancestor of the selected release.")
    actions = [action for entry in between for action in entry["required_actions"]]
    if actions and not args.ack_required_actions:
        for action in actions:
            print("Required action: " + action, file=sys.stderr)
        raise Refused("Complete the required actions, then repeat prepare with --ack-required-actions.")
    snapshot = Snapshot(args, fetch=False, write_cache=False)
    processes = {"daemon": snapshot.daemon_process, "web": snapshot.web_process}
    if any(proc is None or proc.checkout(kind) != root or not proc.start
           for kind, proc in processes.items()):
        raise Refused("The daemon and web host must both be running locally from this install.")
    if snapshot.daemon is None or snapshot.web is None:
        raise Refused("The running daemon and web host must both answer before preparing an upgrade.")
    if not snapshot.stores:
        raise Refused("The running daemon's database files could not be discovered.")
    environments = {kind: carried_environment(proc) for kind, proc in processes.items()}
    # Capture first and verify identities again before publishing the pointer.
    for proc in processes.values():
        current = d.process(proc.pid)
        if current is None or current.start != proc.start:
            raise Refused("A process changed while preparing. Repeat prepare with both processes running.")
    deploy.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="upgrade-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-", dir=deploy))
    record = {"schema_version": 1, "previous": {"commit": head, "version": installed["version"] if installed else None},
              "target": {"commit": target["commit"], "version": target["version"]}, "tested_start": tested,
              "required_actions": actions, "stores": [str(path) for path in snapshot.stores]}
    for kind, proc in processes.items():
        argv = list(proc.argv)
        if kind == "daemon":
            argv[0] = snapshot.python
        record[kind] = {"pid": proc.pid, "start": proc.start, "argv": argv, "cwd": str(proc.cwd),
                        "environment": sorted(environments[kind])}
        # env -i enforces the same allowlist even if launched from a rich shell.
        command = ["/usr/bin/env", "-i", *[key + "=" + value for key, value in environments[kind].items()], *argv]
        script = "#!/bin/bash\nset -eu\ncd " + shlex.quote(str(proc.cwd)) + "\nexec " + shlex.join(command) + "\n"
        private_text(directory / ("start-" + kind + ".sh"), script, 0o700)
    atomic_json(directory / "plan.json", record)
    values = {"plan": str(directory), "previous": head, "target": target["commit"], "py": snapshot.python}
    exported = {"PENTACLE_CONFIG": str(snapshot.config_path), "PENTACLE_DAEMON_URL": snapshot.daemon_url,
                "PENTACLE_WEB_URL": snapshot.web_url, "PENTACLE_RELEASE_REMOTE": args.release_remote}
    text = "".join(key + "=" + shlex.quote(value) + "\n" for key, value in values.items())
    text += "".join("export " + key + "=" + shlex.quote(value) + "\n" for key, value in exported.items())
    private_text(directory / "plan.env", text)
    restore = "#!/bin/bash\nset -eu\nexec " + shlex.join([sys.executable, str(helper / "tools/pentacle/stores.py"), str(directory)]) + "\n"
    private_text(directory / "restore-stores.sh", restore, 0o700)
    private_text(pointer, str(directory) + "\n")
    print("Upgrade plan prepared. Continue with UPGRADE.md; no service or source was changed.")
    print("Release source: " + manifest["release_remote"])
    print("Directly tested starting point: " + ("yes" if tested else "no"))
    return 0


def update(args):
    # The mode-less command must not even fetch or write a cache.
    if not (args.check or args.prepare or args.backup):
        print("Updating is done by following UPGRADE.md. Use --check to inspect available releases.", file=sys.stderr)
        return 2
    root = Path(args.install).expanduser().resolve()
    try:
        if args.check:
            return check(args, root)
        if args.prepare:
            return prepare(args, root)
        directory, plan = load_plan(root)
        backup(directory, plan)
        print("All database copies are verified. Continue with UPGRADE.md.")
        return 0
    except (Refused, OSError, ValueError):
        if args.prepare:
            (root / ".pentacle-deploy/upgrade-current").unlink(missing_ok=True)
        error = sys.exc_info()[1]
        print(str(error) if isinstance(error, Refused) else "The checklist files could not be read or written.", file=sys.stderr)
        return 2
