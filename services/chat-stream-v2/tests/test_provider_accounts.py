"""Synthetic subscription homes; never load the execution host's credentials."""
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

import provider_accounts as accounts


@pytest.fixture
def registry(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    return accounts.Registry(directory / "accounts.json", directory / "homes")


def enrolled(registry, provider="codex", alias="primary", host="host-a", generation="login-a", **kw):
    draft = registry.prepare(provider=provider, alias=alias, host=host,
                             generation=generation, profile_ref="browser-a",
                             executable="/bin/echo", version="fixture-1", platform="macos")
    receipt = {"provider_id": "identity-" + alias, "pool_id": "pool-" + alias,
               "auth_mode": "subscription", "store": "file" if provider == "codex" else "keychain",
               "minimal_turn": True, "independent_login": True}
    receipt.update(kw)
    if receipt["store"] == "file":
        target = Path(draft.home) / ("auth.json" if provider == "codex" else ".credentials.json")
        target.write_text("fake-fixture-only")
        target.chmod(0o600)
    return registry.activate(draft, receipt, expected_provider_id="identity-" + alias)


@pytest.mark.parametrize("provider,platform,store", [
    ("codex", "macos", "file"), ("codex", "wsl", "file"),
    ("claude", "macos", "keychain"), ("claude", "wsl", "file"),
])
def test_native_final_home_and_atomic_activation(registry, provider, platform, store):
    draft = registry.prepare(provider=provider, alias="primary", host="host-a", generation="first",
                             profile_ref="browser-a", executable="/bin/echo", version="fixture-1", platform=platform)
    assert Path(draft.home).stat().st_mode & 0o777 == 0o700
    assert registry.active(provider, "primary", "host-a") is None
    assert accounts.environment(draft, {"HOME": "/real/home", "PATH": "/bin", "OPENAI_API_KEY": "fake",
        "CLAUDE_CODE_OAUTH_TOKEN": "fake", "CODEX_HOME": "/wrong", "ANTHROPIC_BASE_URL": "bad"}) == {
        "HOME": "/real/home", "PATH": "/bin",
        "CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR": draft.home,
    }
    receipt = {"provider_id": "id-a", "pool_id": "pool-a", "auth_mode": "subscription", "store": store,
               "minimal_turn": True, "independent_login": True}
    if store == "file":
        credential = Path(draft.home) / ("auth.json" if provider == "codex" else ".credentials.json")
        credential.write_text("fake-fixture-only"); credential.chmod(0o600)
    result = registry.activate(draft, receipt, expected_provider_id="id-a")
    assert registry.active(provider, "primary", "host-a") == result
    assert registry.path.stat().st_mode & 0o777 == 0o600
    assert result.transcript_root == str(Path(result.home) / ("sessions" if provider == "codex" else "projects"))


@pytest.mark.parametrize("bad", ["wrong", "cancelled", "locked_keychain", "api", "no_turn", "copied"])
def test_failed_enrollment_preserves_old_generation(registry, bad):
    old = enrolled(registry)
    draft = registry.prepare(provider="codex", alias="primary", host="host-a", generation="next",
        profile_ref="browser-a", executable="/bin/echo", version="fixture-1", platform="macos")
    receipt = {"provider_id": "identity-primary", "pool_id": "pool-primary", "auth_mode": "subscription",
        "store": "file", "minimal_turn": True, "independent_login": True}
    if bad == "wrong": receipt["provider_id"] = "foreign"
    elif bad == "api": receipt["auth_mode"] = "api"
    elif bad == "no_turn": receipt["minimal_turn"] = False
    elif bad == "copied": receipt["independent_login"] = False
    else: receipt["outcome"] = bad
    with pytest.raises(accounts.AccountError):
        registry.activate(draft, receipt, expected_provider_id="identity-primary")
    assert registry.active("codex", "primary", "host-a") == old
    assert Path(old.home).is_dir()


def test_symlink_insecure_or_reused_home_rejected(registry):
    old = enrolled(registry)
    with pytest.raises(accounts.AccountError):
        registry.prepare(provider="codex", alias="primary", host="host-a", generation="login-a",
            profile_ref="browser-a", executable="/bin/echo", version="fixture-1", platform="macos")
    Path(old.home).chmod(0o755)
    with pytest.raises(accounts.AccountError): registry.active("codex", "primary", "host-a")
    Path(old.home).chmod(0o700)
    link = Path(old.home).parent / "linked"
    link.symlink_to(old.home)
    with pytest.raises(accounts.AccountError): accounts.validate_home(replace(old, home=str(link)), registry.root)


@pytest.mark.parametrize("field", ["account", "account_alias", "account_home", "provider_account", "CODEX_HOME", "CLAUDE_CONFIG_DIR"])
def test_workers_cannot_choose_account_fields(field):
    with pytest.raises(accounts.AccountError, match="account_override_forbidden"):
        accounts.reject_overrides({field: "fake"})


def test_provision_only_reviewed_noncredential_manifest(registry):
    draft = registry.prepare(provider="codex", alias="primary", host="host-a", generation="first",
        profile_ref="browser-a", executable="/bin/echo", version="fixture-1", platform="macos")
    registry.provision(draft, {"AGENTS.md": "Fixture instructions", "config.toml": 'model = "fixture-model"\n'})
    assert (Path(draft.home) / "AGENTS.md").read_text() == "Fixture instructions"
    assert 'cli_auth_credentials_store = "file"' in (Path(draft.home) / "config.toml").read_text()
    for manifest in ({"auth.json": "fake"}, {"../escape": "bad"}, {"config.toml": 'model_provider = "paid"'}):
        with pytest.raises(accounts.AccountError): registry.provision(draft, manifest)


def test_provision_rejects_symlinked_skill_parent_and_preserves_common_resources(registry, tmp_path):
    draft = registry.prepare(provider="claude", alias="primary", host="host-a", generation="first",
        profile_ref="browser-a", executable="/bin/echo", version="fixture-1", platform="macos")
    registry.provision(draft, {"skills/fixture/SKILL.md": "Fixture skill", "skills/fixture/references/example.md": "Fixture reference",
                              "settings.json": json.dumps({"permissions": {"allow": ["Read"]}})})
    assert (Path(draft.home) / "skills/fixture/references/example.md").read_text() == "Fixture reference"
    assert json.loads((Path(draft.home) / "settings.json").read_text())["forceLoginMethod"] == "claudeai"
    external = tmp_path / "external"; external.mkdir(mode=0o700)
    (Path(draft.home) / "skills/linked").symlink_to(external)
    with pytest.raises(accounts.AccountError): registry.provision(draft, {"skills/linked/SKILL.md": "Escape attempt"})
    assert not (external / "SKILL.md").exists()


def test_auth_context_wrapper_and_native_observer_executable_are_both_pinned(tmp_path):
    from ingest import _provider_command_matches
    private=tmp_path/'private';private.mkdir(mode=0o700)
    wrapper=private/'auth-context-wrapper'
    wrapper.write_text('#!/bin/sh\nexec /bin/echo "$@"\n');wrapper.chmod(0o700)
    registry=accounts.Registry(private/'registry.json',private/'homes')
    draft=registry.prepare(provider='claude',alias='primary',host='host-a',generation='one',profile_ref='browser-a',
        executable=str(wrapper),observer_executable='/bin/echo',version='fixture',platform='macos')
    assert draft.executable==str(wrapper)
    assert draft.observer=='/bin/echo' and draft.observer_executable_sha256
    assert not _provider_command_matches('/bin/echo native-args',draft.executable)
    assert _provider_command_matches('/bin/echo native-args',draft.observer)
