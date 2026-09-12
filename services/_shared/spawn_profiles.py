"""Canonical spawn model/effort profiles shared by agent-orch and chat_streamd.

This module deliberately has no environment or provider-CLI dependencies: a
spawn request is resolved before it is serialized, then the daemon validates
the complete tuple without silently consulting host defaults.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


# Compatibility fallback for checkouts that predate spawn_defaults.json. New
# launches resolve their defaults from that deployment-owned policy file.
CATALOG_VERSION = "spawn-catalog-v2"
SCHEMA_VERSION = "SpawnRequestV2"
HANDOFF_TUPLE_FIELDS = ("provider", "model", "effort", "role")
PROFILES = {
    "agent_orch": {
        "claude": ("claude-opus-4-8", "high"),
        "codex": ("gpt-5.6-sol", "high"),
    },
    "desktop_manual": {
        "claude": ("claude-opus-4-8", "high"),
        "codex": ("gpt-5.6-sol", "high"),
    },
}
DEFAULT_MAX_CONCURRENT_BOOTS = 3
DEFAULT_SPAWN_QUEUE_TIMEOUT_SECONDS = 180
SPAWN_DEFAULTS_PATH = Path(__file__).resolve().with_name("spawn_defaults.json")
MODELS = {
    "claude": {
        "claude-opus-4-8": {"aliases": ("opus", "claude-opus-4-8"), "efforts": ("low", "medium", "high", "xhigh", "max")},
        "claude-opus-5": {"aliases": ("opus-5", "claude-opus-5"), "efforts": ("low", "medium", "high", "xhigh", "max")},
        "claude-sonnet-5": {"aliases": ("sonnet", "claude-sonnet-5"), "efforts": ("low", "medium", "high", "xhigh", "max")},
        "claude-fable-5-1": {"aliases": ("fable", "claude-fable-5-1", "claude-fable-5"), "efforts": ("low", "medium", "high", "xhigh", "max")},
    },
    "codex": {
        "gpt-5.6-terra": {"aliases": ("gpt-5.6-terra", "terra"), "efforts": ("low", "medium", "high", "xhigh", "max")},
        "gpt-5.6-sol": {"aliases": ("gpt-5.6-sol", "sol"), "efforts": ("low", "medium", "high", "xhigh", "max")},
        "gpt-5.6-luna": {"aliases": ("gpt-5.6-luna", "luna"), "efforts": ("low", "medium", "high", "xhigh", "max")},
        "gpt-6-astra": {"aliases": ("gpt-6-astra", "astra"), "efforts": ("low", "medium", "high", "xhigh", "max")},
    },
}
# Ids the daemon knows a context window for (see chat-stream context_thresholds)
# but that we deliberately do NOT expose as spawn targets: superseded generations
# you would never launch a fresh lane on. Every such id must be listed here so the
# catalog-consistency guard (unspawnable_gap) can tell an intentional omission from
# drift — a newly released model absent from both MODELS and this set fails the guard.
# The "opus" alias stays pinned to claude-opus-4-8 (the profile default); claude-opus-5
# is reached via "opus-5"/"claude-opus-5" so `--model opus` never diverges from the
# no-model default. "claude-fable-5" stays an alias of claude-fable-5-1 (operator
# ruling 2026-09-01: never launch Fable 5 when 5.1 exists) so old clients and
# handoffs from Fable 5 seats resolve to 5.1 instead of failing.
INTENTIONALLY_UNSPAWNABLE = {
    "claude": frozenset({
        "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5",
        "claude-sonnet-4-6", "claude-haiku-4-5", "claude-fable-5",
    }),
    "codex": frozenset({"gpt-5.4", "gpt-5.5"}),
}


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


def _fallback_spawn_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "max_concurrent_boots": DEFAULT_MAX_CONCURRENT_BOOTS,
        "spawn_queue_timeout_seconds": DEFAULT_SPAWN_QUEUE_TIMEOUT_SECONDS,
        "providers": {
            provider: {"model": model, "effort": effort}
            for provider, (model, effort) in PROFILES["agent_orch"].items()
        },
        "host_overrides": {},
        "profiles": {
            "agent_orch": {
                "handoff": {
                    "inheritance": "source_effective",
                    "missing_effective": "reject",
                    "tuple_change": "warn_and_proceed",
                    "confirmation_flag": "--confirm-model-change",
                    "confirmation_flag_effect": "suppress_warning",
                }
            }
        },
    }


def _config_error(message: str) -> SpawnProfileError:
    return SpawnProfileError("spawn_config_invalid", message)


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _config_error(f"{field} must be a positive integer")
    return value


def _config_tuple(provider: str, values: object) -> tuple[str, str]:
    if not isinstance(values, dict):
        raise _config_error(f"missing {provider} default")
    model = values.get("model")
    effort = values.get("effort")
    if not isinstance(model, str) or not isinstance(effort, str):
        raise _config_error(f"invalid {provider} default")
    canonical = canonical_model(provider, model)
    if effort not in MODELS[provider][canonical]["efforts"]:
        raise _config_error(f"unsupported {provider} default tuple: {canonical}/{effort}")
    return canonical, effort


@lru_cache(maxsize=1)
def load_spawn_config() -> dict[str, Any]:
    """Load the deployment-owned default policy.

    The in-module fallback preserves compatibility for old checkouts which
    predate the config file. A present-but-invalid policy fails closed rather
    than silently launching with an unexpected tuple.
    """
    if not SPAWN_DEFAULTS_PATH.exists():
        return _fallback_spawn_config()
    try:
        raw = json.loads(SPAWN_DEFAULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _config_error(f"cannot load spawn defaults: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise _config_error("unsupported spawn defaults schema")
    # Deployment-owned per-host limits live beside the shipped policy in
    # ``spawn_defaults.local.json`` (never shipped); only ``host_overrides`` is
    # merged, so the shared file stays identical across checkouts.
    local_path = SPAWN_DEFAULTS_PATH.with_name("spawn_defaults.local.json")
    if local_path.exists():
        try:
            local = json.loads(local_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise _config_error(f"cannot load local spawn defaults: {exc}") from exc
        if (
            not isinstance(local, dict)
            or local.get("schema_version") != 1
            or set(local) - {"schema_version", "host_overrides"}
            or not isinstance(local.get("host_overrides", {}), dict)
        ):
            raise _config_error("unsupported local spawn defaults")
        raw["host_overrides"] = {**raw.get("host_overrides", {}), **local.get("host_overrides", {})}
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        raise _config_error("spawn defaults providers must be an object")
    for provider in MODELS:
        _config_tuple(provider, providers.get(provider))
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
            if provider not in MODELS or not isinstance(partial, dict):
                raise _config_error("invalid provider host override")
            merged = {**providers[provider], **partial}
            _config_tuple(provider, merged)
    profiles = raw.get("profiles", {})
    handoff = profiles.get("agent_orch", {}).get("handoff") if isinstance(profiles, dict) else None
    if not isinstance(handoff, dict):
        raise _config_error("agent_orch handoff policy is required")
    return raw


def _default_tuple(provider: str, host: str | None) -> tuple[str, str]:
    config = load_spawn_config()
    providers = config["providers"]
    values = dict(providers[provider])
    if host:
        host_overrides = config.get("host_overrides", {})
        override = host_overrides.get(host, {}).get(provider, {})
        if isinstance(override, dict):
            values.update(override)
    return _config_tuple(provider, values)


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


def _handoff_policy() -> dict[str, Any]:
    raw = load_spawn_config().get("profiles", {}).get("agent_orch", {}).get("handoff")
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


def canonical_model(provider: str, model: str) -> str:
    for canonical, entry in MODELS.get(provider, {}).items():
        if model in entry["aliases"]:
            return canonical
    raise SpawnProfileError("spawn_model_unsupported", f"unsupported {provider} model: {model}")


def resolve_spawn(*, provider: str, model: str | None = None, effort: str | None = None,
                  spawn_profile: str = "agent_orch", host: str | None = None,
                  legacy: bool = False) -> dict[str, str]:
    if spawn_profile not in PROFILES:
        raise SpawnProfileError("spawn_profile_unknown", f"unknown spawn profile: {spawn_profile}")
    if provider not in MODELS:
        raise SpawnProfileError("spawn_provider_unavailable", f"unsupported provider: {provider}")
    default_model, default_effort = _default_tuple(provider, host)
    canonical = canonical_model(provider, model) if model else default_model
    resolved_effort = effort or default_effort
    allowed = MODELS[provider][canonical]["efforts"]
    if resolved_effort not in allowed:
        known = {item for entry in MODELS[provider].values() for item in entry["efforts"]}
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
    policy = _handoff_policy()
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
        host=host,
    )
    target_resolved = resolve_spawn(
        provider=provider or source_resolved["provider"],
        model=model if model is not None else source_resolved["model"],
        effort=effort if effort is not None else source_resolved["effort"],
        host=host,
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
    resolved = resolve_spawn(
        provider=provider,
        model=model,
        effort=effort,
        spawn_profile=spawn_profile,
        host=host,
    )
    if resolution_source not in {
        "profile_default",
        "explicit_override",
        "legacy_server_fallback",
        "handoff_inherited",
        "handoff_warn_and_proceed",
    }:
        raise SpawnProfileError("spawn_request_invalid", "invalid V2 resolution source")
    defaults = _default_tuple(provider, host)
    if resolution_source == "profile_default" and (model, effort) != defaults:
        raise SpawnProfileError("spawn_request_invalid", "profile_default tuple differs from profile defaults")
    if resolution_source == "legacy_server_fallback" and (model, effort) != defaults:
        raise SpawnProfileError("spawn_request_invalid", "legacy fallback tuple differs from profile defaults")
    resolved["resolution_source"] = resolution_source
    return resolved


def catalog() -> dict:
    config = load_spawn_config()
    return {
        "schema_version": "CatalogV1",
        "catalog_version": CATALOG_VERSION,
        "profiles": {
            profile: {provider: _default_tuple(provider, None) for provider in MODELS}
            for profile in PROFILES
        },
        "spawn_defaults": {
            "schema_version": config["schema_version"],
            "max_concurrent_boots": config["max_concurrent_boots"],
            "spawn_queue_timeout_seconds": config["spawn_queue_timeout_seconds"],
            "providers": config["providers"],
            "host_overrides": config.get("host_overrides", {}),
            "profiles": config.get("profiles", {}),
        },
        "models": MODELS,
    }


def unspawnable_gap(provider: str, known_ids) -> set[str]:
    """Catalog-drift guard: ids the daemon knows (``known_ids``, e.g. a caller's
    context-window map) that are neither spawnable in ``MODELS`` nor listed in
    ``INTENTIONALLY_UNSPAWNABLE``. An empty set means the spawn catalog and the
    caller's model list agree; a non-empty set is drift — a model shipped and was
    wired for context sizing but never triaged for spawn. Kept hermetic (the known
    set is passed in, not imported) so the module stays free of provider/env deps."""
    if provider not in MODELS:
        raise SpawnProfileError("spawn_provider_unavailable", f"unsupported provider: {provider}")
    spawnable = set(MODELS[provider])
    allowed = INTENTIONALLY_UNSPAWNABLE.get(provider, frozenset())
    return set(known_ids) - spawnable - allowed
