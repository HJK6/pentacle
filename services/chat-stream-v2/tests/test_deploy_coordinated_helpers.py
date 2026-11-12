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
from .test_deploy_gate_evidence import read_timing_receipt
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
        self.catalog_drift = self.input_drift = self.identity_mismatch = False
        self.verified_prior = self.recovery_authorization = None

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
        if rollback:
            # Observed fresh boot, PID change and runtime SHA readback.
            self.verified_prior = {"pid": 222, "birth": "prior-birth", "sha": "c" * 40}
        completed = []
        for step in (("readback", "dispatch", "canary", "slow_consumer", "cleanup") if rollback else ("readback", "dispatch")):
            try:
                self.event(step + "_" + direction)
            except RuntimeError as exc:
                exc.partial_result = {"outcome": "failed", "completed": list(completed)}
                raise
            completed.append(step)
        if rollback and "unhealthy_" + direction in self.failures:
            return {"outcome": "failed", "fleet_smoke": {"outcome": "failed"}, "slow_consumer": {"outcome": "passed"},
                    "boot": {"outcome": "observed"}, "runtime": {"sha": "b" * 40}, "helper_dispatch": [],
                    **({"restoration": {"outcome": "failed"}} if rollback else {})}
        return ({"outcome":"passed", "restoration":{"outcome":"passed"},
                 "model_canary":{"outcome":"not_run_by_design"}} if rollback else {"outcome":"passed"})

    def post_checks(self, receipt, serving):
        for step in ("canary", "slow_consumer", "cleanup"):
            self.event(step + "_target")
        return {"outcome": "failed" if "unhealthy_target" in self.failures else "passed",
                "fleet_smoke": {"outcome": "failed" if "unhealthy_target" in self.failures else "passed"}}

    def prior_still_running(self, receipt, identity):
        self.event("confirm_prior")
        return not self.identity_mismatch and self.pid == identity["pid"]

    def validate_recovery(self, receipt, journal):
        self.event("validate_recovery")
        self.seen_authorization = self.recovery_authorization
        if self.catalog_drift: raise c.Invalid("catalog drift")
        if journal["phase"] == "committed" and self.current(receipt) != journal["postimage"]:
            raise c.Invalid("post-commit drift")

    def validate_restored(self, receipt, journal):
        assert self.current(receipt) == journal["restored"]

    def keep_stopped(self, receipt):
        self.event("keep_stopped")
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
    "bootstrap_target", "readback_target", "dispatch_target"])
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
    adapter.failures = {"readback_target", "policy_prior"}
    result = tx.apply(intent, path)
    assert result["phase"] == "recovery_required" and adapter.pid is None
    adapter.failures.clear()
    result = tx.rollback(path)
    assert result["phase"] == "rolled_back"
    assert result["forward_failure"]["reason"] == "readback_target"
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
        adapter.failures.add("readback_target")
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
    assert c.ordinary_guard(adapter.repo, {}, rollback=False) is None
    # Existing coordinated ancestry in a live stamp still requires its receipt.
    with pytest.raises(c.Invalid, match="coordinated ancestry"):
        c.ordinary_guard(adapter.repo, {"coordinated_receipt": "/prior/receipt"}, rollback=True)
    value = json.loads(intent.read_bytes()); value["transaction_id"] = "example-transaction-02"
    fresh_intent = private_json(intent.parent / "intent2.json", value)
    assert tx.prepare(fresh_intent, intent.parent / "receipt2.json")["phase"] == "prepared"
    with pytest.raises(c.Invalid, match="stale receipt"): tx.rollback(path)


def test_legacy_forward_and_plain_rollback_cannot_bypass_journal(transaction):
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    with pytest.raises(c.Invalid, match="unfinished"): c.ordinary_guard(adapter.repo, {}, rollback=False)
    tx.apply(intent, path)
    assert c.ordinary_guard(adapter.repo, {}, rollback=False) == str(path)
    with pytest.raises(c.Invalid, match=str(path)): c.ordinary_guard(adapter.repo, {}, rollback=True)


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
    {"coordinated_plan": Path("/intent"), "coordinated_receipt": Path("/receipt"), "rollback": True},
    {"coordinated_plan": Path("/intent"), "coordinated_receipt": Path("/receipt"), "recovery_authorization": Path("/auth")},
    {"coordinated_plan": Path("/intent"), "coordinated_receipt": Path("/receipt"), "prepare_only": True,
     "recovery_authorization": Path("/auth")},
    {"rollback": True, "recovery_authorization": Path("/auth"), "recovery_go_tell_id": "go"},
    {"rollback": True, "coordinated_receipt": Path("/receipt"), "recovery_authorization": Path("/auth")},
    {"rollback": True, "coordinated_receipt": Path("/receipt"), "recovery_go_tell_id": "go"},
])
def test_unsupported_flag_combinations_refuse_before_mutation(tmp_path, kwargs):
    calls = []
    with pytest.raises(deploy_mod.DeployError, match="coordinated"):
        deploy_mod.deploy(deploy_mod.SERVICES["chat-streamd-v2"], release_checkout=tmp_path,
            ref="origin/main", runner=lambda *a: calls.append(a), **kwargs)
    assert calls == []


def test_coordinated_disposition_uses_public_exit_map(monkeypatch, capsys):
    from .test_deploy_script import _run_main
    for value, expected in (({"phase": "committed", "disposition":"active and checks passed"}, 0),
                            ({"phase": "rolled_back", "disposition":"target not active, prior restored"}, 4)):
        assert _run_main(monkeypatch, capsys, stamp=value)[0] == expected


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


def _journal(tx, path):
    return tx.load(path)[1]


def test_failed_late_health_is_journaled_without_recovery(transaction):
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    adapter.failures.add("unhealthy_target")
    result = tx.apply(intent, path)
    journal = _journal(tx, path)
    assert result["phase"] == "committed" and result["outcome"] == "failed"
    assert journal["result"]["fleet_smoke"]["outcome"] == "failed"
    assert journal["forward_failure"] is None and "switch_prior" not in adapter.calls


def test_late_checker_raise_keeps_candidate_active(transaction):
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    adapter.failures.add("slow_consumer_target")
    result = tx.apply(intent, path)
    journal = _journal(tx, path)
    assert result["phase"] == "committed" and result["outcome"] == "failed"
    assert journal["result"]["error"]["reason"] == "slow_consumer_target"
    assert "switch_prior" not in adapter.calls


