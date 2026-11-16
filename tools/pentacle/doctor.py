"""Fixed diagnostic vocabulary; never interpolate raw discovery data."""
from __future__ import annotations

import json
import datetime as dt
import os
import re
import shutil
import sys
from pathlib import Path

from . import discovery as d
from .install import commit, identity, release_manifest, safe_sha
from .releases import OFFICIAL_REMOTE, VERSION, ancestor

# id, dependencies, failure finding, fix. Success and blocked text are fixed too.
CHECKS = [
    ("prereq.os", [], "This operating system is unsupported.", "Use macOS or Linux."),
    ("prereq.node", [], "Node is missing or older than 22.12.", "Install Node 22.12 or newer."),
    ("prereq.python", [], "Python is older than 3.11.", "Run this command with Python 3.11 or newer."),
    ("prereq.tmux", [], "tmux is missing.", "Install tmux and add it to PATH."),
    ("prereq.git", [], "Git or the installation checkout is unavailable.", "Install Git and select the checkout with --install."),
    ("source.clean", ["prereq.git"], "Tracked installation files were modified.", "Commit or preserve tracked changes before updating."),
    ("source.remote", ["prereq.git"], "The checkout origin differs from the official remote.", "Confirm that this is the intended checkout."),
    ("source.release", ["prereq.git"], "This checkout is a development build.", "Follow UPGRADE.md to select a stable release."),
    ("source.latest", ["prereq.git"], "Latest stable release is unknown, cached, or ahead of this install.", "Connect to the release source and run pentacle update --check."),
    ("deps.node", ["prereq.node"], "Node packages do not match the lock file.", "Run npm ci in the checkout."),
    ("deps.python", ["prereq.python"], "The daemon interpreter cannot satisfy its requirements.", "Install the daemon requirements in its own interpreter."),
    ("deps.cli", ["prereq.python"], "agent-orch is missing or imports from another install.", "Install services/agent-orch with the daemon interpreter and add its bin directory to PATH."),
    ("daemon.reachable", [], "No daemon welcome arrived within ten seconds.", "Start the daemon and check the configured connection."),
    ("daemon.commit", ["daemon.reachable", "prereq.git"], "The daemon commit differs from the checkout.", "Restart the installed daemon from the matching release."),
    ("daemon.process", ["daemon.reachable"], "The local daemon uses another checkout or interpreter.", "Run the daemon from this checkout and its interpreter."),
    ("daemon.service", ["daemon.reachable"], "The daemon unit names another checkout.", "Correct the installed daemon unit to use this checkout."),
    ("web.reachable", [], "Web health did not answer within ten seconds.", "Start the web host and check its connection."),
    ("web.commit", ["web.reachable", "prereq.git"], "The served web commit differs from the checkout.", "Build the web client and restart its host from the matching release."),
    ("config.readable", ["prereq.node"], "The selected config is missing or could not load.", "Create or repair the selected Pentacle config."),
    ("config.warnings", ["config.readable"], "The config has warnings.", "Review the supported config fields in docs/desktop_config.md."),
    ("config.permissions", ["config.readable"], "The token or config directory is missing or readable by other users.", "Make the config directory private and the token readable only by its owner."),
    ("providers.executables", ["daemon.reachable"], "A configured provider is missing or has no version.", "Install the provider executable and verify its version."),
    ("workspace.roots", [], "Workspace roots differ or a configured root is missing.", "Set the daemon and client to the same existing memory directory."),
    ("live.reply", ["daemon.commit", "deps.cli"], "The functional reply or session cleanup failed.", "Check provider authentication and run pentacle doctor --live again."),
]


def evaluate(facts, *, live=False):
    checks, by_id = [], {}
    for check_id, dependencies, finding, fix in CHECKS:
        blocked = [key for key in dependencies if by_id[key]["status"] != "ok"]
        status = facts.get(check_id, "fail")
        if blocked:
            status = "skip"
        if check_id == "live.reply" and not live:
            status, blocked = "skip", []
        item = {"id": check_id, "status": status,
                "finding": "Check passed." if status == "ok" else "Check skipped." if status == "skip" else finding,
                "fix": "" if status in {"ok", "skip"} else fix, "blocked_by": blocked}
        checks.append(item)
        by_id[check_id] = item
    return checks


def private_config(path: Path, token: Path):
    try:
        for target, directory in [(path.parent, True), (token, False)]:
            info = target.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077 or (target.is_dir() != directory):
                return False
        return True
    except OSError:
        return False


