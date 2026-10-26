from __future__ import annotations

from functools import partial
import importlib.util
import json
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from .test_deploy_script import DEPLOY_PATH


def _load_deploy(home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    spec = importlib.util.spec_from_file_location("deploy_log_guard_under_test", DEPLOY_PATH)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("user", ["operator-one", "operator-two"])
def test_default_guard_uses_current_home_without_override(tmp_path, monkeypatch, user):
    home = tmp_path / user
    log_dir = home / "Library/Logs/pentacle/chat-streamd-v2"
    log_dir.mkdir(parents=True)
    module = _load_deploy(home, monkeypatch)

    assert module.build_parser().parse_args(["chat-streamd-v2"]).log_guard_dir is None
    service = module.SERVICES["chat-streamd-v2"]
    assert Path(service.log_guard_dir) == log_dir
    module.verify_log_guard(service.log_guard_dir)
    log_dir.rmdir()


def test_volume_guard_still_requires_a_mount(monkeypatch):
    module = _load_deploy(Path("/Users/example"), monkeypatch)
    monkeypatch.setattr(Path, "is_dir", lambda _path: True)
    monkeypatch.setattr(Path, "is_mount", lambda _path: False)
    with pytest.raises(module.DeployError, match="log guard unavailable"):
        module.verify_log_guard("/Volumes/example-logs")
    monkeypatch.setattr(Path, "is_mount", lambda _path: True)
    module.verify_log_guard("/Volumes/example-logs")


@pytest.fixture
def isolated_release(tmp_path, monkeypatch):
    """Real Git/stamp files; launchd, gate execution and live probes are counterparts."""
    module = _load_deploy(tmp_path / "home", monkeypatch)
    service = module.SERVICES["chat-streamd-v2"]
    repo, origin = tmp_path / "release", tmp_path / "origin.git"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, text=True, capture_output=True, check=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Deploy test")
    git("config", "user.email", "deploy-test@example.invalid")
    (repo / ".gitignore").write_text(".pentacle-deploy/\nservices/chat-stream-v2/.venv/\n")
    main = repo / "services/chat-stream-v2/main.py"
    main.parent.mkdir(parents=True)
    main.write_text("version = 1\n")
    (repo / service.requirements_path).write_text("")
    git("add", ".")
    git("commit", "-qm", "prior")
    prior = git("rev-parse", "HEAD")
    main.write_text("version = 2\n")
    git("commit", "-qam", "target")
    target = git("rev-parse", "HEAD")
    git("init", "--bare", "-q", str(origin))
    git("remote", "add", "origin", str(origin))
    git("push", "-q", "origin", "HEAD:refs/heads/main")
    git("checkout", "-q", "--detach", prior)
    python = module._venv_python(repo, service)
    python.parent.mkdir(parents=True)
    python.touch()
    guard = Path(service.log_guard_dir)
    assert guard.is_relative_to(tmp_path), "test guard must stay inside the owned temporary home"
    guard.mkdir(parents=True)
    plist = tmp_path / "daemon.plist"
    plist.write_bytes(plistlib.dumps({
        "ProgramArguments": [str(python), str(main)],
        "StandardOutPath": str(guard / "launchd.log"),
        "EnvironmentVariables": {"HOME": str(tmp_path / "home")},
    }))
    monkeypatch.setattr(module, "_launchd_plist_path", lambda _label: plist)
    # A stale stamp must not replace the checkout's actual pre-activation identity. The managed
    # digest is stamped to match the on-disk plist, so an unchanged definition still kickstarts
    # (no spurious reload); out-of-band drift is covered by its own test below.
    module._write_stamp(repo, service, {
        "sha": "c" * 40, "prior_sha": target,
        "requirements_hash": module._sha256(repo / service.requirements_path),
        "launchd_plist_digest": module._launchd_plist_managed_digest(service.launchd_label),
    })
    before = module._stamp_path(repo, service).read_bytes()
    monkeypatch.setattr(module, "verify_deploy_guard", lambda *_a, **_k: {"sole_deployer": True})
    monkeypatch.setattr(module, "_ensure_v2_usage_probe_launchd", lambda *_a: False)
    monkeypatch.setattr(module, "_v2_usage_probe_rollback", lambda *_a: lambda: None)
    calls = []
    # The job is loaded (pid 111) until a bootout, and gone after it, as launchd reports it.
    monkeypatch.setattr(module, "_launchd_state", lambda *_a: (
        module.LaunchdState(False, None, True) if any(call[1:2] == ("bootout",) for call in calls)
        else module.LaunchdState(True, 111)))
    # Exercise the real post-activation/stamp boundary, replacing only the live boot probe.
    monkeypatch.setattr(module, "_apply_post_activation", partial(
        module._apply_post_activation,
        verify_boot=lambda *_a, **_k: module.BootReadback(
            module.BOOT_NOT_OBSERVED, "isolated probe", 0, 222, 111
        ),
    ))
    evidence = tmp_path / "gate.json"
    evidence.write_text(json.dumps({
        "schema": "pentacle.v2.gate-evidence.v1", "gate": "merge", "sha": target,
        "source": {"sha_before": target, "sha_after": target,
                   "clean_before": True, "clean_after": True},
        "tiers": [{"tier": tier, "passed": True} for tier in ("unit", "smoke")],
        "passed": True,
    }))

    def activate(*, reload=False, rollback=False, same_ref=False, fail=False, override=None):
        selected = prior if same_ref else target
        payload = json.loads(evidence.read_text())
        payload["sha"] = selected
        payload["source"].update(sha_before=selected, sha_after=selected)
        evidence.write_text(json.dumps(payload))
        monkeypatch.setattr(module, "_ensure_launchd_environment", lambda *_a, **_k: reload)

        def runner(command, cwd):
            calls.append(tuple(command))
            if command[0] == "git":
                return subprocess.run(command, cwd=cwd, text=True, capture_output=True)
            assert command[0] == "launchctl", command
            # Inspect actual disk bytes at the moment activation is attempted.
            stamp = module._read_stamp(repo, service)
            assert stamp["prior_sha"] == prior
            assert stamp["sha"] == selected
            assert stamp["restart_activation"]["pre_restart_pid"] == 111
            return subprocess.CompletedProcess(command, 1 if fail else 0, "", "isolated failure" if fail else "")

        return module.deploy(
            service, release_checkout=repo,
            ref=prior if rollback or same_ref else "origin/main", rollback=rollback,
            gate_evidence=evidence, deployer_stream_id="test-host:deployer",
            log_guard_dir=override, runner=runner,
        )

    yield module, service, repo, prior, target, before, calls, activate
    if guard.is_dir():
        guard.rmdir()


@pytest.mark.parametrize("reload", [False, True], ids=["kickstart", "plist-reload"])
@pytest.mark.parametrize("operation", ["deploy", "rollback", "same-ref"])
def test_every_activation_stamps_prior_checkout_before_restart(isolated_release, reload, operation):
    module, service, repo, prior, target, _before, calls, activate = isolated_release
    stamp = activate(reload=reload, rollback=operation == "rollback", same_ref=operation == "same-ref")
    expected = prior if operation == "same-ref" else target
    assert stamp["sha"] == expected
    assert stamp["prior_sha"] == prior
    assert module._read_stamp(repo, service) == stamp
    assert module._git(repo, "rev-parse", "HEAD", runner=module._run) == expected
    assert [call[1] for call in calls if call[0] == "launchctl"] == (
        ["bootout", "bootstrap"] if reload else ["kickstart"]
    )


def _make_stamp_stale(module, repo, service):
    """Simulate the 2026-10-19 incident: the on-disk plist drifted from what the last deploy
    bootstrapped, so the stamped managed digest no longer matches."""
    stamp = module._read_stamp(repo, service)
    stamp["launchd_plist_digest"] = "stale" + "0" * 59
    module._write_stamp(repo, service, stamp)


def test_out_of_band_plist_change_forces_rebootstrap(isolated_release):
    """End-to-end scope 1: when the on-disk plist drifted from what the last deploy bootstrapped
    (the 2026-10-19 incident), deploy must bootout+bootstrap even though its own env/usage edits
    reported no change — not kickstart the stale loaded definition."""
    module, service, repo, _prior, _target, _before, calls, activate = isolated_release
    _make_stamp_stale(module, repo, service)
    activate(reload=False)  # _ensure_launchd_environment reports no change (old contract: kickstart)
    assert [call[1] for call in calls if call[0] == "launchctl"] == ["bootout", "bootstrap"]


def test_unobserved_rebootstrap_is_retried_on_the_next_deploy(isolated_release):
    """End-to-end scope 1 failure semantics: the fixture never observes boot, so a reload that
    completes but is not confirmed must NOT advance the stamped digest; a follow-on deploy with the
    same on-disk plist therefore still diverges and re-attempts the reload."""
    module, service, repo, prior, _target, _before, calls, activate = isolated_release
    _make_stamp_stale(module, repo, service)
    activate(reload=False)  # diverge -> reload (bootout+bootstrap); boot NOT observed
    assert module._read_stamp(repo, service)["launchd_plist_digest"] == "stale" + "0" * 59
    # Reset the simulated launchd (loaded again) and HEAD, then drive a genuine follow-on deploy.
    calls.clear()
    subprocess.run(["git", "checkout", "-q", "--detach", prior], cwd=repo, check=True)
    activate(reload=False)
    assert [call[1] for call in calls if call[0] == "launchctl"] == ["bootout", "bootstrap"]


def test_failed_rebootstrap_is_retried_on_the_next_deploy(isolated_release):
    """End-to-end scope 1 failure semantics: a failed restart leaves the stamped digest at
    D_prior, so a follow-on deploy still diverges and re-attempts the reload."""
    module, service, repo, prior, _target, _before, calls, activate = isolated_release
    _make_stamp_stale(module, repo, service)
    stamp = activate(reload=False, fail=True)  # diverge -> reload attempted -> launchctl rc1 -> fails
    assert "post_activation_error" in stamp
    assert module._read_stamp(repo, service)["launchd_plist_digest"] == "stale" + "0" * 59
    calls.clear()
    subprocess.run(["git", "checkout", "-q", "--detach", prior], cwd=repo, check=True)
    activate(reload=False, fail=False)  # follow-on retries the reload
    assert [call[1] for call in calls if call[0] == "launchctl"] == ["bootout", "bootstrap"]


@pytest.mark.parametrize("reload", [False, True])
def test_restart_failure_retains_applied_stamp(isolated_release, reload):
    module, service, repo, prior, target, _before, _calls, activate = isolated_release
    stamp = activate(reload=reload, fail=True)
    assert "post_activation_error" in stamp
    assert (stamp["sha"], stamp["prior_sha"]) == (target, prior)
    assert module._read_stamp(repo, service) == stamp
    assert module._git(repo, "rev-parse", "HEAD", runner=module._run) == target


@pytest.mark.parametrize("missing", ["default", "override"])
def test_missing_guard_refuses_and_restores_before_restart(isolated_release, missing, tmp_path):
    module, service, repo, prior, _target, before, calls, activate = isolated_release
    override = tmp_path / "absent-override" if missing == "override" else None
    if missing == "default":
        Path(service.log_guard_dir).rmdir()
    with pytest.raises(module.DeployError, match="log guard unavailable"):
        activate(override=override)
    assert not any(call[0] == "launchctl" for call in calls)
    assert module._git(repo, "rev-parse", "HEAD", runner=module._run) == prior
    assert module._stamp_path(repo, service).read_bytes() == before


def test_explicit_guard_overrides_missing_default(isolated_release, tmp_path):
    module, service, _repo, _prior, target, _before, calls, activate = isolated_release
    Path(service.log_guard_dir).rmdir()
    override = tmp_path / "custom-logs"
    override.mkdir()
    try:
        assert activate(override=override)["sha"] == target
        assert any(call[0] == "launchctl" for call in calls)
    finally:
        override.rmdir()


def _managed_window(module, tmp_path, monkeypatch, stdout, stderr):
    from managed_logs import Config
    config = Config(tmp_path / 'daemon.out', tmp_path / 'daemon.err')
    for path in (config.stdout, config.stderr):
        path.write_text('prior\n')
    monkeypatch.setattr(module, '_daemon_log_paths', lambda _: (config.stdout, config.stderr))
    monkeypatch.setattr(module, '_managed_log_config', lambda _: config)
    offsets = module._capture_log_offsets((config.stdout, config.stderr))
    for path, content in ((config.stdout, stdout), (config.stderr, stderr)):
        with path.open('a') as stream:
            stream.write(content)
    return module._scan_slow_consumer_window(module.SERVICES['chat-streamd-v2'], offsets)


@pytest.mark.parametrize('sink', ['stdout', 'stderr', 'both', 'neither'])
@pytest.mark.parametrize('diagnostic', ['', 'INFO ws client=reader close code=1011\n',
                                      'WARNING slow_consumer overflow client=reader\n'])
def test_legacy_boot_prefix_cannot_prove_managed_generation(tmp_path, monkeypatch, sink, diagnostic):
    module = _load_deploy(tmp_path, monkeypatch)
    boot = module.V2_BOOT_LINE + ' 127.0.0.1:1234\n'
    result = _managed_window(module, tmp_path, monkeypatch,
                             boot if sink in ('stdout', 'both') else '',
                             (boot if sink in ('stderr', 'both') else '') + diagnostic)
    assert result['outcome'] == module.SLOW_CONSUMER_FAILED
    assert result['attribution']['outcome'] == 'attribution_ambiguous'
    assert result['attribution']['reasons']
    assert result['scan_complete']
