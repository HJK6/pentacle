"""Coordinated deployment contract; private stand-ins, no production endpoints."""
import json
import asyncio
import copy
import importlib.util
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from .test_deploy_script import deploy_mod
from .test_deploy_log_guard import isolated_release
from .test_deploy_fleet_verdict import _rows
from deploy import coordinated as c


def private_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


class Counterparts:
    """Lifecycle/transport counterpart; real plan/seal/journal/verdict remain subjects."""
    def __init__(self, root):
        self.repo = root / "release"
        self.repo.mkdir()
        self.calls, self.failures = [], set()
        self.source, self.policy, self.pid = "prior", "prior", 111
        self.catalog_drift = self.input_drift = False

    def event(self, name):
        self.calls.append(name)
        if name in self.failures:
            raise RuntimeError(name)

    def prepare(self, intent, folder):
        self.event("prepare")
        return {"preimages": {"source": self.source, "policy": self.policy},
                "scope": c.host_scope(intent["machines_file"], intent["excluded_hosts"])}

    def revalidate(self, receipt, *, forward):
        self.event("revalidate")
        if self.input_drift: raise c.Invalid("input drift")

    def current(self, receipt):
        return {"source": self.source, "policy": self.policy}

    def provenance(self, receipt):
        return {"pid": self.pid}

    def stop(self, receipt, provenance=None):
        self.event("stop")
        self.pid = None

    def revalidate_stopped(self, receipt, rollback=False):
        self.event("revalidate_stopped")
        assert self.pid is None

    def switch(self, receipt, *, rollback):
        assert self.pid is None, "no mixed-version daemon may serve"
        direction = "prior" if rollback else "target"
        self.event("switch_" + direction)
        self.source = direction
        self.event("policy_" + direction)
        self.policy = direction

    def start_verify(self, receipt, *, rollback):
        assert self.source == self.policy
        direction = "prior" if rollback else "target"
        self.event("bootstrap_" + direction)
        self.pid = 222 if rollback else 333
        for step in ("readback", "dispatch", "canary", "slow_consumer", "cleanup"):
            self.event(step + "_" + direction)
        return {"outcome": "passed"}

    def validate_recovery(self, receipt, journal):
        self.event("validate_recovery")
        if self.catalog_drift: raise c.Invalid("catalog drift")
        if journal["phase"] == "committed" and self.current(receipt) != journal["postimage"]:
            raise c.Invalid("post-commit drift")

    def validate_restored(self, receipt, journal):
        assert self.current(receipt) == journal["restored"]

    def keep_stopped(self, receipt):
        self.pid = None


