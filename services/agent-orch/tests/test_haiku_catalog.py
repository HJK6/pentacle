"""Exact-tuple admission for the bounded Haiku pilot; defaults stay unchanged."""
import pytest

from _shared.spawn_profiles import SpawnProfileError, catalog, resolve_spawn, validate_v2


MODEL = "claude-haiku-5-5"


def test_haiku_high_resolves_and_daemon_revalidates():
    request = resolve_spawn(provider="claude", model=MODEL, effort="high")
    assert (request["model"], request["effort"]) == (MODEL, "high")
    accepted = validate_v2(
        schema="SpawnRequestV2", provider="claude", model=MODEL, effort="high",
        spawn_profile="agent_orch", catalog_version="spawn-catalog-v2",
        resolution_source="explicit_override",
    )
    assert accepted["model"] == MODEL
    advertised = catalog()["available_models"]["claude"]
    assert advertised[MODEL]["efforts"] == ("high",)


@pytest.mark.parametrize("effort", ["low", "medium", "xhigh", "max"])
def test_haiku_other_efforts_refuse_in_client_and_daemon(effort):
    with pytest.raises(SpawnProfileError) as client_error:
        resolve_spawn(provider="claude", model=MODEL, effort=effort)
    assert client_error.value.code == "spawn_pair_unsupported"
    with pytest.raises(SpawnProfileError) as daemon_error:
        validate_v2(
            schema="SpawnRequestV2", provider="claude", model=MODEL, effort=effort,
            spawn_profile="agent_orch", catalog_version="spawn-catalog-v2",
            resolution_source="explicit_override",
        )
    assert daemon_error.value.code == "spawn_pair_unsupported"


def test_existing_defaults_luna_qa_and_unknown_id_behavior_are_preserved():
    assert resolve_spawn(provider="claude")["model"] == "claude-opus-4-8"
    assert resolve_spawn(provider="claude", model="sonnet")["model"] == "claude-sonnet-5-5"
    qa = resolve_spawn(provider="codex", model="gpt-6-luna", effort="max")
    assert (qa["model"], qa["effort"]) == ("gpt-6-luna", "max")
    with pytest.raises(SpawnProfileError) as unknown:
        resolve_spawn(provider="claude", model="claude-haiku-unknown", effort="high")
    assert unknown.value.code == "spawn_model_unsupported"