def test_committed_journal_failure_does_not_restore_serving_candidate(transaction, monkeypatch):
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    real = c.atomic_json
    def no_result(destination, value):
        if Path(destination).name == "journal.json" and "result" in value:
            raise OSError("owned journal unavailable")
        return real(destination, value)
    monkeypatch.setattr(c, "atomic_json", no_result)
    result = tx.apply(intent, path)
    assert result["phase"] == "committed" and result["outcome"] == "failed"
    assert result["journal_failure"] == "OSError" and "switch_prior" not in adapter.calls


@pytest.mark.parametrize("failure", ["readback_prior", "dispatch_prior", "canary_prior",
                                     "slow_consumer_prior", "cleanup_prior", "unhealthy_prior"])
def test_prior_proof_failure_after_verified_boot_leaves_prior_running(transaction, failure):
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    adapter.failures.update({"readback_target", failure})
    result = tx.apply(intent, path)
    journal = _journal(tx, path)
    assert result["phase"] == "recovery_required" and result["exit_code"] == 12 and result["outcome"] == "failed"
    assert adapter.pid == 222 and "keep_stopped" not in adapter.calls
    assert adapter.calls.index("confirm_prior") > adapter.calls.index("bootstrap_prior")
    assert journal["rollback_result"]["outcome"] == "failed"
    assert journal["rollback_result"]["prior_identity"] == {"pid": 222, "birth": "prior-birth", "sha": "c" * 40}
    assert journal["prior_running"] == journal["rollback_result"]["prior_identity"]
    assert journal["forward_failure"]["reason"] == "readback_target"
    assert result["full_fleet_acceptance"] is False


@pytest.mark.parametrize("failure", ["switch_prior", "policy_prior", "bootstrap_prior"])
def test_admitted_failure_before_verified_boot_keeps_stopping(transaction, failure):
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    adapter.failures.update({"readback_target", failure})
    result = tx.apply(intent, path)
    assert result["phase"] == "recovery_required" and adapter.pid is None
    assert "keep_stopped" in adapter.calls and "confirm_prior" not in adapter.calls
    assert "prior_running" not in _journal(tx, path)


def test_identity_mismatch_at_decision_point_signals_nothing(transaction):
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    adapter.failures.update({"readback_target", "dispatch_prior"})
    adapter.identity_mismatch = True
    result = tx.apply(intent, path)
    journal = _journal(tx, path)
    assert result["phase"] == "recovery_required"
    assert "keep_stopped" not in adapter.calls and adapter.pid == 222
    assert journal["prior_identity_mismatch"] == {"pid": 222, "birth": "prior-birth", "sha": "c" * 40}
    assert "prior_running" not in journal


def test_journal_failure_after_passed_restoration_leaves_proven_prior_running(transaction, monkeypatch):
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    adapter.failures.add("readback_target")
    real = c.atomic_json
    def fail_rolled_back(destination, value):
        if Path(destination).name == "journal.json" and value.get("phase") == "rolled_back":
            raise OSError("owned journal unavailable")
        return real(destination, value)
    monkeypatch.setattr(c, "atomic_json", fail_rolled_back)
    result = tx.apply(intent, path)
    assert result["phase"] == "recovery_required" and result["journal_failure"] == "OSError"
    assert adapter.pid == 222 and "keep_stopped" not in adapter.calls
    assert _journal(tx, path)["prior_running"]["pid"] == 222


def test_pre_admission_refusal_of_explicit_recovery_leaves_running_prior_untouched(transaction):
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    adapter.failures.update({"readback_target", "dispatch_prior"})
    tx.apply(intent, path)
    adapter.failures.clear(); adapter.calls.clear(); adapter.catalog_drift = True
    with pytest.raises(c.Invalid, match="catalog drift"):
        tx.rollback(path)
    assert adapter.pid == 222 and "stop" not in adapter.calls and "keep_stopped" not in adapter.calls
    adapter.catalog_drift = False
    assert tx.rollback(path)["phase"] == "rolled_back" and adapter.pid == 222


def _authorization(tx, path, **changes):
    receipt = c.load_receipt(path)
    value = {"transaction_id": receipt["intent"]["transaction_id"], "receipt_sha256": receipt["receipt_sha256"],
             "original_controller_sha": receipt["intent"]["controller_sha"], "replacement_controller_sha": "e" * 40,
             "operation": "recovery", "expected_prior_sha": receipt["intent"]["prior_sha"],
             "expected_target_sha": receipt["intent"]["target_sha"],
             "expected_live": {"sha": receipt["intent"]["prior_sha"], "pid": 222, "birth": "prior-birth"},
             "authorized_by": "host:fd-example", "fd_gate_reference": "fd-gate-example"}
    for key, item in changes.items():
        if item is None: value.pop(key)
        else: value[key] = item
    return private_json(Path(receipt["transaction_directory"]) / "recovery-authorization.json", value)


def _running_prior_after_proof_failure(transaction):
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    adapter.failures.update({"readback_target", "dispatch_prior"})
    tx.apply(intent, path)
    adapter.failures.clear(); adapter.calls.clear()
    return tx, adapter, path


def test_authorized_replacement_recovery_is_admitted_and_journaled(transaction):
    tx, adapter, path = _running_prior_after_proof_failure(transaction)
    auth = _authorization(tx, path)
    result = tx.rollback(path, authorization=auth, go_tell_id="go-tell-example")
    journal = _journal(tx, path)
    assert result["phase"] == "rolled_back"
    assert adapter.seen_authorization["replacement_controller_sha"] == "e" * 40
    assert journal["recovery_authorization"] == {"sha256": c.file_digest(auth), "replacement_controller_sha": "e" * 40,
        "original_controller_sha": "a" * 40, "authorized_by": "host:fd-example",
        "fd_gate_reference": "fd-gate-example", "go_tell_id": "go-tell-example"}


@pytest.mark.parametrize("change", [
    {"transaction_id": "other-transaction-01"}, {"receipt_sha256": "0" * 64},
    {"original_controller_sha": "b" * 40}, {"replacement_controller_sha": "a" * 40},
    {"replacement_controller_sha": "E" * 40}, {"operation": "forward"},
    {"expected_prior_sha": "d" * 40}, {"expected_target_sha": "d" * 40},
    {"expected_live": {"sha": "c" * 40, "pid": 222}}, {"authorized_by": None}, {"fd_gate_reference": ""},
    {"extra": "field"}, "world_readable", "outside_transaction", "no_go_tell"])