@pytest.fixture
def transaction(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    machines = private_json(root / "machines.json", [
        {"name": "primary", "ssh_target": None},
        {"name": "peer", "ssh_target": "peer.example.invalid"},
        {"name": "optional", "ssh_target": "optional.example.invalid", "smoke_optional": True},
    ])
    policy = private_json(root / "policy.json", {"executors": {}})
    intent = private_json(root / "intent.json", {
        "schema": c.INTENT_SCHEMA, "transaction_id": "example-transaction-01",
        "controller_sha": "a" * 40, "target_sha": "b" * 40, "prior_sha": "c" * 40,
        "machines_file": str(machines), "policy_file": str(policy),
        "gate_evidence": str(root / "target-gate.json"), "prior_gate_evidence": str(root / "prior-gate.json"),
        "excluded_hosts": [{"host": "optional", "reason": "tracked defect", "authority": "fd:example"}],
    })
    adapter = Counterparts(root)
    with c.transaction_lock(adapter.repo) as state:
        tx = c.Transaction(adapter, state)
        receipt = root / "receipt.json"
        yield tx, adapter, intent, receipt


def test_prepare_apply_and_explicit_committed_rollback(transaction):
    tx, adapter, intent, path = transaction
    prepared = tx.prepare(intent, path)
    assert prepared["phase"] == "prepared"
    assert (adapter.source, adapter.policy, adapter.pid) == ("prior", "prior", 111)
    sealed = path.read_bytes()
    result = tx.apply(intent, path)
    assert result["exit_code"] == 11 and result["full_fleet_acceptance"] is False
    assert result["phase"] == "committed"
    assert path.read_bytes() == sealed
    assert adapter.calls.index("stop") < adapter.calls.index("switch_target") < adapter.calls.index("bootstrap_target")
    restored = tx.rollback(path)
    assert restored["phase"] == "rolled_back" and restored["exit_code"] == 11
    assert (adapter.source, adapter.policy) == ("prior", "prior")
    assert tx.rollback(path) == restored
    assert path.read_bytes() == sealed


@pytest.mark.parametrize("failure", ["stop", "revalidate_stopped", "switch_target", "policy_target",
    "bootstrap_target", "readback_target", "dispatch_target", "canary_target", "slow_consumer_target", "cleanup_target"])
def test_every_post_stop_failure_is_durable_and_failure_dominant(transaction, failure):
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    adapter.failures.add(failure)
    result = tx.apply(intent, path)
    assert result["exit_code"] == 12
    assert result["outcome"] == "failed"
    assert result["forward_failure"]["reason"] == failure
    assert result["phase"] in {"rolled_back", "recovery_required"}
    if result["phase"] == "rolled_back":
        assert adapter.source == adapter.policy == "prior"
    else:
        assert adapter.pid is None


def test_failed_recovery_retries_idempotently_without_rpc(transaction):
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    adapter.failures = {"canary_target", "policy_prior"}
    result = tx.apply(intent, path)
    assert result["phase"] == "recovery_required" and adapter.pid is None
    adapter.failures.clear()
    result = tx.rollback(path)
    assert result["phase"] == "rolled_back"
    assert result["forward_failure"]["reason"] == "canary_target"
    assert result["exit_code"] == 11


@pytest.mark.parametrize("phase", ["stopping", "stopped", "switching", "verifying", "committed", "rolling_back"])
def test_kill_reentry_resumes_only_compensating_recovery(transaction, monkeypatch, phase):
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    persist = tx.phase
    def killed(journal, stage, **updates):
        persist(journal, stage, **updates)
        if stage == phase: raise KeyboardInterrupt("simulated process kill")
    monkeypatch.setattr(tx, "phase", killed)
    if phase == "rolling_back":
        adapter.failures.add("canary_target")
    with pytest.raises(KeyboardInterrupt): tx.apply(intent, path)
    monkeypatch.setattr(tx, "phase", persist)
    with pytest.raises(c.Invalid, match="prepared phase"): tx.apply(intent, path)
    adapter.failures.clear()
    assert tx.rollback(path)["phase"] == "rolled_back"


@pytest.mark.parametrize("kind", ["intent", "inputs", "stale_receipt", "receipt_digest"])
def test_pre_stop_drift_refuses_without_mutation(transaction, kind):
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    if kind == "intent":
        value = json.loads(intent.read_bytes()); value["target_sha"] = "d" * 40; private_json(intent, value)
    elif kind == "inputs": adapter.input_drift = True
    elif kind == "stale_receipt":
        c.atomic_json(tx.root / "active.json", {"receipt": str(path), "receipt_sha256": "wrong"})
    else:
        value = json.loads(path.read_bytes()); value["repo"] = "/substituted"; private_json(path, value)
    with pytest.raises(c.Invalid): tx.apply(intent, path)
    assert "stop" not in adapter.calls and adapter.pid == 111


def test_post_commit_drift_refuses_before_stop(transaction):
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path); tx.apply(intent, path)
    adapter.policy = "newer-unrelated-policy"
    adapter.calls.clear()
    with pytest.raises(c.Invalid, match="post-commit drift"): tx.rollback(path)
    assert "stop" not in adapter.calls


