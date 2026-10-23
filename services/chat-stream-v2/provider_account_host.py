"""Internal authenticated-SSH executor for one configured host's account cells.

The daemon supplies typed action/alias/episode identifiers, never home, profile,
credential or destination overrides. This is not an agent control endpoint.
Install/configure it only with the account feature's separate activation grant.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from provider_accounts import AccountError, Registry, _component, _mkdir, _private, environment, write_private_json
from provider_account_native import (
    claude_observation, codex_observation, enroll, native_login, verify_binary,
    native_run, classify_error,
)


class NativeHost:
    def __init__(self, config, *, run=subprocess.run, login=native_login, observation=None):
        self.config, self.run, self.login = config, native_run if run is subprocess.run else run, login
        self.registry = Registry(Path(config["registry"]), Path(config["root"]))
        self.journal = self.registry.path.parent / "account-operations"
        self.observation = observation or self._observation

    def _cell(self, provider, alias):
        try: cell = self.config["cells"][f"{provider}:{alias}"]
        except (KeyError, TypeError): raise AccountError("account_cell_unconfigured") from None
        if (cell.get("enabled") is not True or not (cell.get("expected_provider_id") or cell.get("expected_email"))
                or not cell.get("version") or not cell.get("profile_ref") or not cell.get("executable_sha256")):
            raise AccountError("account_cell_disabled")
        return cell

    def _observation(self, binding):
        verify_binary(binding, run=self.run)
        cell = self._cell(binding.provider, binding.alias)
        if binding.provider == "codex": return codex_observation(binding, expected_email=cell.get("expected_email"))
        return claude_observation(binding, collect_usage=self._claude_usage, run=self.run, expected_email=cell.get("expected_email"))

    def _claude_usage(self, binding, env):
        cell = self._cell(binding.provider, binding.alias)
        command = self.config.get("claude_usage_command")
        if not isinstance(command, list) or not command or not all(isinstance(p, str) for p in command):
            raise AccountError("account_usage_adapter_unconfigured")
        # The existing /usage adapter owns its tmux socket and group cleanup.
        env = env | {"PENTACLE_USAGE_CLAUDE_BIN": binding.executable,
                     "PENTACLE_USAGE_CLAUDE_OAUTH": "0", "PENTACLE_USAGE_CWD": cell["cwd"],
                     "PENTACLE_USAGE_NAMED_ACCOUNT": "1"}
        try:
            result = self.run(command, env=env, capture_output=True, text=True, timeout=70, start_new_session=True, umask=0o077)
        except (OSError, subprocess.SubprocessError): raise AccountError("provider_probe_failed") from None
        if result.returncode: raise AccountError(classify_error((result.stdout or "") + (result.stderr or "")))
        # The display scrape is discarded; the one coherent named-home cache
        # read supplies identity, all windows and the actual upstream timestamp.

    def _browser(self, binding, url):
        helper = self.config.get("browser_helper")
        if not isinstance(helper, dict): raise AccountError("account_browser_unconfigured")
        try:
            import hashlib
            import importlib.util
            path = Path(helper["path"])
            if not path.is_absolute() or hashlib.sha256(path.read_bytes()).hexdigest() != helper["sha256"]:
                raise AccountError("account_browser_changed")
            spec = importlib.util.spec_from_file_location("_pentacle_account_browser", path)
            module = importlib.util.module_from_spec(spec)
            # reauth.py and its existing broker are beside the reviewed helper.
            sys.path.insert(0, str(path.parent))
            try:
                spec.loader.exec_module(module)
                value = module.authorize(binding, url)
            finally: sys.path.remove(str(path.parent))
        except AccountError: raise
        except Exception:
            return {"outcome": "human_reseed_required"}
        return {key: value[key] for key in ("outcome", "code") if key in value}

    def _read_operation(self, operation_id):
        path = self.journal / (_component(operation_id) + ".json")
        if not path.exists(): return None
        _private(path, directory=False)
        try: return json.loads(path.read_text())
        except ValueError: raise AccountError("account_operation_corrupt") from None

    def _save_operation(self, operation_id, state):
        _mkdir(self.journal)
        write_private_json(self.journal / (_component(operation_id) + ".json"), state)

    def handle(self, action, *, provider, alias, expected_generation, operation_id, generation=None):
        _component(alias)
        if provider not in {"codex", "claude"}:
            raise AccountError("account_request_invalid")
        cell = self._cell(provider, alias)
        with self.registry.lock():
            active = self.registry.active(provider, alias, self.config["host"])
            if action == "observe":
                if active is None or active.generation != expected_generation:
                    raise AccountError("account_login_generation_stale")
                result = self.observation(active)
                return {"outcome": "ok", "binding_key": active.key, "observation": result}
            if action not in {"enroll", "status"}: raise AccountError("account_action_invalid")
            prior = self._read_operation(operation_id)
            identity = {"provider": provider, "alias": alias, "expected_generation": expected_generation,
                        "generation": generation}
            if prior:
                if any(prior.get(key) != value for key, value in identity.items()):
                    raise AccountError("account_operation_conflict")
                if prior["state"] == "done": return prior
                # A crash after activation but before the receipt is repaired
                # from the active generation. No second login is attempted.
                if active and active.generation == generation and (not cell.get("expected_provider_id") or active.provider_id == cell["expected_provider_id"]):
                    result = identity | {"state": "done", "outcome": "ok", "binding": active.public()}
                    self._save_operation(operation_id, result); return result
                return prior | {"outcome": "execution_unresolved"}
            if action == "status": return {"state": "absent", "outcome": "execution_unresolved"}
            if (active.generation if active else "unenrolled") != expected_generation:
                raise AccountError("account_login_generation_stale")
            _component(generation)
            intent = identity | {"state": "started", "outcome": "execution_unresolved"}
            self._save_operation(operation_id, intent)
            try:
                draft = self.registry.prepare(provider=provider, alias=alias, host=self.config["host"], generation=generation,
                    profile_ref=cell["profile_ref"], executable=cell["executable"], version=cell["version"], platform=cell["platform"],
                    observer_executable=cell.get("observer_executable"))
                if (draft.executable_sha256 != cell["executable_sha256"] or
                        (draft.observer_executable and draft.observer_executable_sha256 != cell.get("observer_executable_sha256"))):
                    raise AccountError("provider_executable_changed")
                def identity_readback(binding):
                    row = self.observation(binding)
                    return {key: row.get(key) for key in ("provider_id", "pool_id", "auth_mode", "email_verified")}
                bound = enroll(self.registry, draft, expected_provider_id=cell.get("expected_provider_id"),
                    browser_authorize=self._browser, login=self.login, identity=identity_readback,
                    model=cell["model"], effort=cell["effort"], cwd=cell["cwd"], manifest=cell.get("bootstrap", {}), run=self.run)
                result = identity | {"state": "done", "outcome": "ok", "binding": bound.public()}
            except AccountError as exc:
                safe = str(exc) if str(exc) in {"human_reseed_required", "cancelled", "locked_keychain"} else "account_enrollment_failed"
                result = identity | {"state": "done", "outcome": safe}
            self._save_operation(operation_id, result)
            return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("observe", "enroll", "status"))
    parser.add_argument("--provider", choices=("codex", "claude"), required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--login-generation", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--new-generation")
    args = parser.parse_args(argv)
    # The location is host-owned configuration, never a worker's --target.
    path = Path.home() / ".config/pentacle/provider-account-host.json"
    try:
        _private(path.parent, directory=True); _private(path, directory=False)
        config = json.loads(path.read_text())
        def expired(*_): raise AccountError("provider_timeout")
        signal.signal(signal.SIGALRM, expired)
        signal.alarm(300 if args.action == "enroll" else 90)
        result = NativeHost(config).handle(args.action, provider=args.provider, alias=args.account,
            expected_generation=args.login_generation, operation_id=args.operation_id, generation=args.new_generation)
        print(json.dumps(result))
        return 0
    except AccountError as exc:
        print(json.dumps({"outcome": str(exc)})); return 1
    except Exception:
        print(json.dumps({"outcome": "account_host_failed"})); return 1
    finally: signal.alarm(0)


if __name__ == "__main__": raise SystemExit(main())
