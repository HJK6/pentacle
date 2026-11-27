#!/usr/bin/env python3
"""Stage and prove account helpers before a plain daemon release."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
PLAIN = SERVICE / "deploy/plain_release.sh"
sys.path[:0] = [str(SERVICE), str(SERVICE.parent)]
from deploy import coordinated_runtime as cr  # noqa: E402
from deploy.coordinated import Invalid  # noqa: E402

PREFIX = "PENTACLE_RELEASE_"
SETTINGS = ("RUNTIME_CHECKOUT", "LAUNCHD_LABEL", "RUNTIME_PYTHON", "DAEMON_URL")
SHA = re.compile(r"[0-9a-f]{40}")


def say(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


def read(path):
    return json.loads(Path(path).read_bytes())


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(data)
            f.flush()
            os.fchmod(f.fileno(), mode)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def save(work, state):
    atomic_write(work / "state.json", json.dumps(state, indent=1, sort_keys=True).encode(), 0o600)


def run(argv, cwd=None):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120)


def environment(label):
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    env, argv = plist["EnvironmentVariables"], plist["ProgramArguments"]
    return (Path(env["PENTACLE_PROVIDER_ACCOUNT_POLICY"]), Path(env["PENTACLE_MACHINES_FILE"]),
            Path(argv[argv.index("--db") + 1]))


def prepare(args, work):
    if not SHA.fullmatch(args.target) or run(("git", "rev-parse", "HEAD"), SERVICE).stdout.strip() != args.target:
        raise Invalid("run from a tooling worktree checked out at exactly TARGET")
    policy_path, machines_path, database = environment(args.label)
    policy_stat(policy_path)
    policy_bytes = policy_path.read_bytes()
    policy = json.loads(policy_bytes)
    machines = read(machines_path)
    machines = {row["name"]: row for row in (machines["machines"] if isinstance(machines, dict) else machines)}
    hosts = args.hosts or list(policy["managed_hosts"])
    if not hosts or len(set(hosts)) != len(hosts) or set(hosts) - (set(policy["executors"]) & set(machines)):
        raise Invalid("each selected host must be distinct and have a policy executor and machine record")
    # Read-only ledger preflight, with the coordinated scope fence.
    accounts = cr.Runtime.account_snapshot(None, policy, str(database), scope={"included_hosts": hosts})
    installer = cr.load_installer(SERVICE.parents[1])
    identity = cr.code_identity_at(args.runtime, args.target, run)
    state = {"target": args.target, "identity": identity,
             "policy": {"path": str(policy_path), "sha256": digest(policy_bytes)},
             "hosts": {}, "phase": "prepared"}
    save(work, state)
    say("preflight ok:", "hosts", hosts, "identity", identity[:16], "pin", accounts["pin_digest"][:16])

    bundle = work / "bundle"
    bundle.mkdir(mode=0o700)
    overlay = args.runtime / "services/_shared/spawn_defaults.local.json"
    manifest = installer._archive(args.runtime, args.target, bundle, provider_cli_pin=accounts["pin"],
                                  spawn_settings=overlay.read_bytes() if overlay.exists() else None)
    state["manifest_archive_sha256"] = manifest["archive_sha256"]
    save(work, state)
    say("bundle", manifest["archive_sha256"][:16])

    prover = types.SimpleNamespace(installer=installer)
    prover.remote_json = lambda helper, argv: cr.Runtime.remote_json(prover, helper, argv)
    for host in hosts:
        executor = policy["executors"][host]
        root = str(cr.helper_release_root(executor["script"]))
        ssh = machines[host].get("ssh_target")
        if args.command == "rehearse" and ssh is not None:
            raise Invalid("rehearsal host must use the local executor")
        if ssh is not None and (not isinstance(ssh, str) or not ssh or ssh.startswith("-")):
            raise Invalid("invalid machine SSH target")
        target = installer.Target(host, ssh or "", root, ssh is None)
        release = str(Path(root) / args.target)
        existed = bool(installer._remote(target, f"test -e {installer._remote_quote(release)} && printf yes || true", dry_run=False))
        release = installer.stage_helper_source(target, bundle, args.target, manifest)
        helper = {"target": vars(target), "python": executor["python"], "script": release + "/app/" + cr.HELPER,
                  "old_script": executor["script"], "created": not existed}
        state["hosts"][host] = helper
        save(work, state)
        say("staged", host, release, "(already present)" if existed else "(new)")
        proof = cr.Runtime._helper_proof(prover, helper, accounts, identity, manifest, args.target)
        helper["proof"] = {"identity": proof["closure"]["identity"], "python": proof["closure"]["python"],
                           "artifacts": [item.get("binding_key") for item in proof["artifacts"]]}
        save(work, state)
        say("proved", host, "identity", proof["closure"]["identity"][:16], "bindings", len(proof["artifacts"]))
    return state, policy


def policy_stat(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise Invalid("policy must be a regular file owned by the release user")
    return info


def preimage(work, state):
    meta = state["policy"]
    path = Path(meta["path"])
    before = path.read_bytes()
    if digest(before) != meta["sha256"]:
        raise Invalid("policy changed since preflight")
    atomic_write(work / "policy.before", before, 0o600)
    meta["mode"] = policy_stat(path).st_mode & 0o777
    save(work, state)


def rewrite(work, state, policy):
    after = json.loads(json.dumps(policy))
    for host, helper in state["hosts"].items():
        after["executors"][host]["script"] = helper["script"]
    meta = state["policy"]
    path = Path(meta["path"])
    expected = (json.dumps(after, indent=2) + "\n").encode()
    meta["written_sha256"] = digest(expected)
    save(work, state)
    if policy_stat(path).st_mode & 0o777 != meta["mode"] or digest(path.read_bytes()) != meta["sha256"]:
        raise Invalid("policy changed since its preimage")
    atomic_write(path, expected, meta["mode"])
    if path.read_bytes() != expected:
        raise Invalid("policy readback differs from the expected rewrite")
    state["phase"] = "policy_written"
    save(work, state)
    say("policy rewritten for", sorted(state["hosts"]))


def restore_policy(work):
    state = read(work / "state.json")
    meta = state["policy"]
    path = Path(meta["path"])
    before = (work / "policy.before").read_bytes()
    if digest(before) != meta["sha256"]:
        raise Invalid("saved policy preimage is damaged")
    policy_stat(path)
    current = digest(path.read_bytes())
    if current != meta["sha256"]:
        if current != meta.get("written_sha256"):
            raise Invalid("current policy differs from both saved and written policy")
        atomic_write(path, before, meta["mode"])
    if path.read_bytes() != before or (path.stat().st_mode & 0o777) != meta["mode"]:
        raise Invalid("policy readback differs from its preimage")
    state["phase"] = "policy_restored"
    save(work, state)
    say("policy restored to", meta["sha256"][:16])
    return 0


def unstage(work):
    state = read(work / "state.json")
    policy = read(state["policy"]["path"])
    installer = cr.load_installer(SERVICE.parents[1])
    for host, helper in state["hosts"].items():
        release = str(Path(helper["script"]).parents[3])
        if not helper["created"] or helper.get("removed"):
            continue
        if policy["executors"][host]["script"] == helper["script"]:
            raise Invalid(f"{host}: the policy names {release}; restore the policy first")
        target = installer.Target(**helper["target"])
        installer._remote(target, f"rm -r -- {installer._remote_quote(release)}", dry_run=False)
        gone = installer._remote(target, f"test -e {installer._remote_quote(release)} || printf gone", dry_run=False)
        if gone != "gone":
            raise Invalid(f"{host}: {release} still present")
        helper["removed"] = True
        save(work, state)
        say("removed", host, release)
    return 0


def plain(args, *, dry_run=False, work=None):
    env = os.environ.copy()
    env.pop(PREFIX + "PUTBACK_WORK", None)
    if work is not None:
        env[PREFIX + "PUTBACK_WORK"] = str(work)
    argv = [str(PLAIN), args.target]
    if dry_run:
        argv.append("--dry-run")
    return subprocess.run(argv, env=env).returncode


def release(args):
    work = args.work
    try:
        preflight = plain(args, dry_run=True, work=work)
    except OSError as exc:
        say("REFUSED: could not invoke plain release:", exc)
        return 2
    if preflight:
        say("REFUSED by plain release preflight; no helper was staged")
        return 2
    try:
        work.mkdir(mode=0o700)
    except OSError as exc:
        say("REFUSED: could not create a new work directory:", exc)
        return 2
    try:
        state, policy = prepare(args, work)
        preimage(work, state)
    except Exception as exc:
        saved = work / "state.json"
        staged = saved.is_file() and bool(read(saved)["hosts"])
        say("FAILED before the policy write:", type(exc).__name__, exc)
        if staged:
            say("nothing serving changed; use unstage to remove run-owned copies")
        return 3 if staged else 2
    if args.dry_run:
        say("DRY RUN: policy unchanged; staged copies are inert")
        return 0
    try:
        rewrite(work, state, policy)
    except Exception as exc:
        say("FAILED at the policy write:", exc)
        if "written_sha256" not in state["policy"]:
            return 3
        try:
            restore_policy(work)
        except Exception as restore_exc:
            say("NEEDS A HUMAN: policy restore failed:", restore_exc)
            return 5
        return 3
    try:
        code = plain(args, work=work)
    except OSError as exc:
        say("NEEDS A HUMAN: could not invoke plain release:", exc)
        return 5
    # The put-back subprocess may have updated the state file.
    state = read(work / "state.json")
    state["plain_release_exit"] = code
    if code in (0, 6):
        state["phase"] = "released"
    save(work, state)
    if code in (0, 6):
        say("RELEASED with new helpers. Verify the target welcome and single listener PID; "
            "confirm current pin proofs with code identity", state["identity"],
            "on released hosts; verify one local and one satellite account-bound spawn "
            "and a delivered tell before lifting the hold.")
        return code
    if code == 2:
        try:
            restore_policy(work)
        except Exception as exc:
            say("NEEDS A HUMAN: policy restore failed:", exc)
            return 5
        return 2
    if code == 4:
        say("PRIOR RESTORED: policy was put back before the prior restart")
        return 4
    say("NEEDS A HUMAN: plain release exit", code, "; state", work / "state.json")
    return 5


def rehearse(args):
    work = args.work
    result = 0
    try:
        work.mkdir(mode=0o700)
    except OSError as exc:
        say("REFUSED: could not create a new work directory:", exc)
        return 2
    try:
        state, _ = prepare(args, work)
        preimage(work, state)
        say("REHEARSAL: no policy write, no restart")
    except Exception as exc:
        result = 2
        say("REHEARSAL FAILED:", type(exc).__name__, exc)
    if (work / "state.json").is_file():
        try:
            unstage(work)
        except Exception as exc:
            say("NEEDS A HUMAN: rehearsal cleanup failed:", exc)
            return 5
        if result:
            return 3
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("release", "rehearse", "restore-policy", "unstage"):
        sub = commands.add_parser(name)
        sub.add_argument("--work", required=True, type=Path)
        if name in {"release", "rehearse"}:
            sub.add_argument("target")
            sub.add_argument("--hosts", nargs="+")
        if name == "release":
            sub.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    args.work = args.work.absolute()
    if args.command in {"release", "rehearse"}:
        values = {name: os.environ.get(PREFIX + name) for name in SETTINGS}
        missing = [PREFIX + name for name, value in values.items() if not value]
        if missing:
            parser.error("set " + ", ".join(missing))
        if not SHA.fullmatch(args.target):
            parser.error("TARGET must be a lowercase 40-character commit SHA")
        args.runtime = Path(values["RUNTIME_CHECKOUT"])
        args.label = values["LAUNCHD_LABEL"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.label):
            parser.error("invalid launchd label")
        python = Path(values["RUNTIME_PYTHON"])
        if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
            parser.error("runtime interpreter must be an executable absolute path")
        if args.work.exists() or args.work.is_symlink():
            parser.error("--work must name a new directory")
    if args.command == "rehearse":
        local = os.environ.get(PREFIX + "LOCAL_HOST")
        if not local or args.hosts != [local]:
            parser.error("rehearse requires --hosts matching PENTACLE_RELEASE_LOCAL_HOST")
    if args.command == "release":
        return release(args)
    if args.command == "rehearse":
        return rehearse(args)
    try:
        return (restore_policy if args.command == "restore-policy" else unstage)(args.work)
    except Exception as exc:
        say("NEEDS A HUMAN:", type(exc).__name__, exc)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