@pytest.mark.parametrize("drift", ["none", "inputs", "catalog", "policy", "source"])
def test_prepared_rollback_discards_without_runtime_effects_and_allows_reprepare(transaction, drift):
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    sealed = path.read_bytes()
    if drift == "inputs": adapter.input_drift = True
    elif drift == "catalog": adapter.catalog_drift = True
    elif drift == "policy": adapter.policy = "newer-unrelated-policy"
    elif drift == "source": adapter.source = "newer-unrelated-source"
    if drift == "inputs":
        with pytest.raises(c.Invalid, match="input drift"): tx.apply(intent, path)
    before = (adapter.current(None), adapter.pid)
    adapter.calls.clear()
    result = tx.rollback(path)
    assert result["phase"] == "discarded" and result["outcome"] == "discarded"
    assert result["exit_code"] == 0 and result["full_fleet_acceptance"] is False
    assert result["runtime_mutated"] is False
    assert adapter.calls == [] and (adapter.current(None), adapter.pid) == before
    assert path.read_bytes() == sealed and tx.rollback(path) == result
    assert c.legacy_guard(adapter.repo, {}, rollback=False) is None
    # Existing coordinated ancestry in a live stamp still requires its receipt.
    with pytest.raises(c.Invalid, match="coordinated ancestry"):
        c.legacy_guard(adapter.repo, {"coordinated_receipt": "/prior/receipt"}, rollback=True)
    value = json.loads(intent.read_bytes()); value["transaction_id"] = "example-transaction-02"
    fresh_intent = private_json(intent.parent / "intent2.json", value)
    assert tx.prepare(fresh_intent, intent.parent / "receipt2.json")["phase"] == "prepared"
    with pytest.raises(c.Invalid, match="stale receipt"): tx.rollback(path)


def test_legacy_forward_and_plain_rollback_cannot_bypass_journal(transaction):
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    with pytest.raises(c.Invalid, match="unfinished"): c.legacy_guard(adapter.repo, {}, rollback=False)
    tx.apply(intent, path)
    assert c.legacy_guard(adapter.repo, {}, rollback=False) == str(path)
    with pytest.raises(c.Invalid, match=str(path)): c.legacy_guard(adapter.repo, {}, rollback=True)


def test_concurrent_apply_or_recovery_denied(transaction):
    tx, adapter, intent, path = transaction
    with pytest.raises(c.Invalid, match="another coordinated"):
        with c.transaction_lock(adapter.repo): pass


@pytest.mark.parametrize("change", ["required", "local", "unknown", "duplicate", "no_reason", "unknown_field"])
def test_invalid_exclusions_refuse_before_preparation(transaction, change):
    tx, adapter, intent, path = transaction
    value = json.loads(intent.read_bytes())
    if change in {"required", "local", "unknown"}:
        value["excluded_hosts"][0]["host"] = {"required": "peer", "local": "primary", "unknown": "unknown"}[change]
    elif change == "duplicate": value["excluded_hosts"] *= 2
    elif change == "no_reason": value["excluded_hosts"][0]["reason"] = ""
    else: value["shell_hook"] = "forbidden"
    private_json(intent, value)
    with pytest.raises(c.Invalid): tx.prepare(intent, path)
    assert adapter.calls == []


@pytest.mark.parametrize("kind", ["missing", "world_readable", "symlink", "malformed"])
def test_private_intent_is_mandatory(transaction, kind):
    tx, adapter, intent, path = transaction
    if kind == "missing": intent.unlink()
    elif kind == "world_readable": intent.chmod(0o644)
    elif kind == "symlink":
        other = intent.with_name("other.json"); intent.rename(other); intent.symlink_to(other)
    else: intent.write_text("not-json")
    with pytest.raises(c.Invalid): tx.prepare(intent, path)
    assert adapter.calls == []


