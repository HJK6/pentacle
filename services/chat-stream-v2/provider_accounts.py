"""Host-local subscription account registry. No credential content is read here.

Only the daemon and its authenticated host executor use this module. Registry
mutations are serialized with native-login/browser work by ``Registry.lock``.
The account root and registry are private configuration, never source assets.
"""
from __future__ import annotations

import contextlib
from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import hashlib
from functools import wraps
import tomllib
from typing import Mapping


class AccountError(ValueError):
    """A safe typed code, with no provider output or credential values."""

    def __init__(self, code, **context):
        super().__init__(code)
        safe = {"host", "provider", "account", "reason", "expected_version", "observed_version", "next_action", "pin_revision"}
        self.context = {k:v for k,v in context.items() if k in safe and type(v) in {str, int}}


def _serialized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.lock(): return method(self, *args, **kwargs)
    return wrapped


def _component(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", value):
        raise AccountError("invalid_account_component")
    return value


def validate_policy_scope(policy: dict) -> None:
    """An explicit provider scope must name its complete host/provider matrix."""
    if not isinstance(policy, dict) or type(policy.get("enabled")) is not bool:
        raise AccountError("account_policy_invalid")
    hosts = policy.get("managed_hosts")
    if "managed_hosts" in policy:
        if not isinstance(hosts, list): raise AccountError("account_policy_invalid")
        try:
            for host in hosts: _component(host)
        except AccountError: raise AccountError("account_policy_invalid") from None
        if len(set(hosts)) != len(hosts): raise AccountError("account_policy_invalid")
    if "managed_providers" not in policy: return
    scopes = policy["managed_providers"]
    if not isinstance(hosts, list) or not isinstance(scopes, dict) or set(scopes) != set(hosts):
        raise AccountError("account_policy_invalid")
    for providers in scopes.values():
        if (not isinstance(providers, list)
                or any(not isinstance(provider, str) or provider not in {"codex", "claude"} for provider in providers)
                or len(set(providers)) != len(providers)):
            raise AccountError("account_policy_invalid")


def policy_manages(policy: dict, host: str, provider: str | None = None) -> bool:
    """None asks whether any provider is managed, for raw-command admission."""
    validate_policy_scope(policy)
    if policy["enabled"] is not True: return False
    if "managed_hosts" in policy and host not in policy["managed_hosts"]: return False
    if "managed_providers" not in policy: return True
    providers = policy["managed_providers"].get(host, [])
    return bool(providers) if provider is None else provider in providers


def _private(path: Path, *, directory: bool) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise AccountError("account_path_missing") from None
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or (not directory and info.st_nlink != 1)):
        raise AccountError("account_path_insecure")
    if path.absolute() != path.resolve():
        raise AccountError("account_symlink_forbidden")


def _mkdir(path: Path) -> None:
    # Do not repair an existing directory's permissions/ownership silently.
    if path.is_symlink(): raise AccountError("account_symlink_forbidden")
    if not path.exists():
        if not path.parent.exists():
            _mkdir(path.parent)
        path.mkdir(mode=0o700)
    _private(path, directory=True)


@dataclass(frozen=True)
class Binding:
    provider: str
    alias: str
    host: str
    home: str
    generation: str
    profile_ref: str
    executable: str
    version: str
    platform: str
    provider_id: str = ""
    pool_id: str = ""
    enabled: bool = False
    healthy: bool = False
    executable_sha256: str = ""
    observer_executable: str = ""
    observer_executable_sha256: str = ""

    @property
    def observer(self) -> str:
        return self.observer_executable or self.executable

    @property
    def key(self) -> str:
        return ":".join((self.host, self.provider, self.alias, self.generation))

    @property
    def transcript_root(self) -> str:
        return str(Path(self.home) / ("sessions" if self.provider == "codex" else "projects"))

    default_revision: int = 0

    @classmethod
    def from_public(cls, value):
        if not isinstance(value, Mapping): raise AccountError("account_binding_invalid")
        fields = dict(value)
        root = fields.pop("transcript_root", None)
        fields["profile_ref"] = ""
        try: binding = cls(**fields)
        except TypeError: raise AccountError("account_binding_invalid") from None
        if root is not None and root != binding.transcript_root: raise AccountError("account_binding_invalid")
        return binding

    def public(self) -> dict:
        # Browser profile references remain host-local.
        return {k: v for k, v in asdict(self).items() if k != "profile_ref"} | {
            "transcript_root": self.transcript_root}


