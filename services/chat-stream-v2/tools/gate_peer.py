#!/usr/bin/env python3
"""Run a bundled commit's gate on one configured peer."""
import argparse
import json
import inspect
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid

SERVICE = Path(__file__).resolve().parents[1]
ROOT = SERVICE.parents[1]
sys.path.insert(0, str(SERVICE))
import machines
GATE_SCRIPTS = ROOT / "scripts"

def closed_env(scratch, venv):
    sys.path.insert(0, str(GATE_SCRIPTS))
    import gate_env
    config = gate_env.load_config()
    profile = config["profiles"]["pentacle"]
    search = os.pathsep.join([str(venv / "bin"), *config["search_path"]["darwin" if sys.platform == "darwin" else "linux"], *config["system_path"]])
    toolchain = {name: {"path": path} for name in ("node", "npm", "tmux") if (path := shutil.which(name, path=search))}
    env = gate_env.build_env(config, profile, profile["targets"]["v2-contract"], checkout=scratch / "repo", toolchain=toolchain,
                              venv=venv, home=scratch / "home", tmp=scratch / "tmp", locale_name="en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8",
                              evidence=scratch / "evidence", attempt_id="peer", pass_env={}, ambient={})
    return {key: value for key, value in env.items() if not key.startswith("PENTACLE_")}

def bundle_has_commit(bundle, commit):
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        return False
    try:
        with open(bundle, "rb") as stream:
            header = []
            while line := stream.readline().strip():
                header.append(line.decode("ascii"))
        prerequisites = [line[1:].split()[0] for line in header if line.startswith("-")]
        heads = [line.split()[0] for line in header if line and line[0] in "0123456789abcdef"]
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "clone", "-qs", "--no-checkout", str(ROOT), tmp + "/repo"], check=True, capture_output=True)
            for head in heads:
                subprocess.run(["git", "-C", tmp + "/repo", "fetch", "-q", str(Path(bundle).resolve()), head], check=True, capture_output=True)
            result = subprocess.run(["git", "-C", tmp + "/repo", "rev-list", *heads, "--not", *prerequisites], check=True, capture_output=True, text=True)
            return commit in result.stdout.splitlines()
    except (OSError, UnicodeError, subprocess.CalledProcessError):
        return False