def test_invalid_recovery_authorization_refuses_before_stop(transaction, change):
    tx, adapter, path = _running_prior_after_proof_failure(transaction)
    if isinstance(change, dict):
        auth = _authorization(tx, path, **change)
    else:
        auth = _authorization(tx, path)
        if change == "world_readable": auth.chmod(0o644)
        elif change == "outside_transaction":
            auth = private_json(path.parent / "recovery-authorization.json", json.loads(auth.read_bytes()))
    with pytest.raises(c.Invalid):
        tx.rollback(path, authorization=auth, go_tell_id=None if change == "no_go_tell" else "go-tell-example")
    assert adapter.pid == 222 and "stop" not in adapter.calls and "keep_stopped" not in adapter.calls
    assert "recovery_authorization" not in _journal(tx, path)


def test_recovery_authorization_is_refused_by_forward_apply_and_prepare(transaction):
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    auth = _authorization(tx, path)
    adapter.calls.clear()
    with pytest.raises(c.Invalid, match="recovery authorization"):
        tx.apply(intent, path, authorization=auth)
    with pytest.raises(c.Invalid, match="recovery authorization"):
        tx.prepare(intent, path.with_name("receipt-other.json"), authorization=auth)
    assert adapter.calls == [] and adapter.pid == 111


def test_real_disposable_process_helper_forward_failure_and_explicit_rollback(tmp_path):
    from deploy.coordinated_harness import Bindings, run
    root = tmp_path / "process-rehearsal"
    bindings = Bindings(root, "com.pentacle.test.coordinated.pytest", "ws://127.0.0.1:0", root / "config.json",
        c.state_root(root / "release"), root / "logs", root / "schedule.plist", root / "collector.plist")
    result = run(bindings, Path(__file__).resolve().parents[3])
    assert result["outcome"] == "passed" and result["fleet_readiness"] == "not_tested"
    assert len([e for e in result["events"] if e["event"] == "started"]) == 9
    assert len([e for e in result["events"] if e["event"] == "exited"]) == 9


def test_real_disposable_process_prior_proof_failure_leaves_prior_serving(tmp_path):
    from deploy.coordinated_harness import Bindings, Rehearsal
    root = tmp_path / "process-prior-proof"
    bindings = Bindings(root, "com.pentacle.test.coordinated.prior", "ws://127.0.0.1:0", root / "config.json",
        c.state_root(root / "release"), root / "logs", root / "schedule.plist", root / "collector.plist")
    adapter = Rehearsal(bindings, Path(__file__).resolve().parents[3])
    try:
        adapter.start()
        machines = bindings.root / "machines.json"
        c.atomic_json(machines, [{"name": "fixture", "ssh_target": None}])
        intent, receipt = bindings.root / "intent.json", bindings.root / "receipt.json"
        c.atomic_json(intent, {"schema": c.INTENT_SCHEMA, "transaction_id": "disposable-prior-proof",
            "controller_sha": "a" * 40, "target_sha": adapter.target, "prior_sha": adapter.prior,
            "machines_file": str(machines), "policy_file": str(adapter.policy), "excluded_hosts": [],
            "gate_evidence": str(bindings.root / "synthetic-target-gate.json"),
            "prior_gate_evidence": str(bindings.root / "synthetic-prior-gate.json")})
        with c.transaction_lock(adapter.repo) as state:
            tx = c.Transaction(adapter, state)
            tx.prepare(intent, receipt)
            adapter.fail_forward = adapter.fail_prior_proof = True
            result = tx.apply(intent, receipt)
            serving = adapter.process
            assert result["phase"] == "rolled_back" and result["exit_code"] == 12
            assert serving is not None and serving.poll() is None
            assert tx.load(receipt)[1]["rollback_result"]["restoration"]["outcome"] == "failed"
            assert adapter.git("rev-parse", "HEAD") == adapter.prior
    finally:
        adapter.close()


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
    timing = root / "read-timing.json"
    timing.write_text(json.dumps(read_timing_receipt(fixture.target)))
    runtime = Runtime(d, service, fixture.repo, runner, stream="fixture:deployer", boot_wait_seconds=2,
                      read_timing_receipt=timing)
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
    # This preparation fixture represents an already production-qualified prior.
    # The no-inference rollback contract binds that historical witness, not just Git.
    rows = [{"host":b["host"], "provider":b["provider"], "prompt_mode":mode,
             "outcome":"passed", "account_binding":b}
            for b in config["bindings"] for mode in ("prompted","promptless")]
    d._write_stamp(fixture.repo, service, {"sha":fixture.prior,
        "daemon_runtime_readback":{"sha":fixture.prior},
        "daemon_boot_readback":{"outcome":"observed"},
        "fleet_smoke":{"outcome":"passed", "cells":rows}})
    monkeypatch.setenv("HOME", str(fixture.home))
    yield runtime, fixture, intent, root
    fixture.close()


@pytest.mark.parametrize("interpreter_flags", [[], ["-u"]])
def test_real_runtime_prepare_preserves_source_policy_and_proves_configured_interpreters(runtime_preparation, interpreter_flags):
    import plistlib
    runtime, fixture, intent, root = runtime_preparation
    plist_path = root / "daemon.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    # Installed launchd shape: checkout Python, optional -u, checkout main.py.
    plist["ProgramArguments"][1:1] = interpreter_flags
    plist_path.write_bytes(plistlib.dumps(plist))
    before = (fixture.git("rev-parse", "HEAD"), fixture.policy.read_bytes(), plist_path.read_bytes())
    folder = root / "preparation"; folder.mkdir(mode=0o700)
    prepared = runtime.prepare(intent, folder)
    assert before == (fixture.git("rev-parse", "HEAD"), fixture.policy.read_bytes(), plist_path.read_bytes())
    assert prepared["source"]["target"]["code_identity"] != prepared["source"]["prior"]["code_identity"]
    assert all(row["proof"]["closure"]["python"] == sys.executable for source in prepared["source"].values() for row in source["helpers"].values())
    assert len(prepared["source"]["target"]["helpers"]["fixture"]["proof"]["artifacts"]) == 2
    assert prepared["source"]["target"]["smoke_plan"]["hosts"] == ["fixture"]


