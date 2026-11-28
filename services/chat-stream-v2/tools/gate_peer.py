#!/usr/bin/env python3
"""Run a bundled commit's gate on a configured peer."""
import argparse
import inspect
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
ROOT = SERVICE.parents[1]
sys.path.insert(0, str(SERVICE))
import machines


def closed_env(scratch, venv):
    sys.path.insert(0, str(scratch / "repo" / "scripts"))
    import gate_env

    config = gate_env.load_config()
    profile = config["profiles"]["pentacle"]
    platform = "darwin" if sys.platform == "darwin" else "linux"
    path = os.pathsep.join([str(venv / "bin"), *config["search_path"][platform], *config["system_path"]])
    tools = {n: {"path": p} for n in ("node", "npm", "tmux") if (p := shutil.which(n, path=path))}
    locale = "en_US.UTF-8" if platform == "darwin" else "C.UTF-8"
    return gate_env.build_env(
        config, profile, profile["targets"]["v2-contract"], checkout=scratch / "repo", toolchain=tools,
        venv=venv, home=scratch / "home", tmp=scratch / "tmp", locale_name=locale,
        evidence=scratch / "evidence", attempt_id="peer", pass_env={}, ambient={},
    )


REMOTE = r'''
import getpass
import io
import socket
import subprocess
import tarfile
import tempfile
from pathlib import Path
sys.dont_write_bytecode = True
venv, url, commit, tier, workers, run_id, min_free_mb = sys.argv[1:]
lock = Path("/tmp") / "pentacle-peer-gate.lock"
try:
    lock.mkdir(mode=0o700)
except FileExistsError:
    owner = (lock / "owner").read_text() if (lock / "owner").is_file() else "unknown"
    print("busy owner=" + owner, file=sys.stderr)
    sys.exit(73)
try:
    (lock / "owner").write_text(run_id)
    if shutil.disk_usage("/tmp").free < int(min_free_mb) * 1048576:
        raise RuntimeError("insufficient scratch space")
    with tempfile.TemporaryDirectory(prefix="peer-gate-", dir="/tmp") as directory:
        scratch = Path(directory)
        repo = scratch / "repo"
        for name in ("home", "tmp", "evidence"):
            (scratch / name).mkdir()
        bundle = scratch / "candidate.bundle"
        bundle.write_bytes(sys.stdin.buffer.read())
        git_env = {"HOME": str(scratch / "home"), "TMPDIR": str(scratch / "tmp"), "PATH": os.defpath,
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
        for cmd in (["git", "clone", "-q", "--no-checkout", url, str(repo)],
                    ["git", "fetch", "-q", str(bundle), commit],
                    ["git", "checkout", "-q", "--detach", commit]):
            subprocess.run(cmd, cwd=repo if repo.exists() else scratch, env=git_env, check=True, capture_output=True)
        runner = repo / "services/chat-stream-v2/tools/run_gate.py"
        cmd = [str(Path(venv) / "bin/python"), str(runner), tier, "--unit-workers", workers,
               "--evidence-dir", str(scratch / "evidence"), "--basetemp", str(scratch / "tmp/gate")]
        result = subprocess.run(cmd, cwd=repo, env=closed_env(scratch, Path(venv)), capture_output=True)
        (scratch / "evidence" / "runner.log").write_bytes(result.stdout + result.stderr)
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as archive:
            for path in (scratch / "evidence").iterdir():
                if path.name not in ("v2-" + tier + "-evidence.json", "runner.log"):
                    continue
                data = path.read_bytes()
                for item in (getpass.getuser(), socket.gethostname()):
                    data = data.replace(item.encode(), b"[redacted]")
                info = tarfile.TarInfo(path.name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        sys.exit(result.returncode)
except Exception as exc:
    reason = exc.cmd[1] if isinstance(exc, subprocess.CalledProcessError) else type(exc).__name__
    print("peer failure: " + str(reason), file=sys.stderr)
    sys.exit(1)
finally:
    try:
        if (lock / "owner").read_text() == run_id:
            (lock / "owner").unlink()
            lock.rmdir()
    except OSError:
        pass
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog="Remove a stale lock only after owner verification.")
    for name in ("peer", "bundle", "commit", "venv"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--tier", choices=("unit", "contract"), required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    args = parser.parse_args(argv)
    try:
        cmd = ["git", "-C", str(ROOT), "bundle"]
        verify = subprocess.run([*cmd, "verify", args.bundle], capture_output=True)
        heads = subprocess.run([*cmd, "list-heads", args.bundle], capture_output=True, text=True)
        commits = [line.split()[0] for line in heads.stdout.splitlines()]
        if verify.returncode or heads.returncode or args.commit not in commits:
            raise ValueError("bundle does not contain commit")
        peer = next(m for m in machines.load_machines() if m.name == args.peer and m.ssh_target)
        origin = subprocess.check_output(["git", "-C", str(ROOT), "remote", "get-url", "origin"], text=True).strip()
        code = "import os, shutil, sys\nfrom pathlib import Path\n" + inspect.getsource(closed_env) + "\n" + REMOTE
        floor = json.loads((ROOT / "scripts/gate-env.json").read_text())["min_free_mb"]
        command = shlex.join(["python3", "-c", code, args.venv, origin, args.commit, args.tier,
                              str(args.workers), os.urandom(16).hex(), str(floor)])
        result = subprocess.run(machines.ssh_command(peer.ssh_target, command, multiplex=False),
                                input=Path(args.bundle).read_bytes(), capture_output=True,
                                timeout={"contract": 600, "unit": 18600}[args.tier])
        evidence = {}
        if result.stdout:
            args.evidence_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:gz") as archive:
                for item in archive:
                    if item.isfile() and item.name in ("v2-" + args.tier + "-evidence.json", "runner.log"):
                        data = archive.extractfile(item).read()
                        for name in (args.peer, peer.ssh_target):
                            data = data.replace(name.encode(), b"[redacted]")
                        evidence[item.name] = data
                        (args.evidence_dir / item.name).write_bytes(data)
        if result.returncode:
            print(args.tier + " FAIL: " + result.stderr.decode(errors="replace"), file=sys.stderr)
            return result.returncode
        if json.loads(evidence["v2-" + args.tier + "-evidence.json"])["sha"] != args.commit:
            raise ValueError("runner evidence commit mismatch")
        print(args.tier + " PASS")
        return 0
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError, tarfile.TarError) as exc:
        print("peer failure: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
