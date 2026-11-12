"""Private, durable helper/daemon transaction used by the supported deployer.

Intent is operator-owned input; the sealed receipt is immutable preparation
output. Only the journal changes during apply/recovery. This module never
imports code from the daemon checkout it switches.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import shutil
import uuid


INTENT_SCHEMA = "pentacle.coordinated-deploy.intent.v1"
RECEIPT_SCHEMA = "pentacle.coordinated-deploy.receipt.v2"
PHASES = {"prepared", "discarded", "stopping", "stopped", "switching", "verifying", "committed",
          "rolling_back", "rolled_back", "recovery_required"}
TERMINAL = {"committed", "rolled_back", "discarded"}
AUTHORIZATION_FIELDS = {"transaction_id", "receipt_sha256", "original_controller_sha", "replacement_controller_sha",
                        "operation", "expected_prior_sha", "expected_target_sha", "expected_live",
                        "authorized_by", "fd_gate_reference"}


class Invalid(RuntimeError):
    pass


class HelperProofFailure(Invalid):
    """A refused helper child, with safe diagnostic text only."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def strict_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Invalid("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique)


def absolute(value):
    if not isinstance(value, str) or not value or "\0" in value:
        raise Invalid("invalid absolute path")
    path = Path(value)
    if not path.is_absolute() or path != path.resolve():
        raise Invalid("path must be absolute and have no symlink components")
    return path


def private_file(path):
    path = absolute(str(path))
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise Invalid("private input must be an owner-only regular file")
    return path


def private_directory(path):
    path = absolute(str(path))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise Invalid("transaction directory must be owner-only")
    return path


def read_json(path):
    try:
        value = strict_json(private_file(path).read_bytes())
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError) as exc:
        raise Invalid("missing or invalid private JSON input") from exc


def atomic_bytes(path, data, *, mode=0o600):
    path = Path(path)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_bytes(path, canonical(value) + b"\n")


def host_scope(machines_file, excluded):
    payload = strict_json(Path(machines_file).read_bytes())
    rows = payload.get("machines") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise Invalid("installed machine catalog is empty")
    hosts, local, optional = [], [], []
    for row in rows:
        if not isinstance(row, dict) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}", str(row.get("name", ""))):
            raise Invalid("invalid installed host")
        name = row["name"]
        if name in hosts or type(row.get("smoke_optional", False)) is not bool:
            raise Invalid("duplicate host or invalid optional marker")
        hosts.append(name)
        if row.get("ssh_target") is None:
            local.append(name)
        elif not isinstance(row["ssh_target"], str) or not row["ssh_target"].strip() or row["ssh_target"].startswith("-"):
            raise Invalid("invalid installed transport")
        if row.get("smoke_optional") is True:
            optional.append(name)
    if not local:
        raise Invalid("installed local host is missing")
    if not isinstance(excluded, list):
        raise Invalid("excluded_hosts must be a list")
    names = []
    for row in excluded:
        if not isinstance(row, dict) or set(row) != {"host", "reason", "authority"}:
            raise Invalid("exclusion requires host, reason and authority")
        name = row["host"]
        if name in names or name not in optional or name in local:
            raise Invalid("only unique configured optional remote hosts may be excluded")
        if any(not isinstance(row[key], str) or not row[key].strip() or len(row[key]) > 1024 for key in ("reason", "authority")):
            raise Invalid("exclusion requires reason and authority")
        names.append(name)
    return {"hosts": hosts, "local_hosts": local, "optional_hosts": optional,
            "required_hosts": [h for h in hosts if h in local or h not in optional],
            "included_hosts": [h for h in hosts if h not in names],
            "excluded_hosts": copy.deepcopy(excluded), "machines": rows}


def validate_intent(path):
    intent = read_json(path)
    fields = {"schema", "transaction_id", "controller_sha", "target_sha", "prior_sha",
              "machines_file", "policy_file", "excluded_hosts", "gate_evidence", "prior_gate_evidence"}
    if set(intent) - {'smoke_accounts'} != fields or intent.get("schema") != INTENT_SCHEMA:
        raise Invalid("unsupported coordinated intent schema or fields")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{7,95}", str(intent["transaction_id"])):
        raise Invalid("invalid transaction id")
    for key in ("controller_sha", "target_sha", "prior_sha"):
        if not re.fullmatch(r"[0-9a-f]{40}", str(intent[key])):
            raise Invalid("intent requires exact lowercase source SHAs")
    for key in ("machines_file", "policy_file", "gate_evidence", "prior_gate_evidence"):
        absolute(intent[key])
    private_file(intent["policy_file"])
    scope = host_scope(intent["machines_file"], intent["excluded_hosts"])
    smoke_account_map(intent.get('smoke_accounts', []), scope)
    return intent