PYTHON_DEPS = r"""
import importlib.metadata as m, pathlib, sys
from pip._vendor.packaging.requirements import Requirement
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    line=line.strip()
    if not line or line.startswith('#'): continue
    req=Requirement(line)
    if req.marker and not req.marker.evaluate(): continue
    if not req.specifier.contains(m.version(req.name), prereleases=True): sys.exit(1)
"""
CLI_DEPS = r"""
import pathlib, sys, agent_orch
root=pathlib.Path(sys.argv[1]).resolve()
path=pathlib.Path(agent_orch.__file__).resolve()
sys.exit(0 if path.is_relative_to(root/'services/agent-orch') else 1)
"""
NODE_DEPS = r"""
const fs=require('fs'),path=require('path');
const root=process.argv[1],lock=JSON.parse(fs.readFileSync(path.join(root,'package-lock.json')));
function supported(values, actual) {
  return !values || (!values.includes('!'+actual) && (values.every(v=>v.startsWith('!')) || values.includes(actual)));
}
for(const [rel,want] of Object.entries(lock.packages||{})) {
  if(!rel||want.link) continue;
  if(want.optional && (!supported(want.os,process.platform)||!supported(want.cpu,process.arch))) continue;
  if(want.optional && !fs.existsSync(path.join(root,rel,'package.json'))) continue;
  const got=JSON.parse(fs.readFileSync(path.join(root,rel,'package.json')));
  if(got.version!==want.version) process.exit(1);
}
"""