def test_actual_identity_and_account_verifier_accept_only_matching_source_pairs(tmp_path, monkeypatch):
    from account_executor import AccountExecutor
    from provider_accounts import AccountError
    from provider_cli_fixtures import release_pair
    from test_account_rotation import binding
    from dataclasses import replace
    from deploy.coordinated_runtime import code_identity_at
    from provider_cli_artifact import code_identity
    source = Path(code_identity.__code__.co_filename).parent
    modules = []
    for label in ("old", "new"):
        root = tmp_path / label / "services/chat-stream-v2"
        root.mkdir(parents=True)
        shutil.copytree(source.parent / "_shared", root.parent / "_shared", ignore=shutil.ignore_patterns("__pycache__"))
        for path in source.glob("*.py"): shutil.copy2(path, root / path.name)
        if label == "new":
            path = root / "provider_accounts.py"; path.write_text(path.read_text() + "\n# changed identity input\n")
        spec = importlib.util.spec_from_file_location("identity_" + label, root / "provider_cli_artifact.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        modules.append(module)
        repo = root.parents[1]
        for argv in (["git", "init", "-q"], ["git", "add", "."], ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", label]):
            subprocess.run(argv, cwd=repo, check=True, capture_output=True)
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        assert code_identity_at(repo, sha, lambda argv, cwd: subprocess.run(argv, cwd=cwd, text=True, capture_output=True)) == module.code_identity()
    assert modules[0].code_identity() != modules[1].code_identity()
    pin = release_pair()
    account = replace(binding(), executable_sha256=pin["providers"]["codex"]["artifacts"]["darwin-arm64"]["native_sha256"])
    for consumer in modules:
        monkeypatch.setitem(sys.modules, "provider_cli_artifact", consumer)
        for helper in modules:
            async def execute(*args, **kwargs):
                return {"outcome": "ok", "binding_key": account.key, "platform": "darwin-arm64",
                    "artifact": pin["providers"]["codex"]["artifacts"]["darwin-arm64"],
                    "pin_revision": pin["revision"], "pin_digest": consumer.digest(pin), "code_identity": helper.code_identity()}
            executor = AccountExecutor(None, None, execute=execute)
            if helper is consumer:
                assert asyncio.run(executor.verify_artifact(account, pin))["code_identity"] == consumer.code_identity()
            else:
                with pytest.raises(AccountError, match="provider_cli_pin_mismatch"):
                    asyncio.run(executor.verify_artifact(account, pin))


@pytest.mark.parametrize("kwargs", [
    {"prepare_only": True}, {"coordinated_plan": Path("/intent")},
    {"coordinated_receipt": Path("/receipt")},
    {"coordinated_plan": Path("/intent"), "coordinated_receipt": Path("/receipt"), "skip_kickstart": True},
    {"coordinated_plan": Path("/intent"), "coordinated_receipt": Path("/receipt"), "release_inputs": Path("/input")},
    {"coordinated_plan": Path("/intent"), "coordinated_receipt": Path("/receipt"), "rollback": True},
])
def test_unsupported_flag_combinations_refuse_before_mutation(tmp_path, kwargs):
    calls = []
    with pytest.raises(deploy_mod.DeployError, match="coordinated"):
        deploy_mod.deploy(deploy_mod.SERVICES["chat-streamd-v2"], release_checkout=tmp_path,
            ref="origin/main", runner=lambda *a: calls.append(a), **kwargs)
    assert calls == []


def test_scoped_exit_is_distinct_and_failed_forward_never_turns_zero(monkeypatch, capsys):
    from .test_deploy_script import _run_main
    for value, expected in (({"exit_code": 11, "phase": "committed"}, 11),
                            ({"exit_code": 12, "phase": "rolled_back", "forward_failure": {"reason": "canary"}}, 12)):
        assert _run_main(monkeypatch, capsys, stamp=value)[0] == expected
    from deploy import release_selection
    with pytest.raises(release_selection.Invalid, match="non-green"):
        release_selection.check_result({"passed": True, "returncode": 11, "outcome": "pass"})


def test_journal_write_failure_after_stop_is_never_a_refusal(transaction, monkeypatch):
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    real = c.atomic_json
    def fail_after_stopped(destination, value):
        if Path(destination).name == "journal.json" and value.get("phase") != "stopping":
            raise OSError("owned journal unavailable")
        return real(destination, value)
    monkeypatch.setattr(c, "atomic_json", fail_after_stopped)
    result = tx.apply(intent, path)
    assert result["exit_code"] == 12 and result["journal_failure"] == "OSError"
    assert result["phase"] == "recovery_required" and adapter.pid is None


def test_real_disposable_process_helper_forward_failure_and_explicit_rollback(tmp_path):
    from deploy.coordinated_harness import Bindings, run
    root = tmp_path / "process-rehearsal"
    bindings = Bindings(root, "com.pentacle.test.coordinated.pytest", "ws://127.0.0.1:0", root / "config.json",
        c.state_root(root / "release"), root / "logs", root / "schedule.plist", root / "collector.plist")
    result = run(bindings, Path(__file__).resolve().parents[3])
    assert result["outcome"] == "passed" and result["fleet_readiness"] == "not_tested"
    assert len([e for e in result["events"] if e["event"] == "started"]) == 5
    assert len([e for e in result["events"] if e["event"] == "exited"]) == 5


@pytest.mark.parametrize("field,value", [("label", "com.pentacle.chat-streamd-v2"),
    ("url", "ws://127.0.0.1:7791"), ("url", "ws://remote.example.invalid:9999"),
    ("config", Path("/etc/production.json")), ("schedule", Path("/etc/production.plist")),
    ("collector", Path("/etc/collector.plist")), ("journal", Path("/etc/journal")), ("logs", Path("/var/log"))])
def test_disposable_harness_rejects_production_resources_before_start(tmp_path, field, value):
    from dataclasses import replace
    from deploy.coordinated_harness import Bindings
    root = tmp_path / "never-started"
    bindings = Bindings(root, "com.pentacle.test.coordinated.pytest", "ws://127.0.0.1:0", root / "config.json",
        c.state_root(root / "release"), root / "logs", root / "schedule.plist", root / "collector.plist")
    with pytest.raises(c.Invalid): replace(bindings, **{field: value}).validate()
    assert not root.exists()


@pytest.fixture
def runtime_preparation(tmp_path, monkeypatch):
    """Real source archives/interpreters/helper proofs; launchd is installed-file data."""
    from deploy.coordinated_harness import Bindings, Rehearsal
    from deploy.coordinated_runtime import Runtime
    from store import Store
    from account_store import AccountStore
    from provider_accounts import Binding
    import plistlib
    root = tmp_path / "runtime-prepare"
    binding = Bindings(root, "com.pentacle.test.coordinated.prepare", "ws://127.0.0.1:0", root / "config.json",
        c.state_root(root / "release"), root / "logs", root / "schedule.plist", root / "collector.plist")
    fixture = Rehearsal(binding, Path(__file__).resolve().parents[3])
    config = c.read_json(binding.config)
    # A separate frozen controller, including the actual touched staging code.
    controller = root / "controller"; controller.mkdir(mode=0o700)
    installer_path = controller / "services/agent-orch/deploy/install_fleet_spawn_tooling.py"
    installer_path.parent.mkdir(parents=True)
    shutil.copy2(Path(__file__).resolve().parents[2] / "agent-orch/deploy/install_fleet_spawn_tooling.py", installer_path)
    for command in (["git", "init", "-q"], ["git", "add", "."], ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "controller"],
                    ["git", "remote", "add", "origin", "https://example.invalid/public.git"]):
        subprocess.run(command, cwd=controller, check=True, capture_output=True)
    fixture.git("remote", "add", "origin", "https://example.invalid/public.git")
    fixture.git("update-ref", "refs/remotes/origin/main", fixture.target)
    python = fixture.repo / "services/chat-stream-v2/.venv/bin/python"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(python.parents[1])], check=True, capture_output=True)
    import sysconfig
    packages = python.parents[1] / "lib" / ("python" + str(sys.version_info.major) + "." + str(sys.version_info.minor)) / "site-packages"
    (packages / "provisioned.pth").write_text(sysconfig.get_path("purelib") + "\n")
    (fixture.repo / ".git/info/exclude").write_text("services/chat-stream-v2/.venv\n")
    database = Path(config["database"])
    store = Store(str(database)); store.start(); store.stop()
    policy = c.read_json(fixture.policy)
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        accounts = AccountStore(conn, policy)
        for raw in config["bindings"]:
            account = Binding(**raw); accounts.register(account)
            accounts.set_default(host=account.host, provider=account.provider, alias=account.alias)
        conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", ("provider_cli.release_pin", json.dumps(config["pin"])))
        conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", ("provider_cli.pin_initialized", "1"))
        conn.execute("INSERT OR REPLACE INTO v2_account_recovery VALUES (?,?,?)", ("provider-cli-pin:fixture", "done", json.dumps({
            "operation_id": "fixture", "pin": config["pin"], "digest": c.digest(config["pin"])})))
        conn.commit()
    machines = root / "machines.json"; c.atomic_json(machines, [{"name": "fixture", "ssh_target": None}])
    plist = root / "daemon.plist"
    plist.write_bytes(plistlib.dumps({"ProgramArguments": [str(python), str(fixture.repo / "services/chat-stream-v2/main.py"), "--db", str(database)],
        "EnvironmentVariables": {"PENTACLE_MACHINES_FILE": str(machines), "PENTACLE_PROVIDER_ACCOUNT_POLICY": str(fixture.policy)},
        "StandardOutPath": str(binding.logs / "out.log"), "StandardErrorPath": str(binding.logs / "err.log")}))
    d = SimpleNamespace(**{name: getattr(deploy_mod, name) for name in dir(deploy_mod)})
    d._launchd_plist_path = lambda label: binding.schedule if label == "com.pentacle.spawn-fleet-smoke" else plist
    d._usage_collector_plist_path = lambda: binding.collector
    d._launchd_plist = lambda label: plistlib.loads(plist.read_bytes())
    d._daemon_log_paths = lambda svc: (binding.logs / "out.log", binding.logs / "err.log")
    service = deploy_mod.ServiceConfig("chat-streamd-v2", binding.label, "services/chat-stream-v2/requirements.txt", (), str(binding.logs))
    def runner(argv, cwd):
        result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True)
        if argv[0] == "/usr/bin/env" and result.returncode:
            pytest.fail("disposable target runner failed: " + result.stderr)
        return result
    runtime = Runtime(d, service, fixture.repo, runner, stream="fixture:deployer", boot_wait_seconds=2)
    runtime.controller = controller
    intent = {"schema": c.INTENT_SCHEMA, "transaction_id": "runtime-prepare-example", "controller_sha": d._git(controller, "rev-parse", "HEAD", runner=runner),
        "target_sha": fixture.target, "prior_sha": fixture.prior, "machines_file": str(machines), "policy_file": str(fixture.policy), "excluded_hosts": []}
    for direction in ("target", "prior"):
        path = root / (direction + "-gate.json")
        sha = intent[direction + "_sha"]
        c.atomic_json(path, {"schema": "pentacle.v2.gate-evidence.v1", "gate": "merge", "sha": sha,
            "source": {"sha_before": sha, "sha_after": sha, "clean_before": True, "clean_after": True},
            "tiers": [{"tier": tier, "passed": True} for tier in ("unit", "smoke")], "passed": True})
        intent["gate_evidence" if direction == "target" else "prior_gate_evidence"] = str(path)
    monkeypatch.setenv("HOME", str(fixture.home))
    yield runtime, fixture, intent, root
    fixture.close()