def smoke_account_map(entries, scope):
    from tools.spawn_fleet_smoke import account_map
    if not isinstance(entries, list) or any(not isinstance(v, str) for v in entries):
        raise Invalid('smoke_accounts must be an array of account declarations')
    cells = tuple((h, p, m) for h in scope['included_hosts']
                  for p in ('claude', 'codex') for m in ('prompted', 'promptless'))
    try:
        return account_map(entries, cells)
    except ValueError as exc:
        raise Invalid('invalid smoke_accounts: ' + str(exc)) from exc


def state_root(repo):
    repo = Path(repo).resolve()
    return repo.parent / (".pentacle-coordinated-" + hashlib.sha256(str(repo).encode()).hexdigest()[:16])


@contextmanager
def transaction_lock(repo):
    root = private_directory(state_root(repo))
    path = root / "lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_uid != os.getuid() or stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            raise Invalid("insecure transaction lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Invalid("another coordinated apply/recovery is running") from exc
        yield root
    finally:
        os.close(fd)


def active_transaction(repo):
    pointer = state_root(repo) / "active.json"
    return read_json(pointer) if pointer.exists() else None


def ordinary_guard(repo, stamp, *, rollback, helper_identity_equal=False):
    active = active_transaction(repo)
    ancestry = stamp.get("coordinated_receipt")
    if active:
        journal = read_json(Path(active["journal"]))
        if journal.get("phase") not in TERMINAL:
            raise Invalid("unfinished coordinated transaction; recover with --rollback --coordinated-receipt " + active["receipt"])
        if journal["phase"] == "committed":
            ancestry = active["receipt"]
    if rollback and ancestry and not helper_identity_equal:
        raise Invalid("coordinated ancestry requires --rollback --coordinated-receipt " + str(ancestry))
    return ancestry