@pytest.mark.parametrize("prefix", [["-I"], ["-u", "-u"], ["-c"], ["/other/main.py"], ["-u", "/other/main.py"]])
def test_runtime_prepare_rejects_other_entrypoint_prefixes_before_staging(runtime_preparation, prefix):
    import plistlib
    runtime, fixture, intent, root = runtime_preparation
    plist_path = root / "daemon.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    plist["ProgramArguments"][1:1] = prefix
    plist_path.write_bytes(plistlib.dumps(plist))
    before = (fixture.git("rev-parse", "HEAD"), fixture.policy.read_bytes(), plist_path.read_bytes())
    folder = root / "preparation"; folder.mkdir(mode=0o700)
    with pytest.raises(c.Invalid, match="installed daemon must execute the target checkout main.py"):
        runtime.prepare(intent, folder)
    assert not list(folder.iterdir())
    assert before == (fixture.git("rev-parse", "HEAD"), fixture.policy.read_bytes(), plist_path.read_bytes())


def _concrete_counterparts(runtime_preparation, monkeypatch):
    """Real Runtime with launchd/daemon-wire counterparts; returns the shared lifecycle."""
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
    # Shared report callable is defined in deploy.py; route its boundaries to
    # this fixture's isolated counterparts rather than the host launchd/logs.
    monkeypatch.setattr(deploy_mod, "_scan_slow_consumer_window", lambda *a, **k: runtime.d._scan_slow_consumer_window(*a, **k))
    monkeypatch.setattr(deploy_mod, "_process_start", lambda *a: "counterpart-start")
    monkeypatch.setattr(deploy_mod, "_notify_failed_checks", lambda *a: None)
    actual_runner = runtime.runner
    def runner(argv, cwd):
        if argv[0] == "launchctl":
            assert argv[1] == "bootstrap"
            lifecycle.append("bootstrap")
            running["pid"] = running["next"]; running["next"] += 1
            return subprocess.CompletedProcess(argv, 0, "", "")
        if any(str(arg).endswith("release_fd_check.py") for arg in argv):
            return subprocess.CompletedProcess(argv, 0, json.dumps({"outcome":"passed"}), "")
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
            direction = "target" if fixture.git("rev-parse", "HEAD") == fixture.target else "prior"
            profile = sealed["source"][direction]["smoke_plan"]["test_profile"]
            from _shared.spawn_profiles import test_prompt_tuple
            rows = _rows("fixture")
            for row in rows:
                model, effort = test_prompt_tuple(row["provider"], profile)
                row.update(model=model, effort=effort, test_profile=profile,
                    tuple_evidence={"requested_model": model, "requested_effort": effort,
                                    "effective_model": model, "effective_effort": effort})
            return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "status": "PASS", "cells": rows}), "")
        return actual_runner(argv, cwd)
    runtime.runner = runner
    # Intercept only the daemon wire/provider boundary. Run the real no-input
    # verifier, ownership fence and helper subprocess/artifact proof on rollback.
    from contextlib import contextmanager
    from deploy import restoration
    from _shared.spawn_profiles import catalog
    @contextmanager
    def connection(url, token, timeout, registry):
        registry.configure_runtime({"capabilities":{"close_expected_generation":True}})
        rows = []
        def rpc(payload, prefix):
            assert payload["type"] not in {"send","resume"}
            assert not set(payload) & {"initial_prompt","brief","role","text"}
            if prefix == "spawn_catalog_get": return catalog()
            if prefix == "account":
                return {"type":"account.ok", "accounts":c.read_json(fixture.b.config)["bindings"]}
            if prefix == "list_sessions": return {"type":"list_sessions.ok", "active":rows}
            if prefix == "spawn":
                sealed = c.load_receipt(receipt_path)
                raw = next(b for b in sealed["accounts"]["selected_bindings"] if b["provider"]==payload["provider"])
                direction = "target" if fixture.git("rev-parse", "HEAD") == fixture.target else "prior"
                helpers = sealed["source"][direction]["helpers" if direction == "target" else "original_helpers"]
                helper = {**helpers[raw["host"]],
                          **c.read_json(fixture.policy)["executors"][raw["host"]]}
                proof = runtime.remote_json(helper, [helper["python"], helper["script"], "verify-artifact",
                    "--provider", raw["provider"], "--account", raw["alias"], "--login-generation", raw["generation"],
                    "--operation-id", "zero-input-launch", "--pin-json", json.dumps(sealed["accounts"]["pin"])])
                from provider_accounts import Binding
                with sqlite3.connect(sealed["accounts"]["database"]) as conn:
                    conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", ("provider_cli.verified:"+Binding(**raw).key,
                        json.dumps({"proof":proof, "verified_at":time.time()})))
                row = {"stream_id":"fixture:"+payload["session_name"], "host":"fixture",
                       "session_name":payload["session_name"], "session_generation":"fresh-rollback",
                       "account_binding":raw, "bootstrap_state":"ready"}
                rows.append(row); return row
            if prefix == "close": rows.clear(); return {"type":"close.ok"}
            raise AssertionError(prefix)
        def register(payload, result): registry.register_spawn(payload, result, rows[0])
        register.prepare_owned_spawn = registry.prepare_spawn
        def close(stream):
            registry.close(stream, inventory=lambda deadline:rows,
                send_close=lambda payload:rpc(payload,"close"), wait_gone=lambda owned,deadline:not rows, timeout=1)
        yield rpc, lambda stream:None, lambda *a:pytest.fail("no model response expected"), register, close
    monkeypatch.setattr(restoration, "_operator_connection", connection)
    runtime.d._launchd_state = lambda label, runner: deploy_mod.LaunchdState(loaded=running["pid"] is not None, pid=running["pid"])
    return runtime, fixture, plan, receipt_path, running, lifecycle


def test_concrete_adapter_apply_and_committed_rollback_with_real_helper_dispatch(runtime_preparation, monkeypatch):
    runtime, fixture, plan, receipt_path, running, lifecycle = _concrete_counterparts(runtime_preparation, monkeypatch)
    with c.transaction_lock(runtime.repo) as folder:
        tx = c.Transaction(runtime, folder)
        tx.prepare(plan, receipt_path)
        assert tx.apply(plan, receipt_path)["phase"] == "committed"
        assert fixture.git("rev-parse", "HEAD") == fixture.target
        assert tx.rollback(receipt_path)["phase"] == "rolled_back"
        assert fixture.git("rev-parse", "HEAD") == fixture.prior
    assert lifecycle == ["stop", "bootstrap", "stop", "bootstrap"]