class Snapshot:
    def __init__(self, args, *, fetch=True, write_cache=True):
        self.root = Path(args.install).expanduser().resolve()
        self.config_path = Path(args.config).expanduser().resolve()
        self.config = d.config(self.root, self.config_path)
        self.daemon_url = args.daemon_url or (self.config or {}).get("daemon_url") or "ws://127.0.0.1:7791"
        self.web_url = args.web_url or "http://127.0.0.1:7795"
        self.remote = args.release_remote
        self.daemon = d.welcome(self.daemon_url)
        self.web = d.health(self.web_url)
        self.daemon_process = d.listener(self.daemon_url)
        self.web_process = d.listener(self.web_url)
        self.daemon_unit = d.unit_for(self.daemon_process, "daemon")
        self.stores = d.open_stores(self.daemon_process)
        self.manifest = release_manifest(self.root, self.remote, fetch=fetch, write_cache=write_cache)
        self.identity = identity(self.root, self.manifest, self.daemon, self.web)
        self.python = self.interpreter()

    def interpreter(self):
        if self.daemon_process:
            executable = (self.daemon_process.env.get("__PYVENV_LAUNCHER__") if sys.platform == "darwin" else None) or self.daemon_process.argv[0]
            if Path(executable).is_absolute():
                return executable
        for path in (self.root / "services/chat-stream-v2/.venv/bin/python", self.root / ".venv/bin/python"):
            if path.is_file():
                return str(path)
        return str(self.root / "services/chat-stream-v2/.venv/bin/python")

    def cli_environment(self):
        # A diagnostic session acts for this install, independently of any
        # invoking agent seat. The credential remains inside the subprocess.
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_ORCH_") and k != "PENTACLE_STREAM_ID"}
        env["AGENT_ORCH_WS_URL"] = self.daemon_url
        # The editable CLI package imports shared source modules for v2 proof.
        # Keep this subprocess on the installation's own shared code.
        env["PYTHONPATH"] = str(self.root / "services")
        env["AGENT_ORCH_HOST_ID"] = (self.daemon_process.option("--local-host") if self.daemon_process else None) or __import__("socket").gethostname().split(".")[0]
        token = Path((self.config or {}).get("token_path") or "~/.config/pentacle-stream/token").expanduser()
        try:
            env["AGENT_ORCH_TOKEN"] = token.read_text().strip()
        except OSError:
            pass
        return env

    def facts(self, *, live=False):
        facts = {}
        node = d.run(["node", "--version"])
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)\s*", node.stdout)
        facts["prereq.os"] = "ok" if sys.platform in {"darwin", "linux"} else "fail"
        facts["prereq.node"] = "ok" if node.returncode == 0 and match and tuple(map(int, match.groups())) >= (22, 12, 0) else "fail"
        facts["prereq.python"] = "ok" if sys.version_info >= (3, 11) else "fail"
        facts["prereq.tmux"] = "ok" if shutil.which("tmux") else "fail"
        head = self.identity["install"]["commit"]
        facts["prereq.git"] = "ok" if shutil.which("git") and head else "fail"
        dirty = d.run(["git", "-C", self.root, "status", "--porcelain", "--untracked-files=no"])
        facts["source.clean"] = "ok" if dirty.returncode == 0 and not dirty.stdout else "fail"
        origin = d.run(["git", "-C", self.root, "remote", "get-url", "origin"])
        facts["source.remote"] = "ok" if origin.returncode == 0 and origin.stdout.strip() == OFFICIAL_REMOTE else "warn"
        facts["source.release"] = "ok" if self.identity["install"]["version"] else "warn"
        recent = self.manifest["source"] == "fetched" or (self.manifest["source"] == "cache" and self.manifest["age_days"] <= 7)
        facts["source.latest"] = "ok" if recent and self.identity["latest"]["commit"] == head and head else "warn"
        if facts["prereq.node"] == "ok":
            packages = d.run(["node", "-e", NODE_DEPS, self.root], cwd=self.root)
            listing = d.run(["npm", "ls", "--all", "--json"], cwd=self.root)
            facts["deps.node"] = "ok" if packages.returncode == listing.returncode == 0 else "fail"
        if facts["prereq.python"] == "ok":
            result = d.run([self.python, "-c", PYTHON_DEPS, self.root / "services/chat-stream-v2/requirements.txt"], cwd=self.root)
            facts["deps.python"] = "ok" if result.returncode == 0 else "fail"
            cli = d.run([self.python, "-c", CLI_DEPS, self.root], cwd=self.root)
            executable = shutil.which("agent-orch")
            cli_path_ok = False
            if executable:
                try:
                    lines = Path(executable).read_text().splitlines()
                    first = lines[0]
                    command = __import__("shlex").split(first[2:]) if first.startswith("#!") else []
                    if command == ["/bin/sh"] and len(lines) >= 3 and lines[2] == "' '''":
                        trampoline = __import__("shlex").split(lines[1])
                        if len(trampoline) == 4 and trampoline[0] == "exec" and trampoline[2:] == ["$0", "$@"]:
                            command = [trampoline[1]]
                    if command and Path(command[0]).name == "env":
                        command = command[1:]
                    if command and "python" in Path(command[0]).name:
                        located = d.run([*command, "-c", CLI_DEPS, self.root], cwd=self.root)
                        cli_path_ok = located.returncode == 0
                except (OSError, ValueError, IndexError, UnicodeError):
                    pass
            facts["deps.cli"] = "ok" if cli.returncode == 0 and cli_path_ok else "fail"
        facts["daemon.reachable"] = "ok" if self.daemon is not None else "fail"
        facts["daemon.commit"] = "ok" if head and self.identity["daemon"]["commit"] == head else "fail"
        facts["web.reachable"] = "ok" if self.web is not None else "fail"
        facts["web.commit"] = "ok" if head and self.identity["web"]["build_commit"] == head else "fail"
        proc = self.daemon_process
        local = d.loopback(self.daemon_url)
        checkout = proc.checkout("daemon") if proc else None
        own_interpreter = Path(self.python).is_relative_to(self.root)
        facts["daemon.process"] = "skip" if not local else "ok" if checkout == self.root and own_interpreter else "fail"
        unit = self.daemon_unit
        unit_checkout = d.Process(0, unit["argv"], Path(unit["cwd"])).checkout("daemon") if unit else None
        facts["daemon.service"] = "skip" if not local else "ok" if unit is None or unit_checkout == self.root else "fail"
        facts["config.readable"] = "ok" if self.config is not None else "fail"
        facts["config.warnings"] = "warn" if (self.config or {}).get("warnings") else "ok"
        token = Path((self.config or {}).get("token_path") or "~/.config/pentacle-stream/token").expanduser()
        facts["config.permissions"] = "ok" if private_config(self.config_path, token) else "fail"
        facts["providers.executables"] = self.provider_status()
        facts["workspace.roots"] = self.workspace_status()
        if live and facts["daemon.commit"] == facts.get("deps.cli") == "ok":
            from .live import probe
            facts["live.reply"] = "ok" if probe(self) else "fail"
        return facts

    def provider_status(self):
        proc = self.daemon_process
        if not d.loopback(self.daemon_url):
            return "skip"
        if proc is None:
            return "fail"
        providers = [proc.option(name) for name in ("--claude-bin", "--codex-bin", "--gemini-bin", "--cursor-agent-bin")]
        status = "ok"
        for value in filter(None, providers):
            path = Path(value)
            if not path.is_absolute():
                resolved = shutil.which(value, path=proc.env.get("PATH", ""))
                path = Path(resolved) if resolved else proc.cwd / value
            if not path.is_file() or not os.access(path, os.X_OK):
                return "fail"
            result = d.run([path, "--version"], env=proc.env, cwd=proc.cwd)
            if result.returncode or not (result.stdout.strip() or result.stderr.strip()):
                status = "warn"
        return status

    def workspace_status(self):
        if not d.loopback(self.daemon_url):
            return "skip"
        daemon = (self.daemon_process.env.get("PENTACLE_MEMORY_ROOT") if self.daemon_process else None)
        config = d.read_json(Path.home() / ".agent-orch/config.json")
        client = os.environ.get("AGENT_ORCH_MEMORY_REPO") or (config.get("memory_repo_path") if isinstance(config, dict) else None)
        paths = [Path(v).expanduser().resolve() for v in (daemon, client) if isinstance(v, str) and v]
        if not paths:
            return "skip"
        return "ok" if all(p.is_dir() for p in paths) and len(set(paths)) == 1 else "fail"


def report(snapshot: Snapshot, *, live=False):
    checks = evaluate(snapshot.facts(live=live), live=live)
    summary = {status: sum(c["status"] == status for c in checks) for status in ("ok", "warn", "fail", "skip")}
    return {"schema_version": 1, "identity": snapshot.identity, "checks": checks, "summary": summary}