def test_real_runtime_prepare_preserves_source_policy_and_proves_configured_interpreters(runtime_preparation):
    runtime, fixture, intent, root = runtime_preparation
    before = (fixture.git("rev-parse", "HEAD"), fixture.policy.read_bytes())
    folder = root / "preparation"; folder.mkdir(mode=0o700)
    prepared = runtime.prepare(intent, folder)
    assert before == (fixture.git("rev-parse", "HEAD"), fixture.policy.read_bytes())
    assert prepared["source"]["target"]["code_identity"] != prepared["source"]["prior"]["code_identity"]
    assert all(row["proof"]["closure"]["python"] == sys.executable for source in prepared["source"].values() for row in source["helpers"].values())
    assert len(prepared["source"]["target"]["helpers"]["fixture"]["proof"]["artifacts"]) == 2
    assert prepared["source"]["target"]["smoke_plan"]["hosts"] == ["fixture"]


def test_concrete_adapter_apply_and_committed_rollback_with_real_helper_dispatch(runtime_preparation):
    import time
    runtime, fixture, intent, root = runtime_preparation
    plan = private_json(root / "intent.json", intent)
    receipt_path = root / "receipt.json"
    running = {"pid": 111, "next": 222}
    lifecycle = []
    runtime.guard = lambda *a, **k: {"sole_deployer": True}
    runtime.provenance = lambda *a: {"processes": []}
    def stop(*a, **k):
        lifecycle.append("stop")
        runtime.old_pid = running["pid"]; runtime.old_start = "prior-start"
        runtime.log_offsets = {}; runtime.stopped = True; running["pid"] = None
    runtime.stop = stop
    runtime.d.verify_fresh_daemon_log_line = lambda *a, **k: deploy_mod.BootReadback(deploy_mod.BOOT_OBSERVED, "counterpart boot", 0, running["pid"], runtime.old_pid)
    runtime.d._verify_runtime_sha = lambda sha, pid: (True, {"sha": sha, "pid": pid})
    runtime.d._process_start = lambda *a: "counterpart-start"
    runtime.d._scan_slow_consumer_window = lambda *a, **k: {"outcome": "passed"}
    actual_runner = runtime.runner
    def runner(argv, cwd):
        if argv[0] == "launchctl":
            assert argv[1] == "bootstrap"
            lifecycle.append("bootstrap")
            running["pid"] = running["next"]; running["next"] += 1
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "/usr/bin/env" and "--dry-run" not in argv:
            policy = c.read_json(fixture.policy)
            sealed = c.load_receipt(receipt_path)
            with sqlite3.connect(sealed["accounts"]["database"]) as conn:
                for raw in sealed["accounts"]["selected_bindings"]:
                    helper = {**sealed["source"]["target"]["helpers"][raw["host"]], **policy["executors"][raw["host"]]}
                    proof = runtime.remote_json(helper, [helper["python"], helper["script"], "verify-artifact",
                        "--provider", raw["provider"], "--account", raw["alias"], "--login-generation", raw["generation"],
                        "--operation-id", "synthetic-canary", "--pin-json", json.dumps(sealed["accounts"]["pin"])])
                    from provider_accounts import Binding
                    conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", ("provider_cli.verified:" + Binding(**raw).key,
                        json.dumps({"proof": proof, "verified_at": time.time()})))
                conn.commit()
            return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "status": "PASS", "cells": _rows("fixture")}), "")
        return actual_runner(argv, cwd)
    runtime.runner = runner
    with c.transaction_lock(runtime.repo) as folder:
        tx = c.Transaction(runtime, folder)
        tx.prepare(plan, receipt_path)
        assert tx.apply(plan, receipt_path)["phase"] == "committed"
        assert fixture.git("rev-parse", "HEAD") == fixture.target
        assert tx.rollback(receipt_path)["phase"] == "rolled_back"
        assert fixture.git("rev-parse", "HEAD") == fixture.prior
    assert lifecycle == ["stop", "bootstrap", "stop", "bootstrap"]


