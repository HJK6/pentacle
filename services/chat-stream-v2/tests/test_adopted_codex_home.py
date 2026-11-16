"""Adopt-in-place behavior with disposable homes and a synthetic native CLI."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from provider_account_host import NativeHost
from provider_accounts import AccountError


@pytest.fixture
def adoption(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    home = tmp_path / "signed-in-home"
    home.mkdir(mode=0o700)
    auth = home / "auth.json"
    auth.write_text("synthetic-auth-only")
    auth.chmod(0o600)
    control, calls = tmp_path / "control.json", tmp_path / "calls.jsonl"
    control.write_text(json.dumps({"reply": "OK\n", "returncode": 0}))
    executable = tmp_path / "fake-codex"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\nfrom pathlib import Path\n"
        f"with Path({str(calls)!r}).open('a') as stream:\n"
        "    stream.write(json.dumps({'argv': sys.argv[1:], 'home': os.environ.get('CODEX_HOME')}) + '\\n')\n"
        "if sys.argv[1:] == ['--version']:\n    print('fixture-1')\nelse:\n"
        f"    control = json.loads(Path({str(control)!r}).read_text())\n"
        "    print(control['reply'], end='')\n    sys.exit(control['returncode'])\n"
    )
    executable.chmod(0o700)
    cell = dict(enabled=True, expected_provider_id="fixture-id", profile_ref="fixture-profile",
                executable=str(executable), executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
                version="fixture-1", platform="wsl", model="fixture-model", effort="medium", cwd=str(tmp_path),
                adopt_home=str(home), bootstrap={"AGENTS.md": "fixture instructions"})
    config = dict(host="host-a", registry=str(private / "registry.json"), root=str(private / "homes"),
                  cells={"codex:primary": cell})
    reads = []
    receipt = dict(provider_id="fixture-id", pool_id="fixture-pool", auth_mode="subscription")

    def observation(binding):
        reads.append(binding)
        return dict(receipt)

    def no_login(*_):
        raise AssertionError("Adoption must never start a login")

    host = NativeHost(config, login=no_login, observation=observation)
    return SimpleNamespace(host=host, config=config, home=home, auth=auth, control=control, calls=calls,
                           reads=reads, receipt=receipt, no_login=no_login, observation=observation)


def adopt(fixture, *, host=None, provider="codex", alias="primary", expected="unenrolled", generation="first"):
    return (host or fixture.host).handle("adopt", provider=provider, alias=alias, expected_generation=expected,
                                        operation_id=generation, generation=generation)


def auth_snapshot(fixture):
    info = fixture.auth.stat()
    return fixture.auth.read_bytes(), info.st_ino, info.st_mtime_ns, info.st_mode


def retained_config(fixture, content):
    path = fixture.home / "config.toml"
    path.write_text(content)
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("content", [
    'model_provider = "alternate"\n',
    'cli_auth_credentials_store = "keyring"\n',
    '[mcp_servers.fixture]\ncommand = "fixture-command"\n',
    'model = [\n',
])
def test_adopt_rejects_unsafe_retained_config_before_native_execution(adoption, content):
    path = retained_config(adoption, content)
    before = auth_snapshot(adoption)
    result = adopt(adoption)
    assert result["outcome"] == "account_enrollment_failed"
    assert adoption.host.registry.active("codex", "primary", "host-a") is None
    assert not adoption.calls.exists() and not adoption.reads
    assert path.read_text() == content and auth_snapshot(adoption) == before


def test_adopt_keeps_allowlisted_retained_config_and_auth_bytes(adoption):
    content = 'model = "fixture-model"\n[projects."/fixture/project"]\ntrust_level = "trusted"\n'
    path = retained_config(adoption, content)
    before = auth_snapshot(adoption)
    assert adopt(adoption)["outcome"] == "ok"
    assert path.read_text() == content and auth_snapshot(adoption) == before
    calls = [json.loads(line) for line in adoption.calls.read_text().splitlines()]
    assert len(calls) == 2 and all(call["home"] == str(adoption.home) for call in calls)
    assert calls[0]["argv"] == ["--version"] and "exec" in calls[1]["argv"]
    assert len(adoption.reads) == 2


@pytest.mark.parametrize("kind", ["insecure", "symlink", "invalid-utf8"])
def test_adopt_rejects_unreadable_or_nonprivate_retained_config(adoption, tmp_path, kind):
    path = retained_config(adoption, 'model = "fixture-model"\n')
    if kind == "insecure":
        path.chmod(0o644)
    elif kind == "symlink":
        target = tmp_path / "config-target"
        path.rename(target)
        path.symlink_to(target)
    else:
        path.write_bytes(b"\xff")
    before = auth_snapshot(adoption)
    assert adopt(adoption)["outcome"] == "account_enrollment_failed"
    assert not adoption.calls.exists() and not adoption.reads
    assert auth_snapshot(adoption) == before


@pytest.mark.parametrize("cell_key", ["codex:peer", "claude:peer"])
@pytest.mark.parametrize("enabled", [True, False])
def test_host_refuses_two_cells_configured_to_adopt_one_home(adoption, cell_key, enabled):
    config = deepcopy(adoption.config)
    config["cells"][cell_key] = dict(config["cells"]["codex:primary"], enabled=enabled,
                                    adopt_home=str(adoption.home) + "/.")
    with pytest.raises(AccountError, match="account_home_owner_conflict"):
        NativeHost(config, login=adoption.no_login, observation=adoption.observation)
    assert not adoption.calls.exists()


def test_adopted_home_cannot_be_prepared_for_an_unconfigured_owner(adoption):
    cell = adoption.config["cells"]["codex:primary"]
    with pytest.raises(AccountError, match="account_home_owner_conflict"):
        adoption.host.registry.prepare(provider="codex", alias="peer", host="host-a", generation="peer",
            profile_ref=cell["profile_ref"], executable=cell["executable"], version=cell["version"],
            platform=cell["platform"], home=str(adoption.home))
    assert not adoption.calls.exists()


@pytest.mark.parametrize("retire_first_home", [False, True])
def test_registered_home_cannot_be_reassigned_after_host_configuration_changes(adoption, tmp_path, retire_first_home):
    assert adopt(adoption)["outcome"] == "ok"
    if retire_first_home:
        def synthetic_login(binding, _browser):
            auth = Path(binding.home) / "auth.json"
            auth.write_text("synthetic-replacement-auth-only")
            auth.chmod(0o600)
            return "ok"
        host = NativeHost(deepcopy(adoption.config), login=synthetic_login, observation=adoption.observation)
        assert host.handle("enroll", provider="codex", alias="primary", expected_generation="first",
                           generation="replacement", operation_id="replacement")["outcome"] == "ok"
    before = adoption.host.registry.path.read_bytes(), auth_snapshot(adoption)
    config = deepcopy(adoption.config)
    config["cells"] = {"codex:peer": config["cells"]["codex:primary"]}
    host = NativeHost(config, login=adoption.no_login, observation=adoption.observation)
    assert adopt(adoption, host=host, alias="peer", generation="peer")["outcome"] == "account_enrollment_failed"
    assert (adoption.host.registry.path.read_bytes(), auth_snapshot(adoption)) == before


def test_adopt_same_owner_can_refresh_generation(adoption):
    assert adopt(adoption)["outcome"] == "ok"
    before = auth_snapshot(adoption)
    config = deepcopy(adoption.config)
    config["cells"]["codex:primary"]["adopt_home"] += "/."
    fresh = NativeHost(config, login=adoption.no_login, observation=adoption.observation)
    result = adopt(adoption, host=fresh, expected="first", generation="second")
    assert result["outcome"] == "ok" and result["binding"]["generation"] == "second"
    assert auth_snapshot(adoption) == before


def test_distinct_adopted_cells_keep_independent_bindings(adoption, tmp_path):
    config = deepcopy(adoption.config)
    peer = tmp_path / "peer-home"
    peer.mkdir(mode=0o700)
    auth = peer / "auth.json"
    auth.write_text("synthetic-peer-auth-only")
    auth.chmod(0o600)
    config["cells"]["codex:peer"] = dict(config["cells"]["codex:primary"], adopt_home=str(peer))
    host = NativeHost(config, login=adoption.no_login, observation=adoption.observation)
    assert adopt(adoption, host=host)["outcome"] == "ok"
    assert adopt(adoption, host=host, alias="peer", generation="peer")["outcome"] == "ok"
    before = host.registry.path.read_bytes(), auth_snapshot(adoption), auth.read_bytes()
    adoption.control.write_text(json.dumps({"reply": "OK", "returncode": 1}))
    assert adopt(adoption, host=host, expected="first", generation="failed")["outcome"] == "account_enrollment_failed"
    assert (host.registry.path.read_bytes(), auth_snapshot(adoption), auth.read_bytes()) == before
    assert host.registry.active("codex", "primary", "host-a").home == str(adoption.home)
    assert host.registry.active("codex", "peer", "host-a").home == str(peer)


@pytest.mark.parametrize("reply", ["", "WRONG\n", "OK\nextra text\n"])
def test_adopt_rejects_successful_process_without_exact_minimal_reply(adoption, reply):
    adoption.control.write_text(json.dumps({"reply": reply, "returncode": 0}))
    before = auth_snapshot(adoption)
    assert adopt(adoption)["outcome"] == "account_enrollment_failed"
    assert adoption.host.registry.active("codex", "primary", "host-a") is None
    assert auth_snapshot(adoption) == before


@pytest.mark.parametrize("failure", ["changed-identity", "failed-turn", "api-auth", "insecure-home", "claude"])
def test_adoption_refusals_leave_auth_and_existing_binding_intact(adoption, failure):
    assert adopt(adoption)["outcome"] == "ok"
    before = auth_snapshot(adoption), adoption.host.registry.path.read_bytes()
    kwargs = dict(expected="first", generation="failed")
    if failure == "changed-identity":
        def changed(binding):
            adoption.reads.append(binding)
            return adoption.receipt | {"provider_id": "changed" if len(adoption.reads) == 4 else "fixture-id"}
        adoption.host.observation = changed
    elif failure == "failed-turn":
        adoption.control.write_text(json.dumps({"reply": "OK", "returncode": 1}))
    elif failure == "api-auth":
        adoption.receipt["auth_mode"] = "api_key"
    elif failure == "insecure-home":
        adoption.home.chmod(0o755)
    else:
        config = deepcopy(adoption.config)
        config["cells"] = {"claude:primary": config["cells"]["codex:primary"]}
        kwargs.update(host=NativeHost(config, login=adoption.no_login, observation=adoption.observation),
                      provider="claude", expected="unenrolled")
    calls_before = adoption.calls.read_bytes()
    try:
        if failure == "insecure-home":
            with pytest.raises(AccountError, match="account_path_insecure"):
                adopt(adoption, **kwargs)
        else:
            assert adopt(adoption, **kwargs)["outcome"] == "account_enrollment_failed"
        if failure in {"insecure-home", "claude"}:
            assert adoption.calls.read_bytes() == calls_before
        assert (auth_snapshot(adoption), adoption.host.registry.path.read_bytes()) == before
    finally:
        adoption.home.chmod(0o700)
    assert adoption.host.registry.active("codex", "primary", "host-a").generation == "first"