def recovery_authorization(path, receipt, go_tell_id):
    """FD-authorized replacement controller for recovering one sealed receipt.

    The file is not self-authorizing: the FD GO tell quotes its digest and the
    replacement SHA, and its id is journaled with the recovery evidence.
    """
    path = private_file(path)
    if path.parent != Path(receipt["transaction_directory"]):
        raise Invalid("recovery authorization must be in the transaction directory")
    value = read_json(path)
    intent = receipt["intent"]
    sha = re.compile(r"[0-9a-f]{40}")
    live = value.get("expected_live")
    if (set(value) != AUTHORIZATION_FIELDS
            or any(not isinstance(value[key], str) or not sha.fullmatch(value[key])
                   for key in ("original_controller_sha", "replacement_controller_sha", "expected_prior_sha", "expected_target_sha"))
            or value["transaction_id"] != intent["transaction_id"] or value["receipt_sha256"] != receipt["receipt_sha256"]
            or value["original_controller_sha"] != intent["controller_sha"]
            or value["replacement_controller_sha"] == intent["controller_sha"]
            or value["operation"] != "recovery" or value["expected_prior_sha"] != intent["prior_sha"]
            or value["expected_target_sha"] != intent["target_sha"]
            or not isinstance(live, dict) or set(live) != {"sha", "pid", "birth"}
            or not isinstance(live["sha"], str) or not sha.fullmatch(live["sha"])
            or type(live["pid"]) is not int or live["pid"] <= 0 or not isinstance(live["birth"], str) or not live["birth"]
            or any(not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > 1024
                   for key in ("authorized_by", "fd_gate_reference"))
            or not isinstance(go_tell_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", go_tell_id)):
        raise Invalid("recovery authorization does not bind this transaction, receipt and FD GO")
    record = {"sha256": file_digest(path), "replacement_controller_sha": value["replacement_controller_sha"],
              "original_controller_sha": value["original_controller_sha"], "authorized_by": value["authorized_by"],
              "fd_gate_reference": value["fd_gate_reference"], "go_tell_id": go_tell_id}
    return value, record


def seal_receipt(receipt):
    receipt = copy.deepcopy(receipt)
    receipt["receipt_sha256"] = digest(receipt)
    return receipt


def load_receipt(path):
    receipt = read_json(path)
    sealed = dict(receipt)
    expected = sealed.pop("receipt_sha256", None)
    if receipt.get("schema") != RECEIPT_SCHEMA or expected != digest(sealed):
        raise Invalid("invalid sealed coordinated receipt")
    return receipt


class Transaction:
    """Ordered state machine. The adapter owns concrete OS/transport operations."""

    def __init__(self, adapter, root):
        self.adapter, self.root = adapter, Path(root)

    def phase(self, journal, phase, **updates):
        if phase not in PHASES:
            raise Invalid("invalid transaction phase")
        journal.update(updates, phase=phase)
        journal.setdefault("history", []).append(phase)
        atomic_json(self.root / journal["transaction_id"] / "journal.json", journal)

    def prepare(self, intent_path, receipt_path, authorization=None):
        if authorization is not None:
            raise Invalid("recovery authorization is accepted only by --rollback --coordinated-receipt")
        intent = validate_intent(intent_path)
        receipt_path = absolute(str(receipt_path))
        private_directory(receipt_path.parent)
        if receipt_path.exists():
            raise Invalid("receipt exists; use its sealed transaction or a new id")
        active = active_transaction(self.adapter.repo)
        if active and read_json(active["journal"]).get("phase") not in TERMINAL:
            raise Invalid("unfinished coordinated transaction blocks preparation")
        folder = self.root / intent["transaction_id"]
        if folder.exists():
            raise Invalid("transaction id already exists")
        # Claim a new directory exclusively. Never delete an existing/unowned one.
        folder.mkdir(mode=0o700)
        folder.chmod(0o700)
        owned = folder.stat()
        try:
            prepared = self.adapter.prepare(intent, folder)
            receipt = seal_receipt({"schema": RECEIPT_SCHEMA, "intent": intent,
                                    "intent_path": str(private_file(intent_path)),
                                    "intent_sha256": file_digest(intent_path),
                                    "repo": str(self.adapter.repo), "receipt_path": str(receipt_path),
                                    "transaction_directory": str(folder), **prepared})
            atomic_json(receipt_path, receipt)
            journal = {"transaction_id": intent["transaction_id"], "receipt": str(receipt_path),
                       "receipt_sha256": receipt["receipt_sha256"], "forward_failure": None,
                       "rollback_failure": None, "readbacks": [], "history": []}
            if getattr(self.adapter, "helper_wait_events", None):
                journal["helper_proof_waits"] = copy.deepcopy(self.adapter.helper_wait_events)
            self.phase(journal, "prepared")
            # Prepared transactions also block ordinary deploys: their preimages must
            # not silently become stale during the FD window.
            atomic_json(self.root / "active.json", {"receipt": str(receipt_path),
                        "receipt_sha256": receipt["receipt_sha256"], "journal": str(folder / "journal.json")})
            return {"phase": "prepared", "coordinated_receipt": str(receipt_path),
                    "receipt_sha256": receipt["receipt_sha256"], "prepared_only": True}
        except Exception:
            if folder.exists() and folder.stat().st_ino == owned.st_ino and folder.stat().st_uid == os.getuid():
                shutil.rmtree(folder)
            # No receipt is usable unless preparation and its journal completed.
            receipt_path.unlink(missing_ok=True)
            raise

    def load(self, receipt_path):
        receipt = load_receipt(receipt_path)
        if receipt["repo"] != str(self.adapter.repo) or receipt["receipt_path"] != str(absolute(str(receipt_path))):
            raise Invalid("receipt checkout/path substitution")
        folder = self.root / receipt["intent"]["transaction_id"]
        if receipt["transaction_directory"] != str(folder):
            raise Invalid("receipt transaction-directory substitution")
        journal = read_json(folder / "journal.json")
        active = active_transaction(self.adapter.repo)
        if (not active or active.get("receipt_sha256") != receipt["receipt_sha256"]
                or active.get("receipt") != str(receipt_path)
                or active.get("journal") != str(folder / "journal.json")
                or journal.get("transaction_id") != receipt["intent"]["transaction_id"]
                or journal.get("phase") not in PHASES
                or journal.get("receipt_sha256") != receipt["receipt_sha256"]):
            raise Invalid("stale receipt: not the active coordinated transaction")
        return receipt, journal

    @contextmanager
    def helper_wait_journal(self, journal):
        previous = getattr(self.adapter, "helper_wait_observer", None)
        def record(row):
            journal.setdefault("helper_proof_waits", []).append(copy.deepcopy(row))
            try:
                atomic_json(self.root / journal["transaction_id"] / "journal.json", journal)
            except OSError:
                pass  # Diagnostics cannot replace a refusal or change admission.
        self.adapter.helper_wait_observer = record
        try:
            yield
        finally:
            self.adapter.helper_wait_observer = previous

    def apply(self, intent_path, receipt_path, authorization=None):
        if authorization is not None:
            raise Invalid("recovery authorization is accepted only by --rollback --coordinated-receipt")
        receipt, journal = self.load(receipt_path)
        with self.helper_wait_journal(journal):
            return self._apply(intent_path, receipt, journal)

    def _apply(self, intent_path, receipt, journal):
        if journal["phase"] != "prepared":
            raise Invalid("forward deployment requires prepared phase; recover unfinished transactions explicitly")
        if (file_digest(private_file(intent_path)) != receipt["intent_sha256"]
                or validate_intent(intent_path) != receipt["intent"]):
            raise Invalid("intent changed after preparation")
        try:
            self.adapter.revalidate(receipt, forward=True)
        except HelperProofFailure as exc:
            # Diagnostic only: keep prepared phase, original refusal and the
            # existing no-stop/no-retry behavior, even if journaling fails.
            journal["helper_proof_failure"] = {"type": type(exc).__name__, "reason": str(exc)}
            try:
                atomic_json(self.root / journal["transaction_id"] / "journal.json", journal)
            except OSError:
                pass
            raise
        # This readback is persisted before the stop: recovery does not need RPC.
        before = self.adapter.current(receipt)
        journal["before_stop"] = before
        journal["stop_provenance"] = self.adapter.provenance(receipt)
        self.phase(journal, "stopping")
        try:
            self.adapter.stop(receipt, provenance=journal["stop_provenance"])
            self.phase(journal, "stopped")
            self.adapter.revalidate_stopped(receipt)
            self.phase(journal, "switching")
            self.adapter.switch(receipt, rollback=False)
            self.phase(journal, "verifying", installed=self.adapter.current(receipt))
            try:
                result = self.adapter.start_verify(receipt, rollback=False)
            except Exception as exc:
                # Keep the sub-results a later verifier's raise would discard.
                if isinstance(getattr(exc, "partial_result", None), dict):
                    journal["partial_result"] = copy.deepcopy(exc.partial_result)
                raise
            if result.get("outcome") != "passed":
                journal["result"] = result
                try:
                    atomic_json(self.root / journal["transaction_id"] / "journal.json", journal)
                except OSError as journal_exc:
                    journal["journal_failure"] = type(journal_exc).__name__
                raise Invalid("candidate health failed")
        except Exception as exc:
            # Never replace the original failure with a successful recovery.
            journal["forward_failure"] = {"type": type(exc).__name__, "reason": str(exc)}
            try:
                self.phase(journal, journal["phase"])
            except Exception as journal_exc:
                journal["journal_failure"] = type(journal_exc).__name__
            self.recover(receipt, journal, automatic=True)
            return self.result(receipt, journal)
        try:
            self.phase(journal, "committed", result=result, postimage=self.adapter.current(receipt))
        except Exception as exc:
            # Serving and helper proofs passed. A journal write failure is not
            # authority to stop the proven candidate.
            journal.update(phase="committed", result={**result, "outcome": "failed"},
                           journal_failure=type(exc).__name__)
            return self.result(receipt, journal)
        # The commit is outside the automatic recovery region. Late failures,
        # including exceptions and report persistence failures, never stop it.
        try:
            checks = self.adapter.post_checks(receipt, result)
        except Exception as exc:
            checks = {"outcome": "failed", "error": {"type": type(exc).__name__, "reason": str(exc)}}
        journal["result"] = {**result, **checks}
        try:
            # Only our report stamp changed after the serving commit. Keep
            # original source/policy/helper preimages for later drift checks.
            postimage = copy.deepcopy(journal["postimage"])
            current = self.adapter.current(receipt)
            if "stamp" in postimage.get("files", {}):
                postimage["files"]["stamp"] = current["files"]["stamp"]
            self.phase(journal, "committed", postimage=postimage)
        except Exception as exc:
            journal["journal_failure"] = type(exc).__name__
            journal["result"]["outcome"] = "failed"
        return self.result(receipt, journal)

    def rollback(self, receipt_path, authorization=None, go_tell_id=None):
        receipt, journal = self.load(receipt_path)
        record = None
        if authorization is not None or go_tell_id is not None:
            if journal["phase"] in {"prepared", "discarded", "rolled_back"}:
                raise Invalid("recovery authorization requires an unfinished applied transaction")
            value, record = recovery_authorization(authorization, receipt, go_tell_id)
            self.adapter.recovery_authorization = value
        with self.helper_wait_journal(journal):
            return self._rollback(receipt, journal, record)

    def _rollback(self, receipt, journal, authorization=None):
        if journal["phase"] in {"prepared", "discarded"}:
            # Apply persists 'stopping' before its first runtime side effect.
            # A never-applied preparation needs no restoration or live proofs:
            # discard only its journal claim, even when sealed inputs drifted.
            # Keep receipt/staging bytes and any older live stamp ancestry.
            if journal["phase"] == "prepared":
                self.phase(journal, "discarded")
            return {"coordinated_receipt": receipt["receipt_path"], "receipt_sha256": receipt["receipt_sha256"],
                    "phase": "discarded", "outcome": "discarded", "scope": "preparation_only",
                    "full_fleet_acceptance": False, "runtime_mutated": False}
        if journal["phase"] == "rolled_back":
            self.adapter.validate_restored(receipt, journal)
            return self.result(receipt, journal, explicit_rollback=True)
        self.adapter.validate_recovery(receipt, journal)
        self.recover(receipt, journal, automatic=False, authorization=authorization)
        return self.result(receipt, journal, explicit_rollback=True)

    def recover(self, receipt, journal, *, automatic, authorization=None):
        admitted = False
        result = None
        try:
            # Validate readbacks/preimages before stopping an unrelated process.
            self.adapter.validate_recovery(receipt, journal)
            admitted = True
            self.adapter.verified_prior = None
            if authorization is not None:
                journal["recovery_authorization"] = authorization
            self.phase(journal, "rolling_back")
            self.adapter.stop(receipt, provenance=journal.get("stop_provenance"))
            self.adapter.revalidate_stopped(receipt, rollback=True)
            self.adapter.switch(receipt, rollback=True)
            try:
                result = self.adapter.start_verify(receipt, rollback=True)
            except Exception as exc:
                partial = getattr(exc, "partial_result", None)
                result = copy.deepcopy(partial) if isinstance(partial, dict) else None
                raise
            if result.get("outcome") != "passed":
                raise Invalid("prior serving verification failed")
            try:
                self.phase(journal, "rolled_back", rollback_result=result,
                           restored=self.adapter.current(receipt), rollback_failure=None)
            except OSError as journal_exc:
                journal["journal_failure"] = type(journal_exc).__name__
                journal.pop("restored", None)
                if journal.get("history", [])[-1:] == ["rolled_back"]:
                    journal["history"].pop()  # never persisted
                raise
        except Exception as exc:
            journal["rollback_failure"] = {"type": type(exc).__name__, "reason": str(exc)}
            identity = getattr(self.adapter, "verified_prior", None) if admitted else None
            if identity:
                # The exact prior booted and identified itself; only proof or
                # evidence failed. Leave it serving unless it is no longer the
                # process we observed, and then signal nothing at all.
                try:
                    running = self.adapter.prior_still_running(receipt, identity)
                except Exception as check_exc:
                    running = False
                    journal["prior_identity_check_failure"] = type(check_exc).__name__
                if running:
                    journal["prior_running"] = copy.deepcopy(identity)
                    journal["rollback_result"] = {**(result or {}), "outcome": "failed",
                                                  "prior_identity": copy.deepcopy(identity)}
                else:
                    journal["prior_identity_mismatch"] = copy.deepcopy(identity)
            elif admitted:
                try:
                    self.adapter.keep_stopped(receipt)
                except Exception as stop_exc:
                    journal["stop_failure"] = type(stop_exc).__name__
            try:
                self.phase(journal, "recovery_required")
            except Exception as journal_exc:
                journal["phase"] = "recovery_required"
                journal["journal_failure"] = type(journal_exc).__name__

    @staticmethod
    def result(receipt, journal, *, explicit_rollback=False):
        scoped = bool(receipt["intent"]["excluded_hosts"])
        active = journal["phase"] == "committed"
        checks_passed = journal.get("result", {}).get("outcome") == "passed"
        success = (journal["phase"] == "rolled_back" if explicit_rollback else active and checks_passed)
        disposition = ("active and checks passed" if checks_passed else "active with failed checks") if active else (
            "target not active, prior restored" if journal["phase"] == "rolled_back" else "target not active, restore unproven")
        if explicit_rollback and success:
            disposition = "active and checks passed"
        return {"coordinated_receipt": receipt["receipt_path"], "receipt_sha256": receipt["receipt_sha256"],
                "phase": journal["phase"], "outcome": "passed" if success else "failed", "disposition": disposition,
                "scope": "declared_optional_exclusions" if scoped else "full_matrix",
                "full_fleet_acceptance": success and not scoped and not explicit_rollback,
                "verification_kind": "restoration" if explicit_rollback else "model_canary",
                "forward_failure": journal.get("forward_failure"), "rollback_failure": journal.get("rollback_failure"),
                "journal_failure": journal.get("journal_failure"),
                "verification": journal.get("rollback_result" if explicit_rollback else "result")}