def test_identity_change_refuses_before_active_checkout(isolated_release, monkeypatch):
    module, service, repo, prior, target, before, calls, activate = isolated_release
    # The identity computation itself is exercised below with real source bytes.
    monkeypatch.setattr(module, "_code_identity_at", lambda _repo, sha, _runner: sha, raising=False)
    with pytest.raises(module.DeployError, match="coordinated"):
        activate()
    assert module._git(repo, "rev-parse", "HEAD", runner=module._run) == prior
    assert module._stamp_path(repo, service).read_bytes() == before
    assert not any(call[0] == "launchctl" for call in calls)


def test_real_git_identity_change_refuses_without_a_plan(isolated_release):
    module, service, repo, prior, target, before, calls, activate = isolated_release
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, text=True, stderr=subprocess.DEVNULL).strip()
    git("checkout", "-q", "--detach", target)
    path = repo / "services/chat-stream-v2/provider_accounts.py"
    path.write_text(path.read_text() + "\n# actual changed shared identity input\n")
    git("commit", "-qam", "changed shared identity")
    # This fixture origin is an owned local bare repository, never public.
    git("push", "-q", "origin", "HEAD:refs/heads/main")
    git("checkout", "-q", "--detach", prior)
    with pytest.raises(module.DeployError, match="coordinated plan"): activate()
    assert git("rev-parse", "HEAD") == prior
    assert module._stamp_path(repo, service).read_bytes() == before
    assert not any(call[0] == "launchctl" for call in calls)