@pytest.mark.parametrize("kind", ["unhealthy", "raises"])
def test_concrete_forward_failure_journals_verification(runtime_preparation, monkeypatch, kind):
    runtime, fixture, plan, receipt_path, running, lifecycle = _concrete_counterparts(runtime_preparation, monkeypatch)
    def slow(*a, **k):
        if fixture.git("rev-parse", "HEAD") == fixture.prior: return {"outcome": "passed"}
        if kind == "raises": raise c.Invalid("slow-consumer scan unavailable")
        return {"outcome": "failed", "keepalive_timeout": 2}
    runtime.d._scan_slow_consumer_window = slow
    with c.transaction_lock(runtime.repo) as folder:
        tx = c.Transaction(runtime, folder)
        tx.prepare(plan, receipt_path)
        result = tx.apply(plan, receipt_path)
        journal = tx.load(receipt_path)[1]
    assert result["phase"] == "committed"
    assert result["disposition"] == "active with failed checks"
    assert journal["result"]["outcome"] == "failed"
    assert journal["result"]["slow_consumer"]["outcome"] == "failed"
    assert {"boot", "runtime", "helper_dispatch"} <= set(journal["result"])
    assert lifecycle == ["stop", "bootstrap"]
    assert fixture.git("rev-parse", "HEAD") == fixture.target


def test_concrete_prior_proof_failure_after_verified_boot_leaves_prior_running(runtime_preparation, monkeypatch):
    runtime, fixture, plan, receipt_path, running, lifecycle = _concrete_counterparts(runtime_preparation, monkeypatch)
    runtime.d._verify_runtime_sha = lambda sha, pid: (
        sha == fixture.prior, {"sha": sha, "pid": pid})
    def no_restoration(*a, **k):
        raise c.Invalid("incomplete no-input restoration proof")
    runtime.restoration_proof = no_restoration
    with c.transaction_lock(runtime.repo) as folder:
        tx = c.Transaction(runtime, folder)
        tx.prepare(plan, receipt_path)
        result = tx.apply(plan, receipt_path)
        journal = tx.load(receipt_path)[1]
    assert result["phase"] == "rolled_back" and result["exit_code"] == 12
    assert lifecycle == ["stop", "bootstrap", "stop", "bootstrap"] and running["pid"] == 223
    assert fixture.git("rev-parse", "HEAD") == fixture.prior
    assert journal["rollback_result"]["restoration"]["outcome"] == "failed"
    assert journal["rollback_failure"] is None


def test_concrete_identity_mismatch_at_decision_point_signals_nothing(runtime_preparation, monkeypatch):
    runtime, fixture, plan, receipt_path, running, lifecycle = _concrete_counterparts(runtime_preparation, monkeypatch)
    runtime.d._verify_runtime_sha = lambda sha, pid: (
        sha == fixture.prior, {"sha": sha, "pid": pid})
    def replaced(*a, **k):
        running["pid"] = 999  # launchd now names a different process
        raise c.Invalid("incomplete no-input restoration proof")
    runtime.restoration_proof = replaced
    with c.transaction_lock(runtime.repo) as folder:
        tx = c.Transaction(runtime, folder)
        tx.prepare(plan, receipt_path)
        result = tx.apply(plan, receipt_path)
        journal = tx.load(receipt_path)[1]
    assert result["phase"] == "recovery_required"
    assert lifecycle == ["stop", "bootstrap", "stop", "bootstrap"] and running["pid"] == 999
    assert journal["prior_identity_mismatch"]["pid"] == 223 and "prior_running" not in journal


def _replacement_controller(runtime):
    (runtime.controller / "replacement.txt").write_text("repaired controller\n")
    for command in (["git", "add", "."], ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "replacement"]):
        subprocess.run(command, cwd=runtime.controller, check=True, capture_output=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=runtime.controller, text=True).strip()


def _prepared_receipt(runtime, intent, root, name):
    folder = root / name; folder.mkdir(mode=0o700)
    receipt = {"intent": intent, "transaction_directory": str(folder), "receipt_path": str(folder / "receipt.json"),
               "receipt_sha256": "0" * 64, **runtime.prepare(intent, folder)}
    runtime.guard = lambda *a, **k: {"sole_deployer": True}
    return receipt


def _runtime_authorization(intent, replacement, **live):
    return {"transaction_id": intent["transaction_id"], "original_controller_sha": intent["controller_sha"],
            "replacement_controller_sha": replacement, "operation": "recovery",
            "expected_prior_sha": intent["prior_sha"], "expected_target_sha": intent["target_sha"],
            "expected_live": {"sha": intent["prior_sha"], "pid": 4242, "birth": "live-birth", **live},
            "authorized_by": "fixture:fd", "fd_gate_reference": "fixture-gate"}


def test_replacement_controller_admitted_only_with_exact_authorization(runtime_preparation):
    runtime, fixture, intent, root = runtime_preparation
    receipt = _prepared_receipt(runtime, intent, root, "replacement")
    # An authorization in force binds the replacement exactly; the original
    # pinned controller no longer satisfies it.
    runtime.recovery_authorization = _runtime_authorization(intent, "f" * 40)
    with pytest.raises(c.Invalid, match="controller source drift"):
        runtime.validate_common(receipt, forward=False)
    runtime.recovery_authorization = None
    runtime.validate_common(receipt, forward=False)
    replacement = _replacement_controller(runtime)
    with pytest.raises(c.Invalid, match="controller source drift"):
        runtime.validate_common(receipt, forward=False)
    runtime.recovery_authorization = _runtime_authorization(intent, replacement)
    runtime.validate_common(receipt, forward=False)
    with pytest.raises(c.Invalid, match="controller source drift"):
        runtime.validate_common(receipt, forward=True)
    runtime.recovery_authorization = _runtime_authorization(intent, "f" * 40)
    with pytest.raises(c.Invalid, match="controller source drift"):
        runtime.validate_common(receipt, forward=False)
    runtime.recovery_authorization = _runtime_authorization(intent, replacement)
    (runtime.controller / "dirty.txt").write_text("drift")
    with pytest.raises(c.Invalid, match="controller source drift"):
        runtime.validate_common(receipt, forward=False)
    (runtime.controller / "dirty.txt").unlink()
    subprocess.run(["git", "remote", "set-url", "origin", "https://example.invalid/other.git"], cwd=runtime.controller, check=True)
    with pytest.raises(c.Invalid, match="controller source drift"):
        runtime.validate_common(receipt, forward=False)


