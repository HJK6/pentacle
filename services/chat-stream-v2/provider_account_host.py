"""Internal authenticated-SSH executor for one configured host's account cells.

The daemon supplies typed action/alias/episode identifiers, never home, profile,
credential or destination overrides. This is not an agent control endpoint.
Install/configure it only with the account feature's separate activation grant.
"""
from __future__ import annotations
import argparse
from functools import partial
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import stat
from dataclasses import replace, asdict
from urllib.parse import urlsplit

from provider_accounts import AccountError, Registry, _component, _mkdir, _private, environment, write_private_json
from provider_account_native import (
    adopt, claude_observation, codex_observation, enroll, native_login, verify_binary,
    native_run, classify_error, minimal_turn,
)


def authorization_url(value):
    """Validate in memory; errors never include the authorization material."""
    if (not isinstance(value, str) or not 1 <= len(value) <= 8192
            or any(ord(c) <= 32 or ord(c) >= 127 for c in value)):
        raise AccountError("account_browser_url_invalid")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.netloc != "auth.openai.com"
                or parsed.username or parsed.password or parsed.fragment):
            raise ValueError()
    except ValueError: raise AccountError("account_browser_url_invalid") from None
    return value


class OperatorBrowser:
    """One private inherited pipe/socket. Never inherited by native children."""
    def __init__(self, fd):
        try:
            if type(fd) is not int or fd < 3: raise ValueError()
            mode = os.fstat(fd).st_mode
            if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)) or os.isatty(fd): raise ValueError()
            import fcntl
            if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY: raise ValueError()
            os.set_inheritable(fd, False)
        except (OSError, ValueError): raise AccountError("account_browser_fd_invalid") from None
        self.fd = fd

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __call__(self, binding, url):
        if self.fd is None or binding.provider != "codex": raise AccountError("account_browser_fd_invalid")
        try:
            data = (authorization_url(url) + "\n").encode()
            # A socket/pipe write can be partial; the reader enforces the same bound.
            while data:
                count = os.write(self.fd, data)
                if count <= 0: raise OSError()
                data = data[count:]
            return {"outcome": "ok"}
        except OSError: raise AccountError("account_browser_delivery_failed") from None
        finally: self.close()