def test_declared_optional_exclusion_is_explicit_scoped_success(tmp_path):
    path = tmp_path / "machines.json"
    path.write_text(json.dumps([
        {"name": "primary", "ssh_target": None},
        {"name": "peer", "ssh_target": "peer.example.invalid"},
        {"name": "optional", "ssh_target": "optional.example.invalid", "smoke_optional": True},
    ]))
    result = subprocess.CompletedProcess([], 0, json.dumps({
        "status": "PASS", "ok": True, "cells": _rows("primary") + _rows("peer"),
        "failures": [], "untested": [],
    }), "")
    verdict = deploy_mod._fleet_smoke_verdict(result, str(path), declared_exclusions=[{
        "host": "optional", "reason": "tracked readiness defect", "authority": "fd:grant-example",
    }])
    assert verdict["outcome"] == "passed"
    assert verdict["scope"] == "declared_optional_exclusions"
    assert verdict["full_fleet_acceptance"] is False
    assert len(verdict["cells"]) == 12
    assert len(verdict["skipped"]) == 4
    assert {row["outcome"] for row in verdict["skipped"]} == {"SKIPPED_DECLARED"}


def test_parser_exposes_coordinated_intent_and_receipt():
    args = deploy_mod.build_parser().parse_args([
        "chat-streamd-v2", "--coordinated-plan", "/private/intent.json",
        "--coordinated-receipt", "/private/receipt.json", "--prepare-only",
    ])
    assert args.prepare_only