def validate_home(binding: Binding, root: Path, adopted: Mapping[str, str] | None = None, *, _registry_anchor=None) -> None:
    for value in (binding.alias, binding.host, binding.generation):
        _component(value)
    if binding.provider not in {"codex", "claude"} or binding.platform not in {"macos", "wsl"}:
        raise AccountError("unsupported_account_provider_platform")
    if binding.home in (adopted or {}):
        # A host-configured, already signed-in dedicated home registered in place.
        if binding.provider != "codex": raise AccountError("account_adopt_unsupported")
        if adopted[binding.home] != f"{binding.provider}:{binding.alias}":
            raise AccountError("account_home_owner_conflict")
        _private(Path(binding.home), directory=True)
    else:
        generation=binding.generation
        if _registry_anchor is not None:
            if any(getattr(binding,k)!=getattr(_registry_anchor,k) for k in
                   ("host","provider","alias","home","profile_ref","platform","provider_id","pool_id")):
                raise AccountError("account_home_owner_conflict")
            generation=_registry_anchor.generation
        expected = root / binding.host / binding.provider / binding.alias / generation
        if not root.is_absolute() or Path(binding.home) != expected:
            raise AccountError("account_home_outside_registry")
        current = root
        _private(current, directory=True)
        for component in expected.relative_to(root).parts:
            current /= component
            _private(current, directory=True)
    if not Path(binding.executable).is_absolute() or not binding.version:
        raise AccountError("account_executable_unpinned")


# An allowlist avoids the expanding family of API/WIF/cloud/endpoint/token
# overrides. HOME is always the real host's home, never the provider home.
ENV_ALLOWLIST = frozenset({
    "HOME", "USER", "LOGNAME", "PATH", "SHELL", "TERM", "LANG", "LC_ALL",
    "TMPDIR", "TZ", "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
    "SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS", "BROWSER",
    "PENTACLE_STREAM_ID", "AGENT_ORCH_STREAM_ID", "AGENT_ORCH_STREAM_TOKEN_FILE",
    "PENTACLE_HOST_ID", "AGENT_ORCH_HOST_ID", "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN",
    "PENTACLE_AUTH_SSM_PARAMETER", "PENTACLE_AUTH_LOGIN_KEYCHAIN", "PENTACLE_AUTH_CLAUDE_BIN",
})
AUTH_HOME_KEYS = ("CODEX_HOME", "CLAUDE_CONFIG_DIR")


