import json

from _shared import spawn_profiles
from _shared.spawn_profiles import SpawnProfileError, catalog, resolve_handoff, resolve_spawn, validate_v2


def test_sonnet5_and_opus5_are_spawnable_across_all_efforts() -> None:
    for alias, canonical in (
        ("sonnet", "claude-sonnet-5"), ("claude-sonnet-5", "claude-sonnet-5"),
        ("opus-5", "claude-opus-5"), ("claude-opus-5", "claude-opus-5"),
    ):
        for effort in ("low", "medium", "high", "xhigh", "max"):
            resolved = resolve_spawn(provider="claude", model=alias, effort=effort)
            assert (resolved["model"], resolved["effort"]) == (canonical, effort)
            # daemon-side re-validation accepts the same canonical tuple
            revalidated = validate_v2(
                provider="claude", spawn_profile="agent_orch", schema="SpawnRequestV2",
                model=canonical, effort=effort, catalog_version="spawn-catalog-v2",
                resolution_source="explicit_override",
            )
            assert revalidated["model"] == canonical
    # the profile default is unchanged: bare "opus" and no-model both stay opus-4-8
    assert resolve_spawn(provider="claude", model="opus")["model"] == "claude-opus-4-8"
    assert resolve_spawn(provider="claude")["model"] == "claude-opus-4-8"


def test_validate_v2_accepts_alias_and_returns_canonical() -> None:
    resolved = validate_v2(
        provider="claude",
        spawn_profile="agent_orch",
        schema="SpawnRequestV2",
        model="claude-fable-5",
        effort="high",
        catalog_version="spawn-catalog-v2",
        resolution_source="explicit_override",
    )
    assert resolved["model"] == "claude-fable-5-1"
    try:
        validate_v2(
            provider="claude",
            spawn_profile="agent_orch",
            schema="SpawnRequestV2",
            model="claude-fable-6",
            effort="high",
            catalog_version="spawn-catalog-v2",
            resolution_source="explicit_override",
        )
    except SpawnProfileError as exc:
        assert exc.code == "spawn_model_unsupported"
    else:
        raise AssertionError("unknown model was accepted")


def test_luna_is_spawnable_across_all_supported_efforts() -> None:
    for alias in ("luna", "gpt-5.6-luna"):
        for effort in ("low", "medium", "high", "xhigh", "max"):
            resolved = resolve_spawn(provider="codex", model=alias, effort=effort)
            assert (resolved["model"], resolved["effort"]) == ("gpt-5.6-luna", effort)
            revalidated = validate_v2(
                provider="codex", spawn_profile="agent_orch", schema="SpawnRequestV2",
                model="gpt-5.6-luna", effort=effort, catalog_version="spawn-catalog-v2",
                resolution_source="explicit_override",
            )
            assert revalidated["model"] == "gpt-5.6-luna"


def test_all_codex_models_are_spawnable_at_max_effort() -> None:
    for model in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra"):
        resolved = resolve_spawn(provider="codex", model=model, effort="max")
        assert (resolved["model"], resolved["effort"]) == (model, "max")
        revalidated = validate_v2(
            provider="codex", spawn_profile="agent_orch", schema="SpawnRequestV2",
            model=model, effort="max", catalog_version="spawn-catalog-v2",
            resolution_source="explicit_override",
        )
        assert (revalidated["model"], revalidated["effort"]) == (model, "max")


def test_astra_is_cataloged_with_effort_ladder_and_policy_defaults() -> None:
    astra = catalog()["models"]["codex"]["gpt-6-astra"]
    assert astra == {
        "aliases": ("gpt-6-astra", "astra"),
        "efforts": ("low", "medium", "high", "xhigh", "max"),
    }
    for profile in ("agent_orch", "desktop_manual"):
        request = resolve_spawn(provider="codex", spawn_profile=profile)
        assert (request["model"], request["effort"]) == ("gpt-5.6-luna", "max")


def test_astra_alias_resolves_to_canonical_model() -> None:
    request = resolve_spawn(provider="codex", model="astra", effort="high")
    assert request["model"] == "gpt-6-astra"
    assert request["effort"] == "high"


def test_bogus_claude_model_still_rejected() -> None:
    try:
        resolve_spawn(provider="claude", model="claude-opus-6")
    except SpawnProfileError as exc:
        assert exc.code == "spawn_model_unsupported"
    else:
        raise AssertionError("bogus model was accepted")


def test_profiles_derive_same_provider_default_and_legacy_fallback() -> None:
    for profile in ("agent_orch", "desktop_manual"):
        request = resolve_spawn(provider="codex", spawn_profile=profile)
        assert (request["model"], request["effort"], request["resolution_source"]) == (
            "gpt-5.6-luna", "max", "profile_default",
        )
    legacy = resolve_spawn(provider="codex", legacy=True)
    assert (legacy["model"], legacy["effort"], legacy["resolution_source"]) == (
        "gpt-5.6-luna", "max", "legacy_server_fallback",
    )
    for profile in ("agent_orch", "desktop_manual"):
        request = resolve_spawn(provider="claude", spawn_profile=profile)
        assert (request["model"], request["effort"]) == ("claude-opus-4-8", "high")