@pytest.mark.parametrize("kind", ["match", "helper_proof", "live_pid", "live_birth", "live_sha"])
def test_authorized_recovery_recomputes_prior_proofs_and_live_identity_before_admission(runtime_preparation, kind):
    runtime, fixture, intent, root = runtime_preparation
    receipt = _prepared_receipt(runtime, intent, root, "recompute-" + kind)
    runtime.recovery_authorization = _runtime_authorization(intent, _replacement_controller(runtime))
    runtime.d._launchd_state = lambda label, runner: deploy_mod.LaunchdState(loaded=True, pid=4242)
    runtime.d._process_start = lambda pid, runner: "live-birth" if pid == 4242 else None
    calls = []
    real = runtime.helper_proof
    def proof(*args, **kwargs):
        calls.append(kwargs.get("verify_release"))
        return real(*args, **kwargs)
    runtime.helper_proof = proof
    if kind == "helper_proof":
        for helper in receipt["source"]["prior"]["original_helpers"].values():
            helper["proof"] = {**helper["proof"], "code_identity": "substituted"}
    elif kind == "live_pid": runtime.recovery_authorization["expected_live"]["pid"] = 4343
    elif kind == "live_birth": runtime.recovery_authorization["expected_live"]["birth"] = "other-birth"
    elif kind == "live_sha": runtime.recovery_authorization["expected_live"]["sha"] = intent["target_sha"]
    journal = {"phase": "recovery_required"}
    if kind == "match":
        runtime.validate_recovery(receipt, journal)
        assert calls.count(False) == len(receipt["source"]["prior"]["original_helpers"])
    else:
        with pytest.raises(c.Invalid, match="before stop" if kind == "helper_proof" else "expected live"):
            runtime.validate_recovery(receipt, journal)
    assert not runtime.stopped and fixture.git("rev-parse", "HEAD") == fixture.prior


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
    args = deploy_mod.build_parser().parse_args([
        "chat-streamd-v2", "--rollback", "--coordinated-receipt", "/private/receipt.json",
        "--recovery-controller-authorization", "/private/auth.json", "--recovery-go-tell-id", "go-tell",
    ])
    assert args.recovery_controller_authorization == Path("/private/auth.json") and args.recovery_go_tell_id == "go-tell"


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


@pytest.mark.parametrize('kind', ['exit', 'timeout'])
def test_helper_child_refusal_keeps_bounded_redacted_diagnostics(monkeypatch, kind):
    from deploy.coordinated_runtime import Runtime
    runtime = object.__new__(Runtime)
    secret = 'CanarySecretValue123456789012345'
    stdout = 'x' * 5000 + '\n{"outcome":"account_busy"}\n'
    stderr = ('Authorization: Bearer short-secret\npassword=short-password\ntoken=short-token\n'
              'api_key: "' + secret + '"\n' + secret + '\nhttps://person:short-pass@example.invalid\n'
              '\x1bTraceback: lock unavailable\n')
    failure = (subprocess.CalledProcessError(1, ['ssh', '--private-token=' + secret], output=stdout, stderr=stderr)
               if kind == 'exit' else subprocess.TimeoutExpired(['ssh', secret], 210, output=stdout.encode(), stderr=stderr.encode()))
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(runtime, '_helper_proof', fail)
    with pytest.raises(c.Invalid) as caught:
        runtime.helper_proof({}, {}, 'identity', {}, 'sha')
    detail = str(caught.value)
    assert 'ssh' in detail and 'account_busy' in detail and 'lock unavailable' in detail
    assert kind == 'timeout' and '210' in detail or kind == 'exit' and 'rc=1' in detail
    assert len(detail) < 2600
    assert all(value not in detail for value in [secret, 'short-secret', 'short-password', 'short-token', 'short-pass', '--private-token'])
    assert '\x1b' not in detail


def test_helper_child_refusal_is_journaled_without_starting_apply(transaction, monkeypatch):
    from deploy.coordinated_runtime import Runtime
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    runtime = object.__new__(Runtime)
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ['ssh', 'private-argument'], output='{"outcome":"account_busy"}', stderr='')
    monkeypatch.setattr(runtime, '_helper_proof', fail)
    monkeypatch.setattr(adapter, 'revalidate', lambda *args, **kwargs: runtime.helper_proof({}, {}, '', {}, ''))
    with pytest.raises(c.Invalid, match='account_busy'):
        tx.apply(intent, path)
    _, journal = tx.load(path)
    assert journal['phase'] == 'prepared' and journal['history'] == ['prepared']
    assert 'account_busy' in journal['helper_proof_failure']['reason']
    assert 'private-argument' not in json.dumps(journal)
    assert adapter.calls == ['prepare']
    assert (adapter.source, adapter.policy, adapter.pid) == ('prior', 'prior', 111)


def test_helper_child_refusal_uses_actual_local_subprocess(monkeypatch):
    from deploy.coordinated_runtime import Runtime, load_installer
    runtime = object.__new__(Runtime)
    runtime.installer = load_installer(Path(__file__).resolve().parents[3])
    helper = {'target': {'name': 'fixture', 'ssh': '', 'root': '/unused', 'local': True}}
    def child(*args, **kwargs):
        return runtime.remote_json(helper, [sys.executable, '-c',
            'import sys; print(\'{"outcome":"account_busy"}\'); print("lock unavailable",file=sys.stderr); sys.exit(1)'])
    monkeypatch.setattr(runtime, '_helper_proof', child)
    with pytest.raises(c.Invalid, match='account_busy') as caught:
        runtime.helper_proof({}, {}, '', {}, '')
    assert 'lock unavailable' in str(caught.value) and '"argv0": "sh"' in str(caught.value)