def environment(binding: Binding, base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {key: value for key, value in (os.environ if base is None else base).items()
           if key in ENV_ALLOWLIST}
    env["CODEX_HOME" if binding.provider == "codex" else "CLAUDE_CONFIG_DIR"] = binding.home
    env.pop("PENTACLE_AUTH_CLAUDE_BIN", None)
    if binding.provider == "claude" and binding.observer_executable:
        env["PENTACLE_AUTH_CLAUDE_BIN"] = binding.observer_executable
    return env


OVERRIDE_FIELDS = frozenset({
    "account", "account_alias", "account_home", "account_binding", "provider_account",
    "provider_identity", "login_generation", "quota_pool", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
    "provider_accounts", "account_generation", "account_override",
})


def reject_overrides(message: Mapping, *, allow_account=False) -> None:
    forbidden = OVERRIDE_FIELDS - {"account"} if allow_account else OVERRIDE_FIELDS
    if forbidden.intersection(message):
        raise AccountError("account_override_forbidden")
    for key in ("env", "environment", "overrides", "launch_overrides"):
        if isinstance(message.get(key), Mapping):
            if OVERRIDE_FIELDS.intersection(message[key]):
                raise AccountError("account_override_forbidden")


def watcher_sources() -> dict[str, str]:
    """Fixed per-host watcher principals, with the existing file-token verifier."""
    policy_path = os.environ.get("PENTACLE_PROVIDER_ACCOUNT_POLICY")
    if not policy_path: return {}
    try:
        path = Path(policy_path); _private(path, directory=False)
        data = json.loads(path.read_text())
        result = {}
        for actor, entry in data.get("watchers", {}).items():
            if not isinstance(entry, dict): continue
            host = _component(entry.get("host"))
            token_file = entry.get("token_file")
            if actor == f"{host}:account-watcher" and isinstance(token_file, str) and Path(token_file).is_absolute():
                result[actor] = token_file
        return result
    except (OSError, ValueError, TypeError): return {}


def transcript_allowed(path: str, provider: str, binding: Mapping | None = None) -> bool:
    if not isinstance(path, str) or not path.endswith(".jsonl"):
        return False
    if binding is None:
        if provider == "gemini":
            candidate = Path(path)
            native_root = (Path.home() / ".gemini/tmp").resolve()
            try:
                resolved = candidate.resolve(strict=True)
                relative = resolved.relative_to(native_root)
                stable = candidate.parent.name == "sessions" and candidate.parent.parent.name == ".gemini"
                return (candidate.is_absolute() and resolved.is_file()
                        and len(relative.parts) == 3 and relative.parts[1] == "chats"
                        and (candidate == resolved or (stable and candidate.is_symlink())))
            except (OSError, ValueError, RuntimeError):
                return False
        fragment = "/.codex/sessions/" if provider == "codex" else "/.claude/"
        return fragment in path
    if provider == "gemini":
        return False  # Named subscription-account roots are not implemented.
    if binding.get("provider") != provider:
        return False
    root = binding.get("transcript_root")
    if not isinstance(root, str) or not os.path.isabs(root):
        return False
    try:
        candidate = Path(path)
        return (candidate.is_absolute() and candidate == candidate.resolve()
                and candidate.is_relative_to(Path(root)) and Path(root) == Path(root).resolve())
    except (OSError, ValueError):
        return False


def _codex_config(content: str) -> dict:
    # Reuse the bootstrap policy for retained config; no credential/endpoint overrides.
    keys = {"model", "model_reasoning_effort", "sandbox_mode", "approval_policy",
            "developer_instructions", "check_for_update_on_startup", "cli_auth_credentials_store",
            "sandbox_workspace_write", "projects", "features"}
    try: config = tomllib.loads(content)
    except ValueError: raise AccountError("account_config_not_allowlisted") from None
    if set(config) - keys or config.get("cli_auth_credentials_store", "file") != "file":
        raise AccountError("account_config_not_allowlisted")
    return config


class Registry:
    def __init__(self, path: Path, root: Path, adopted: Mapping[str, str] | None = None):
        self.path, self.root = Path(path), Path(root)
        self.adopted = dict(adopted or {})
        if any(not Path(home).is_absolute() for home in self.adopted):
            raise AccountError("account_path_not_absolute")
        self._lock_guard = threading.RLock()
        self._lock_fd = None
        if not self.path.is_absolute() or not self.root.is_absolute():
            raise AccountError("account_path_not_absolute")
        _private(self.path.parent, directory=True)

    @contextlib.contextmanager
    def lock(self):
        # One host lock also serializes shared browser-profile use. Held across
        # native authorization, identity proof and activation by the executor.
        with self._lock_guard:
            if self._lock_fd is not None:
                yield
                return
            fd = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                    raise AccountError("account_lock_insecure")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._lock_fd = fd
                yield
            except BlockingIOError:
                raise AccountError("account_busy") from None
            finally:
                self._lock_fd = None
                os.close(fd)

    def _load(self) -> dict:
        if not self.path.exists():
            if self.path.is_symlink(): raise AccountError("account_symlink_forbidden")
            return {"schema": 1, "active": {}, "generations": {}}
        _private(self.path, directory=False)
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with os.fdopen(fd, encoding="utf8") as stream:
                value = json.load(stream)
        except (ValueError, OSError):
            raise AccountError("account_registry_invalid") from None
        if value.get("schema") != 1 or not isinstance(value.get("active"), dict) or not isinstance(value.get("generations"), dict):
            raise AccountError("account_registry_invalid")
        return value

    def _save(self, data: dict) -> None:
        write_private_json(self.path, data)

    def _validate_home(self, binding: Binding) -> None:
        state=self._load()
        anchor=None
        if binding.home not in self.adopted and Path(binding.home).name!=binding.generation:
            if state["generations"].get(binding.key)!=asdict(binding):
                raise AccountError("account_home_outside_registry")
            for raw in state["generations"].values():
                candidate=Binding(**raw)
                if candidate.home==binding.home and Path(candidate.home)==self.root/candidate.host/candidate.provider/candidate.alias/candidate.generation:
                    anchor=candidate
                    break
            if anchor is None: raise AccountError("account_home_outside_registry")
        validate_home(binding, self.root, self.adopted, _registry_anchor=anchor)
        if binding.home in self.adopted:
            owner = (binding.host, binding.provider, binding.alias)
            # Historical generations retain ownership across config edits/restarts.
            for row in self._load()["generations"].values():
                if row["home"] == binding.home and (row["host"], row["provider"], row["alias"]) != owner:
                    raise AccountError("account_home_owner_conflict")

    @_serialized
    def prepare(self, *, provider: str, alias: str, host: str, generation: str,
                profile_ref: str, executable: str, version: str, platform: str, observer_executable: str | None = None,
                home: str | None = None) -> Binding:
        for value in (provider, alias, host, generation): _component(value)
        if not isinstance(profile_ref, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", profile_ref):
            raise AccountError("account_profile_invalid")
        if provider not in {"codex", "claude"} or platform not in {"macos", "wsl"}:
            raise AccountError("unsupported_account_provider_platform")
        if home is not None:
            # Adoption never creates or renames a home; it must be configured and already private.
            home = str(Path(home))
            if home not in self.adopted: raise AccountError("account_home_outside_registry")
            home = Path(home); _private(home, directory=True)
        else:
            home = self.root / host / provider / alias / generation
            if home.exists() or home.is_symlink():
                raise AccountError("account_generation_exists")
            _mkdir(home)
        result = Binding(provider, alias, host, str(home), generation, profile_ref, executable, version, platform)
        from dataclasses import replace
        try: result = replace(result, executable_sha256=hashlib.sha256(Path(executable).read_bytes()).hexdigest())
        except OSError: raise AccountError("account_executable_unpinned") from None
        if observer_executable is not None:
            if not Path(observer_executable).is_absolute(): raise AccountError("account_executable_unpinned")
            try: digest = hashlib.sha256(Path(observer_executable).read_bytes()).hexdigest()
            except OSError: raise AccountError("account_executable_unpinned") from None
            result = replace(result, observer_executable=observer_executable, observer_executable_sha256=digest)
        self._validate_home(result)
        return result

    @_serialized
    def provision(self, draft: Binding, manifest: Mapping[str, str]) -> None:
        self._validate_home(draft)
        if draft.home in self.adopted:
            retained = Path(draft.home) / "config.toml"
            if retained.exists() or retained.is_symlink():
                _private(retained, directory=False)
                try: _codex_config(retained.read_text(encoding="utf8"))
                except (OSError, UnicodeError):
                    raise AccountError("account_config_not_allowlisted") from None
        # Config keys are a positive allowlist; custom providers, API helpers,
        # hooks, OAuth overrides and alternate endpoints are never imported.
        claude_keys = {"permissions", "model", "language", "outputStyle", "forceLoginMethod"}
        prepared = {}
        for name, content in manifest.items():
            path = Path(name)
            if (path.is_absolute() or ".." in path.parts or not isinstance(content, str)
                    or not (name in {"AGENTS.md", "CLAUDE.md", "config.toml", "settings.json"}
                            or re.fullmatch(r"skills/[a-zA-Z0-9_-]+/(?:[a-zA-Z0-9_-]+/)*[a-zA-Z0-9_.-]+\.(?:md|py|sh|json|svg|txt)", name))):
                raise AccountError("account_bootstrap_not_allowlisted")
            if path.name in {"auth.json", ".credentials.json", "credentials.json"}:
                raise AccountError("account_bootstrap_not_allowlisted")
            if name == "config.toml":
                config = _codex_config(content)
                if "cli_auth_credentials_store" not in config:
                    content += '\ncli_auth_credentials_store = "file"\n'
            if name == "settings.json":
                config = json.loads(content)
                if (not isinstance(config, dict) or set(config) - claude_keys
                        or config.get("forceLoginMethod", "claudeai") != "claudeai"):
                    raise AccountError("account_config_not_allowlisted")
                config["forceLoginMethod"] = "claudeai"
                content = json.dumps(config)
            prepared[name] = content
        if draft.provider == "codex" and "config.toml" not in prepared:
            prepared["config.toml"] = 'cli_auth_credentials_store = "file"\n'
        for name, content in prepared.items():
            path = Path(draft.home) / name
            _mkdir(path.parent)
            if path.exists() or path.is_symlink():
                _private(path, directory=False)
                if draft.home in self.adopted: continue  # adopted homes keep existing files
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream: stream.write(content)

    @_serialized
    def activate(self, draft: Binding, receipt: Mapping, *, expected_provider_id: str) -> Binding:
        self._validate_home(draft)
        if any(row["home"]==draft.home for row in self._load()["generations"].values()):
            raise AccountError("account_generation_exists")
        store = "keychain" if draft.provider == "claude" and draft.platform == "macos" else "file"
        if (receipt.get("outcome", "ok") != "ok" or receipt.get("auth_mode") != "subscription"
                or receipt.get("provider_id") != expected_provider_id or not expected_provider_id
                or not isinstance(receipt.get("pool_id"), str) or not receipt["pool_id"]
                or receipt.get("store") != store or receipt.get("minimal_turn") is not True
                or receipt.get("adopted_home" if draft.home in self.adopted else "independent_login") is not True):
            raise AccountError("account_enrollment_unverified")
        if store == "file":
            _private(Path(draft.home) / ("auth.json" if draft.provider == "codex" else ".credentials.json"), directory=False)
        else:
            fallback = Path(draft.home) / ".credentials.json"
            if fallback.exists() or fallback.is_symlink(): raise AccountError("unexpected_store_fallback")
        from dataclasses import replace
        result = replace(draft, provider_id=expected_provider_id, pool_id=receipt["pool_id"], enabled=True, healthy=True)
        state = self._load()
        if result.key in state["generations"]:
            raise AccountError("account_generation_exists")
        # No generation rename: Claude Keychain identity includes the final home.
        state["generations"][result.key] = asdict(result)
        state["active"][f"{result.host}:{result.provider}:{result.alias}"] = result.key
        self._save(state)
        return result

    def active(self, provider: str, alias: str, host: str) -> Binding | None:
        state = self._load()
        key = state["active"].get(f"{host}:{provider}:{alias}")
        if not key: return None
        binding = Binding(**state["generations"][key])
        self._validate_home(binding)
        return binding

    def bindings(self) -> list[Binding]:
        state = self._load()
        result = [Binding(**state["generations"][key]) for key in state["active"].values()]
        for binding in result: self._validate_home(binding)
        return result

    @_serialized
    def repin(self, old: Binding, successor: Binding) -> Binding:
        state=self._load()
        if state["active"].get(f"{old.host}:{old.provider}:{old.alias}")!=old.key:
            raise AccountError("account_login_generation_stale")
        self._validate_home(old)
        if successor.key in state["generations"]: raise AccountError("account_generation_exists")
        changed={"generation","executable","executable_sha256","version"}
        if any(getattr(old,k)!=getattr(successor,k) for k in asdict(old) if k not in changed):
            raise AccountError("account_repin_binding_invalid")
        state["generations"][successor.key]=asdict(successor)
        state["active"][f"{old.host}:{old.provider}:{old.alias}"]=successor.key
        self._save(state)
        return successor


def write_private_json(path: Path, data: dict):
    _private(path.parent, directory=True)
    if path.exists() or path.is_symlink(): _private(path, directory=False)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".accounts-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(data, stream, separators=(",", ":")); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(parent)
        finally: os.close(parent)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