def test_codex_partial_explicit_overrides_are_pinned_for_both_profiles() -> None:
    cases = (
        (None, None, "gpt-5.6-luna", "max", "profile_default"),
        ("sol", None, "gpt-5.6-sol", "max", "explicit_override"),
        (None, "high", "gpt-5.6-luna", "high", "explicit_override"),
        ("sol", "high", "gpt-5.6-sol", "high", "explicit_override"),
    )
    for profile in ("agent_orch", "desktop_manual"):
        for model, effort, expected_model, expected_effort, source in cases:
            request = resolve_spawn(
                provider="codex", spawn_profile=profile, model=model, effort=effort,
            )
            assert (request["model"], request["effort"], request["resolution_source"]) == (
                expected_model, expected_effort, source,
            )


def test_local_spawn_defaults_overlay_supplies_host_overrides(monkeypatch, tmp_path) -> None:
    """Deployment-owned per-host limits come from spawn_defaults.local.json beside the shipped file."""
    config = json.loads(spawn_profiles.SPAWN_DEFAULTS_PATH.read_text(encoding="utf-8"))
    config["host_overrides"] = {}
    config_path = tmp_path / "spawn_defaults.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "spawn_defaults.local.json").write_text(
        json.dumps({"schema_version": 1, "host_overrides": {"hostc": {"max_concurrent_boots": 5}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(spawn_profiles, "SPAWN_DEFAULTS_PATH", config_path)
    spawn_profiles.load_spawn_config.cache_clear()
    try:
        assert spawn_profiles.boot_limits("hostc")[0] == 5
        assert spawn_profiles.boot_limits("hosta")[0] == config["max_concurrent_boots"]
        assert catalog()["spawn_defaults"]["host_overrides"] == {"hostc": {"max_concurrent_boots": 5}}
    finally:
        spawn_profiles.load_spawn_config.cache_clear()


def test_host_override_and_policy_readback_share_one_config(monkeypatch, tmp_path) -> None:
    config = json.loads(spawn_profiles.SPAWN_DEFAULTS_PATH.read_text(encoding="utf-8"))
    config["host_overrides"] = {"hostc": {"codex": {"model": "gpt-5.6-terra"}}}
    config_path = tmp_path / "spawn_defaults.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(spawn_profiles, "SPAWN_DEFAULTS_PATH", config_path)
    spawn_profiles.load_spawn_config.cache_clear()
    try:
        assert (resolve_spawn(provider="codex", host="hostc")["model"], resolve_spawn(provider="codex", host="hostc")["effort"]) == ("gpt-5.6-terra", "max")
        assert resolve_spawn(provider="codex", host="hosta")["model"] == "gpt-5.6-luna"
        assert validate_v2(
            provider="codex", spawn_profile="agent_orch", schema="SpawnRequestV2",
            model="gpt-5.6-terra", effort="max", catalog_version="spawn-catalog-v2",
            resolution_source="profile_default", host="hostc",
        )["model"] == "gpt-5.6-terra"
        try:
            validate_v2(
                provider="codex", spawn_profile="agent_orch", schema="SpawnRequestV2",
                model="gpt-5.6-sol", effort="max", catalog_version="spawn-catalog-v2",
                resolution_source="profile_default", host="hostc",
            )
        except SpawnProfileError as exc:
            assert exc.code == "spawn_request_invalid"
        else:
            raise AssertionError("daemon accepted an unconfigured host default")
        assert catalog()["spawn_defaults"]["host_overrides"]["hostc"]["codex"]["model"] == "gpt-5.6-terra"
    finally:
        spawn_profiles.load_spawn_config.cache_clear()


def test_explicit_claude_and_codex_efforts_are_canonical() -> None:
    assert resolve_spawn(provider="claude", model="opus", effort="max")["resolution_source"] == "explicit_override"
    assert resolve_spawn(provider="codex", model="sol", effort="xhigh")["model"] == "gpt-5.6-sol"
    terra = resolve_spawn(provider="codex", model="terra", effort="high")
    assert (terra["model"], terra["effort"], terra["resolution_source"]) == (
        "gpt-5.6-terra", "high", "explicit_override",
    )


def test_unknown_tuple_is_rejected_without_fallback() -> None:
    try:
        resolve_spawn(provider="codex", model="gpt-5.6-terra", effort="banana")
    except SpawnProfileError as exc:
        assert exc.code == "spawn_effort_unsupported"
    else:
        raise AssertionError("unsupported effort was accepted")


def test_v2_rejects_forged_default_source_and_unknown_schema() -> None:
    for values, code in (
        ({"schema": "SpawnRequestV2", "model": "gpt-5.6-terra", "effort": "high", "catalog_version": "spawn-catalog-v2", "resolution_source": "profile_default"}, "spawn_request_invalid"),
        ({"schema": "SpawnRequestV3", "model": "gpt-5.6-sol", "effort": "high", "catalog_version": "spawn-catalog-v2", "resolution_source": "profile_default"}, "spawn_schema_unsupported"),
        ({"schema": "SpawnRequestV2", "model": "gpt-5.6-sol", "effort": "high", "catalog_version": "spawn-catalog-v1", "resolution_source": "profile_default"}, "spawn_catalog_version_conflict"),
    ):
        try:
            validate_v2(provider="codex", spawn_profile="agent_orch", **values)
        except SpawnProfileError as exc:
            assert exc.code == code
        else:
            raise AssertionError("forged V2 request was accepted")


def test_handoff_inherits_source_effective_tuple_not_host_defaults(monkeypatch, tmp_path) -> None:
    config = json.loads(spawn_profiles.SPAWN_DEFAULTS_PATH.read_text(encoding="utf-8"))
    config["host_overrides"] = {"hostc": {"codex": {"model": "gpt-5.6-sol", "effort": "high"}}}
    config_path = tmp_path / "spawn_defaults.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(spawn_profiles, "SPAWN_DEFAULTS_PATH", config_path)
    spawn_profiles.load_spawn_config.cache_clear()
    try:
        resolution = resolve_handoff(
            source_provider="codex",
            source_model="gpt-5.6-terra",
            source_effort="low",
            source_role="qa",
            host="hostc",
        )
        assert resolution.spawn["model"] == "gpt-5.6-terra"
        assert resolution.spawn["effort"] == "low"
        assert resolution.source["role"] == "qa"
        assert resolution.spawn["role"] == "qa"
        assert resolution.spawn["resolution_source"] == "handoff_inherited"
        assert resolution.changed_fields == ()
    finally:
        spawn_profiles.load_spawn_config.cache_clear()


def test_handoff_canonicalizes_alias_and_identifies_exact_changes() -> None:
    same = resolve_handoff(
        source_provider="claude",
        source_model="claude-fable-5",
        source_effort="xhigh",
        provider="claude",
        model="fable",
        effort="xhigh",
    )
    assert same.changed is False
    changed = resolve_handoff(
        source_provider="codex",
        source_model="gpt-5.6-sol",
        source_effort="high",
        effort="xhigh",
    )
    assert changed.changed_fields == ("effort",)
    assert changed.spawn["resolution_source"] == "handoff_warn_and_proceed"


def test_handoff_role_only_change_warns_and_proceeds() -> None:
    resolution = resolve_handoff(
        source_provider="codex",
        source_model="gpt-5.6-sol",
        source_effort="high",
        source_role="qa",
        role="nexus",
    )
    assert resolution.changed_fields == ("role",)
    assert resolution.source["role"] == "qa"
    assert resolution.spawn["role"] == "nexus"
    assert resolution.spawn["resolution_source"] == "handoff_warn_and_proceed"


def test_handoff_missing_effective_value_is_rejected() -> None:
    try:
        resolve_handoff(
            source_provider="codex",
            source_model=None,
            source_effort="high",
        )
    except SpawnProfileError as exc:
        assert exc.code == "handoff_effective_tuple_missing"
    else:
        raise AssertionError("missing source effective model was accepted")


def test_handoff_policy_unknown_or_missing_value_is_rejected(monkeypatch, tmp_path) -> None:
    config = json.loads(spawn_profiles.SPAWN_DEFAULTS_PATH.read_text(encoding="utf-8"))
    del config["profiles"]["agent_orch"]["handoff"]["confirmation_flag"]
    config["profiles"]["agent_orch"]["handoff"]["unknown"] = True
    config_path = tmp_path / "spawn_defaults.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(spawn_profiles, "SPAWN_DEFAULTS_PATH", config_path)
    spawn_profiles.load_spawn_config.cache_clear()
    try:
        try:
            resolve_handoff(
                source_provider="codex",
                source_model="gpt-5.6-sol",
                source_effort="high",
            )
        except SpawnProfileError as exc:
            assert exc.code == "spawn_config_invalid"
        else:
            raise AssertionError("invalid handoff policy was accepted")
    finally:
        spawn_profiles.load_spawn_config.cache_clear()


def test_v2_accepts_handoff_resolution_sources() -> None:
    for source in ("handoff_inherited", "handoff_warn_and_proceed"):
        resolved = validate_v2(
            provider="codex",
            spawn_profile="agent_orch",
            schema="SpawnRequestV2",
            model="gpt-5.6-sol",
            effort="high",
            catalog_version="spawn-catalog-v2",
            resolution_source=source,
        )
        assert resolved["resolution_source"] == source


def test_supported_handoff_source_is_preserved() -> None:
    """Regression guard for handoffs between supported provider profiles."""
    assert "claude-fable-5-1" in spawn_profiles.MODELS.get("claude", {})
    resolution = resolve_handoff(
        source_provider="claude", source_model="claude-fable-5-1", source_effort="high",
        provider="claude", model="claude-opus-5", effort="high",
    )
    assert resolution.source["model"] == "claude-fable-5-1"
    assert resolution.spawn["model"] == "claude-opus-5"
    assert resolution.spawn["resolution_source"] == "handoff_warn_and_proceed"