def test_helper_child_journal_write_failure_preserves_original_refusal(transaction, monkeypatch):
    from deploy.coordinated_runtime import Runtime
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    runtime = object.__new__(Runtime)
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(255, ['ssh'], stderr='connection closed')
    monkeypatch.setattr(runtime, '_helper_proof', fail)
    monkeypatch.setattr(adapter, 'revalidate', lambda *args, **kwargs: runtime.helper_proof({}, {}, '', {}, ''))
    def disk_full(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(c, 'atomic_json', disk_full)
    with pytest.raises(c.Invalid, match='rc=255.*connection closed'):
        tx.apply(intent, path)
    _, journal = tx.load(path)
    assert journal['phase'] == 'prepared' and adapter.calls == ['prepare']


def test_helper_child_post_stop_failure_keeps_diagnostic_in_recovery_journal(transaction, monkeypatch):
    from deploy.coordinated_runtime import Runtime
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    runtime = object.__new__(Runtime)
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ['ssh'], output='{"outcome":"account_busy"}', stderr='')
    monkeypatch.setattr(runtime, '_helper_proof', fail)
    original = adapter.start_verify
    def verify(receipt, *, rollback):
        if not rollback:
            runtime.helper_proof({}, {}, '', {}, '')
        return original(receipt, rollback=rollback)
    monkeypatch.setattr(adapter, 'start_verify', verify)
    result = tx.apply(intent, path)
    _, journal = tx.load(path)
    assert result['exit_code'] == 12 and journal['phase'] == 'rolled_back'
    assert 'account_busy' in journal['forward_failure']['reason']
    assert (adapter.source, adapter.policy) == ('prior', 'prior')


def test_helper_waits_for_actual_nativehost_registry_flock(runtime_preparation, monkeypatch):
    """Real flock + NativeHost + AccountExecutor, only host transport is local."""
    import threading
    import time
    from account_executor import AccountExecutor
    from provider_account_host import NativeHost
    from provider_accounts import Binding, AccountError
    runtime, fixture, intent, root = runtime_preparation
    config = c.read_json(fixture.home / '.config/pentacle/provider-account-host.json')
    account = Binding(**c.read_json(fixture.b.config)['bindings'][0])
    pin = c.read_json(fixture.b.config)['pin']
    held = threading.Event()
    release = threading.Event()
    def observation(binding):
        held.set()
        assert release.wait(5)
        return {}
    holder = NativeHost(config, observation=observation)
    holder_thread = threading.Thread(target=lambda: holder.handle('observe', provider=account.provider,
        alias=account.alias, expected_generation=account.generation, operation_id='fixture-holder'))
    holder_thread.start()
    assert held.wait(5)
    helper_host = NativeHost(config, run=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0,
        next(row['version'] for row in config['cells'].values() if row['executable'] == argv[0]) + '\n', ''))
    calls = []
    class Hosts:
        async def run_command(self, host, *argv, **kwargs):
            calls.append(argv)
            try:
                value = helper_host.handle(argv[2], provider=account.provider, alias=account.alias,
                    expected_generation=account.generation, operation_id='fixture-proof', pin=pin)
                return 0, json.dumps(value)
            except AccountError as exc:
                return 1, json.dumps({'outcome': str(exc)})
    executor = AccountExecutor(SimpleNamespace(policy={'executors':{account.host:{'python':sys.executable,
        'script':'/fixture/provider_account_host.py'}}}), Hosts())
    def remote(target, command, **kwargs):
        try:
            value = asyncio.run(executor.execute(account, 'verify-artifact', 'fixture-proof', pin=pin))
            return json.dumps(value)
        except AccountError as exc:
            raise subprocess.CalledProcessError(1, ['sh'], output=json.dumps({'outcome':str(exc)}), stderr='') from exc
    monkeypatch.setattr(runtime.installer, '_remote', remote)
    helper = {'target':{'name':account.host,'ssh':'','root':str(root),'local':True}}
    monkeypatch.setattr(runtime, '_helper_proof', lambda *args, **kwargs: runtime.remote_json(helper,
        [sys.executable, '/fixture/provider_account_host.py', 'verify-artifact']))
    timer = threading.Timer(.35, release.set)
    timer.start()
    started = time.monotonic()
    try:
        proof = runtime.helper_proof(helper, {}, '', {}, '')
        assert proof['outcome'] == 'ok'
        assert len(calls) >= 2 and time.monotonic() - started < 5
    finally:
        release.set(); timer.cancel(); holder_thread.join(timeout=5)
    assert not holder_thread.is_alive()


@pytest.mark.parametrize('returncode,output', [(1,'{"outcome":"account_host_failed"}'),
    (255,'{"outcome":"account_busy"}'), (1,'{"outcome":"account_busy","extra":true}'), (1,'not-json')])
def test_helper_nonbusy_child_failures_never_wait(monkeypatch, returncode, output):
    from deploy.coordinated_runtime import Runtime
    runtime = object.__new__(Runtime)
    calls = []
    def remote(*args, **kwargs):
        calls.append(1)
        raise subprocess.CalledProcessError(returncode, ['ssh'], output=output, stderr='')
    runtime.installer = SimpleNamespace(Target=lambda **kwargs:None, _remote=remote)
    monkeypatch.setattr(runtime, '_helper_proof', lambda *args, **kwargs: runtime.remote_json({'target':{}},
        ['python','provider_account_host.py','verify-artifact']))
    with pytest.raises(c.Invalid): runtime.helper_proof({}, {}, '', {}, '')
    assert len(calls) == 1


def test_busy_deadline_is_shared_and_exhaustion_journals_without_stop(transaction, monkeypatch):
    import deploy.coordinated_runtime as module
    runtime = object.__new__(module.Runtime)
    runtime.installer = SimpleNamespace(Target=lambda **kwargs:SimpleNamespace(name='fixture'))
    calls = []
    def remote(*args, **kwargs):
        calls.append(1)
        raise subprocess.CalledProcessError(1, ['ssh'], output='{"outcome":"account_busy"}', stderr='')
    runtime.installer._remote = remote
    monkeypatch.setattr(module, 'HELPER_BUSY_DEADLINE_SECONDS', .08)
    monkeypatch.setattr(module, 'HELPER_BUSY_BACKOFF_SECONDS', .01)
    monkeypatch.setattr(runtime, '_helper_proof', lambda *args, **kwargs: runtime.remote_json({'target':{}},
        ['python','provider_account_host.py','verify-artifact']))
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    # The real adapter owns the wait; the transaction observer journals progress.
    def revalidate(*args, **kwargs):
        runtime.helper_wait_observer = adapter.helper_wait_observer
        runtime.helper_proof({}, {}, '', {}, '')
    monkeypatch.setattr(adapter, 'revalidate', revalidate)
    with pytest.raises(c.Invalid, match='account_busy') as caught:
        tx.apply(intent, path)
    _, journal = tx.load(path)
    assert journal['phase'] == 'prepared' and adapter.calls == ['prepare']
    waits = journal['helper_proof_waits']
    assert len(waits) >= 2 and waits[-1]['elapsed_seconds'] >= .08
    assert waits[-1]['last_outcome'] == 'account_busy'
    assert waits[-1]['attempt_count'] == len(calls)
    assert 'attempt_count' in str(caught.value)
    before = len(calls)
    # A later proof does not get another deadline or sleep budget.
    with pytest.raises(c.Invalid, match='account_busy'):
        runtime.helper_proof({}, {}, '', {}, '')
    assert len(calls) == before + 1


