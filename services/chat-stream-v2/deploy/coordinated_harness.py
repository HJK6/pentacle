"""Disposable transaction/process rehearsal; never addresses production resources.

Run with the provisioned Python: ``python -m deploy.coordinated_harness --root
/absolute/owned/scratch``. Native version responses are the sole provider mock.
The daemon is a real Server/Store process and dispatches the real staged helper
through AccountExecutor's unchanged identity verifier.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import plistlib
import runpy
import shutil
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit

from deploy import coordinated as c


@dataclass(frozen=True)
class Bindings:
    root: Path
    label: str
    url: str
    config: Path
    journal: Path
    logs: Path
    schedule: Path
    collector: Path

    def validate(self):
        root = self.root.resolve()
        if self.root != root or root == Path.home() or not self.label.startswith("com.pentacle.test.coordinated."):
            raise c.Invalid("disposable root/label required")
        parsed = urlsplit(self.url)
        if parsed.scheme != "ws" or parsed.hostname != "127.0.0.1" or parsed.port in {None, 7791, 7790} or parsed.path not in {"", "/"}:
            raise c.Invalid("disposable loopback endpoint required")
        for path in (self.config, self.journal, self.logs, self.schedule, self.collector):
            if path != path.resolve() or not path.is_relative_to(root) or path == root:
                raise c.Invalid("all harness resource paths must be inside its owned root")
        if root.exists() and any(root.iterdir()):
            raise c.Invalid("harness root must be empty before starting")


def source_paths(source):
    sys.path[:0] = [str(source / "services/chat-stream-v2"), str(source / "services"), str(source / "services/agent-orch")]


def validate_child(root, *, source=None, script=None, home=None, config=None):
    """Internal child modes retain containment when invoked directly."""
    root = c.absolute(str(root))
    marker = c.read_json(root / ".coordinated-harness.json")
    if marker != {"schema": "pentacle.coordinated-harness.v1", "root": str(root)}:
        raise c.Invalid("disposable harness ownership marker required")
    if home is not None and home != root / "home":
        raise c.Invalid("disposable helper home substitution")
    if source is not None and source != root / "release":
        raise c.Invalid("disposable daemon source substitution")
    if script is not None:
        relative = script.relative_to(root)
        valid = (relative.as_posix() == "release/services/chat-stream-v2/provider_account_host.py"
                 or (len(relative.parts) == 6 and relative.parts[0] == "helpers"
                     and relative.parts[2:] == ("app", "services", "chat-stream-v2", "provider_account_host.py")))
        if not valid or script != script.resolve():
            raise c.Invalid("disposable staged helper source required")
    if config is not None:
        if config != root / "config.json": raise c.Invalid("disposable config substitution")
        value = c.read_json(config)
        port = value.get("port")
        if type(port) is not int or not 0 <= port <= 65535 or port in {7791, 7790}:
            raise c.Invalid("production endpoint forbidden in disposable child")
        for key, name in (("policy", "policy.json"), ("database", "sessions.db"), ("ready", "ready.json"), ("home", "home")):
            if value.get(key) != str(root / name): raise c.Invalid("disposable child resource substitution")


def child_helper(script, home, args):
    validate_child(home.parent, script=script, home=home)
    source_paths(script.parents[2])
    os.environ["HOME"] = str(home)
    # Mock the native executable boundary only. The actual helper CLI, host
    # registry, source identity, pin/resource hashes and verifier all run.
    host_config = json.loads((home / ".config/pentacle/provider-account-host.json").read_bytes())
    def native(argv, **kwargs):
        version = next(row["version"] for row in host_config["cells"].values() if row["executable"] == argv[0])
        return subprocess.CompletedProcess(argv, 0, version + "\n", "")
    import provider_account_native
    provider_account_native.native_run = native
    sys.argv = [str(script), *args]
    runpy.run_path(str(script), run_name="__main__")


async def child_daemon(source, config_path):
    validate_child(config_path.parent, source=source, config=config_path)
    source_paths(source)
    from account_executor import AccountExecutor
    from account_runtime import load_policy
    from provider_accounts import Binding, AccountError
    from server import Server
    from sessions import Sessions
    from store import Store
    config = json.loads(config_path.read_bytes())
    policy = load_policy(config["policy"])
    async def execute(binding, action, operation, *, pin):
        executor = policy["executors"][binding.host]
        proc = await asyncio.create_subprocess_exec(executor["python"], "-m", "deploy.coordinated_harness",
            "--helper", executor["script"], "--home", config["home"], "--", action,
            "--provider", binding.provider, "--account", binding.alias, "--login-generation", binding.generation,
            "--operation-id", operation, "--pin-json", json.dumps(pin),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, error = await asyncio.wait_for(proc.communicate(), 15)
        if proc.returncode: raise AccountError("disposable actual helper execution failed: " + out.decode()[-200:] + error.decode()[-800:])
        return json.loads(out)
    executor = AccountExecutor(None, None, execute=execute)
    proofs = [await executor.verify_artifact(Binding(**raw), config["pin"]) for raw in config["bindings"]]
    store = Store(config["database"]); store.start()
    server = Server(host="127.0.0.1", port=config["port"], store=store,
        sessions=Sessions(store, local_host="fixture"), local_host="fixture", dot_principal_stream_ids=[],
        dot_tls_port=0, dot_tls_cert="", dot_tls_key="", dot_tls_binds=["127.0.0.1"],
        dot_read_enabled=False, seat_operator_authority=False)
    server.runtime_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    try:
        port = await server.bind()
        c.atomic_json(config["ready"], {"pid": os.getpid(), "port": port, "sha": server.runtime_sha,
                      "loaded_policy": c.file_digest(config["policy"]), "helper_dispatch": proofs})
        await stop.wait()
    finally:
        await server.close()
        store.stop()


class Rehearsal:
    """Concrete disposable OS adapter for the production transaction state machine."""
    def __init__(self, bindings, controller):
        bindings.validate()  # Before any mkdir, process, staging or socket action.
        self.b, self.controller = bindings, controller
        self.b.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        c.atomic_json(self.b.root / ".coordinated-harness.json", {"schema": "pentacle.coordinated-harness.v1", "root": str(self.b.root)})
        self.repo = self.b.root / "release"
        self.repo.mkdir(mode=0o700)
        self.process = None
        self.fail_forward = self.fail_prior_proof = False
        self.verified_prior = None
        self.events = []
        self.policy = self.b.root / "policy.json"
        self.ready = self.b.root / "ready.json"
        self.home = self.b.root / "home"
        self.home.mkdir(mode=0o700)
        self.b.logs.mkdir(mode=0o700)
        self.installer = self._installer()
        self.seed()

    def _installer(self):
        from deploy.coordinated_runtime import load_installer
        return load_installer(self.controller)

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, text=True, stderr=subprocess.DEVNULL).strip()

    def seed(self):
        from provider_account_host import NativeHost
        from provider_cli_artifact import platform_key
        source = self.controller / "services/chat-stream-v2"
        (self.repo / ".gitignore").write_text(".pentacle-deploy/\nservices/chat-stream-v2/.venv/\n__pycache__/\n")
        for relative in ("services/chat-stream-v2", "services/_shared", "services/agent-orch/agent_orch"):
            destination = self.repo / relative
            destination.mkdir(parents=True, exist_ok=True)
            for path in (self.controller / relative).glob("*.py"):
                shutil.copy2(path, destination / path.name)
        shutil.copytree(source / "tools", self.repo / "services/chat-stream-v2/tools", ignore=shutil.ignore_patterns("__pycache__"))
        for relative in ("pentacle-chat-core/lane_icons.json", "pentacle-chat-core/lane_hues.json",
                         "pentacle-chat-core/tests/fixtures/summary_prose_cases.json",
                         "services/_shared/spawn_defaults.json", "services/chat-stream-v2/requirements.txt", "services/agent-orch/pyproject.toml",
                         "services/agent-orch/deploy/runtime_python.py",
                         "services/chat-stream-v2/deploy/com.pentacle.usage-state-collector.plist"):
            destination = self.repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.controller / relative, destination)
        # The helper subprocess reuses this harness module from the controller,
        # while imports inside the actual helper come from its staged source.
        self.git("init", "-q")
        self.git("config", "user.name", "Test Agent")
        self.git("config", "user.email", "test@example.invalid")
        self.git("add", "."); self.git("commit", "-qm", "prior")
        self.prior = self.git("rev-parse", "HEAD")
        changed = self.repo / "services/chat-stream-v2/provider_accounts.py"
        changed.write_text(changed.read_text() + "\n# disposable identity-changing candidate\n")
        self.git("commit", "-qam", "target")
        self.target = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "--detach", self.prior)
        native_platform = platform_key()
        family = "macos" if native_platform.startswith("darwin") else "wsl"
        config = {"host": "fixture", "registry": str(self.home / "registry.json"), "root": str(self.home / "accounts"), "cells": {}}
        pin = {"schema": 1, "revision": 1, "providers": {}}
        for provider, version in (("claude", "2.1.312"), ("codex", "0.160.0")):
            executable = self.home / provider
            executable.write_text("#!/bin/sh\nprintf '%s\\n' '" + version + "'\n")
            executable.chmod(0o700)
            checksum = c.file_digest(executable)
            config["cells"][provider + ":primary"] = {"enabled": True, "expected_provider_id": "fixture-id",
                "profile_ref": "fixture-profile", "executable": str(executable), "version": version,
                "executable_sha256": checksum, "resource_root": str(executable), "platform": family}
            pin["providers"][provider] = {"version": version, "artifacts": {native_platform: {
                "native_sha256": checksum, "resource_sha256": checksum,
                "provenance": "https://releases.example.test/fixture/" + provider}}}
        host = NativeHost(config)
        bindings = []
        for provider, cell in config["cells"].items():
            provider = provider.split(":")[0]
            draft = host.registry.prepare(provider=provider, alias="primary", host="fixture", generation="login-one",
                profile_ref=cell["profile_ref"], executable=cell["executable"], version=cell["version"], platform=family)
            store = "keychain" if family == "macos" and provider == "claude" else "file"
            if store == "file": c.atomic_bytes(Path(draft.home) / ("auth.json" if provider == "codex" else ".credentials.json"), b"invented-fixture-only")
            binding = host.registry.activate(draft, {"provider_id": "fixture-id", "pool_id": "fixture-pool",
                "auth_mode": "subscription", "store": store, "minimal_turn": True, "independent_login": True}, expected_provider_id="fixture-id")
            bindings.append(vars(binding))
        host_path = self.home / ".config/pentacle/provider-account-host.json"
        host_path.parent.mkdir(mode=0o700, parents=True)
        host_path.parent.parent.chmod(0o700)
        c.atomic_json(host_path, config)
        c.atomic_json(self.policy, {"enabled": True, "executors": {"fixture": {
            "python": sys.executable, "script": str(self.repo / "services/chat-stream-v2/provider_account_host.py")}}})
        c.atomic_json(self.b.config, {"policy": str(self.policy), "pin": pin, "bindings": bindings,
            "home": str(self.home), "port": urlsplit(self.b.url).port,
            "database": str(self.b.root / "sessions.db"), "ready": str(self.ready)})
        for path in (self.b.schedule, self.b.collector):
            c.atomic_bytes(path, plistlib.dumps({"Label": self.b.label + ".inert"}))
        self.original_policy = self.policy.read_bytes()

    def environment(self):
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home),
                "PYTHONPATH": str(self.controller / "services/chat-stream-v2") + os.pathsep + str(self.controller / "services"),
                "LANG": "en_US.UTF-8"}

    def start(self):
        self.ready.unlink(missing_ok=True)
        self.log_handle = (self.b.logs / (str(time.time_ns()) + ".log")).open("wb")
        self.process = subprocess.Popen([sys.executable, "-m", "deploy.coordinated_harness", "--daemon", str(self.repo),
            "--config", str(self.b.config)], env=self.environment(), stdout=self.log_handle, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 20
        while not self.ready.exists():
            if self.process.poll() is not None or time.monotonic() >= deadline:
                detail = Path(self.log_handle.name).read_text(errors="replace")[-1800:]
                raise c.Invalid("disposable daemon did not become ready: " + detail)
            time.sleep(0.05)
        self.started_ns = time.time_ns()
        self.events.append({"event": "started", "pid": self.process.pid, "sha": self.git("rev-parse", "HEAD")})

    def prepare(self, intent, folder):
        stages = {}
        for direction, sha in (("prior", self.prior), ("target", self.target)):
            bundle = folder / direction; bundle.mkdir(mode=0o700)
            with (bundle / "pentacle.tar").open("wb") as output:
                subprocess.run(["git", "archive", sha], cwd=self.repo, stdout=output, check=True)
            manifest = {"commit": sha, "archive_sha256": c.file_digest(bundle / "pentacle.tar")}
            (bundle / "manifest.json").write_text(json.dumps(manifest))
            target = self.installer.Target("fixture", "localhost", str(self.b.root / "helpers"), True)
            release = self.installer.stage_helper_source(target, bundle, sha, manifest)
            stages[direction] = {"sha": sha, "script": release + "/app/services/chat-stream-v2/provider_account_host.py"}
        c.atomic_bytes(folder / "policy.before", self.policy.read_bytes())
        return {"source": stages, "policy_sha256": c.file_digest(self.policy),
                "scope": c.host_scope(intent["machines_file"], intent["excluded_hosts"])}

    def current(self, receipt):
        return {"sha": self.git("rev-parse", "HEAD"), "policy": c.file_digest(self.policy)}

    def revalidate(self, receipt, *, forward):
        if self.current(receipt) != {"sha": self.prior, "policy": receipt["policy_sha256"]}:
            raise c.Invalid("disposable preimage drift")

    def provenance(self, receipt):
        return {"pid": self.process.pid if self.process else None}

    def stop(self, receipt, provenance=None):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
                self.events.append({"event": "forced_owned_cleanup", "pid": self.process.pid})
            self.events.append({"event": "exited", "pid": self.process.pid, "returncode": self.process.returncode})
            self.log_handle.close()
            self.process = None

    def revalidate_stopped(self, receipt, rollback=False):
        if self.process is not None: raise c.Invalid("disposable process still running")

    def switch(self, receipt, *, rollback):
        self.revalidate_stopped(receipt)
        direction = "prior" if rollback else "target"
        self.git("checkout", "-q", "--detach", receipt["source"][direction]["sha"])
        policy = json.loads(self.original_policy)
        if not rollback: policy["executors"]["fixture"]["script"] = receipt["source"][direction]["script"]
        c.atomic_bytes(self.policy, self.original_policy if rollback else c.canonical(policy))
        self.events.append({"event": "switched", "direction": direction})

    def validate_recovery(self, receipt, journal):
        if journal["phase"] == "committed" and self.current(receipt) != journal["postimage"]:
            raise c.Invalid("disposable post-commit drift")

    def validate_restored(self, receipt, journal):
        if self.current(receipt) != journal["restored"]: raise c.Invalid("disposable restored drift")

    def keep_stopped(self, receipt):
        self.stop(receipt)

    def prior_still_running(self, receipt, identity):
        return (self.process is not None and self.process.poll() is None and self.process.pid == identity["pid"]
                and str(self.started_ns) == identity["birth"] and self.git("rev-parse", "HEAD") == identity["sha"])

    def start_verify(self, receipt, *, rollback):
        self.start()
        ready = c.read_json(self.ready)
        from websockets.sync.client import connect
        with connect("ws://127.0.0.1:" + str(ready["port"])) as socket:
            welcome = json.loads(socket.recv(timeout=5))
        expected = self.prior if rollback else self.target
        if welcome.get("runtime_sha") != expected or ready["loaded_policy"] != c.file_digest(self.policy):
            raise c.Invalid("disposable runtime/policy readback mismatch")
        if self.fail_forward and not rollback: raise c.Invalid("controlled post-switch failure")
        if rollback:
            self.verified_prior = {"pid": self.process.pid, "birth": str(self.started_ns), "sha": expected}

        result = {"outcome": "passed", "runtime": ready, "native_boundary": "mocked", "fleet_readiness": "not_tested"}
        if rollback:
            # The disposable adapter proves ordering only; native input is mocked.
            result.update(restoration={"outcome":"failed" if self.fail_prior_proof else "passed", "scope":"disposable_ordering"},
                          model_canary={"outcome":"not_run_by_design"})
        return result

    def post_checks(self, receipt, serving):
        if getattr(self, "fail_late", False):
            return {"outcome": "failed", "fleet_smoke": {"outcome": "failed", "scope": "disposable"}}
        return {"outcome": "passed", "fleet_smoke": {"outcome": "passed", "scope": "disposable"}}

    def close(self):
        self.stop(None)


def run(bindings, controller):
    adapter = Rehearsal(bindings, controller)
    results = []
    try:
        adapter.start()
        machines = bindings.root / "machines.json"
        c.atomic_json(machines, [{"name": "fixture", "ssh_target": None}])
        for index, (inject, late, prior_report_failure) in enumerate(((True, False, False), (False, False, False), (False, True, False), (True, False, True))):
            intent = bindings.root / (f"intent-{index}.json")
            receipt = bindings.root / (f"receipt-{index}.json")
            c.atomic_json(intent, {"schema": c.INTENT_SCHEMA, "transaction_id": f"disposable-transaction-{index}",
                "controller_sha": "a" * 40, "target_sha": adapter.target, "prior_sha": adapter.prior,
                "machines_file": str(machines), "policy_file": str(adapter.policy), "excluded_hosts": [],
                "gate_evidence": str(bindings.root / "synthetic-target-gate.json"),
                "prior_gate_evidence": str(bindings.root / "synthetic-prior-gate.json")})
            with c.transaction_lock(adapter.repo) as root:
                if bindings.journal != root: raise c.Invalid("harness journal binding mismatch")
                transaction = c.Transaction(adapter, root)
                transaction.prepare(intent, receipt)
                adapter.fail_forward = inject
                adapter.fail_late = late
                adapter.fail_prior_proof = prior_report_failure
                result = transaction.apply(intent, receipt)
                if inject:
                    if result["phase"] != "rolled_back" or result["outcome"] != "failed":
                        raise c.Invalid("disposable automatic recovery did not preserve forward failure")
                elif result["phase"] != "committed" or transaction.rollback(receipt)["phase"] != "rolled_back":
                    raise c.Invalid("disposable forward/explicit rollback did not complete")
                if late and result["disposition"] != "active with failed checks":
                    raise c.Invalid("late disposable check rolled back serving target")
                results.append(result)
        return {"outcome": "passed", "transactions": results, "events": adapter.events,
                "fleet_readiness": "not_tested", "native_boundary": "mocked"}
    finally:
        adapter.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--daemon", type=Path)
    parser.add_argument("--helper", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--home", type=Path)
    args, extra = parser.parse_known_args()
    if args.helper:
        child_helper(args.helper, args.home, extra[1:] if extra[:1] == ["--"] else extra)
    elif args.daemon:
        asyncio.run(child_daemon(args.daemon, args.config))
    else:
        root = args.root.resolve()
        bindings = Bindings(root, "com.pentacle.test.coordinated.rehearsal", "ws://127.0.0.1:0",
            root / "config.json", c.state_root(root / "release"), root / "logs", root / "schedule.plist", root / "collector.plist")
        print(json.dumps(run(bindings, Path(__file__).resolve().parents[3]), sort_keys=True))


if __name__ == "__main__": main()
