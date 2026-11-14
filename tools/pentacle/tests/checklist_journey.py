#!/usr/bin/env python3
"""Real isolated installs and the exact UPGRADE.md Bash blocks, on macOS/Linux."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SOURCE / "tools"))
from pentacle import discovery as d
from pentacle.install import atomic_json

BASELINE = "5c6d154117dbde29ea34556e349fd825e490d2b4"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Journey:
    def __init__(self, evidence):
        self.evidence = evidence.resolve()
        self.evidence.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.root = Path(tempfile.mkdtemp(prefix="pentacle journey ")).resolve()
        self.checkout = self.root / "install space"
        self.home = self.root / "users/reader space"
        self.python = self.checkout / ".venv/bin/python"
        self.config = self.home / ".config/private config/pentacle.config.js"
        self.token = self.config.parent / "desktop.token"
        self.release = self.root / "release repo"
        self.daemon_url = f"ws://127.0.0.1:{free_port()}"
        self.web_url = f"http://127.0.0.1:{free_port()}"
        self.children = []
        self.active = {}
        self.records = []
        self.stream = None
        self.socket_root = Path(tempfile.mkdtemp(prefix="pj-", dir="/tmp"))
        self.tmux = self.root / "tmux"
        self.env = {"HOME": str(self.home), "PATH": str(self.python.parent) + os.pathsep + os.environ.get("PATH", os.defpath),
                    "LANG": "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"}
        self.candidate = d.run(["git", "-C", SOURCE, "rev-parse", "HEAD"]).stdout.strip()
        self.blocks = re.findall(r"```bash\n(.*?)\n\s*```", (SOURCE / "UPGRADE.md").read_text(), flags=re.S)
        self.blocks = ["\n".join(line[3:] if line.startswith("   ") else line for line in block.splitlines()) + "\n" for block in self.blocks]
        assert len(self.blocks) == 9, "The reviewed checklist has exactly nine fenced Bash blocks"
        atomic_json(self.evidence / "identity.json", {"candidate": self.candidate, "baseline": BASELINE,
                    "platform": sys.platform, "python": sys.version, "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "checklist_sha256": hashlib.sha256((SOURCE / "UPGRADE.md").read_bytes()).hexdigest(), "fixture": str(self.root)})

    def run(self, argv, *, cwd=None, env=None, expected=0, timeout=900, input=None, secret=False):
        result = subprocess.run(list(map(str, argv)), cwd=cwd or self.checkout, env=self.env if env is None else env,
                                input=input, capture_output=True, text=True, timeout=timeout)
        index = len(self.records)
        record = {"command": list(map(str, argv)), "returncode": result.returncode}
        if not secret:
            path = self.evidence / f"command-{index:03}.log"
            path.write_text(result.stdout + result.stderr)
            record.update(log=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        self.records.append(record)
        atomic_json(self.evidence / "commands.json", self.records)
        if expected is not None and result.returncode != expected:
            raise RuntimeError(f"Command {index} exited {result.returncode}; expected {expected}")
        return result

    def git(self, *args, cwd=None):
        return self.run(["git", *args], cwd=cwd).stdout.strip()

    def setup(self, baseline):
        self.home.mkdir(parents=True)
        self.git("clone", "--no-hardlinks", "--no-checkout", SOURCE, self.checkout, cwd=self.root)
        self.git("checkout", "--detach", BASELINE if baseline else self.candidate)
        self.git("remote", "set-url", "origin", "https://github.com/HJK6/pentacle.git")
        self.run([sys.executable, "-m", "venv", self.python.parent.parent])
        self.install_and_build()
        provider = self.root / "provider"
        provider.mkdir()
        shutil.copyfile(SOURCE / "services/chat-stream-v2/tests/smoke/ingest_provider_stub.py", provider / "ingest_provider_stub.py")
        self.provider = provider / "claude"
        self.provider.write_text("#!" + sys.executable + "\nimport runpy,sys\nfrom pathlib import Path\nif '--version' in sys.argv:\n print('2.1.312 (fixture)');sys.exit(0)\nrunpy.run_path(str(Path(__file__).with_name('ingest_provider_stub.py')),run_name='__main__')\n")
        self.provider.chmod(0o700)
        self.config.parent.mkdir(parents=True, mode=0o700)
        self.config.parent.chmod(0o700)
        self.config.write_text("module.exports=" + json.dumps({"localHostId": "local", "features": {"mic": False},
                              "chatStream": {"url": self.daemon_url, "hosts": ["local"], "tokenPath": str(self.token)}}) + ";\n")
        self.config.chmod(0o600)
        issued = self.run([self.python, "services/chat-stream-v2/tools/operator_auth_cli.py", "issue", "--client-kind", "pentacle", "--label", "fixture"], secret=True)
        self.token.write_text(json.loads(issued.stdout)["code"])
        self.token.chmod(0o600)
        self.tmux.write_text("#!/bin/sh\nexec " + shlex.join([shutil.which("tmux"), "-S", str(self.socket_root / "socket")]) + ' "$@"\n')
        self.tmux.chmod(0o700)
        (self.home / "workspace").mkdir()
        self.process_env = {**self.env, "PENTACLE_CONFIG": str(self.config), "CHAT_STREAM_STUB_ROOT": str(self.home / ".claude/projects")}
        self.commands = {
            "daemon": [self.python, self.checkout / "services/chat-stream-v2/main.py", "--host", "127.0.0.1",
                       "--port", str(d.endpoint_port(self.daemon_url)), "--local-host", "local", "--db", self.home / "state space/sessions.db",
                       "--spawn-cwd", self.home / "workspace", "--projects-root", self.home / ".claude/projects",
                       "--claude-bin", self.provider, "--tmux-bin", self.tmux],
            "web": [shutil.which("node"), self.checkout / "server", "--port", str(d.endpoint_port(self.web_url))]}
        self.make_release()
        # Every subsequent Git command, including the extracted blocks, passes
        # a recording wrapper that refuses non-local fetch destinations.
        directory = self.root / "recording tools"; directory.mkdir()
        wrapper = directory / "git"
        real_git = shutil.which("git")
        self.git_calls = self.evidence / "git.jsonl"
        wrapper.write_text("#!" + sys.executable + "\n" + f'''import json,os,sys
args=sys.argv[1:]
with open({str(self.git_calls)!r},'a') as out: out.write(json.dumps(args)+'\\n')
verb=args[2:] if args[:1]==['-C'] else args
if verb[:1]==['fetch'] and verb[1]!={str(self.release)!r}: sys.exit(93)
os.execv({real_git!r},[{real_git!r},*args])
''')
        wrapper.chmod(0o700)
        self.env["PATH"] = str(directory) + os.pathsep + self.env["PATH"]

    def install_and_build(self):
        self.run([self.python, "-m", "pip", "install", "-r", "services/chat-stream-v2/requirements.txt", "-e", "services/agent-orch"])
        self.run(["npm", "ci"])
        self.run(["npm", "run", "build:web"])

    def make_release(self):
        self.git("clone", "--no-hardlinks", "--no-checkout", SOURCE, self.release, cwd=self.root)
        self.git("checkout", "-B", "main", self.candidate, cwd=self.release)
        self.git("config", "user.name", "Example Agent", cwd=self.release)
        self.git("config", "user.email", "agent@example.invalid", cwd=self.release)
        entry = {"version": "2.0.0", "commit": self.candidate, "date": "2026-11-14", "changelog_sections": ["2026-11-13"],
                 "tested_update_from": [BASELINE], "required_actions": ["Confirm the fixture checklist action."], "evidence_sha256": "a" * 64}
        atomic_json(self.release / "releases/manifest.json", {"schema_version": 1, "releases": [entry]})
        self.git("add", "releases/manifest.json", cwd=self.release)
        self.git("commit", "-qm", "Fixture release record", cwd=self.release)

    def start(self, launchers=False):
        for kind in ("web", "daemon"):
            self.start_one(kind, launchers)
        self.wait_ready()

    def start_one(self, kind, launchers=False):
        command = [self.plan / f"start-{kind}.sh"] if launchers else self.commands[kind]
        env = {} if launchers else self.process_env
        with (self.evidence / (kind + ".log")).open("a") as log:
            child = subprocess.Popen(list(map(str, command)), cwd=self.checkout, env=env, stdout=log, stderr=subprocess.STDOUT)
        self.children.append(child); self.active[kind] = child

    def wait_ready(self):
        head = self.git("rev-parse", "HEAD")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            welcome = d.welcome(self.daemon_url, timeout=2); web = d.health(self.web_url, timeout=2)
            if welcome and welcome.get("runtime_sha") == head and web:
                return
            if any(child.poll() is not None for child in self.active.values()):
                raise RuntimeError("An owned service exited before readiness")
            time.sleep(0.2)
        raise RuntimeError("Owned services did not become ready")

    def stop(self):
        for kind in ("web", "daemon"):
            self.stop_one(kind)

    def stop_one(self, kind):
        child = self.active.pop(kind, None)
        if child:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=20)

    def client(self, mode, marker=""):
        env = {**self.env, "PYTHONPATH": str(self.checkout / "services"), "AGENT_ORCH_WS_URL": self.daemon_url,
               "AGENT_ORCH_HOST_ID": "local", "AGENT_ORCH_TOKEN": self.token.read_text().strip()}
        result = self.run([self.python, SOURCE / "tools/pentacle/tests/journey_client.py", mode, self.stream or "", marker], env=env, timeout=180)
        value = json.loads(result.stdout)
        self.stream = None if mode == "close" else value["stream"]
        return value

    def block(self, index, expected=0, prepare_env=False):
        env = dict(self.env)
        if prepare_env:
            env.update(PENTACLE_CONFIG=str(self.config), PENTACLE_DAEMON_URL=self.daemon_url,
                       PENTACLE_WEB_URL=self.web_url, PENTACLE_RELEASE_REMOTE=str(self.release), PENTACLE_KEEP_ENV="CHAT_STREAM_STUB_ROOT")
        return self.run(["bash"], input=self.blocks[index], env=env, expected=expected)

    def doctor(self, *, live=False, env=None, expected=0):
        command = [self.python, self.checkout / "bin/pentacle", "doctor", "--config", self.config,
                   "--daemon-url", self.daemon_url, "--web-url", self.web_url, "--release-remote", self.release, "--json"]
        if live:
            command.append("--live")
        result = self.run(command, env=env, expected=expected, timeout=180)
        return json.loads(result.stdout)

    def seed_stores(self):
        self.stores = d.open_stores(d.listener(self.daemon_url))
        expected = {self.home / "state space/sessions.db", self.home / ".local/share/pentacle-stream/notifications.db",
                    self.home / ".local/share/pentacle-stream/assets.db"}
        assert set(self.stores) == expected, "All three real stores, including README defaults, must be found"
        for path in self.stores:
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE doctor_rehearsal(value TEXT)")
                db.execute("INSERT INTO doctor_rehearsal VALUES ('before-upgrade')")

    def read_seeds(self, paths):
        for path in paths:
            with closing(sqlite3.connect(path)) as db:
                assert db.execute("SELECT value FROM doctor_rehearsal").fetchall() == [("before-upgrade",)]

    def rehearsal(self):
        self.setup(baseline=True)
        self.start()
        original_config = self.config.read_bytes()
        self.seed_stores()
        self.client("spawn", "REHEARSAL_BEFORE_UPGRADE")
        original_sessions = self.run([self.tmux, "list-sessions", "-F", "#{session_name}"]).stdout
        self.block(0, prepare_env=True)
        refused = self.block(1, expected=2, prepare_env=True)
        assert "Confirm the fixture checklist action." in refused.stderr
        assert not (self.checkout / ".pentacle-deploy/upgrade-current").exists()
        self.block(3, expected=2)
        assert self.block(4, expected=None).returncode != 0
        assert self.git("rev-parse", "HEAD") == BASELINE
        self.block(2, prepare_env=True)
        self.plan = Path((self.checkout / ".pentacle-deploy/upgrade-current").read_text().strip())
        plan = d.read_json(self.plan / "plan.json")
        assert set(plan["stores"]) == set(map(str, self.stores))
        assert plan["daemon"]["argv"][0] == str(self.python)
        self.block(3, expected=2)
        assert self.block(4, expected=None).returncode != 0
        self.stop()
        self.block(3)
        copies = [self.plan / f"store-{i}.db" for i in range(3)]
        self.read_seeds(copies)
        hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in copies]
        for index in (4, 5, 6):
            self.block(index)
        self.start(launchers=True)
        self.block(7)
        assert self.config.read_bytes() == original_config
        assert self.run([self.tmux, "list-sessions", "-F", "#{session_name}"]).stdout == original_sessions
        self.client("inspect", "REHEARSAL_BEFORE_UPGRADE")
        self.client("send", "REHEARSAL_AFTER_UPGRADE")
        self.read_seeds(self.stores)
        assert d.health(self.web_url)["build_commit"] == self.candidate
        # Mac's kernel launcher and the language runtimes may synthesize their
        # own variables; no unapproved caller environment may be inherited.
        for kind in ("daemon", "web"):
            proc = d.listener(self.daemon_url if kind == "daemon" else self.web_url)
            assert proc is not None
            assert "CHAT_STREAM_STUB_ROOT" in proc.env
            allowed = set(plan[kind]["environment"]) | {"__PYVENV_LAUNCHER__", "LC_CTYPE", "_", "SHLVL", "PWD"}
            assert set(proc.env) <= allowed, "Launcher inherited an unapproved environment name"
        self.stop()
        self.block(8)
        self.start(launchers=True)
        assert self.git("rev-parse", "HEAD") == BASELINE
        self.client("inspect", "REHEARSAL_BEFORE_UPGRADE")
        self.client("send", "REHEARSAL_AFTER_ROLLBACK")
        assert hashes == [hashlib.sha256(path.read_bytes()).hexdigest() for path in copies]
        self.client("close")
        for path in self.stores:
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("INSERT INTO doctor_rehearsal VALUES ('after-copy')")
        self.stop()
        self.run([self.plan / "restore-stores.sh"])
        assert [path.read_bytes() for path in self.stores] == [path.read_bytes() for path in copies]
        assert all(not Path(str(path) + suffix).exists() for path in self.stores for suffix in ("-wal", "-shm"))
        # These prepare refusals must invalidate the old successful plan too.
        with (self.checkout / "README.md").open("a") as target:
            target.write("\nOwned refusal fixture\n")
        self.block(2, expected=2, prepare_env=True)
        assert not (self.checkout / ".pentacle-deploy/upgrade-current").exists()
        self.git("restore", "README.md")
        self.git("checkout", "--orphan", "divergent-fixture")
        self.git("config", "user.name", "Example Agent")
        self.git("config", "user.email", "agent@example.invalid")
        self.git("commit", "-qm", "Non-ancestor fixture")
        self.block(2, expected=2, prepare_env=True)
        assert not (self.checkout / ".pentacle-deploy/upgrade-current").exists()
        atomic_json(self.evidence / "rehearsal.json", {"status": "passed", "candidate": self.candidate,
                    "stores": 3, "copy_hashes": hashes, "exact_bash_blocks": len(self.blocks), "upgrade_reply": True,
                    "rollback_reply": True, "retained_history": True, "explicit_restore": True})

    def install(self):
        self.setup(baseline=False)
        self.start()
        healthy = self.doctor(live=True)
        assert healthy["summary"]["fail"] == 0
        expected = {key: "ok" for key in (
            "prereq.os", "prereq.node", "prereq.python", "prereq.tmux", "prereq.git",
            "source.clean", "source.remote", "source.release", "source.latest", "deps.node", "deps.python", "deps.cli",
            "daemon.reachable", "daemon.commit", "daemon.process", "daemon.service", "web.reachable", "web.commit",
            "config.readable", "config.warnings", "config.permissions", "providers.executables")}
        expected.update({"workspace.roots": "skip", "live.reply": "skip"})
        cases = []

        def check(name, changes=None, env=None):
            want = {**expected, **(changes or {})}
            value = self.doctor(env=env, expected=1 if "fail" in want.values() else 0)
            actual = {item["id"]: item["status"] for item in value["checks"]}
            cases.append({"fault": name, "expected": want, "actual": actual})
            atomic_json(self.evidence / "fault-status-table.json", cases)
            assert actual == want, name + ": " + str({key: (want.get(key), actual.get(key)) for key in want.keys() | actual.keys() if want.get(key) != actual.get(key)})
            return value

        check("healthy")
        tools = self.root / "fault tools"; tools.mkdir()
        node = tools / "node"
        node.write_text("#!/bin/sh\necho v18.0.0\n"); node.chmod(0o700)
        check("old Node", {"prereq.node": "fail", "deps.node": "skip", "config.readable": "skip",
                           "config.warnings": "skip", "config.permissions": "skip"},
              {**self.env, "PATH": str(tools) + os.pathsep + self.env["PATH"]})
        node.unlink()
        # Keep the existing tools available while making only tmux absent.
        for name in ("node", "npm", "git", "ps", "lsof", "python3", "agent-orch"):
            found = shutil.which(name, path=self.env["PATH"])
            if found:
                (tools / name).symlink_to(found)
        check("missing tmux", {"prereq.tmux": "fail"}, {**self.env, "PATH": str(tools)})
        tracked = self.checkout / "README.md"; original = tracked.read_bytes()
        tracked.write_bytes(original + b"\nFixture modified file\n")
        check("modified source", {"source.clean": "fail"})
        tracked.write_bytes(original)
        package = self.checkout / "node_modules/ws"; hidden = self.root / "hidden package"
        package.rename(hidden)
        check("missing Node dependency", {"deps.node": "fail"})
        hidden.rename(package)
        metadata = next((self.python.parent.parent / "lib").glob("python*/site-packages/websockets-*.dist-info"))
        hidden = self.root / "hidden metadata"; metadata.rename(hidden)
        check("missing Python requirement", {"deps.python": "fail"})
        hidden.rename(metadata)
        second = self.root / "second checkout"
        self.git("clone", "--no-hardlinks", SOURCE, second, cwd=self.root)
        self.git("checkout", "--detach", self.candidate, cwd=second)
        self.run([self.python, "-m", "pip", "install", "--no-deps", "-e", second / "services/agent-orch"])
        check("CLI from another checkout", {"deps.cli": "fail"})
        self.run([self.python, "-m", "pip", "install", "--no-deps", "-e", self.checkout / "services/agent-orch"])
        self.stop_one("daemon")
        check("stopped daemon", {"daemon.reachable": "fail", "daemon.commit": "skip", "daemon.process": "skip",
                                  "daemon.service": "skip", "providers.executables": "skip"})
        first_command = list(self.commands["daemon"])
        self.commands["daemon"][1] = second / "services/chat-stream-v2/main.py"
        self.start_one("daemon"); self.wait_ready()
        check("daemon from another checkout", {"daemon.process": "fail"})
        self.stop_one("daemon"); self.commands["daemon"] = first_command
        self.start_one("daemon"); self.wait_ready()
        self.stop_one("web")
        build = self.checkout / "dist/build-commit.json"; original_build = build.read_bytes()
        build.write_text(json.dumps({"schema_version": 1, "build_commit": BASELINE}))
        self.start_one("web"); self.wait_ready()
        check("stale web build", {"web.commit": "fail"})
        self.stop_one("web"); build.write_bytes(original_build)
        self.start_one("web"); self.wait_ready()
        original_config = self.config.read_bytes()
        self.config.write_bytes(b"module.exports.UNKNOWN_KEY_SEED='CONFIG_VALUE_SEED';\n" + original_config.replace(b"module.exports=", b"Object.assign(module.exports," ).replace(b";\n", b");\n"))
        check("unknown config key", {"config.warnings": "warn"})
        self.config.write_bytes(original_config)
        self.token.chmod(0o644)
        check("readable token", {"config.permissions": "fail"})
        self.token.chmod(0o600)
        hidden = self.root / "hidden provider"; self.provider.rename(hidden)
        check("missing provider", {"providers.executables": "fail"})
        hidden.rename(self.provider)
        first = self.home / "memory first"; first.mkdir()
        other = self.home / "memory other"; other.mkdir()
        client_config = self.home / ".agent-orch/config.json"; client_config.parent.mkdir(exist_ok=True)
        atomic_json(client_config, {"memory_repo_path": str(other)})
        self.stop_one("daemon"); self.process_env["PENTACLE_MEMORY_ROOT"] = str(first)
        self.start_one("daemon"); self.wait_ready()
        check("different workspace roots", {"workspace.roots": "fail"})
        self.stop_one("daemon"); del self.process_env["PENTACLE_MEMORY_ROOT"]; client_config.unlink()
        self.start_one("daemon"); self.wait_ready()
        unavailable = self.root / "unavailable release"; self.release.rename(unavailable)
        cached = check("offline with fresh cache")
        assert cached["identity"]["manifest"]["source"] == "cache"
        cache = self.checkout / ".pentacle-deploy/releases-cache.json"
        cached_data = d.read_json(cache); cached_data["fetched_at"] = "2020-01-01T00:00:00+00:00"; atomic_json(cache, cached_data)
        check("offline with stale cache", {"source.latest": "warn"})
        cache.unlink()
        check("offline without cache", {"source.latest": "warn", "source.release": "warn"})
        unavailable.rename(self.release)
        (self.release / "releases/manifest.json").write_text("malformed")
        self.git("add", "releases/manifest.json", cwd=self.release)
        self.git("commit", "-qm", "Malformed fixture manifest", cwd=self.release)
        check("malformed manifest", {"source.latest": "warn", "source.release": "warn"})
        atomic_json(self.evidence / "install.json", {"status": "passed", "candidate": self.candidate, "faults": len(cases) - 1,
                    "live_reply": healthy["checks"][-1]["status"]})

    def cleanup(self):
        errors = []
        if self.stream and "daemon" in self.active:
            try:
                self.client("close")
            except Exception:
                errors.append("conversation close failed")
        try:
            self.stop()
        except Exception:
            errors.append("process stop failed")
        remaining = 0
        if self.tmux.exists():
            result = d.run([self.tmux, "list-sessions", "-F", "#{session_name}"], env=self.env)
            remaining = len(result.stdout.splitlines()) if result.returncode == 0 else 0
            d.run([self.tmux, "kill-server"], env=self.env)
        alive = [child.pid for child in self.children if child.poll() is None]
        if alive:
            errors.append("owned processes remain")
        # Both directories were exclusively created with mkdtemp by this run.
        if not alive:
            shutil.rmtree(self.root)
            shutil.rmtree(self.socket_root)
        atomic_json(self.evidence / "cleanup.json", {"owned_processes": len(self.children), "remaining_processes": alive,
                    "sessions_before_teardown": remaining, "fixture_removed": not self.root.exists(),
                    "socket_removed": not self.socket_root.exists(), "service_units_touched": 0, "errors": errors})
        if errors:
            raise RuntimeError("Owned fixture cleanup failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["rehearsal", "diagnostic", "install"])
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    journey = Journey(args.evidence)
    try:
        if args.mode == "install":
            journey.install()
        elif args.mode == "rehearsal":
            journey.rehearsal()
        else:
            journey.setup(baseline=False)
            journey.start()
            journey.doctor(live=True)
        return 0
    except Exception as error:
        atomic_json(journey.evidence / "failure.json", {"classification": "unclassified", "type": type(error).__name__, "message": str(error)})
        print("Journey failed; inspect the evidence directory.", file=sys.stderr)
        return 1
    finally:
        journey.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