@pytest.mark.parametrize('failure', ['nonbusy', 'timeout'])
def test_failure_after_busy_is_not_retried(monkeypatch, failure):
    import deploy.coordinated_runtime as module
    runtime = object.__new__(module.Runtime)
    calls = []
    def remote(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, ['ssh'], output='{"outcome":"account_busy"}', stderr='')
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(['ssh'], .01, stderr=b'connection stalled')
        raise subprocess.CalledProcessError(255, ['ssh'], output='', stderr='connection closed')
    runtime.installer = SimpleNamespace(Target=lambda **kwargs:SimpleNamespace(name='fixture'), _remote=remote)
    monkeypatch.setattr(module, 'HELPER_BUSY_BACKOFF_SECONDS', .001)
    monkeypatch.setattr(runtime, '_helper_proof', lambda *args, **kwargs: runtime.remote_json({'target':{}},
        ['python','provider_account_host.py','verify-artifact']))
    with pytest.raises(c.Invalid, match='connection'):
        runtime.helper_proof({}, {}, '', {}, '')
    assert len(calls) == 2
    assert runtime.helper_wait_events[-1]['last_outcome'] == ('child_timeout' if failure == 'timeout' else 'child_failed')


def test_busy_outside_artifact_command_is_not_retried(monkeypatch):
    from deploy.coordinated_runtime import Runtime
    runtime = object.__new__(Runtime)
    calls = []
    def remote(*args, **kwargs):
        calls.append(1)
        raise subprocess.CalledProcessError(1, ['sh'], output='{"outcome":"account_busy"}', stderr='')
    runtime.installer = SimpleNamespace(Target=lambda **kwargs:None, _remote=remote)
    monkeypatch.setattr(runtime, '_helper_proof', lambda *args, **kwargs: runtime.remote_json({'target':{}}, ['python','-c','closure']))
    with pytest.raises(c.Invalid): runtime.helper_proof({}, {}, '', {}, '')
    assert calls == [1]


@pytest.mark.parametrize('payload', ['https://ghp_abc1234567890@example.invalid/path',
    '{"bearer":"smallopaque9"}', 'Bearer=smallopaque9'])
def test_credential_forms_are_redacted_in_refusal_and_prepared_journal(transaction, monkeypatch, payload):
    from deploy.coordinated_runtime import Runtime
    tx, adapter, intent, path = transaction
    tx.prepare(intent, path)
    runtime = object.__new__(Runtime)
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ['ssh', 'private-argument'],
            output='{"outcome":"account_busy"}', stderr=payload)
    monkeypatch.setattr(runtime, '_helper_proof', fail)
    monkeypatch.setattr(adapter, 'revalidate', lambda *args, **kwargs: runtime.helper_proof({}, {}, '', {}, ''))
    with pytest.raises(c.Invalid) as caught:
        tx.apply(intent, path)
    _, journal = tx.load(path)
    forbidden = 'ghp_abc1234567890' if 'ghp_' in payload else 'smallopaque9'
    assert forbidden not in str(caught.value) and forbidden not in json.dumps(journal)
    assert 'account_busy' in str(caught.value)
    assert journal['phase'] == 'prepared' and journal['history'] == ['prepared']
    assert adapter.calls == ['prepare']


@pytest.mark.parametrize('drift', ['sealed_profile', 'settings'])
def test_test_settings_and_snapshot_drift_refuse_before_stop(runtime_preparation, drift):
    runtime, fixture, intent, root = runtime_preparation
    folder = root / 'profile-drift'; folder.mkdir(mode=0o700)
    receipt = {'intent': intent, 'transaction_directory': str(folder), **runtime.prepare(intent, folder)}
    runtime.guard = lambda *a, **k: {'sole_deployer': True}
    if drift == 'sealed_profile':
        path = Path(receipt['source']['target']['test_profile']['path'])
        data = c.read_json(path); data['profile_sha256'] = '0' * 64; c.atomic_json(path, data)
    else:
        (fixture.repo / 'services/_shared/spawn_defaults.local.json').write_text('{}')
    with pytest.raises(c.Invalid): runtime.revalidate(receipt, forward=True)
    assert fixture.git('rev-parse', 'HEAD') == fixture.prior
    assert not runtime.stopped


def test_legacy_dry_plan_still_refuses_forward_model_smoke(runtime_preparation):
    runtime, fixture, intent, root = runtime_preparation
    scope = c.host_scope(intent['machines_file'], [])
    legacy = {'source': intent['machines_file'], 'hosts': ['fixture'], 'optional_hosts': [],
        'excluded_present': [], 'excluded_absent': [],
        'cells': [{'host': 'fixture', 'provider': p, 'prompt_mode': m}
                  for p in ('claude', 'codex') for m in ('prompted', 'promptless')]}
    # The legacy runner is the counterpart; use the real scope/profile validator.
    runtime.checked = lambda *a, **k: subprocess.CompletedProcess([], 0, json.dumps(legacy), '')
    with pytest.raises(c.Invalid, match='compatible test profile'):
        runtime.smoke_plan(fixture.repo, scope, intent['machines_file'])
    assert not runtime.stopped


def test_prepare_fetches_missing_target_before_reachability(runtime_preparation):
    runtime, fixture, intent, root = runtime_preparation
    observed = []
    def missing(*args):
        raise deploy_mod.DeployError('missing local object')
    runtime.d._resolve_sha = missing
    actual = runtime.runner
    def runner(argv, cwd):
        if tuple(argv[:3]) == ('git', 'fetch', 'origin'):
            observed.append(tuple(argv))
            return subprocess.CompletedProcess(argv, 0, '', '')
        return actual(argv, cwd)
    runtime.runner = runner
    def reachable(*args):
        assert observed == [('git', 'fetch', 'origin')]
        raise c.Invalid('stop before fixture staging')
    runtime.d._verify_origin_reachable = reachable
    folder = root / 'fetch-check'; folder.mkdir(mode=0o700)
    with pytest.raises(c.Invalid, match='stop before fixture staging'):
        runtime.prepare(intent, folder)
