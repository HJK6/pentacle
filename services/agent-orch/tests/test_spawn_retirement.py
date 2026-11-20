"""Fresh admission retires old models while stored tuples remain resolvable."""
import json

import pytest

from _shared import spawn_profiles as profiles
from agent_orch import cli
from agent_orch.config import Config

RETIRED = ("gpt-6.1-sol", "sol-6.1", "gpt-6-astra", "astra-6")


@pytest.mark.parametrize("model", RETIRED)
def test_old_ids_reject_fresh_but_preserve_known_identity(model):
    known = profiles.resolve_spawn(provider="codex", model=model, effort="low")
    assert profiles.canonical_model("codex", model) == known["model"]
    with pytest.raises(profiles.SpawnProfileError, match="retired") as exc:
        profiles.resolve_fresh_spawn(provider="codex", model=model, effort="low")
    assert exc.value.code == "spawn_model_retired"
    for key in ("models", "available_models"):
        assert known["model"] not in profiles.catalog()[key]["codex"]


@pytest.mark.parametrize("host", ("host-a", "host-b"))
@pytest.mark.parametrize("profile", profiles.SPAWN_PROFILES)
def test_generic_default_stays_frozen_with_explicit_lead_override(host, profile):
    value = profiles.resolve_fresh_spawn(provider="codex", host=host, spawn_profile=profile)
    assert (value["model"], value["effort"]) == ("gpt-6-luna", "max")
    lead = profiles.resolve_fresh_spawn(provider="codex", model="sol", effort="high", host=host, spawn_profile=profile)
    assert (lead["model"], lead["effort"]) == ("gpt-6.2-sol", "high")
    assert profiles.test_prompt_tuple("codex") == ("gpt-6-luna", "low")
    assert profiles.resolve_fresh_spawn(provider="codex", model="terra")["model"] == "gpt-6.1-terra"


@pytest.mark.parametrize("family", ("sol", "astra"))
@pytest.mark.parametrize("effort", ("low", "medium", "high", "xhigh", "max"))
def test_current_model_aliases_accept_every_supported_effort(family, effort):
    for alias in (family, family + "-6.2", "gpt-6.2-" + family):
        value = profiles.resolve_fresh_spawn(provider="codex", model=alias, effort=effort)
        assert (value["model"], value["effort"]) == ("gpt-6.2-" + family, effort)


def test_retired_handoff_inherits_only_its_own_model():
    args = dict(source_provider="codex", source_model="gpt-6.1-sol", source_effort="high")
    for model in (None, "sol-6.1", "gpt-6.1-sol"):
        assert profiles.resolve_handoff(**args, model=model).spawn["model"] == "gpt-6.1-sol"
    assert profiles.resolve_handoff(**args, model="sol").spawn["model"] == "gpt-6.2-sol"
    with pytest.raises(profiles.SpawnProfileError, match="retired"):
        profiles.resolve_handoff(**args, model="gpt-6-astra")


@pytest.mark.parametrize("scheduled", (False, True))
def test_cli_refuses_retired_fresh_before_rpc(monkeypatch, capsys, tmp_path, scheduled):
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "host-a", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _: None)
    def forbidden(*args, **kwargs):
        raise AssertionError("a retired fresh request reached RPC")
    monkeypatch.setattr(cli, "spawn_once", forbidden)
    monkeypatch.setattr(cli, "schedule_once", forbidden)
    flags = ["spawn", "--provider", "codex", "--model", "gpt-6.1-sol", "--effort", "low"]
    if scheduled:
        flags += ["--delay", "1h"]
    assert cli.spawn(cli.build_parser().parse_args(flags)) != 0
    assert "spawn_model_retired" in capsys.readouterr().err


def test_stale_availability_and_local_catalog_cannot_reenable_retirement(tmp_path, monkeypatch):
    config = json.loads(profiles.SPAWN_DEFAULTS_PATH.read_text())
    path = tmp_path / "spawn_defaults.json"
    path.write_text(json.dumps(config))
    local = {"schema_version": 1, "available_models": {"codex": ["gpt-6.1-sol", "gpt-6.2-sol"]},
             "models": {"codex": config["models"]["codex"]}}
    local["models"]["codex"]["gpt-6.1-sol"]["spawnable"] = True
    path.with_name("spawn_defaults.local.json").write_text(json.dumps(local))
    monkeypatch.setattr(profiles, "SPAWN_DEFAULTS_PATH", path)
    profiles.load_spawn_config.cache_clear()
    try:
        assert list(profiles.catalog()["available_models"]["codex"]) == ["gpt-6.2-sol"]
        with pytest.raises(profiles.SpawnProfileError, match="retired"):
            profiles.resolve_fresh_spawn(provider="codex", model="gpt-6.1-sol")
    finally:
        profiles.load_spawn_config.cache_clear()


@pytest.mark.parametrize("case", ("provider", "host", "test_prompt", "flag"))
def test_invalid_retirement_policy_fails_closed(tmp_path, monkeypatch, case):
    config = json.loads(profiles.SPAWN_DEFAULTS_PATH.read_text())
    if case == "provider":
        config["providers"]["codex"]["model"] = "gpt-6.1-sol"
    elif case == "host":
        config["host_overrides"] = {"host-a": {"codex": {"model": "gpt-6.1-sol"}}}
    elif case == "test_prompt":
        config["profiles"]["test_prompt"]["codex"]["model"] = "gpt-6.1-sol"
    else:
        config["models"]["codex"]["gpt-6.1-sol"]["spawnable"] = "no"
    path = tmp_path / "spawn_defaults.json"
    path.write_text(json.dumps(config))
    monkeypatch.setattr(profiles, "SPAWN_DEFAULTS_PATH", path)
    profiles.load_spawn_config.cache_clear()
    try:
        with pytest.raises(profiles.SpawnProfileError):
            profiles.load_spawn_config()
    finally:
        profiles.load_spawn_config.cache_clear()


def test_narrowed_local_catalog_keeps_retired_continuation_identity(tmp_path, monkeypatch):
    config = json.loads(profiles.SPAWN_DEFAULTS_PATH.read_text())
    path = tmp_path / "spawn_defaults.json"
    path.write_text(json.dumps(config))
    local = {"schema_version": 1, "models": {"codex": {
        model: config["models"]["codex"][model] for model in ("gpt-6.2-sol", "gpt-6-luna")}}}
    path.with_name("spawn_defaults.local.json").write_text(json.dumps(local))
    monkeypatch.setattr(profiles, "SPAWN_DEFAULTS_PATH", path)
    profiles.load_spawn_config.cache_clear()
    try:
        assert set(profiles.catalog()["models"]["codex"]) == {"gpt-6.2-sol", "gpt-6-luna"}
        value = profiles.resolve_handoff(source_provider="codex", source_model="gpt-6.1-sol", source_effort="high")
        assert value.spawn["model"] == "gpt-6.1-sol"
        with pytest.raises(profiles.SpawnProfileError, match="retired"):
            profiles.resolve_fresh_spawn(provider="codex", model="sol-6.1")
    finally:
        profiles.load_spawn_config.cache_clear()


@pytest.mark.parametrize("model", ("gpt-5.6-sol", "gpt-6-sol", "gpt-6-luna", "gpt-6.1-terra"))
def test_models_outside_two_id_retirement_remain_available(model):
    assert profiles.resolve_fresh_spawn(provider="codex", model=model, effort="low")["model"] == model
    for key in ("models", "available_models"):
        assert model in profiles.catalog()[key]["codex"]