def test_managed_helper_root_is_stable_after_a_coordinated_release(tmp_path):
    from deploy.coordinated_runtime import helper_release_root, HELPER
    root = helper_release_root(tmp_path / "runtime" / HELPER)
    assert root == tmp_path / ".pentacle-account-helpers"
    assert helper_release_root(root / ("a" * 40) / "app" / HELPER) == root
    with pytest.raises(c.Invalid): helper_release_root(root / "malformed" / "app" / HELPER)


def test_internal_harness_children_cannot_bypass_disposable_resource_binding(tmp_path):
    from deploy.coordinated_harness import validate_child
    root = tmp_path / "owned"; root.mkdir(mode=0o700)
    c.atomic_json(root / ".coordinated-harness.json", {"schema": "pentacle.coordinated-harness.v1", "root": str(root)})
    with pytest.raises(c.Invalid): validate_child(root, source=Path("/production/runtime"))
    with pytest.raises((c.Invalid, ValueError)): validate_child(root, script=Path("/production/provider_account_host.py"), home=root / "home")
    with pytest.raises(c.Invalid): validate_child(root, home=Path("/production/home"))
    c.atomic_json(root / "config.json", {"port": 7791})
    with pytest.raises(c.Invalid): validate_child(root, source=root / "release", config=root / "config.json")


@pytest.mark.parametrize("kind", ["catalog", "policy", "staged_source", "manifest", "interpreter", "pin_artifact", "gate", "controller"])
def test_concrete_input_drift_refuses_before_stop(runtime_preparation, kind):
    runtime, fixture, intent, root = runtime_preparation
    folder = root / "prepared"; folder.mkdir(mode=0o700)
    receipt = {"intent": intent, "transaction_directory": str(folder), **runtime.prepare(intent, folder)}
    runtime.guard = lambda *a, **k: {"sole_deployer": True}
    target = receipt["source"]["target"]["helpers"]["fixture"]
    if kind == "catalog": Path(intent["machines_file"]).write_text("[]")
    elif kind == "policy":
        policy = c.read_json(fixture.policy); policy["preserve-extra-field"] = True; c.atomic_json(fixture.policy, policy)
    elif kind == "staged_source": Path(target["script"]).write_text("# damaged staged helper\n")
    elif kind == "manifest": (Path(target["script"]).parents[3] / "manifest.json").unlink()
    elif kind == "interpreter": target["python"] = "/different/interpreter"
    elif kind == "pin_artifact": (fixture.home / "codex").write_text("changed native bytes")
    elif kind == "gate": Path(intent["gate_evidence"]).write_text("{}")
    else: (runtime.controller / "unrelated-change").write_text("drift")
    with pytest.raises(c.Invalid): runtime.revalidate(receipt, forward=True)
    assert fixture.git("rev-parse", "HEAD") == fixture.prior
    assert not runtime.stopped
