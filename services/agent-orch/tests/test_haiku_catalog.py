"""Exact-tuple admission for the bounded Haiku pilot; defaults stay unchanged."""
import pytest

from _shared.spawn_profiles import SpawnProfileError, catalog, resolve_spawn, validate_v2


MODEL = "claude-haiku-5-5"


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_haiku_installed_efforts_resolve_and_daemon_revalidates(effort):
    request = resolve_spawn(provider="claude", model=MODEL, effort=effort)
    assert (request["model"], request["effort"]) == (MODEL, effort)
    accepted = validate_v2(
        schema="SpawnRequestV2", provider="claude", model=MODEL, effort=effort,
        spawn_profile="agent_orch", catalog_version="spawn-catalog-v2",
        resolution_source="explicit_override",
    )
    assert (accepted["model"], accepted["effort"]) == (MODEL, effort)
    advertised = catalog()["available_models"]["claude"]
    assert advertised[MODEL]["efforts"] == ("low", "medium", "high", "xhigh", "max")


@pytest.mark.parametrize("effort", ["invalid", "default", "ultra"])
def test_haiku_unknown_efforts_refuse_in_client_and_daemon(effort):
    with pytest.raises(SpawnProfileError):
        resolve_spawn(provider="claude", model=MODEL, effort=effort)
    with pytest.raises(SpawnProfileError):
        validate_v2(schema="SpawnRequestV2", provider="claude", model=MODEL, effort=effort,
            spawn_profile="agent_orch", catalog_version="spawn-catalog-v2",
            resolution_source="explicit_override")


def test_existing_defaults_luna_qa_and_unknown_id_behavior_are_preserved():
    assert resolve_spawn(provider="claude")["model"] == "claude-opus-4-8"
    assert resolve_spawn(provider="claude", model="sonnet")["model"] == "claude-sonnet-5-5"
    qa = resolve_spawn(provider="codex", model="gpt-6-luna", effort="max")
    assert (qa["model"], qa["effort"]) == ("gpt-6-luna", "max")
    with pytest.raises(SpawnProfileError) as unknown:
        resolve_spawn(provider="claude", model="claude-haiku-unknown", effort="high")
    assert unknown.value.code == "spawn_model_unsupported"