class NativeHost:
    def __init__(self, config, *, run=subprocess.run, login=native_login, observation=None, operator_browser=None):
        self.config, self.run, self.login = config, native_run if run is subprocess.run else run, login
        adopted = {}
        for owner, cell in config.get("cells", {}).items():
            if not isinstance(cell, dict) or not cell.get("adopt_home"): continue
            home = str(Path(cell["adopt_home"]))
            if home in adopted: raise AccountError("account_home_owner_conflict")
            adopted[home] = owner
        self.registry = Registry(Path(config["registry"]), Path(config["root"]), adopted)
        self.journal = self.registry.path.parent / "account-operations"
        self.observation = observation or self._observation
        self._probe_observation = observation or partial(self._observation, wait_for_refresh=True)
        self.operator_browser = operator_browser

    def _cell(self, provider, alias):
        try: cell = self.config["cells"][f"{provider}:{alias}"]
        except (KeyError, TypeError): raise AccountError("account_cell_unconfigured") from None
        if (cell.get("enabled") is not True or not (cell.get("expected_provider_id") or cell.get("expected_email"))
                or not cell.get("version") or not cell.get("profile_ref") or not cell.get("executable_sha256")):
            raise AccountError("account_cell_disabled")
        return cell

    def _observation(self, binding, *, wait_for_refresh=False):
        verify_binary(binding, run=self.run)
        cell = self._cell(binding.provider, binding.alias)
        if binding.provider == "codex": return codex_observation(binding, expected_email=cell.get("expected_email"))
        return claude_observation(binding, collect_usage=self._claude_usage, run=self.run,
            expected_email=cell.get("expected_email"), wait_for_refresh=wait_for_refresh)

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
            result = self.run(command, env=env, capture_output=True, text=True, timeout=70,
                interrupt_cleanup=True, start_new_session=True, umask=0o077)
        except (OSError, subprocess.SubprocessError): raise AccountError("provider_probe_failed") from None
        if result.returncode: raise AccountError(classify_error((result.stdout or "") + (result.stderr or "")))
        # The display scrape is discarded; the one coherent named-home cache
        # read supplies identity, all windows and the actual upstream timestamp.

    def _browser(self, binding, url):
        if self.operator_browser is not None: return self.operator_browser(binding, url)
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

    def _repin(self,action,active,cell,identity,operation_id,pin):
        from provider_cli_artifact import verify,assert_drained
        from provider_cli_pin import digest
        from account_revalidation import _valid_observation
        identity={**identity,"kind":"repin","pin_revision":pin["revision"],"pin_digest":digest(pin)}
        prior=self._read_operation(operation_id)
        if prior:
            if any(prior.get(k)!=v for k,v in identity.items()): raise AccountError("account_operation_conflict")
            if prior["state"]=="done":
                if action=="repin-status" and prior.get("outcome") not in {"ok","no_op"}:
                    return self._non_split_status(prior,active,identity)
                return prior
            if active and active.generation==identity["generation"] and prior.get("proof"):
                result={**prior,"state":"done","outcome":"ok","binding":active.public()}
                self._save_operation(operation_id,result)
                return result
            return {**prior,"outcome":"execution_unresolved"}
        if action=="repin-status":
            return self._non_split_status({**identity,"state":"absent","outcome":"execution_unresolved"},active,identity)
        if not active or active.generation!=identity["expected_generation"]:
            raise AccountError("account_login_generation_stale")
        if not active.enabled or not active.healthy: raise AccountError("account_unavailable")
        _component(identity["generation"])
        if identity["generation"]==active.generation: raise AccountError("account_generation_exists")
        if self.registry._load()["generations"].get(":".join((active.host,active.provider,active.alias,identity["generation"]))):
            raise AccountError("account_generation_exists")
        target=replace(active,generation=identity["generation"],executable=cell["executable"],version=cell["version"],
                       executable_sha256=cell["executable_sha256"])
        proof=verify(target,cell,pin,run=self.run)
        if (target.executable_sha256,target.version)==(active.executable_sha256,active.version):
            proof=verify(active,cell,pin,run=self.run)
            result={**identity,"state":"done","outcome":"no_op","binding":active.public(),"proof":proof}
            self._save_operation(operation_id,result)
            return result
        assert_drained(active,run=self.run)
        state={**identity,"state":"started","outcome":"execution_unresolved"}
        self._save_operation(operation_id,state)
        try:
            before=self.observation(target)
            if not _valid_observation(target,before): raise AccountError("account_repin_identity_invalid")
            minimal_turn(target,model=cell["model"],effort=cell["effort"],cwd=cell["cwd"],run=self.run)
            after=self.observation(target)
            if not _valid_observation(target,after): raise AccountError("account_repin_identity_invalid")
            proof=verify(target,cell,pin,run=self.run)
            assert_drained(active,run=self.run)
            state.update(proof=proof,observation=after)
            self._save_operation(operation_id,state)
            self.registry.repin(active,target)
            result={**state,"state":"done","outcome":"ok","binding":target.public()}
        except AccountError as exc:
            result={**identity,"state":"done","outcome":str(exc)}
        self._save_operation(operation_id,result)
        return result

    def _non_split_status(self,receipt,active,identity):
        if active and active.generation==identity["expected_generation"] and active.enabled and active.healthy:
            self.registry._validate_home(active)
            key=":".join((active.host,active.provider,active.alias,identity["generation"]))
            if key not in self.registry._load()["generations"]:
                return {**receipt,"non_split":{"binding":active.public(),"successor_absent":True}}
        return receipt

    def _validate_repin_links(self,context,cell,*,require_active=True):
        from provider_accounts import Binding
        from account_repin import _successor
        if not isinstance(context,dict) or not isinstance(context.get("links"),list) or not context["links"]:
            raise AccountError("account_repin_lineage_invalid")
        old=Binding(**context["root"])
        registry=self.registry._load(); seen={old.key}
        for link in context["links"]:
            if link.get("old")!=asdict(old): raise AccountError("account_repin_lineage_invalid")
            raw=registry["generations"].get(old.key)
            if raw is None: raise AccountError("account_repin_lineage_invalid")
            stored=Binding(**raw)
            if stored.public()!=old.public() or (old.profile_ref and old.profile_ref!=stored.profile_ref):
                raise AccountError("account_repin_lineage_invalid")
            self.registry._validate_home(stored)
            args=link["arguments"]; pin=link["pin"]; receipt=link["receipt"]
            from provider_cli_pin import digest
            identity={"kind":"repin","provider":old.provider,"alias":old.alias,
                "expected_generation":old.generation,"generation":args["new_generation"],
                "pin_revision":pin["revision"],"pin_digest":digest(pin)}
            if (any(args.get(k)!=v for k,v in {"host":old.host,"provider":old.provider,"account":old.alias,
                    "login_generation":old.generation,"expected_pin_revision":pin["revision"],"expected_pin_digest":digest(pin)}.items())
                    or any(receipt.get(k)!=v for k,v in identity.items())
                    or self._read_operation(link["operation_id"])!=receipt):
                raise AccountError("account_repin_lineage_invalid")
            new,no_op=_successor(stored,args,receipt,pin,consumer_identity=link["consumer_code_identity"])
            if no_op or new.key in seen or registry["generations"].get(new.key)!=asdict(new):
                raise AccountError("account_repin_lineage_invalid")
            self.registry._validate_home(new)
            # Retained config identity/auth-store must still identify this very home.
            if (cell.get("platform")!=new.platform or cell.get("profile_ref")!=new.profile_ref
                    or (cell.get("expected_provider_id") and cell["expected_provider_id"]!=new.provider_id)):
                raise AccountError("account_repin_lineage_invalid")
            if new.provider=="codex" or new.platform=="wsl":
                _private(Path(new.home)/("auth.json" if new.provider=="codex" else ".credentials.json"),directory=False)
            elif (Path(new.home)/".credentials.json").exists() or (Path(new.home)/".credentials.json").is_symlink():
                raise AccountError("unexpected_store_fallback")
            seen.add(new.key);old=replace(new,profile_ref=context["root"]["profile_ref"])
        if require_active and registry["active"].get(f"{old.host}:{old.provider}:{old.alias}")!=old.key:
            raise AccountError("account_login_generation_stale")
        return Binding(**registry["generations"][old.key])

    def _reconcile_repin(self,action,active,cell,identity,operation_id,pin,context):
        from provider_cli_artifact import verify,assert_drained
        from provider_cli_pin import digest,version
        identity={**identity,"kind":"repin-reconcile","pin_revision":pin["revision"],
                  "pin_digest":digest(pin),"reconcile":context}
        prior=self._read_operation(operation_id)
        if prior and any(prior.get(k)!=v for k,v in identity.items()): raise AccountError("account_operation_conflict")
        if prior and prior["state"]=="done":
            if action=="repin-reconcile-status": return prior
            final=self._validate_repin_links({"root":context["root"],"links":prior["links"]},cell)
            verify(final,cell,pin,run=self.run)
            return prior
        if action=="repin-reconcile-status" and not prior: return {"state":"absent","outcome":"execution_unresolved"}
        if action=="repin-reconcile-verify": raise AccountError("account_repin_lineage_invalid")
        # A retry can recover a completed target from its exact journal, never a second turn.
        _component(identity["generation"])
        prospective=":".join((active.host,active.provider,active.alias,identity["generation"])) if active else ""
        if not prior and self.registry._load()["generations"].get(prospective):
            raise AccountError("account_generation_exists")
        target_id=operation_id+"-target"
        target_receipt=self._read_operation(target_id) if prior else None
        if action=="repin-reconcile-status" and target_receipt is None:
            return prior
        base=self._validate_repin_links(context,cell,require_active=target_receipt is None)
        assert_drained(base,run=self.run)
        links=context["links"]
        if target_receipt is not None or (version(base.provider,base.version)!=pin["providers"][base.provider]["version"]
                or not any(a["native_sha256"]==base.executable_sha256 for a in pin["providers"][base.provider]["artifacts"].values())):
            if not prior:
                self._save_operation(operation_id,{**identity,"state":"started","outcome":"execution_unresolved"})
            derived={"provider":base.provider,"alias":base.alias,"expected_generation":base.generation,"generation":identity["generation"]}
            result=self._repin("repin-status" if target_receipt is not None else "repin",active,cell,derived,target_id,pin)
            if result.get("state")!="done" or result.get("outcome")!="ok":
                return {**identity,"state":"started","outcome":"execution_unresolved"}
            args={"host":base.host,"provider":base.provider,"account":base.alias,"login_generation":base.generation,
                "new_generation":identity["generation"],"expected_pin_revision":pin["revision"],"expected_pin_digest":digest(pin)}
            links=links+[{"operation_id":target_id,"old":asdict(replace(base,profile_ref=context["root"]["profile_ref"])),"arguments":args,"pin":pin,"receipt":result,"consumer_code_identity":result["proof"]["code_identity"]}]
            final=self._validate_repin_links({"root":context["root"],"links":links},cell)
        else:
            final=base
        proof=verify(final,cell,pin,run=self.run)
        result={**identity,"state":"done","outcome":"ok","links":links,"binding":final.public(),"current_artifact":proof}
        self._save_operation(operation_id,result)
        return result

    def handle(self, action, *, provider, alias, expected_generation, operation_id, generation=None, pin=None, reconcile=None):
        if self.operator_browser is not None and (action != "enroll" or provider != "codex"):
            raise AccountError("account_browser_fd_invalid")
        _component(alias)
        if provider not in {"codex", "claude"}:
            raise AccountError("account_request_invalid")
        cell = self._cell(provider, alias)
        with self.registry.lock():
            active = self.registry.active(provider, alias, self.config["host"])
            if action in {"verify-artifact","repin","repin-status","repin-reconcile","repin-reconcile-status","repin-reconcile-verify"}:
                from provider_cli_pin import validate
                try: pin=validate(pin)
                except ValueError: raise AccountError("provider_cli_pin_unavailable") from None
                if action=="verify-artifact":
                    from provider_cli_artifact import verify
                    if not active or active.generation!=expected_generation: raise AccountError("account_login_generation_stale")
                    return verify(active,cell,pin,run=self.run)
                identity={"provider":provider,"alias":alias,"expected_generation":expected_generation,"generation":generation}
                if action.startswith("repin-reconcile"):
                    return self._reconcile_repin(action,active,cell,identity,operation_id,pin,reconcile)
                return self._repin(action,active,cell,identity,operation_id,pin)
            if action == "observe":
                if active is None or active.generation != expected_generation:
                    raise AccountError("account_login_generation_stale")
                proof=None
                if pin is not None:
                    from provider_cli_pin import validate
                    from provider_cli_artifact import verify
                    try: pin=validate(pin)
                    except ValueError: raise AccountError("provider_cli_pin_unavailable") from None
                    proof=verify(active,cell,pin,run=self.run)
                result = self._probe_observation(active)
                return {"outcome": "ok", "binding_key": active.key, "observation": result,
                        **({"artifact_proof":proof} if proof else {})}
            if action not in {"enroll", "adopt", "status"}: raise AccountError("account_action_invalid")
            if action == "adopt" and not cell.get("adopt_home"): raise AccountError("account_adopt_unsupported")
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
                    observer_executable=cell.get("observer_executable"),
                    home=cell["adopt_home"] if action == "adopt" else None)
                if (draft.executable_sha256 != cell["executable_sha256"] or
                        (draft.observer_executable and draft.observer_executable_sha256 != cell.get("observer_executable_sha256"))):
                    raise AccountError("provider_executable_changed")
                def identity_readback(binding):
                    row = self.observation(binding)
                    return {key: row.get(key) for key in ("provider_id", "pool_id", "auth_mode", "email_verified")}
                if action == "adopt":
                    bound = adopt(self.registry, draft, expected_provider_id=cell.get("expected_provider_id"),
                        identity=identity_readback, model=cell["model"], effort=cell["effort"], cwd=cell["cwd"],
                        manifest=cell.get("bootstrap", {}), run=self.run)
                else:
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
    parser.add_argument("action", choices=("observe", "enroll", "adopt", "status", "verify-artifact", "repin", "repin-status", "repin-reconcile", "repin-reconcile-status", "repin-reconcile-verify"))
    parser.add_argument("--provider", choices=("codex", "claude"), required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--login-generation", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--new-generation")
    parser.add_argument("--operator-browser-fd", type=int)
    parser.add_argument("--pin-json")
    parser.add_argument("--reconcile-json")
    args = parser.parse_args(argv)
    # The location is host-owned configuration, never a worker's --target.
    path = Path.home() / ".config/pentacle/provider-account-host.json"
    browser = None
    try:
        if args.operator_browser_fd is not None:
            if args.action != "enroll" or args.provider != "codex": raise AccountError("account_browser_fd_invalid")
            browser = OperatorBrowser(args.operator_browser_fd)
        _private(path.parent, directory=True); _private(path, directory=False)
        config = json.loads(path.read_text())
        def expired(*_): raise AccountError("provider_timeout")
        signal.signal(signal.SIGALRM, expired)
        signal.alarm(300 if args.action in {"enroll", "adopt", "repin", "repin-reconcile"} else 180 if args.action == "observe" else 90)
        result = NativeHost(config, operator_browser=browser).handle(args.action, provider=args.provider, alias=args.account,
            expected_generation=args.login_generation, operation_id=args.operation_id, generation=args.new_generation,
            pin=json.loads(args.pin_json) if args.pin_json else None,
            reconcile=json.loads(args.reconcile_json) if args.reconcile_json else None)
        print(json.dumps(result))
        return 0
    except AccountError as exc:
        print(json.dumps({"outcome": str(exc)})); return 1
    except Exception:
        print(json.dumps({"outcome": "account_host_failed"})); return 1
    finally:
        signal.alarm(0)
        if browser is not None: browser.close()


if __name__ == "__main__": raise SystemExit(main())