REMOTE = r'''
import getpass, io, os, shutil, socket, subprocess, sys, tarfile, tempfile
from pathlib import Path
sys.dont_write_bytecode = True
venv, url, commit, tier, workers, run_id, min_free_mb = sys.argv[1:]
lock = Path("/tmp") / "pentacle-peer-gate.lock"
try:
    lock.mkdir(mode=0o700)
except FileExistsError:
    try: owner = (lock / "owner").read_text()
    except OSError: owner = "unknown"
    print("busy owner=" + owner, file=sys.stderr); sys.exit(73)
try:
    (lock / "owner").write_text(run_id)
    if shutil.disk_usage("/tmp").free < int(min_free_mb) * 1048576: raise RuntimeError("insufficient scratch space")
    with tempfile.TemporaryDirectory(prefix="peer-gate-", dir="/tmp") as directory:
        scratch = Path(directory); repo = scratch / "repo"
        (scratch / "home").mkdir(); (scratch / "tmp").mkdir(); (scratch / "evidence").mkdir()
        bundle = scratch / "candidate.bundle"; bundle.write_bytes(sys.stdin.buffer.read())
        git_env = {"HOME": str(scratch / "home"), "TMPDIR": str(scratch / "tmp"), "PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
        def step(argv):
            result = subprocess.run(argv, env=git_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if result.returncode: raise RuntimeError("failed " + argv[0] + ": " + result.stderr.decode(errors="replace")[-500:])
        step(["git", "clone", "-q", "--no-checkout", url, str(repo)])
        step(["git", "-C", str(repo), "fetch", "-q", str(bundle), commit])
        step(["git", "-C", str(repo), "checkout", "-q", "--detach", commit])
        GATE_SCRIPTS = repo / "scripts"
        env = closed_env(scratch, Path(venv))
        runner = repo / "services/chat-stream-v2/tools/run_gate.py"
        proc = subprocess.run([str(Path(venv) / "bin/python"), str(runner), tier, "--unit-workers", workers,
                               "--evidence-dir", str(scratch / "evidence"), "--basetemp", str(scratch / "tmp/gate")],
                              cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (scratch / "evidence" / "runner.log").write_bytes(proc.stdout)
        replacements = [getpass.getuser(), socket.gethostname(), venv]
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as archive:
            for path in (scratch / "evidence").iterdir():
                if path.name in ("v2-" + tier + "-evidence.json", tier + ".log", "preflight.log", "runner.log"):
                    data = path.read_bytes()
                    for item in replacements:
                        if item: data = data.replace(item.encode(), b"[redacted]")
                    info = tarfile.TarInfo(path.name); info.size = len(data); archive.addfile(info, io.BytesIO(data))
        sys.exit(proc.returncode)
except Exception as exc:
    message = str(exc)
    for item in (getpass.getuser(), socket.gethostname(), venv, url):
        if item: message = message.replace(item, "[redacted]")
    print("peer failure: " + message, file=sys.stderr); sys.exit(1)
finally:
    try:
        if (lock / "owner").read_text() == run_id:
            (lock / "owner").unlink(); lock.rmdir()
    except OSError: pass
'''

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog="A leftover lock requires manual, owner-verified removal; this tool never reaps it.")
    parser.add_argument("--peer", required=True); parser.add_argument("--tier", choices=("unit", "contract"), required=True)
    parser.add_argument("--bundle", required=True); parser.add_argument("--commit", required=True)
    parser.add_argument("--venv", required=True); parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    args = parser.parse_args(argv)
    if not bundle_has_commit(args.bundle, args.commit):
        print("bundle does not contain commit", file=sys.stderr); return 1
    peer = next((m for m in machines.load_machines() if m.name == args.peer and m.ssh_target), None)
    if peer is None:
        print("unknown remote peer", file=sys.stderr); return 1
    try:
        origin = subprocess.check_output(["git", "-C", str(ROOT), "remote", "get-url", "origin"], text=True).strip()
        code = "import os, shutil, sys\nfrom pathlib import Path\n" + inspect.getsource(closed_env) + "\n" + REMOTE
        command = shlex.join(["python3", "-c", code, args.venv, origin, args.commit, args.tier, str(args.workers), uuid.uuid4().hex, str(json.loads((ROOT / "scripts/gate-env.json").read_text())["min_free_mb"])])
        result = subprocess.run(machines.ssh_command(peer.ssh_target, command, multiplex=False), input=Path(args.bundle).read_bytes(), capture_output=True, timeout={"contract": 600, "unit": 18300}[args.tier])
        evidence = {}
        if result.stdout:
            args.evidence_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:gz") as archive:
                for member in archive:
                    if member.isfile() and member.name in ("v2-" + args.tier + "-evidence.json", args.tier + ".log", "preflight.log", "runner.log"):
                        data = archive.extractfile(member).read()
                        for item in (args.peer, peer.ssh_target): data = data.replace(item.encode(), b"[redacted]")
                        evidence[member.name] = data
                        (args.evidence_dir / member.name).write_bytes(data)
        if result.returncode:
            print(args.tier + " FAIL: " + (result.stderr.decode(errors="replace") or "remote gate failed"), file=sys.stderr); return result.returncode
        receipt = json.loads(evidence["v2-" + args.tier + "-evidence.json"])
        if receipt.get("sha") != args.commit:
            print("runner evidence commit mismatch", file=sys.stderr); return 1
        print(args.tier + " PASS"); return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print("peer failure: " + str(exc), file=sys.stderr); return 1

if __name__ == "__main__":
    sys.exit(main())
