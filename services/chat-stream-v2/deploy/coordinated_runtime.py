"""Concrete deployer adapter. All mutable-runtime dependencies are data only."""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import tarfile
import time

from deploy.coordinated import (Invalid, HelperProofFailure, absolute, atomic_bytes, atomic_json, read_json, canonical, digest,
    file_digest, host_scope, private_file)
from provider_cli_artifact import code_identity, validate_proof
from provider_accounts import AccountError, Binding
from account_store import AccountStore
from account_repin_state import causes, generation_intents
from provider_cli_maintenance import PREFIX as MAINTENANCE_PREFIX, status as maintenance_status
from provider_cli_pin import current as current_pin


SERVICE = "services/chat-stream-v2"
HELPER = SERVICE + "/provider_account_host.py"
RUNNER = SERVICE + "/tools/spawn_fleet_smoke.py"
# Three times the observed registry-lock hold maximum fits within this budget.
# Shared by all helper proofs in one controller invocation; never reset per cell.
HELPER_BUSY_DEADLINE_SECONDS = 15.0
HELPER_BUSY_BACKOFF_SECONDS = 0.25


def _diagnostic_tail(value, limit=1000):
    """Redact complete output before truncation, including byte partials."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    value = str(value or "")
    value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)",
                   "[redacted]", value, flags=re.S)
    value = re.sub(r"(?i)(https?://)[^\s/@]+@", r"\1[redacted]@", value)
    value = re.sub(r"(?im)^.*(?:authorization|bearer|token|api[_-]?key|password|secret|cookie|credential)\s*[:=]?.*$",
                   "[redacted]", value)
    value = re.sub(r"[A-Za-z0-9_+/=-]{20,}", lambda m: "[redacted]" if (
        any(c.isupper() for c in m[0]) and any(c.islower() for c in m[0]) and any(c.isdigit() for c in m[0])) else m[0], value)
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", value)
    return value[-limit:]


def _helper_child_failure(exc):
    command = exc.cmd
    # Never expose the shell command, pin input or remaining private argv.
    argv0 = command[0] if isinstance(command, (list, tuple)) and command else "unknown"
    detail = {"argv0": _diagnostic_tail(Path(str(argv0)).name, 160),
              "stdout_tail": _diagnostic_tail(exc.stdout), "stderr_tail": _diagnostic_tail(exc.stderr)}
    if getattr(exc, "helper_wait", None):
        detail["wait"] = exc.helper_wait
    status = "rc=" + str(exc.returncode) if isinstance(exc, subprocess.CalledProcessError) else "timeout=" + str(exc.timeout)
    return "helper closure/artifact verification failed: " + type(exc).__name__ + " " + status + " " + json.dumps(detail, ensure_ascii=True)


def helper_release_root(script):
    script = Path(script)
    if not script.is_absolute() or not str(script).endswith("/" + HELPER):
        raise Invalid("installed helper source path is unsupported")
    source = script.parents[2]
    if source.name == "app" and re.fullmatch(r"[0-9a-f]{40}", source.parent.name):
        if source.parent.parent.name != ".pentacle-account-helpers":
            raise Invalid("helper source must use a runtime checkout or the managed helper release root")
        return source.parent.parent
    if ".pentacle-account-helpers" in source.parts:
        raise Invalid("malformed managed helper source path")
    return source.parent / ".pentacle-account-helpers"


def code_identity_at(repo, sha, runner):
    """Use the actual identity implementation over isolated Git source bytes.

    Its source manifest is read from the target, not copied into this deployer.
    Refuse a changed algorithm until the controller supports that contract.
    """
    def blob(name):
        result = runner(("git", "show", f"{sha}:{name}"), Path(repo))
        if result.returncode:
            raise Invalid("candidate identity source is unavailable")
        return result.stdout
    source = blob(SERVICE + "/provider_cli_artifact.py")
    tree = ast.parse(source)
    function = next((node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "code_identity"), None)
    if function is None:
        raise Invalid("candidate code_identity implementation is missing")
    installed = ast.parse(Path(code_identity.__code__.co_filename).read_text())
    supported = next(node for node in installed.body if isinstance(node, ast.FunctionDef) and node.name == "code_identity")
    if ast.dump(function) != ast.dump(supported):
        raise Invalid("candidate code_identity algorithm requires a compatible controller")
    # Execute the unchanged real function with a read-only Git-backed Path.
    class SourcePath:
        def __init__(self, name): self.name = name
        @property
        def parent(self): return SourcePath(str(Path(self.name).parent))
        def __truediv__(self, name): return SourcePath(str(Path(self.name) / name))
        def read_bytes(self): return blob(self.name).encode()
    from types import FunctionType
    actual = FunctionType(code_identity.__code__, {**code_identity.__globals__,
        "__file__": SERVICE + "/provider_cli_artifact.py", "Path": SourcePath})
    return actual()


def load_installer(controller):
    path = controller / "services/agent-orch/deploy/install_fleet_spawn_tooling.py"
    spec = importlib.util.spec_from_file_location("coordinated_source_installer", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Runtime:
    def __init__(self, deployer, service, repo, runner, *, stream, boot_wait_seconds, log_guard_dir=None):
        self.d, self.service, self.repo, self.runner = deployer, service, Path(repo).resolve(), runner
        self.stream, self.boot_wait_seconds, self.log_guard_dir = stream, boot_wait_seconds, log_guard_dir
        self.controller = Path(__file__).resolve().parents[3]
        self.installer = load_installer(self.controller)
        self.stopped = False
        self.recovery_authorization = self.verified_prior = None

    def checked(self, argv, cwd=None):
        result = self.runner(tuple(str(a) for a in argv), cwd or self.repo)
        if result.returncode:
            # Do not include private commands/configuration in error receipts.
            raise Invalid("coordinated command failed: " + str(argv[0]) + " rc=" + str(result.returncode))
        return result

    def git(self, *args):
        return self.d._git(self.repo, *args, runner=self.runner)

    def database(self, plist=None):
        plist = plist or self.d._launchd_plist(self.service.launchd_label)
        arguments = plist.get("ProgramArguments", [])
        if "--db" not in arguments or arguments.index("--db") + 1 >= len(arguments):
            raise Invalid("coordinated deploy needs the installed daemon --db path")
        return absolute(arguments[arguments.index("--db") + 1])

    def account_snapshot(self, policy, database, *, accounts=None, scope=None):
        with sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            ledger = AccountStore(conn, policy, initialize=False)
            pin = current_pin(ledger)
            bindings = [json.loads(row[0]) for row in conn.execute(
                "SELECT data FROM v2_account_bindings WHERE active=1 ORDER BY binding_key")]
            defaults = [list(row) for row in conn.execute("SELECT host,provider,alias,revision FROM v2_account_defaults ORDER BY host,provider")]
            selected = [raw for raw in bindings if any((raw["host"], raw["provider"], raw["alias"]) == tuple(row[:3]) for row in defaults)]
            if accounts:
                if policy.get('enabled') is not True:
                    raise Invalid('explicit smoke accounts require enabled account policy')
                from deploy.coordinated import smoke_account_map
                choices = smoke_account_map(accounts, scope)
                selected = []
                for (host, provider), alias in choices.items():
                    try:
                        binding = ledger._available(ledger._cell(host, provider, alias))
                    except AccountError as exc:
                        raise Invalid('selected smoke account unavailable: ' + str(exc)) from exc
                    selected.append(next(raw for raw in bindings if raw['host'] == host
                        and raw['provider'] == provider and raw['alias'] == alias
                        and raw['generation'] == binding.generation))
            cells = {tuple(row) for row in conn.execute("SELECT DISTINCT host,provider,alias FROM v2_account_bindings")}
            # Attempts can precede enrollment. Include their addressed cells as
            # well as registered bindings; historical row states are not fences.
            for row in conn.execute("SELECT data FROM v2_account_recovery"):
                record = json.loads(row[0])
                if not isinstance(record, dict) or not isinstance(record.get("arguments", {}), dict):
                    raise Invalid("invalid account recovery arguments")
                arguments = record.get("arguments", {})
                if {"host", "provider", "account"} <= arguments.keys():
                    cells.add(tuple(arguments[key] for key in ("host", "provider", "account")))
            hosts = {cell[0] for cell in cells}
            hosts.update(row[0] for row in conn.execute("SELECT host FROM v2_spawn_admission_hold"))
            hosts.update(row[0][len(MAINTENANCE_PREFIX):] for row in conn.execute(
                "SELECT k FROM kv WHERE substr(k,1,?)=?", (len(MAINTENANCE_PREFIX), MAINTENANCE_PREFIX)))
            try:
                for host, provider, account in cells:
                    cell = {"host": host, "provider": provider, "account": account}
                    if causes(ledger, cell) or generation_intents(ledger, cell):
                        raise Invalid("in-flight account mutation/recovery blocks deployment")
                for host in hosts:
                    maintenance = maintenance_status(ledger, host)
                    hold = maintenance["hold"]
                    if maintenance["slot"] is not None or (hold and hold["held_until"] > time.time()):
                        raise Invalid("active provider maintenance blocks deployment")
            except AccountError as exc:
                raise Invalid("account admission state invalid: " + str(exc)) from exc
            return {"pin": pin, "pin_digest": digest(pin), "bindings": bindings,
                    "bindings_digest": digest(bindings), "defaults": defaults,
                    "selected_bindings": selected, "database": str(database)}

    def preimage(self, path, folder, name):
        path = absolute(str(path))
        if path.exists():
            info = path.lstat()
            if not path.is_file() or info.st_uid != os.getuid():
                raise Invalid("invalid preimage owner/type")
            data = path.read_bytes()
            saved = folder / name
            atomic_bytes(saved, data)
            return {"path": str(path), "saved": str(saved), "sha256": file_digest(saved),
                    "mode": info.st_mode & 0o777, "uid": info.st_uid}
        return {"path": str(path), "saved": None, "sha256": None, "mode": None, "uid": os.getuid()}

    def installed_prior_bundle(self, sha, bundle, policy, scope, qualification):
        """Preserve the qualified immutable prior instead of restamping it."""
        host = scope["local_hosts"][0]
        target = self.installer.Target(host, "localhost",
            str(helper_release_root(policy["executors"][host]["script"])), True)
        release = Path(target.root) / sha
        if not release.exists() and not release.is_symlink():
            return None
        manifest = json.loads((release / "manifest.json").read_bytes())
        if not isinstance(manifest, dict) or manifest.get("commit") != sha:
            raise Invalid("installed prior manifest source mismatch")
        self.installer.verify_helper_source(target, sha, manifest)
        if qualification.get("receipt"):
            accepted = read_json(qualification["receipt"])
            if accepted.get("source", {}).get("target", {}).get("manifest") != manifest:
                raise Invalid("installed prior differs from qualified manifest")
        for name in ("pentacle.tar", "manifest.json"):
            atomic_bytes(bundle / name, (release / name).read_bytes())
        if (file_digest(bundle / "pentacle.tar") != manifest["archive_sha256"]
                or read_json(bundle / "manifest.json") != manifest):
            raise Invalid("installed prior archive changed during capture")
        # Verify the entire source against the accepted Git SHA, independently
        # of the installed manifest. Private overlay/dependency payloads remain
        # hash-bound by the original archive and are never rewritten.
        git_archive = bundle / "git-source.tar"
        self.checked(("git", "archive", "--format=tar", "--output", str(git_archive), sha))
        with tarfile.open(git_archive) as expected, tarfile.open(bundle / "pentacle.tar") as actual:
            source_files = {m.name: expected.extractfile(m).read() for m in expected if m.isfile()}
            installed_files = {m.name: actual.extractfile(m).read() for m in actual if m.isfile()}
        if (any(installed_files.get(name) != data for name, data in source_files.items())
                or any(name not in source_files and not name.startswith("runtime-deps/")
                       and name != "services/_shared/spawn_defaults.local.json" for name in installed_files)):
            raise Invalid("installed prior archive differs from Git source")
        self.installer.verify_helper_source(target, sha, manifest)
        return manifest

    def prepare(self, intent, folder):
        self.d.verify_release_checkout(self.repo, self.runner)
        if self.repo == self.controller or self.controller.is_relative_to(self.repo):
            raise Invalid("controller must be separate from mutable daemon checkout")
        if self.d._git(self.controller, "rev-parse", "HEAD", runner=self.runner) != intent["controller_sha"]:
            raise Invalid("controller SHA differs from intent")
        if self.d._dirty_entries(self.controller, self.runner):
            raise Invalid("controller must be a frozen clean tooling checkout")
        if self.git("rev-parse", "HEAD") != intent["prior_sha"]:
            raise Invalid("prior daemon SHA differs from intent")
        for sha in (intent["target_sha"], intent["prior_sha"]):
            self.d._verify_origin_reachable(self.repo, sha, self.runner)
        origin = self.git("remote", "get-url", "origin")
        if origin != self.d._git(self.controller, "remote", "get-url", "origin", runner=self.runner):
            raise Invalid("controller and daemon source origins differ")
        plist = self.d._launchd_plist(self.service.launchd_label)
        env = plist.get("EnvironmentVariables", {})
        if env.get("PENTACLE_MACHINES_FILE") != intent["machines_file"] or env.get("PENTACLE_PROVIDER_ACCOUNT_POLICY") != intent["policy_file"]:
            raise Invalid("intent must name the installed machine and policy paths")
        policy = json.loads(private_file(intent["policy_file"]).read_bytes())
        scope = host_scope(intent["machines_file"], intent["excluded_hosts"])
        accounts = self.account_snapshot(policy, self.database(plist),
            accounts=intent.get('smoke_accounts'), scope=scope)
        from deploy.restoration import qualified_prior
        prior_qualification = qualified_prior(self.d._read_stamp(self.repo, self.service),
            intent["prior_sha"], [b for b in accounts["selected_bindings"] if b["host"] in scope["included_hosts"]])
        arguments = plist.get("ProgramArguments", [])
        self.d._validate_v2_launchd_program(self.repo, arguments)
        # Match packet/activation-evidence.py's optional unbuffered prefix.
        main_index = 2 if len(arguments) > 1 and arguments[1] == "-u" else 1
        if len(arguments) <= main_index or arguments[main_index] != str(self.repo / SERVICE / "main.py"):
            raise Invalid("installed daemon must execute the target checkout main.py")
        self.d.verify_log_guard(self.log_guard_dir or self.service.log_guard_dir)
        if not self.d._daemon_log_paths(self.service):
            raise Invalid("daemon log paths are not configured")
        evidence = {}
        for direction, key in (("target", "gate_evidence"), ("prior", "prior_gate_evidence")):
            sha = intent[direction + "_sha"]
            self.d._load_gate_evidence(Path(intent[key]), sha)
            evidence[direction] = {"path": intent[key], "sha256": file_digest(intent[key])}
        machine_copy = folder / "machines.json"
        atomic_bytes(machine_copy, Path(intent["machines_file"]).read_bytes())
        preimages = {"policy": self.preimage(Path(intent["policy_file"]), folder, "policy.before"),
            "plist": self.preimage(self.d._launchd_plist_path(self.service.launchd_label), folder, "daemon.before"),
            "stamp": self.preimage(self.d._stamp_path(self.repo, self.service), folder, "stamp.before"),
            "collector": self.preimage(self.d._usage_collector_plist_path(), folder, "collector.before")}
        # Schedule is never installed for a declared exclusion. Preserve it for
        # drift detection even on a full-matrix coordinated transaction.
        schedule = self.d._launchd_plist_path("com.pentacle.spawn-fleet-smoke")
        preimages["schedule"] = self.preimage(schedule, folder, "schedule.before")
        settings_path = self.repo / "services/_shared/spawn_defaults.local.json"
        preimages["test_settings"] = self.preimage(settings_path, folder, "test-settings.before")
        overlay = settings_path.read_bytes() if settings_path.exists() else None
        source = {}
        hosts = {row["name"]: row for row in scope["machines"]}
        for direction, sha in (("target", intent["target_sha"]), ("prior", intent["prior_sha"])):
            bundle = folder / (direction + "-bundle")
            bundle.mkdir(mode=0o700)
            manifest = (self.installed_prior_bundle(sha, bundle, policy, scope, prior_qualification)
                        if direction == "prior" else None)
            if manifest is None:
                manifest = self.installer._archive(self.repo, sha, bundle, provider_cli_pin=accounts["pin"], spawn_settings=overlay)
            snapshot = folder / (direction + "-source")
            snapshot.mkdir(mode=0o700)
            with tarfile.open(bundle / "pentacle.tar") as archive:
                archive.extractall(snapshot, filter="data")
            # Only forward inference needs a sealed profile or runner API.
            if direction == "target":
                profile_plan = self.smoke_plan(snapshot, scope, machine_copy,
                                               accounts=intent.get('smoke_accounts', []))
                profile_path = folder / "target-test-profile.json"
                atomic_json(profile_path, profile_plan["test_profile"])
                sealed_plan = self.smoke_plan(snapshot, scope, machine_copy,
                    accounts=intent.get('smoke_accounts', []), test_profile=profile_path)
                if profile_plan != sealed_plan:
                    raise Invalid("runner cannot honor sealed test profile")
            identity = code_identity_at(self.repo, sha, self.runner)
            if file_digest(snapshot / self.service.requirements_path) != file_digest(self.repo / self.service.requirements_path):
                raise Invalid("dependency changes require a separately provisioned compatible interpreter")
            candidate_plist = copy.deepcopy(plist)
            candidate_plist.setdefault("EnvironmentVariables", {}).update(dict(self.d._launchd_environment_for_release(self.repo, self.service)))
            # Render and validate inertly; no installed plist/collector changes.
            plistlib.loads(plistlib.dumps(candidate_plist))
            for template in (self.d.USAGE_COLLECTOR_TEMPLATE, self.d.SPAWN_FLEET_SMOKE_TEMPLATE):
                data = (snapshot / template).read_bytes().replace(self.d.RELEASE_CHECKOUT_TOKEN, str(self.repo).encode())
                plistlib.loads(data)
            helpers = {}
            for host in scope["included_hosts"]:
                executor = policy.get("executors", {}).get(host)
                if not isinstance(executor, dict) or not {"script", "python"} <= set(executor):
                    raise Invalid("included executor is missing")
                script = Path(executor["script"])
                helper_release_root(script)
                python = Path(executor["python"])
                if not python.is_absolute():
                    raise Invalid("configured interpreter must be absolute")
                root = str(helper_release_root(script))
                target = self.installer.Target(host, hosts[host].get("ssh_target") or "localhost", root,
                                               hosts[host].get("ssh_target") is None)
                release = self.installer.stage_helper_source(target, bundle, sha, manifest)
                helper = {"target": vars(target), "python": str(python), "script": release + "/app/" + HELPER}
                helper["proof"] = self.helper_proof(helper, accounts, identity, manifest, sha, test_profile=direction == "target")
                if direction == "target" and helper["proof"]["closure"].get("test_profile") != profile_plan["test_profile"]:
                    raise Invalid("staged native helper test settings mismatch")
                helpers[host] = helper
            source[direction] = {"sha": sha, "snapshot": str(snapshot), "manifest": manifest,
                "code_identity": identity, "helpers": helpers}
            if direction == "target":
                source[direction].update(runner_sha256=file_digest(snapshot / RUNNER),
                    test_profile={"path": str(profile_path), "sha256": file_digest(profile_path)},
                    smoke_plan=sealed_plan)
            daemon_helper = {**helpers[scope["local_hosts"][0]], "python": str(self.d._venv_python(self.repo, self.service))}
            source[direction]["daemon_interpreter"] = {"helper": daemon_helper,
                "proof": self.helper_proof(daemon_helper, accounts, identity, manifest, sha, closure_only=True, test_profile=direction == "target")}
            if direction == "prior":
                originals = {}
                for host, staged in helpers.items():
                    original = {"target": staged["target"], **policy["executors"][host]}
                    original["proof"] = self.helper_proof(original, accounts, identity, manifest, sha, verify_release=False)
                    originals[host] = original
                source[direction]["original_helpers"] = originals
        post_policy = copy.deepcopy(policy)
        for host, helper in source["target"]["helpers"].items():
            post_policy["executors"][host]["script"] = helper["script"]
        post_policy_bytes = json.dumps(post_policy, indent=2).encode() + b"\n"
        post_policy_path = folder / "policy.after"
        atomic_bytes(post_policy_path, post_policy_bytes)
        post_plist_path = folder / "daemon.after"
        atomic_bytes(post_plist_path, plistlib.dumps(candidate_plist))
        prepared = {"prior_qualification": prior_qualification, "scope": scope, "origin": origin, "machines_snapshot": str(machine_copy), "machines_sha256": file_digest(machine_copy),
                "accounts": accounts, "source": source, "gates": evidence, "preimages": preimages,
                "post_policy": {"path": str(post_policy_path), "sha256": file_digest(post_policy_path)},
                "post_plist": {"path": str(post_plist_path), "sha256": file_digest(post_plist_path)}}
        if intent.get('smoke_accounts') and not scope['excluded_hosts']:
            path = folder/'schedule.after'
            atomic_bytes(path, self.schedule_definition(source['target'], intent['smoke_accounts'], intent['machines_file']))
            prepared['post_schedule'] = {'path': str(path), 'sha256': file_digest(path)}
        return prepared

    def schedule_definition(self, source, accounts, machines=None):
        from xml.sax.saxutils import escape
        snapshot = Path(source['snapshot'])
        value = plistlib.loads(self.d._render_v2_spawn_fleet_smoke_plist(snapshot, accounts=accounts).replace(
            escape(str(snapshot)).encode(), escape(str(self.repo)).encode()))
        if machines is not None:
            value['EnvironmentVariables']['PENTACLE_MACHINES_FILE'] = str(machines)
        return plistlib.dumps(value)

    def remote_json(self, helper, argv):
        target = self.installer.Target(**helper["target"])
        attempts, last_busy = 0, None
        budget = getattr(self, "_helper_busy_budget", None)

        def record(outcome):
            row = {"host": target.name, "attempt_count": attempts,
                   "elapsed_seconds": round(time.monotonic() - budget["started"], 6),
                   "deadline_seconds": HELPER_BUSY_DEADLINE_SECONDS,
                   "wait_reason": "account_busy", "last_outcome": outcome}
            if not hasattr(self, "helper_wait_events"):
                self.helper_wait_events = []
            self.helper_wait_events.append(row)
            observer = getattr(self, "helper_wait_observer", None)
            if observer:
                observer(row)
            return row

        def bounded(argv, **kwargs):
            timeout = 210 if last_busy is None else max(0.001, budget["deadline"] - time.monotonic())
            return subprocess.run(argv, timeout=timeout, **kwargs)

        while True:
            if last_busy is not None and time.monotonic() >= budget["deadline"]:
                last_busy.helper_wait = record("account_busy")
                raise last_busy
            attempts += 1
            try:
                output = self.installer._remote(target, shlex.join([str(a) for a in argv]), dry_run=False, runner=bounded)
                if last_busy is not None and time.monotonic() >= budget["deadline"]:
                    last_busy.helper_wait = record("account_busy")
                    raise last_busy
                break
            except subprocess.CalledProcessError as exc:
                try:
                    busy = (len(argv) > 2 and argv[2] == "verify-artifact" and exc.returncode == 1
                            and json.loads(exc.stdout, object_pairs_hook=lambda pairs: pairs) == [("outcome", "account_busy")])
                except (ValueError, TypeError):
                    busy = False
                if not busy:
                    if last_busy is not None:
                        exc.helper_wait = record("child_failed")
                    raise
                if budget is None:
                    started = time.monotonic()
                    budget = self._helper_busy_budget = {"started": started, "deadline": started + HELPER_BUSY_DEADLINE_SECONDS}
                last_busy = exc
                exc.helper_wait = record("account_busy")
                time.sleep(min(HELPER_BUSY_BACKOFF_SECONDS, max(0, budget["deadline"] - time.monotonic())))
            except subprocess.TimeoutExpired as exc:
                if last_busy is not None:
                    exc.helper_wait = record("child_timeout")
                raise
        try:
            value = json.loads(output)
            if not isinstance(value, dict): raise ValueError()
            if last_busy is not None:
                record("ok" if value.get("outcome") == "ok" else "response_other")
            return value
        except ValueError as exc:
            if last_busy is not None:
                record("response_invalid")
            raise Invalid("helper proof response invalid") from exc

    def helper_proof(self, helper, accounts, identity, manifest, sha, *, verify_release=True, closure_only=False, test_profile=False):
        try:
            return self._helper_proof(helper, accounts, identity, manifest, sha,
                                      verify_release=verify_release, closure_only=closure_only, test_profile=test_profile)
        except Invalid:
            raise
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise HelperProofFailure(_helper_child_failure(exc)) from exc
        except Exception as exc:
            raise Invalid("helper closure/artifact verification failed: " + type(exc).__name__) from exc

    def _helper_proof(self, helper, accounts, identity, manifest, sha, *, verify_release=True, closure_only=False, test_profile=False):
        target = self.installer.Target(**helper["target"])
        if verify_release:
            self.installer.verify_helper_source(target, sha, manifest)
        app = str(Path(helper["script"]).parents[2])
        program = r'''
import hashlib,importlib.metadata,json,pathlib,site,sys
app=pathlib.Path(sys.argv[1]); sys.path[:0]=[str(app/'services/chat-stream-v2'),str(app/'services'),str(app/'services/agent-orch')]
import provider_account_host,account_executor,provider_cli_artifact,account_runtime,main
profile={}
if sys.argv[2] == 'forward':
    from _shared.spawn_profiles import resolve_test_profile
    profile['test_profile']=resolve_test_profile()
origins={}; dependencies={}
for name,module in list(sys.modules.items()):
    value=getattr(module,'__file__',None)
    if value and (name.split('.')[0] in {'_shared','agent_orch'} or pathlib.Path(value).parent.name=='chat-stream-v2'):
        path=pathlib.Path(value).resolve(); assert path.is_relative_to(app), 'helper_import_source_mismatch'
        origins[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    elif value:
        path=pathlib.Path(value).resolve()
        if path.is_file() and any(path.is_relative_to(pathlib.Path(root).resolve()) for root in site.getsitepackages()):
            dependencies[name]={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
exe=pathlib.Path(sys.executable).resolve()
print(json.dumps(dict(profile,identity=provider_cli_artifact.code_identity(),python=sys.executable,
 real_python=str(exe),python_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),
 version=sys.version,modules=origins,dependency_files=dependencies,
 dependencies=sorted((d.metadata['Name'],d.version) for d in importlib.metadata.distributions()))))
'''
        closure = self.remote_json(helper, [helper["python"], "-I", "-c", program, app, "forward" if test_profile else "closure"])
        if closure.get("identity") != identity or closure.get("python") != helper["python"]:
            raise Invalid("staged helper identity/interpreter mismatch")
        if closure_only:
            return closure
        proofs = []
        for raw in accounts["selected_bindings"]:
            binding = Binding(**raw)
            if binding.host != target.name or not binding.enabled:
                continue
            proof = self.remote_json(helper, [helper["python"], helper["script"], "verify-artifact",
                "--provider", binding.provider, "--account", binding.alias, "--login-generation", binding.generation,
                "--operation-id", digest({"sha": sha, "binding": binding.key}), "--pin-json", canonical(accounts["pin"]).decode()])
            validate_proof(binding, accounts["pin"], proof, consumer_identity=identity)
            proofs.append(proof)
        if {Binding(**r).provider for r in accounts["selected_bindings"] if r["host"] == target.name and r["enabled"]} != {"claude", "codex"}:
            raise Invalid("included host lacks both provider bindings")
        return {"closure": closure, "artifacts": proofs}

    def smoke_argv(self, snapshot, scope, machines, *, accounts=(), dry_run=False, test_profile=None):
        return ("/usr/bin/env", "-u", "PENTACLE_MACHINES_JSON", "-u", "PENTACLE_SMOKE_HOSTS",
                "-u", "PENTACLE_SMOKE_EXCLUDED_HOSTS", "-u", "PENTACLE_SMOKE_ACCOUNTS", "PENTACLE_MACHINES_FILE=" + str(machines),
                "PENTACLE_SMOKE_EXCLUDED_HOSTS=" + ",".join(r["host"] for r in scope["excluded_hosts"]),
                str(self.d._venv_python(self.repo, self.service)), str(Path(snapshot) / RUNNER),
                *(arg for entry in accounts for arg in ('--account', entry)),
                *(("--test-profile-snapshot", str(test_profile)) if test_profile else ()),
                *(("--dry-run",) if dry_run else ()))

    def smoke_plan(self, snapshot, scope, machines, *, accounts=(), test_profile=None):
        from deploy.coordinated import smoke_account_map
        choices = smoke_account_map(list(accounts), scope)
        result = self.checked(self.smoke_argv(snapshot, scope, machines, accounts=accounts, dry_run=True, test_profile=test_profile), Path(snapshot))
        plan = json.loads(result.stdout)
        if (plan.get("source") != str(machines) or list(plan.get("hosts", [])) != scope["included_hosts"]
                or list(plan.get("optional_hosts", [])) != scope["optional_hosts"]
                or list(plan.get("excluded_present", [])) != sorted(r["host"] for r in scope["excluded_hosts"])
                or plan.get("excluded_absent")):
            raise Invalid("target smoke runner scope disagreement")
        expected = [{"host": h, "provider": p, "prompt_mode": m} for h in scope["included_hosts"] for p in ("claude", "codex") for m in ("prompted", "promptless")]
        rows = plan.get('cells')
        if (not isinstance(rows, list) or [{k: r.get(k) for k in ('host', 'provider', 'prompt_mode')}
                for r in rows] != expected):
            raise Invalid("target smoke runner cell-plan disagreement")
        if choices and any(r.get('account') != choices[(r['host'], r['provider'])]
                           or r.get('account_selection') != 'explicit' for r in rows):
            raise Invalid('smoke runner cannot honor explicit accounts')
        from _shared.spawn_profiles import validate_test_profile, test_prompt_tuple
        try:
            profile = validate_test_profile(plan.get("test_profile"))
            if any((r.get("model"), r.get("effort")) != test_prompt_tuple(r["provider"], profile)
                   or r.get("profile_sha256") != profile["profile_sha256"] for r in rows):
                raise ValueError("cell tuple disagreement")
        except ValueError as exc:
            raise Invalid("runner lacks compatible test profile: " + str(exc)) from exc
        return plan

    def current(self, receipt):
        settings = receipt["preimages"]["test_settings"]
        settings_path = Path(settings["path"])
        if settings_path.exists() or settings_path.is_symlink():
            info = settings_path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != settings["uid"]
                    or settings["mode"] is not None and stat.S_IMODE(info.st_mode) != settings["mode"]):
                raise Invalid("test settings owner/type/permission drift")
        paths = {key: value["path"] for key, value in receipt["preimages"].items()}
        return {"sha": self.git("rev-parse", "HEAD"),
                "files": {key: file_digest(path) if Path(path).exists() else None for key, path in paths.items()}}

    def check_preimages(self, receipt):
        for row in receipt["preimages"].values():
            if row["saved"] and (file_digest(private_file(row["saved"])) != row["sha256"] or row["uid"] != os.getuid()):
                raise Invalid("preimage drift")
        for key in ("post_policy", "post_plist", *(("post_schedule",) if 'post_schedule' in receipt else ())):
            if file_digest(private_file(receipt[key]["path"])) != receipt[key]["sha256"]:
                raise Invalid("sealed postimage drift")

    def controller_admitted(self, receipt, *, forward):
        head = self.d._git(self.controller, "rev-parse", "HEAD", runner=self.runner)
        pinned = receipt["intent"]["controller_sha"]
        auth = self.recovery_authorization
        if auth is None:
            return head == pinned
        # An authorization in force binds exactly the FD-named replacement, and
        # only for recovery of this sealed receipt; forward stays pinned.
        return bool(not forward and auth["original_controller_sha"] == pinned
                    and head == auth["replacement_controller_sha"]
                    and self.d._git(self.controller, "remote", "get-url", "origin", runner=self.runner) == receipt["origin"])

    def validate_common(self, receipt, *, forward=True):
        if not self.controller_admitted(receipt, forward=forward) or self.d._dirty_entries(self.controller, self.runner):
            raise Invalid("controller source drift")
        if self.git("remote", "get-url", "origin") != receipt["origin"]:
            raise Invalid("daemon source origin drift")
        folder = Path(receipt["transaction_directory"])
        expected_preimages = {
            "policy": (receipt["intent"]["policy_file"], "policy.before"),
            "plist": (str(self.d._launchd_plist_path(self.service.launchd_label)), "daemon.before"),
            "stamp": (str(self.d._stamp_path(self.repo, self.service)), "stamp.before"),
            "collector": (str(self.d._usage_collector_plist_path()), "collector.before"),
            "schedule": (str(self.d._launchd_plist_path("com.pentacle.spawn-fleet-smoke")), "schedule.before"),
            "test_settings": (str(self.repo / "services/_shared/spawn_defaults.local.json"), "test-settings.before"),
        }
        if set(receipt["preimages"]) != set(expected_preimages):
            raise Invalid("preimage population substitution")
        for key, (path, saved_name) in expected_preimages.items():
            row = receipt["preimages"][key]
            if (row["path"] != path or row["uid"] != os.getuid()
                    or row["saved"] != (str(folder / saved_name) if row["sha256"] is not None else None)):
                raise Invalid("preimage path substitution")
        for key, name in (("post_policy", "policy.after"), ("post_plist", "daemon.after")):
            if receipt[key]["path"] != str(folder / name):
                raise Invalid("postimage path substitution")
        scheduled = bool(receipt['intent'].get('smoke_accounts') and not receipt['scope']['excluded_hosts'])
        if scheduled != ('post_schedule' in receipt):
            raise Invalid('scheduled account intent substitution')
        if scheduled and (receipt['post_schedule']['path'] != str(folder/'schedule.after')
                or Path(receipt['post_schedule']['path']).read_bytes() != self.schedule_definition(
                    receipt['source']['target'], receipt['intent']['smoke_accounts'], receipt['intent']['machines_file'])):
            raise Invalid('scheduled account definition substitution')
        self.check_preimages(receipt)
        if (file_digest(receipt["intent"]["machines_file"]) != receipt["machines_sha256"]
                or file_digest(private_file(receipt["machines_snapshot"])) != receipt["machines_sha256"]):
            raise Invalid("machine catalog drift")
        scope = host_scope(receipt["intent"]["machines_file"], receipt["intent"]["excluded_hosts"])
        if scope != receipt["scope"]:
            raise Invalid("sealed scope substitution")
        original_policy = json.loads(Path(receipt["preimages"]["policy"]["saved"]).read_bytes())
        expected_policy = copy.deepcopy(original_policy)
        hosts = {row["name"]: row for row in scope["machines"]}
        for direction in ("target", "prior"):
            source = receipt["source"][direction]
            sha = receipt["intent"][direction + "_sha"]
            if (source["sha"] != sha or source["manifest"].get("commit") != sha
                    or source["snapshot"] != str(Path(receipt["transaction_directory"]) / (direction + "-source"))
                    or set(source["helpers"]) != set(scope["included_hosts"])
                    or source["code_identity"] != code_identity_at(self.repo, sha, self.runner)):
                raise Invalid("sealed source substitution")
            if direction == "target" and forward:
                profile = source.get("test_profile", {})
                if (profile.get("path") != str(folder / "target-test-profile.json")
                        or file_digest(private_file(profile["path"])) != profile.get("sha256")
                        or read_json(profile["path"]) != source["smoke_plan"]["test_profile"]):
                    raise Invalid("sealed test profile drift")
            for host, helper in source["helpers"].items():
                old = original_policy["executors"][host]
                root = str(helper_release_root(old["script"]))
                expected_target = {"name": host, "root": root, "ssh": hosts[host].get("ssh_target") or "localhost",
                                   "local": hosts[host].get("ssh_target") is None}
                if (helper["target"] != expected_target or helper["python"] != old["python"]
                        or helper["script"] != root + "/" + sha + "/app/" + HELPER):
                    raise Invalid("sealed helper path/interpreter/transport substitution")
                if direction == "target":
                    expected_policy["executors"][host]["script"] = helper["script"]
            daemon_helper = {**source["helpers"][scope["local_hosts"][0]],
                             "python": str(self.d._venv_python(self.repo, self.service))}
            if source["daemon_interpreter"]["helper"] != daemon_helper:
                raise Invalid("sealed daemon interpreter substitution")
        if json.loads(Path(receipt["post_policy"]["path"]).read_bytes()) != expected_policy:
            raise Invalid("coordinated policy may change only included executor script fields")
        expected_plist = plistlib.loads(Path(receipt["preimages"]["plist"]["saved"]).read_bytes())
        expected_plist.setdefault("EnvironmentVariables", {}).update(dict(self.d._launchd_environment_for_release(self.repo, self.service)))
        if plistlib.loads(Path(receipt["post_plist"]["path"]).read_bytes()) != expected_plist:
            raise Invalid("sealed launchd definition substitution")
        policy = json.loads(private_file(receipt["intent"]["policy_file"]).read_bytes())
        accounts = self.account_snapshot(policy, receipt["accounts"]["database"], accounts=receipt["intent"].get("smoke_accounts"), scope=receipt["scope"])
        if accounts != receipt["accounts"]:
            raise Invalid("provider pin/account generation drift")
        from deploy.restoration import qualified_prior
        original_stamp = receipt["preimages"]["stamp"]
        stamp = read_json(original_stamp["saved"]) if original_stamp["saved"] else {}
        if qualified_prior(stamp, receipt["intent"]["prior_sha"], [b for b in accounts["selected_bindings"]
                if b["host"] in scope["included_hosts"]]) != receipt["prior_qualification"]:
            raise Invalid("prior qualification substitution")
        for direction in ("target", "prior"):
            gate = receipt["gates"][direction]
            if file_digest(gate["path"]) != gate["sha256"]:
                raise Invalid("gate evidence drift")
            self.d._load_gate_evidence(Path(gate["path"]), receipt["source"][direction]["sha"])

    def guard(self, receipt, *, rollback=False):
        return self.d.verify_deploy_guard(self.service, self.stream, self.runner, repo=self.repo,
            sha=receipt["intent"]["prior_sha" if rollback else "target_sha"], rollback=rollback,
            rollback_from_sha=self.git("rev-parse", "HEAD"), sessions_db=Path(receipt["accounts"]["database"]))

    def revalidate(self, receipt, *, forward):
        self.validate_common(receipt, forward=forward)
        now = self.current(receipt)
        if now["sha"] != receipt["intent"]["prior_sha"] or any(now["files"][key] != value["sha256"] for key, value in receipt["preimages"].items()):
            raise Invalid("source/policy/plist/stamp drift after preparation")
        if self.d._dirty_entries(self.repo, self.runner):
            raise Invalid("release checkout drift")
        for direction in (("target", "prior") if forward else ("prior",)):
            source = receipt["source"][direction]
            for helper in source["helpers"].values():
                if self.helper_proof(helper, receipt["accounts"], source["code_identity"], source["manifest"], source["sha"], test_profile=direction == "target") != helper["proof"]:
                    raise Invalid("staged helper proof drift")
            daemon = source["daemon_interpreter"]
            if self.helper_proof(daemon["helper"], receipt["accounts"], source["code_identity"], source["manifest"], source["sha"], closure_only=True, test_profile=direction == "target") != daemon["proof"]:
                raise Invalid("daemon interpreter/dependency drift")
        prior = receipt["source"]["prior"]
        for helper in prior["original_helpers"].values():
            if self.helper_proof(helper, receipt["accounts"], prior["code_identity"], prior["manifest"], prior["sha"], verify_release=False) != helper["proof"]:
                raise Invalid("original helper proof drift before stop")
        if forward:
            source = receipt["source"]["target"]
            if self.smoke_plan(source["snapshot"], receipt["scope"], receipt["machines_snapshot"], accounts=receipt["intent"].get("smoke_accounts", []), test_profile=source["test_profile"]["path"]) != source["smoke_plan"]:
                raise Invalid("runner plan drift")
        self.guard(receipt, rollback=not forward)

    def process_rows(self):
        result = self.checked(("ps", "-ax", "-o", "pid=,ppid=,uid=,command="))
        rows = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 3)
            if (len(parts) != 4 or not all(re.fullmatch(r"[0-9]+", p) for p in parts[:2])
                    or not re.fullmatch(r"-?[0-9]+", parts[2])):
                raise Invalid("process provenance is unreadable")
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
        return rows

    def _process_absent(self, pid):
        # A census member can exit before its birth is read. None from
        # _process_start also covers tooling errors, so qualify absence anew.
        if type(pid) is not int or pid <= 0:
            return False
        result = self.runner(("env", "TZ=UTC0", "LC_ALL=C", "ps", "-p", str(pid), "-o", "lstart="), self.repo)
        return (result.returncode == 1 and not (result.stdout or "").strip()
                and not (result.stderr or "").strip())

    def provenance(self, receipt):
        state = self.d._launchd_state(self.service.launchd_label, self.runner)
        if not state.answered:
            raise Invalid("cannot journal daemon process provenance")
        rows = self.process_rows()
        owned = {state.pid} if state.pid else set()
        for _ in rows:
            owned.update(p for p, parent, uid, command in rows if parent in owned and uid == os.getuid())
        processes = []
        for pid, parent, uid, command in rows:
            if pid in owned or (uid == os.getuid() and "provider_account_host" in command and str(self.repo) in command):
                start = self.d._process_start(pid, self.runner)
                if not start:
                    if pid != state.pid and self._process_absent(pid):
                        continue
                    raise Invalid("helper process provenance is unproven")
                processes.append({"pid": pid, "start": start})
        if state.pid and not any(row["pid"] == state.pid for row in processes):
            raise Invalid("daemon PID absent from process census")
        return {"launchd_answered": True, "pid": state.pid, "processes": processes}

    def stop(self, receipt, provenance=None):
        state = self.d._launchd_state(self.service.launchd_label, self.runner)
        if not state.answered:
            raise Invalid("launchd state is unproven")
        pid = state.pid
        start = self.d._process_start(pid, self.runner) if pid else None
        if pid and not start:
            raise Invalid("old daemon process provenance is unproven")
        rows = self.process_rows()
        owned = {pid} if pid else set()
        for _ in rows:
            owned.update(p for p, parent, uid, command in rows if parent in owned and uid == os.getuid())
        tracked = {}
        for p, parent, uid, command in rows:
            if p in owned and p != pid:
                born = self.d._process_start(p, self.runner)
                if born:
                    tracked[p] = born
                elif not self._process_absent(p):
                    raise Invalid("helper process provenance is unproven")
        for row in (provenance or {}).get("processes", []):
            tracked[row["pid"]] = row["start"]
        self.old_pid, self.old_start = pid, start
        self.log_offsets = self.d._capture_log_offsets(self.d._daemon_log_paths(self.service))
        if state.loaded:
            self.checked(("launchctl", "bootout", self.d._launchctl_target(self.service.launchd_label)))
        final = self.d._wait_launchd_unloaded(self.service.launchd_label, self.runner, timeout_s=self.d.RELOAD_UNLOAD_WAIT_S)
        if not final.answered or final.loaded or final.pid is not None:
            raise Invalid("old launchd job exit was not proven")
        deadline = time.monotonic() + self.d.RELOAD_UNLOAD_WAIT_S
        while True:
            rows = self.process_rows()
            surviving = [p for p, parent, uid, command in rows if uid == os.getuid() and
                         (str(self.repo) in command and "provider_account_host" in command)]
            old_alive = bool(pid and not self.d._prior_writer_exited(pid, start, self.runner))
            tracked_alive = any(not born or not self.d._prior_writer_exited(p, born, self.runner) for p, born in tracked.items())
            if not surviving and not old_alive and not tracked_alive:
                break
            if time.monotonic() >= deadline:
                raise Invalid("local old-tree helper/daemon exit was not proven")
            time.sleep(0.25)
        self.stopped = True

    def revalidate_stopped(self, receipt, rollback=False):
        if not self.stopped:
            raise Invalid("source/policy switch requires proven process exit")
        self.validate_common(receipt, forward=not rollback)
        if self.d._dirty_entries(self.repo, self.runner):
            raise Invalid("release source drift while stopping")
        self.guard(receipt, rollback=True)

    def switch(self, receipt, *, rollback):
        if not self.stopped:
            raise Invalid("ordered switch requires stopped daemon")
        direction = "prior" if rollback else "target"
        self.checked(("git", "checkout", "--detach", receipt["source"][direction]["sha"]))
        if rollback:
            for key in ("policy", "plist", "collector", "stamp", "test_settings", *(("schedule",) if 'post_schedule' in receipt else ())):
                row = receipt["preimages"][key]
                if row["saved"]:
                    atomic_bytes(row["path"], Path(row["saved"]).read_bytes(), mode=row["mode"])
                else:
                    Path(row["path"]).unlink(missing_ok=True)
            prior = receipt["source"]["prior"]
            for helper in prior["original_helpers"].values():
                if self.helper_proof(helper, receipt["accounts"], prior["code_identity"], prior["manifest"], prior["sha"], verify_release=False) != helper["proof"]:
                    raise Invalid("original rollback helper drift")
        else:
            atomic_bytes(receipt["intent"]["policy_file"], Path(receipt["post_policy"]["path"]).read_bytes())
            atomic_bytes(receipt["preimages"]["plist"]["path"], Path(receipt["post_plist"]["path"]).read_bytes(),
                         mode=receipt["preimages"]["plist"]["mode"])
            if 'post_schedule' in receipt:
                atomic_bytes(receipt['preimages']['schedule']['path'], Path(receipt['post_schedule']['path']).read_bytes())

    def validate_recovery(self, receipt, journal):
        self.validate_common(receipt, forward=False)
        if self.d._dirty_entries(self.repo, self.runner):
            raise Invalid("unrelated release source changes block recovery")
        now = self.current(receipt)
        if journal["phase"] == "committed":
            if now != journal.get("postimage"):
                raise Invalid("post-commit drift/stale receipt; prepare a new coordinated transaction")
        elif now["sha"] not in {receipt["intent"]["prior_sha"], receipt["intent"]["target_sha"]}:
            raise Invalid("unrelated source blocks recovery")
        for key, value in now["files"].items():
            allowed = {receipt["preimages"][key]["sha256"]}
            if key == "policy": allowed.add(receipt["post_policy"]["sha256"])
            if key == "plist": allowed.add(receipt["post_plist"]["sha256"])
            if key == 'schedule' and 'post_schedule' in receipt:
                allowed.add(receipt['post_schedule']['sha256'])
            if key == "stamp":
                # Our activation stamp is the only additional admitted stamp.
                stamp = self.d._read_stamp(self.repo, self.service)
                if stamp.get("coordinated_receipt") == receipt["receipt_path"] and stamp.get("receipt_sha256") == receipt["receipt_sha256"]:
                    allowed.add(value)
            if value not in allowed:
                raise Invalid("unrelated policy/plist/schedule/stamp drift blocks recovery")
        auth = self.recovery_authorization
        if auth is not None:
            live = auth["expected_live"]
            state = self.d._launchd_state(self.service.launchd_label, self.runner)
            if (not state.answered or state.pid != live["pid"] or now["sha"] != live["sha"]
                    or self.d._process_start(state.pid, self.runner) != live["birth"]):
                raise Invalid("expected live daemon identity drift")
            # The replacement controller's installer must reproduce the sealed
            # prior proofs before any stop, as forward revalidate does.
            prior = receipt["source"]["prior"]
            for helper in prior["original_helpers"].values():
                if self.helper_proof(helper, receipt["accounts"], prior["code_identity"], prior["manifest"], prior["sha"], verify_release=False) != helper["proof"]:
                    raise Invalid("original helper proof drift before stop")

    def validate_restored(self, receipt, journal):
        self.validate_common(receipt, forward=False)
        if self.current(receipt) != journal.get("restored"):
            raise Invalid("restored state drift")

    def keep_stopped(self, receipt):
        self.stop(receipt)

    def prior_still_running(self, receipt, identity):
        state = self.d._launchd_state(self.service.launchd_label, self.runner)
        return bool(state.answered and state.loaded and state.pid == identity["pid"]
                    and self.d._process_start(state.pid, self.runner) == identity["birth"]
                    and self.git("rev-parse", "HEAD") == identity["sha"])

    def start_verify(self, receipt, *, rollback):
        self.verified_prior = None
        partial = {}
        try:
            return self._start_verify(receipt, rollback=rollback, partial=partial)
        except Exception as exc:
            # Completed sub-results survive a later verifier's raise.
            exc.partial_result = {"outcome": "failed", **copy.deepcopy(partial)}
            raise

    def _start_verify(self, receipt, *, rollback, partial):
        self.verification_started = time.time()
        direction = "prior" if rollback else "target"
        source = receipt["source"][direction]
        stamp = self.d._read_stamp(self.repo, self.service) if rollback else {}
        stamp.update(service=self.service.name, sha=source["sha"], prior_sha=receipt["intent"]["prior_sha"],
            coordinated_receipt=receipt["receipt_path"], receipt_sha256=receipt["receipt_sha256"],
            activation_runtime_manifest=self.d.runtime_manifest(self.repo, self.service, self.runner),
            timestamp=self.d._iso_now(), requirements_hash=file_digest(self.repo / self.service.requirements_path))
        self.d._write_stamp(self.repo, self.service, stamp)
        domain = self.d._launchctl_target(self.service.launchd_label).rsplit("/", 1)[0]
        self.checked(("launchctl", "bootstrap", domain, str(self.d._launchd_plist_path(self.service.launchd_label))))
        self.stopped = False
        boot = self.d.verify_fresh_daemon_log_line(self.service, previous_size=self.log_offsets,
            prior_pid=self.old_pid, wait_seconds=self.boot_wait_seconds, runner=self.runner)
        if boot.outcome != self.d.BOOT_OBSERVED or boot.pid == self.old_pid:
            raise Invalid("fresh daemon boot not proven")
        partial["boot"] = boot.as_stamp()
        confirmed, runtime = self.d._verify_runtime_sha(source["sha"], boot.pid)
        if not confirmed:
            raise Invalid("loaded runtime SHA not confirmed")
        partial["runtime"] = runtime
        self.validate_common(receipt, forward=not rollback)
        restoration = None
        if rollback:
            birth = self.d._process_start(boot.pid, self.runner)
            if birth:
                # Exact prior files restored, fresh launchd boot, SHA confirmed.
                self.verified_prior = {"pid": boot.pid, "birth": birth, "sha": source["sha"]}
            restoration = self.restoration_proof(receipt, source, boot)
            partial["restoration"] = restoration
            verdict = None
        else:
            if file_digest(self.repo / RUNNER) != source["runner_sha256"]:
                raise Invalid("installed smoke runner drift")
            if self.smoke_plan(self.repo, receipt["scope"], receipt["machines_snapshot"], accounts=receipt["intent"].get("smoke_accounts", []), test_profile=source["test_profile"]["path"]) != source["smoke_plan"]:
                raise Invalid("pre-canary runner plan drift")
            smoke = self.runner(self.smoke_argv(self.repo, receipt["scope"], receipt["machines_snapshot"], accounts=receipt["intent"].get("smoke_accounts", []), test_profile=source["test_profile"]["path"]), self.repo)
            verdict = self.d._fleet_smoke_verdict(smoke, receipt["machines_snapshot"],
                        declared_exclusions=receipt["intent"]["excluded_hosts"], accounts=receipt["intent"].get("smoke_accounts", []), test_profile=read_json(source["test_profile"]["path"]))
            partial["fleet_smoke"] = verdict
        self.validate_common(receipt, forward=not rollback)
        slow = self.d._scan_slow_consumer_window(self.service, self.log_offsets,
            expected_pid=boot.pid, expected_start=self.d._process_start(boot.pid, self.runner),
            prior_pid=self.old_pid, prior_start=self.old_start, no_outgoing_writer=self.old_pid is None, runner=self.runner)
        partial["slow_consumer"] = slow
        dispatch = self.dispatched_proofs(receipt, source, boot)
        result = {"outcome": "passed" if (restoration if rollback else verdict)["outcome"] == "passed" and slow.get("outcome") == "passed" else "failed",
                  "boot": boot.as_stamp(), "runtime": runtime, "slow_consumer": slow,
                  "helper_dispatch": dispatch}
        if rollback:
            result.update(restoration=restoration, model_canary={"outcome":"not_run_by_design"})
        else:
            result["fleet_smoke"] = verdict
        if result['outcome'] == 'passed' and 'post_schedule' in receipt:
            label = 'com.pentacle.spawn-fleet-smoke'
            self.runner(('launchctl', 'bootout', self.d._launchctl_target(label)), self.repo)
            schedule = receipt['preimages']['schedule']
            if not rollback or schedule['saved']:
                self.checked(('launchctl', 'bootstrap', self.d._launchctl_target(label).rsplit('/', 1)[0], schedule['path']))
        stamp.update(daemon_boot_readback=boot.as_stamp(), daemon_runtime_readback=runtime,
                     fleet_smoke=verdict, slow_consumer=slow, coordinated_phase="rolled_back" if rollback else "committed")
        # Preserve original rollback stamp bytes; its separate verification is
        # journaled. The active journal still prevents unsafe legacy rollback.
        if not rollback:
            self.d._write_stamp(self.repo, self.service, stamp)
        else:
            row = receipt["preimages"]["stamp"]
            if row["saved"]: atomic_bytes(row["path"], Path(row["saved"]).read_bytes(), mode=row["mode"])
            else: Path(row["path"]).unlink(missing_ok=True)
        return result

    def restoration_proof(self, receipt, source, boot):
        from deploy.restoration import verify_no_input
        bindings = [b for b in receipt["accounts"]["selected_bindings"]
                    if b["host"] in receipt["scope"]["included_hosts"] and b["enabled"]]
        result = verify_no_input(bindings, Path(receipt["transaction_directory"]) / "rollback-owned")
        expected = {(b["host"], b["provider"], b["alias"], b["generation"]) for b in bindings}
        cells = result.get("cells", [])
        if (result.get("outcome") != "passed" or len(cells) != len(expected)
                or {(b.get("host"), b.get("provider"), b.get("alias"), b.get("generation")) for b in cells} != expected
                or any(b.get("outcome") != "passed" or b.get("cleanup") != "passed"
                       or not b.get("session_generation") for b in cells)):
            raise Invalid("incomplete no-input restoration proof")
        result.update(included_hosts=receipt["scope"]["included_hosts"],
                      excluded_hosts=receipt["scope"].get("excluded_hosts", []),
                      full_fleet_acceptance=False,
                      runtime={"sha":source["sha"], "pid":boot.pid,
                               "birth":self.d._process_start(boot.pid, self.runner)})
        if not result["runtime"]["birth"]:
            raise Invalid("restored daemon birth unavailable")
        # Same verifier as forward smoke; only the launch source differs.
        result["helper_dispatch"] = self.dispatched_proofs(receipt, source, boot)
        return result

    def dispatched_proofs(self, receipt, source, boot):
        proofs = []
        with sqlite3.connect(Path(receipt["accounts"]["database"]).as_uri() + "?mode=ro", uri=True) as conn:
            for raw in receipt["accounts"]["selected_bindings"]:
                binding = Binding(**raw)
                if binding.host not in receipt["scope"]["included_hosts"] or not binding.enabled: continue
                row = conn.execute("SELECT v FROM kv WHERE k=?", ("provider_cli.verified:" + binding.key,)).fetchone()
                if not row: raise Invalid("actual helper dispatch proof missing")
                value = json.loads(row[0])
                validate_proof(binding, receipt["accounts"]["pin"], value["proof"], consumer_identity=source["code_identity"])
                if value.get("verified_at", 0) < self.verification_started:
                    raise Invalid("actual helper dispatch proof is stale")
                proofs.append({"host": binding.host, "provider": binding.provider, "binding_key": binding.key,
                               "proof": value["proof"], "verified_at": value["verified_at"]})
        return proofs
