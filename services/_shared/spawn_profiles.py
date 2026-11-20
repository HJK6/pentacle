"""Canonical spawn model/effort profiles shared by agent-orch and chat_streamd.

This module deliberately has no environment or provider-CLI dependencies: a
spawn request is resolved before it is serialized, then the daemon validates
the complete tuple without silently consulting host defaults.
"""
from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
import copy
import logging
import threading
from pathlib import Path
from typing import Any


CATALOG_VERSION = "spawn-catalog-v2"
SCHEMA_VERSION = "SpawnRequestV2"
HANDOFF_TUPLE_FIELDS = ("provider", "model", "effort", "role")
KNOWN_PROVIDERS = ("cursor", "claude", "gemini", "codex")
SPAWN_PROFILES = ("agent_orch", "desktop_manual")
DEFAULT_MAX_CONCURRENT_BOOTS = 3
DEFAULT_SPAWN_QUEUE_TIMEOUT_SECONDS = 180
SPAWN_DEFAULTS_PATH = Path(__file__).resolve().with_name("spawn_defaults.json")
# spawn_defaults.json owns model IDs, aliases and intentional omissions.
# Aliases follow the selected generation; older full IDs remain compatible.
# Picker availability narrows advertising only, not explicit spawns/handoffs.
# Opus stays pinned to the default; Codex ultra remains excluded fleet-wide.
log = logging.getLogger(__name__)
_cache_lock = threading.Lock()
_cache_signature = object()
_last_good = None
_load_error = None
_known_ids = {}


@dataclass(frozen=True)
class SpawnProfileError(ValueError):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class HandoffResolution:
    source: dict[str, Any]
    spawn: dict[str, Any]
    changed_fields: tuple[str, ...]
    policy: dict[str, Any]

    @property
    def changed(self) -> bool:
        return bool(self.changed_fields)


def _config_error(message: str) -> SpawnProfileError:
    return SpawnProfileError("spawn_config_invalid", message)


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _config_error(f"{field} must be a positive integer")
    return value


def _config_tuple(provider: str, values: object, model_catalog: dict) -> tuple[str, str]:
    if not isinstance(values, dict):
        raise _config_error(f"missing {provider} default")
    model = values.get("model")
    effort = values.get("effort")
    if not isinstance(model, str) or not isinstance(effort, str):
        raise _config_error(f"invalid {provider} default")
    try:
        canonical = _canonical_model(provider, model, model_catalog)
    except SpawnProfileError as exc:
        raise _config_error(str(exc)) from exc
    if effort not in model_catalog[provider][canonical]["efforts"]:
        raise _config_error(f"unsupported {provider} default tuple: {canonical}/{effort}")
    return canonical, effort


def _load_spawn_config() -> dict[str, Any]:
    """Parse and validate one candidate, without consulting the live cache."""
    try:
        raw = json.loads(SPAWN_DEFAULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _config_error(f"cannot load spawn defaults: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise _config_error("unsupported spawn defaults schema")
    if not isinstance(raw.get('host_overrides', {}), dict):
        raise _config_error('spawn defaults host_overrides must be an object')
    profiles = raw.get('profiles')
    if not isinstance(profiles, dict) or not isinstance(profiles.get('agent_orch'), dict):
        raise _config_error('agent_orch handoff policy is required')
    # Deployment-owned per-host limits live beside the shipped policy in
    # ``spawn_defaults.local.json`` (never shipped); host overrides are merged
    # and optional picker availability replaces the shipped map.
    local_path = SPAWN_DEFAULTS_PATH.with_name("spawn_defaults.local.json")
    if local_path.exists():
        try:
            local = json.loads(local_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise _config_error(f"cannot load local spawn defaults: {exc}") from exc
        if (
            not isinstance(local, dict)
            or local.get("schema_version") != 1
            or set(local) - {"schema_version", "host_overrides", "available_models", "profiles", "models", "intentionally_unspawnable"}
            or not isinstance(local.get("host_overrides", {}), dict)
        ):
            raise _config_error("unsupported local spawn defaults")
        raw["host_overrides"] = {**raw.get("host_overrides", {}), **local.get("host_overrides", {})}
        for key in ("available_models", "models", "intentionally_unspawnable"):
            if key not in local:
                continue
            value = local[key]
            # None is the existing explicit full-picker policy.
            if key == 'available_models' and value is None:
                raw[key] = None
                continue
            if not isinstance(value, dict) or set(value) - set(KNOWN_PROVIDERS):
                raise _config_error(f"invalid local {key}")
            shipped = raw.get(key) or {}
            if not isinstance(shipped, dict):
                raise _config_error(f"invalid {key}")
            raw[key] = {**shipped, **value}
        if "profiles" in local:
            profiles = local["profiles"]
            if not isinstance(profiles, dict) or set(profiles) != {"test_prompt"}:
                raise _config_error("local profiles may only override test_prompt")
            pairs = profiles["test_prompt"]
            if not isinstance(pairs, dict) or set(pairs) - set(KNOWN_PROVIDERS):
                raise _config_error("invalid local test_prompt profile")
            # Each provider pair replaces the entire shipped pair, never fills
            # a missing field from a different policy revision.
            shipped_pairs = raw['profiles'].get('test_prompt', {})
            if not isinstance(shipped_pairs, dict):
                raise _config_error('invalid shipped test_prompt profile')
            raw['profiles']['test_prompt'] = {**shipped_pairs, **pairs}

    model_catalog = _validate_catalog(raw)
    providers = raw.get("providers")
    if not isinstance(providers, dict) or set(providers) - set(KNOWN_PROVIDERS):
        raise _config_error("spawn defaults providers must be an object")
    for provider in KNOWN_PROVIDERS:
        if provider in {"gemini", "cursor"} and provider not in providers:
            continue  # Older deployment policies remain two-provider policies.
        _config_tuple(provider, providers.get(provider), model_catalog)
    _positive_int(raw.get("max_concurrent_boots"), "max_concurrent_boots")
    _positive_int(raw.get("spawn_queue_timeout_seconds"), "spawn_queue_timeout_seconds")
    overrides = raw.get("host_overrides", {})
    if not isinstance(overrides, dict):
        raise _config_error("spawn defaults host_overrides must be an object")
    for host, provider_overrides in overrides.items():
        if not isinstance(host, str) or not isinstance(provider_overrides, dict):
            raise _config_error("invalid host override")
        for provider, partial in provider_overrides.items():
            if provider in {"max_concurrent_boots", "spawn_queue_timeout_seconds"}:
                _positive_int(partial, f"host_overrides[{host}].{provider}")
                continue
            if provider not in model_catalog or provider not in providers or not isinstance(partial, dict):
                raise _config_error("invalid provider host override")
            merged = {**providers[provider], **partial}
            _config_tuple(provider, merged, model_catalog)
    if raw.get("available_models") is not None:
        _validate_available_models(raw["available_models"], model_catalog)
        if not _available_model_ids(raw):
            raise _config_error("available_models must advertise at least one provider")
    profiles = raw.get("profiles", {})
    handoff = profiles.get("agent_orch", {}).get("handoff") if isinstance(profiles, dict) else None
    if not isinstance(handoff, dict):
        raise _config_error("agent_orch handoff policy is required")
    _validate_test_pairs(profiles.get('test_prompt'), model_catalog)
    return raw


def _validate_catalog(config):
    raw = config.get('models')
    if not isinstance(raw, dict) or set(raw) != set(KNOWN_PROVIDERS):
        raise _config_error('models must configure the known providers')
    normalized = {}
    allowed_efforts = {'low', 'medium', 'high', 'xhigh', 'max', 'default'}
    for provider, entries in raw.items():
        if not isinstance(entries, dict) or not entries:
            raise _config_error(f'invalid {provider} model catalog')
        aliases_seen = {}
        normalized[provider] = {}
        for canonical, entry in entries.items():
            if not isinstance(canonical, str) or not canonical.strip() or not isinstance(entry, dict):
                raise _config_error(f'invalid {provider} model definition')
            aliases, efforts = entry.get('aliases'), entry.get('efforts')
            if (not isinstance(aliases, list) or not aliases
                    or any(not isinstance(a, str) or not a.strip() for a in aliases)
                    or canonical not in aliases):
                raise _config_error(f'invalid aliases for {provider}/{canonical}')
            if (not isinstance(efforts, list) or not efforts
                    or any(not isinstance(e, str) or e not in allowed_efforts for e in efforts)):
                raise _config_error(f'invalid efforts for {provider}/{canonical}')
            for alias in aliases:
                if alias in aliases_seen and aliases_seen[alias] != canonical:
                    raise _config_error(f'duplicate alias {provider}/{alias}')
                aliases_seen[alias] = canonical
            normalized[provider][canonical] = {'aliases': tuple(aliases), 'efforts': tuple(efforts)}
    omitted = config.get('intentionally_unspawnable', {})
    if not isinstance(omitted, dict) or set(omitted) - set(KNOWN_PROVIDERS):
        raise _config_error('invalid intentionally_unspawnable providers')
    for provider, ids in omitted.items():
        if (not isinstance(ids, list) or any(not isinstance(i, str) or not i.strip() for i in ids)
                or set(ids) & set(normalized[provider])):
            raise _config_error(f'invalid intentionally_unspawnable for {provider}')
    config['models'] = normalized
    return normalized


def _file_signature(path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size, stat.st_ino


def _warn_catalog_gaps(config):
    for provider, ids in _known_ids.items():
        gap = ids - set(config['models'][provider]) - set(config.get('intentionally_unspawnable', {}).get(provider, ()))
        if gap:
            log.warning('spawn_catalog_gap provider=%s ids=%s', provider, ','.join(sorted(gap)))


def register_known_ids(provider, ids):
    if provider not in KNOWN_PROVIDERS:
        raise SpawnProfileError('spawn_provider_unavailable', f'unsupported provider: {provider}')
    with _cache_lock:
        _known_ids[provider] = frozenset(ids)
        if _last_good is not None:
            _warn_catalog_gaps(_last_good)


def load_spawn_config() -> dict[str, Any]:
    """Stat on every call; reject each bad revision once and retain last-good."""
    global _cache_signature, _last_good, _load_error
    with _cache_lock:
        try:
            signature = (str(SPAWN_DEFAULTS_PATH), _file_signature(SPAWN_DEFAULTS_PATH),
                         _file_signature(SPAWN_DEFAULTS_PATH.with_name('spawn_defaults.local.json')))
        except OSError as exc:
            # A persistent stat error must not disable an already-valid policy.
            signature = (str(SPAWN_DEFAULTS_PATH), type(exc).__name__, exc.errno)
            error = _config_error(f'cannot stat spawn defaults: {exc}')
        else:
            error = None
        if signature != _cache_signature:
            _cache_signature = signature
            try:
                if error is not None:
                    raise error
                candidate = _load_spawn_config()
            except SpawnProfileError as exc:
                _load_error = exc
                log.warning('spawn_config_reload_rejected: %s', exc)
            else:
                _last_good, _load_error = candidate, None
                _warn_catalog_gaps(candidate)
        if _last_good is None:
            raise _load_error
        # Callers cannot accidentally mutate the cache or its last-good value.
        return copy.deepcopy(_last_good)


def _clear_spawn_config_cache():
    global _cache_signature, _last_good, _load_error
    with _cache_lock:
        _cache_signature, _last_good, _load_error = object(), None, None


load_spawn_config.cache_clear = _clear_spawn_config_cache


def models() -> dict:
    return load_spawn_config()['models']


def __getattr__(name):
    if name == 'MODELS':
        return models()
    if name == 'INTENTIONALLY_UNSPAWNABLE':
        return {provider: frozenset(ids) for provider, ids in load_spawn_config().get('intentionally_unspawnable', {}).items()}
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def _profile_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _validate_test_pairs(pairs, model_catalog):
    if not isinstance(pairs, dict) or set(pairs) != set(KNOWN_PROVIDERS):
        raise _config_error("test_prompt must explicitly configure or disable every provider")
    for provider, pair in pairs.items():
        if pair is None:
            continue
        if not isinstance(pair, dict) or set(pair) != {"model", "effort"}:
            raise _config_error("test_prompt requires complete model/effort pairs")
        if _config_tuple(provider, pair, model_catalog) != (pair["model"], pair["effort"]):
            raise _config_error("test_prompt requires canonical model ids")


def validate_test_profile(snapshot: object, *, _catalog=None) -> dict[str, Any]:
    """Validate a frozen run artifact without consulting mutable defaults."""
    fields = {"schema", "providers", "profile_sha256", "settings_sha256", "resolver_sha256"}
    if (not isinstance(snapshot, dict) or set(snapshot) != fields
            or snapshot.get("schema") != "pentacle.test-profile.v1"):
        raise _config_error("invalid test profile snapshot")
    pairs = snapshot["providers"]
    _validate_test_pairs(pairs, _catalog if _catalog is not None else models())
    def sha(value):
        return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    settings = snapshot["settings_sha256"]
    if (not isinstance(settings, dict) or set(settings) != {"spawn_defaults.json", "spawn_defaults.local.json"}
            or not sha(settings["spawn_defaults.json"])
            or settings["spawn_defaults.local.json"] is not None and not sha(settings["spawn_defaults.local.json"])
            or not sha(snapshot["resolver_sha256"])
            or snapshot["profile_sha256"] != _profile_digest(pairs)):
        raise _config_error("test profile identity mismatch")
    return json.loads(json.dumps(snapshot))


def resolve_test_profile() -> dict[str, Any]:
    """Read fresh settings once per test run; never use work defaults/fallbacks."""
    if not SPAWN_DEFAULTS_PATH.is_file():
        raise _config_error("test_prompt settings are missing")
    # Bind identities to the exact bytes parsed, detecting concurrent writers.
    paths = {"spawn_defaults.json": SPAWN_DEFAULTS_PATH,
             "spawn_defaults.local.json": SPAWN_DEFAULTS_PATH.with_name("spawn_defaults.local.json")}
    before = {name: hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None for name, p in paths.items()}
    config = _load_spawn_config()
    after = {name: hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None for name, p in paths.items()}
    if before != after:
        raise _config_error("test_prompt settings changed during resolution")
    pairs = config.get("profiles", {}).get("test_prompt")
    return validate_test_profile({"schema": "pentacle.test-profile.v1", "providers": pairs,
        "profile_sha256": _profile_digest(pairs), "settings_sha256": before,
        "resolver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}, _catalog=config["models"])


def test_prompt_tuple(provider: str, snapshot: dict[str, Any] | None = None) -> tuple[str, str]:
    profile = resolve_test_profile() if snapshot is None else validate_test_profile(snapshot)
    pair = profile["providers"].get(provider)
    if pair is None:
        raise _config_error(f"test_prompt is disabled for {provider}")
    return pair["model"], pair["effort"]


def _available_model_ids(config: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """Model ids advertised to interactive New Chat clients, per provider.

    Account-specific availability is installation policy: an optional
    ``available_models`` map in the spawn config (provider -> list of canonical
    model ids, each a subset of ``MODELS``) narrows the picker to the currently
    launchable set. Absent — the shipped public default — the full ``MODELS``
    catalog is advertised. A missing provider key keeps its full catalog; an
    empty list hides the provider. Full ``MODELS`` remains launchable.
    ``load_spawn_config`` validates every provided id.
    """
    configured = config.get("available_models")
    result: dict[str, tuple[str, ...]] = {}
    for provider in config["models"]:
        if provider not in config["providers"]:
            continue
        ids = configured.get(provider) if isinstance(configured, dict) else None
        if ids == []:
            continue
        result[provider] = tuple(ids) if ids is not None else tuple(config["models"][provider])
    return result


def _validate_available_models(available: object, model_catalog: dict) -> None:
    if not isinstance(available, dict):
        raise _config_error("available_models must be an object")
    for provider, ids in available.items():
        if provider not in model_catalog:
            raise _config_error(f"available_models lists unknown provider: {provider}")
        if not isinstance(ids, list):
            raise _config_error(f"available_models[{provider}] must be a list")
        for model_id in ids:
            if not isinstance(model_id, str) or model_id not in model_catalog[provider]:
                raise _config_error(
                    f"available_models[{provider}] lists unknown model: {model_id}"
                )


def _default_tuple(provider: str, host: str | None, config=None) -> tuple[str, str]:
    config = load_spawn_config() if config is None else config
    providers = config["providers"]
    values = dict(providers.get(provider) or {})
    if host:
        host_overrides = config.get("host_overrides", {})
        override = host_overrides.get(host, {}).get(provider, {})
        if isinstance(override, dict):
            values.update(override)
    return _config_tuple(provider, values, config["models"])


def boot_limits(host: str | None = None) -> tuple[int, int]:
    """Return the Codex boot cap and bounded queue wait for one host."""
    config = load_spawn_config()
    cap = _positive_int(config.get("max_concurrent_boots"), "max_concurrent_boots")
    timeout = _positive_int(
        config.get("spawn_queue_timeout_seconds"), "spawn_queue_timeout_seconds",
    )
    if host:
        override = config.get("host_overrides", {}).get(host, {})
        if isinstance(override, dict):
            if "max_concurrent_boots" in override:
                cap = _positive_int(
                    override["max_concurrent_boots"],
                    f"host_overrides[{host}].max_concurrent_boots",
                )
            if "spawn_queue_timeout_seconds" in override:
                timeout = _positive_int(
                    override["spawn_queue_timeout_seconds"],
                    f"host_overrides[{host}].spawn_queue_timeout_seconds",
                )
    return cap, timeout


def _handoff_policy(config=None) -> dict[str, Any]:
    config = load_spawn_config() if config is None else config
    raw = config.get("profiles", {}).get("agent_orch", {}).get("handoff")
    expected = {
        "inheritance": "source_effective",
        "missing_effective": "reject",
        "tuple_change": "warn_and_proceed",
        "confirmation_flag": "--confirm-model-change",
        "confirmation_flag_effect": "suppress_warning",
    }
    if not isinstance(raw, dict) or raw != expected:
        raise _config_error("invalid agent_orch handoff policy")
    return dict(raw)


def _canonical_model(provider: str, model: str, model_catalog: dict) -> str:
    for canonical, entry in model_catalog.get(provider, {}).items():
        if model in entry["aliases"]:
            return canonical
    raise SpawnProfileError("spawn_model_unsupported", f"unsupported {provider} model: {model}")


def canonical_model(provider: str, model: str) -> str:
    return _canonical_model(provider, model, models())


def resolve_spawn(*, provider: str, model: str | None = None, effort: str | None = None,
                  spawn_profile: str = "agent_orch", host: str | None = None,
                  legacy: bool = False, _config=None) -> dict[str, str]:
    if spawn_profile not in SPAWN_PROFILES:
        raise SpawnProfileError("spawn_profile_unknown", f"unknown spawn profile: {spawn_profile}")
    config = load_spawn_config() if _config is None else _config
    model_catalog = config["models"]
    if provider not in model_catalog:
        raise SpawnProfileError("spawn_provider_unavailable", f"unsupported provider: {provider}")
    default_model, default_effort = _default_tuple(provider, host, config)
    canonical = _canonical_model(provider, model, model_catalog) if model else default_model
    resolved_effort = effort or default_effort
    allowed = model_catalog[provider][canonical]["efforts"]
    if resolved_effort not in allowed:
        known = {item for entry in model_catalog[provider].values() for item in entry["efforts"]}
        code = "spawn_effort_unsupported" if resolved_effort not in known else "spawn_pair_unsupported"
        raise SpawnProfileError(code, f"unsupported {provider} effort/model pair: {resolved_effort}/{canonical}")
    return {
        "schema": SCHEMA_VERSION,
        "spawn_profile": spawn_profile,
        "provider": provider,
        "model": canonical,
        "effort": resolved_effort,
        "catalog_version": CATALOG_VERSION,
        "resolution_source": "legacy_server_fallback" if legacy else ("explicit_override" if model or effort else "profile_default"),
    }


def require_spawnable(provider: str, model: str, *, _config=None) -> None:
    """Fresh admission only; known-model resolution remains continuation-safe."""
    config = load_spawn_config() if _config is None else _config
    canonical = _canonical_model(provider, model, config["models"])
    if config["models"][provider][canonical].get("spawnable") is False:
        raise SpawnProfileError("spawn_model_retired", f"retired model is unavailable for new spawns: {canonical}")



def _normalize_handoff_role(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def resolve_handoff(
    *,
    source_provider: str | None,
    source_model: str | None,
    source_effort: str | None,
    source_role: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    role: str | None = None,
    host: str | None = None,
) -> HandoffResolution:
    config = load_spawn_config()
    policy = _handoff_policy(config)
    normalized_source_role = _normalize_handoff_role(source_role)
    source_values = {
        "provider": str(source_provider or "").strip(),
        "model": str(source_model or "").strip(),
        "effort": str(source_effort or "").strip(),
    }
    missing = tuple(field for field, value in source_values.items() if not value)
    if missing:
        raise SpawnProfileError(
            "handoff_effective_tuple_missing",
            f"retiring stream effective tuple missing: {','.join(missing)}",
        )
    source_resolved = resolve_spawn(
        provider=source_values["provider"],
        model=source_values["model"],
        effort=source_values["effort"],
        host=host, _config=config,
    )
    target_resolved = resolve_spawn(
        provider=provider or source_resolved["provider"],
        model=model if model is not None else source_resolved["model"],
        effort=effort if effort is not None else source_resolved["effort"],
        host=host, _config=config,
    )
    requested_role = _normalize_handoff_role(role)
    target_role = requested_role if requested_role is not None else normalized_source_role
    source = {
        "provider": source_resolved["provider"],
        "model": source_resolved["model"],
        "effort": source_resolved["effort"],
        "role": normalized_source_role,
    }
    target_resolved["role"] = target_role
    changed_fields = tuple(
        field for field in HANDOFF_TUPLE_FIELDS
        if target_resolved[field] != source[field]
    )
    target_resolved["resolution_source"] = (
        "handoff_warn_and_proceed" if changed_fields else "handoff_inherited"
    )
    return HandoffResolution(
        source=source,
        spawn=target_resolved,
        changed_fields=changed_fields,
        policy=policy,
    )


def validate_v2(*, schema: str, provider: str, model: str, effort: str,
                spawn_profile: str, catalog_version: str, resolution_source: str,
                host: str | None = None) -> dict[str, str]:
    """Validate a complete wire tuple without re-resolving its provenance."""
    if schema != SCHEMA_VERSION:
        raise SpawnProfileError("spawn_schema_unsupported", f"unsupported spawn schema: {schema}")
    if catalog_version != CATALOG_VERSION:
        raise SpawnProfileError("spawn_catalog_version_conflict", "spawn catalog version does not match daemon catalog")
    config = load_spawn_config()
    resolved = resolve_spawn(
        provider=provider,
        model=model,
        effort=effort,
        spawn_profile=spawn_profile,
        host=host, _config=config,
    )
    if resolution_source not in {
        "profile_default",
        "explicit_override",
        "legacy_server_fallback",
        "handoff_inherited",
        "handoff_warn_and_proceed",
    }:
        raise SpawnProfileError("spawn_request_invalid", "invalid V2 resolution source")
    defaults = _default_tuple(provider, host, config)
    if resolution_source == "profile_default" and (model, effort) != defaults:
        raise SpawnProfileError("spawn_request_invalid", "profile_default tuple differs from profile defaults")
    if resolution_source == "legacy_server_fallback" and (model, effort) != defaults:
        raise SpawnProfileError("spawn_request_invalid", "legacy fallback tuple differs from profile defaults")
    resolved["resolution_source"] = resolution_source
    return resolved


def catalog() -> dict:
    config = load_spawn_config()
    result = {
        "schema_version": "CatalogV1",
        "catalog_version": CATALOG_VERSION,
        "profiles": {
            profile: {provider: _default_tuple(provider, None, config) for provider in config["providers"] if provider in config["models"]}
            for profile in SPAWN_PROFILES
        },
        "spawn_defaults": {
            "schema_version": config["schema_version"],
            "max_concurrent_boots": config["max_concurrent_boots"],
            "spawn_queue_timeout_seconds": config["spawn_queue_timeout_seconds"],
            "providers": config["providers"],
            "host_overrides": config.get("host_overrides", {}),
            "profiles": config.get("profiles", {}),
        },
        "models": config["models"],
        "available_models": {
            provider: {model: config["models"][provider][model] for model in model_ids}
            for provider, model_ids in _available_model_ids(config).items()
        },
    }

    try:
        test_profile = resolve_test_profile()
        if test_profile['providers'] != config.get('profiles', {}).get('test_prompt'):
            raise _config_error('test_prompt settings changed during catalog resolution')
    except SpawnProfileError as exc:
        # A missing/invalid test policy disables test consumers, while the
        # existing ordinary work catalog remains usable with older policies.
        # Test builders require a validated snapshot and never fall back.
        result["test_prompt_error"] = {"code": exc.code, "message": str(exc)}
    else:
        result["profiles"]["test_prompt"] = {
            provider: (pair["model"], pair["effort"]) if pair else None
            for provider, pair in test_profile["providers"].items()}
        result["test_prompt_profile"] = test_profile
        result["spawn_defaults"]["profiles"] = {
            **config.get("profiles", {}), "test_prompt": test_profile["providers"]}
    return result


def unspawnable_gap(provider: str, known_ids) -> set[str]:
    """Catalog-drift guard: ids the daemon knows (``known_ids``, e.g. a caller's
    context-window map) that are neither spawnable in ``MODELS`` nor listed in
    ``INTENTIONALLY_UNSPAWNABLE``. An empty set means the spawn catalog and the
    caller's model list agree; a non-empty set is drift — a model shipped and was
    wired for context sizing but never triaged for spawn. Kept hermetic (the known
    set is passed in, not imported) so the module stays free of provider/env deps."""
    config = load_spawn_config()
    if provider not in config['models']:
        raise SpawnProfileError("spawn_provider_unavailable", f"unsupported provider: {provider}")
    return set(known_ids) - set(config['models'][provider]) - set(config.get('intentionally_unspawnable', {}).get(provider, ()))
