import hashlib
import io
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import threading
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "deploy" / "install_fleet_spawn_tooling.py"
SPEC = importlib.util.spec_from_file_location("install_fleet_spawn_tooling", SCRIPT)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)

RUNTIME_SELECTOR = SCRIPT.parent / "runtime_python.py"
SELECTOR_SPEC = importlib.util.spec_from_file_location("runtime_python", RUNTIME_SELECTOR)
assert SELECTOR_SPEC and SELECTOR_SPEC.loader
runtime_python = importlib.util.module_from_spec(SELECTOR_SPEC)
sys.modules[SELECTOR_SPEC.name] = runtime_python
SELECTOR_SPEC.loader.exec_module(runtime_python)


def _localhost_bundle(tmp_path: Path, commit: str) -> tuple[Path, dict[str, object]]:
    bundle = tmp_path / f"bundle-{commit[:8]}"
    bundle.mkdir()
    _dependency_bundle, dependency = installer._runtime_dependency_bundle(bundle)
    archive = bundle / "pentacle.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(SCRIPT.parents[1], arcname="services/agent-orch")
        tar.add(SCRIPT.parents[2] / "_shared", arcname="services/_shared")
        tar.add(bundle / "runtime-deps", arcname="runtime-deps")
    manifest: dict[str, object] = {
        "commit": commit,
        "stamp": f"rollout-{commit[:8]}",
        "archive_sha256": installer._sha256(archive),
        "contract": {"sha256": installer._sha256(SCRIPT.parents[2] / "_shared" / "spawn_profiles.py")},
        "catalog_version": "spawn-catalog-v2",
        "provider_packages": {"codex": {"binary": "codex"}},
        "runtime_package": {
            "sha256": installer._archive_tree_sha256(archive, "services/agent-orch/agent_orch"),
            "requires_python": ">=3.11",
            "interpreter_selector": {
                "path": installer.RUNTIME_SELECTOR_PATH,
                "sha256": installer._archive_member_sha256(archive, installer.RUNTIME_SELECTOR_PATH),
            },
            "asset_schema": {"sha256": installer._archive_member_sha256(archive, "services/_shared/asset_schema.py")},
            "dependencies": {"websockets": dependency},
        },
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return bundle, manifest


def _localhost_runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    if argv[0] == "ssh":
        return subprocess.run(argv[-1], shell=True, text=True, capture_output=True, check=False)
    if argv[0] == "scp":
        destination = Path(argv[-1].split(":", 1)[1])
        destination.mkdir(parents=True, exist_ok=True)
        for source in argv[1:-1]:
            shutil.copy2(source, destination / Path(source).name)
        return subprocess.CompletedProcess(argv, 0, "", "")
    raise AssertionError(f"unexpected command: {argv}")


def test_default_install_targets_only_this_user_local_machine() -> None:
    assert installer.HOSTS == {"local": ("localhost", str(Path.home() / ".local/share/pentacle/releases"))}


@pytest.mark.parametrize("mapping", [{}, {"office": {}}, {"office": {"ssh": "office", "release_root": "relative"}}, {"office": {"ssh": "-oProxyCommand=bad", "release_root": "/tmp/releases"}}])
def test_invalid_fleet_configuration_is_rejected_before_target_selection(tmp_path, mapping):
    path = tmp_path / "hosts.json"
    path.write_text(json.dumps(mapping))
    with pytest.raises(ValueError):
        installer._load_hosts(str(path))


def test_configured_local_target_does_not_depend_on_its_name(tmp_path):
    path = tmp_path / "hosts.json"
    path.write_text(json.dumps({"office": {"ssh": "localhost", "release_root": "/tmp/releases"}}))
    hosts = installer._load_hosts(str(path))
    target = installer._targets(["office"], "office", {}, hosts)[0]
    assert (target.name, target.root, target.local, target.loopback) == ("office", "/tmp/releases", True, True)


def test_macos_path_launchers_include_homebrew_precedence() -> None:
    target = installer.Target("hostb", "hostb", "/Users/example/.local/share/pentacle/releases")

    assert installer._path_launchers(target) == [
        "/Users/example/.local/bin/agent-orch",
        "/opt/homebrew/bin/agent-orch",
    ]


def test_installer_cli_documents_all_handoff_capable_peers() -> None:
    help_result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        text=True,
        capture_output=True,
    )
    assert help_result.returncode == 0
    assert "--host-config" in help_result.stdout

    invalid_result = subprocess.run(
        [sys.executable, str(SCRIPT), "--commit", "a" * 40, "--hosts", "unknown", "--dry-run"],
        text=True,
        capture_output=True,
    )
    assert invalid_result.returncode == 2
    assert "configured hosts" in invalid_result.stderr


def test_main_selects_local_run_host_and_ssh_target_alias(monkeypatch, tmp_path: Path) -> None:
    targets: list[installer.Target] = []
    stage_options: list[dict[str, object]] = []
    monkeypatch.setenv("PENTACLE_RELEASE_ROLLOUT_LOCK", str(tmp_path / "rollout.lock"))
    monkeypatch.setattr(installer, "_rollout_stamp", lambda *_args, **_kwargs: "rollout-test")
    monkeypatch.setattr(installer, "_archive", lambda *_args, **_kwargs: {"stamp": "rollout-test"})

    def fake_stage(target, *_args, **kwargs):
        targets.append(target)
        stage_options.append(kwargs)
        return "staged_verified"

    monkeypatch.setattr(installer, "stage", fake_stage)
    monkeypatch.setattr(installer, "_read_pointer", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(installer, "activate", lambda *_args, **_kwargs: {"active": None, "previous": None})
    host_config = tmp_path / "hosts.json"
    host_config.write_text(json.dumps({
        "travel": {"ssh": "travel-ssh", "release_root": "/tmp/travel-releases"},
        "hub": {"ssh": "hub-ssh", "release_root": "/tmp/hub-releases"},
    }))
    monkeypatch.setattr(
        installer.sys,
        "argv",
        [
            "install",
            "--commit",
            "a" * 40,
            "--hosts",
            "travel,hub",
            "--host-config",
            str(host_config),
            "--run-host",
            "travel",
            "--ssh-target",
            "hub=hub-alias",
            "--sftp-timeout",
            "5",
        ],
    )

    assert installer.main() == 0
    assert [(target.name, target.ssh, target.local) for target in targets] == [
        ("travel", "travel-ssh", True),
        ("hub", "hub-alias", False),
    ]
    assert [options["sftp_timeout"] for options in stage_options] == [5, 5]

    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def recording_runner(argv: list[str], **_kwargs: object) -> Result:
        calls.append(argv)
        return Result()

    installer._remote(targets[0], "true", dry_run=False, runner=recording_runner)
    installer._remote(targets[1], "true", dry_run=False, runner=recording_runner)
    assert calls == [
        ["sh", "-lc", "true"],
        ["ssh", "-o", "BatchMode=yes", "hub-alias", "sh -lc true"],
    ]
    assert installer._scp_argv(targets[1], tmp_path / "bundle", "/tmp/staging") == [
        "scp",
        str(tmp_path / "bundle" / "pentacle.tar"),
        str(tmp_path / "bundle" / "manifest.json"),
        "hub-alias:/tmp/staging/",
    ]


def test_default_transport_argv_and_sftp_bound_are_unchanged(monkeypatch, tmp_path: Path) -> None:
    target = installer._targets(["local"], "local", {})[0]
    assert target.local is True
    assert target.loopback is True
    command = "printf '%s' 'alpha beta'"
    assert installer._remote_argv(target, command) == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "localhost",
        f"sh -lc {shlex.quote(command)}",
    ]

    (tmp_path / "pentacle.tar").write_bytes(b"archive")
    monkeypatch.setattr(installer, "_verify_release", lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.CalledProcessError(1, ["verify"])))
    monkeypatch.setattr(installer, "_read_pointer", lambda *_args, **_kwargs: None)
    seen: list[tuple[list[str], dict[str, object]]] = []

    def successful_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(RuntimeError, match="release_hash_verify_failed:local"):
        installer.stage(
            target,
            tmp_path,
            "a" * 40,
            {
                "archive_sha256": "b" * 64,
                "stamp": "rollout-test",
                "runtime_package": {"requires_python": ">=3.11"},
            },
            dry_run=False,
            runner=successful_runner,
        )
    scp_calls = [(argv, kwargs) for argv, kwargs in seen if argv[0] == "scp"]
    assert len(scp_calls) == 1
    scp_argv, scp_kwargs = scp_calls[0]
    staging = f"{target.root}/{'a' * 40}.staging"
    assert scp_argv == [
        "scp",
        str(tmp_path / "pentacle.tar"),
        str(tmp_path / "manifest.json"),
        f"localhost:{staging}/",
    ]
    assert scp_kwargs["timeout"] == 120


def test_installer_rejects_hostile_commit_before_remote_work() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--commit", "abc;touch /tmp/pwned", "--hosts", "hosta", "--dry-run"],
        text=True, capture_output=True,
    )
    assert result.returncode == 2
    assert "full immutable" in result.stderr


def test_manifest_contract_and_stamp_come_from_requested_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    contract = repo / "services" / "_shared" / "spawn_profiles.py"
    contract.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    old_bytes = b'CATALOG_VERSION = "spawn-catalog-v1"\n'
    contract.write_bytes(old_bytes)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "old"], check=True)
    old_commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    contract.write_text('CATALOG_VERSION = "spawn-catalog-v2"\n')
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "new"], check=True)
    destination = tmp_path / "bundle"
    destination.mkdir()
    manifest = installer._archive(repo, old_commit, destination)

    with tarfile.open(destination / "pentacle.tar") as archive:
        archived = archive.extractfile("services/_shared/spawn_profiles.py")
        assert archived is not None
        assert archived.read() == old_bytes
    digest = hashlib.sha256(old_bytes).hexdigest()
    assert manifest["catalog_version"] == "spawn-catalog-v1"
    assert manifest["contract"]["sha256"] == digest
    assert manifest["stamp"] == f"{old_commit[:12]}-{digest[:12]}"


def test_remote_passes_one_shell_quoted_command() -> None:
    seen = []

    class Result:
        returncode = 0
        stderr = ""
        stdout = "ok\n"

    command = "printf '%s' 'alpha beta' && test x = x"
    output = installer._remote(
        installer.Target("test", "example", "/tmp/root"), command, dry_run=False,
        runner=lambda argv, **_kwargs: seen.append(argv) or Result(),
    )
    assert output == "ok"
    assert len(seen[0]) == 5
    assert seen[0][-1].startswith("sh -lc ")
    local = subprocess.run(seen[0][-1], shell=True, text=True, capture_output=True)
    assert local.returncode == 0
    assert local.stdout == "alpha beta"


def test_release_verification_binds_complete_manifest() -> None:
    seen = []

    class Result:
        returncode = 0
        stderr = ""
        stdout = ""

    manifest = {
        "schema": "PentacleReleaseManifestV1", "commit": "a" * 40,
        "stamp": "rollout-stamp", "archive_sha256": "b" * 64,
        "contract": {"path": "services/_shared/spawn_profiles.py", "sha256": "c" * 64},
        "catalog_version": "spawn-catalog-v2", "provider_packages": {"codex": {}},
    }
    installer._verify_release(
        installer.Target("test", "example", "/tmp/root"), "a" * 40, manifest, dry_run=False,
        runner=lambda argv, **_kwargs: seen.append(argv) or Result(),
    )
    assert "rollout-stamp" in seen[0][-1]


def test_release_verification_rejects_tampered_manifest_when_python_optimized(tmp_path: Path) -> None:
    commit = "a" * 40
    release = tmp_path / commit
    contract = release / "app" / "services" / "_shared" / "spawn_profiles.py"
    contract.parent.mkdir(parents=True)
    archive = release / "pentacle.tar"
    archive.write_bytes(b"archive")
    contract.write_bytes(b"contract")
    manifest = {
        "schema": "PentacleReleaseManifestV1", "commit": commit,
        "stamp": "expected-stamp", "archive_sha256": hashlib.sha256(b"archive").hexdigest(),
        "contract": {
            "path": "services/_shared/spawn_profiles.py",
            "sha256": hashlib.sha256(b"contract").hexdigest(),
        },
        "catalog_version": "spawn-catalog-v2", "provider_packages": {"codex": {}},
    }
    (release / "manifest.json").write_text(json.dumps({**manifest, "stamp": "tampered-stamp"}))
    seen = []

    class Result:
        returncode = 0
        stderr = ""
        stdout = ""

    installer._verify_release(
        installer.Target("test", "example", str(tmp_path)), commit, manifest, dry_run=False,
        runner=lambda argv, **_kwargs: seen.append(argv) or Result(),
    )
    result = subprocess.run(
        seen[0][-1], shell=True, text=True, capture_output=True,
        env={**os.environ, "PYTHONOPTIMIZE": "1"},
    )
    assert result.returncode != 0


def test_release_verification_hash_command_executes(tmp_path: Path) -> None:
    commit = "a" * 40
    root = tmp_path / "releases"
    release = root / commit
    profile = release / "app" / "services" / "_shared" / "spawn_profiles.py"
    profile.parent.mkdir(parents=True)
    profile.write_bytes(b"profile contract")
    archive = release / "pentacle.tar"
    archive.write_bytes(b"release archive")
    manifest = {
        "commit": commit,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "contract": {"sha256": hashlib.sha256(profile.read_bytes()).hexdigest()},
        "catalog_version": "spawn-catalog-v2",
        "provider_packages": {"codex": {"binary": "codex"}},
    }
    (release / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    def local_runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(shlex.split(argv[-1]), text=True, capture_output=True, check=False)

    installer._verify_release(
        installer.Target("test", "local", str(root)),
        commit,
        manifest,
        dry_run=False,
        runner=local_runner,
    )


def test_stage_prepares_runtime_after_the_final_release_path_exists(monkeypatch, tmp_path: Path) -> None:
    commit = "a" * 40
    target = installer.Target("test", "example", str(tmp_path / "releases"))
    calls: list[str] = []
    verify_calls = 0

    def fake_verify(*_args, **_kwargs) -> None:
        nonlocal verify_calls
        verify_calls += 1
        if verify_calls == 1:
            raise subprocess.CalledProcessError(1, ["verify"])

    monkeypatch.setattr(installer, "_verify_release", fake_verify)
    monkeypatch.setattr(installer, "_remote", lambda _target, command, **_kwargs: calls.append(command) or "")

    assert installer.stage(target, tmp_path, commit, {"archive_sha256": "b" * 64, "stamp": "rollout-test", "runtime_package": {"requires_python": ">=3.11"}}, dry_run=True) == "staged_verified"
    move_index = next(index for index, command in enumerate(calls) if " mv " in f" {command} ")
    runtime_index = next(index for index, command in enumerate(calls) if "venv" in command)
    assert move_index < runtime_index
    assert f"{commit}.staging/runtime" not in calls[runtime_index]


def test_stage_never_replaces_an_active_release_that_fails_verification(monkeypatch, tmp_path: Path) -> None:
    commit = "a" * 40
    target = installer.Target("test", "example", str(tmp_path / "releases"))
    release = f"{target.root}/{commit}"
    commands: list[str] = []

    monkeypatch.setattr(installer, "_verify_release", lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.CalledProcessError(1, ["verify"])))
    monkeypatch.setattr(installer, "_remote", lambda _target, command, **_kwargs: commands.append(command) or (release if "readlink" in command else ""))

    try:
        installer.stage(target, tmp_path, commit, {"archive_sha256": "b" * 64}, dry_run=True)
    except RuntimeError as exc:
        assert str(exc) == "active_release_failed_verification:test"
    else:
        raise AssertionError("invalid active release was replaced")
    assert not any("rm -rf" in command for command in commands)


def test_stage_leaves_launcher_and_legacy_residue_until_activation(tmp_path: Path) -> None:
    root = tmp_path / "home" / ".local" / "share" / "pentacle" / "releases"
    target = installer.Target("test", "localhost", str(root))
    old_commit, new_commit = "a" * 40, "b" * 40
    old_bundle, old_manifest = _localhost_bundle(tmp_path, old_commit)
    new_bundle, new_manifest = _localhost_bundle(tmp_path, new_commit)

    assert installer.stage(target, old_bundle, old_commit, old_manifest, dry_run=False, runner=_localhost_runner) == "staged_verified"
    old_release = root / old_commit
    (root / "active").symlink_to(old_release)
    legacy_root = root.parents[1] / "agent-orch-releases"
    legacy_launcher = legacy_root / "current" / "bin" / "agent-orch"
    legacy_launcher.parent.mkdir(parents=True)
    legacy_launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher = Path(installer._stable_launcher(target))
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(legacy_launcher)

    assert installer.stage(target, new_bundle, new_commit, new_manifest, dry_run=False, runner=_localhost_runner) == "staged_verified"

    assert os.readlink(launcher) == str(legacy_launcher)
    assert legacy_root.is_dir()
    assert (root / "active").resolve() == old_release

    installer.activate(target, new_commit, new_manifest, str(old_release), dry_run=False, runner=_localhost_runner)
    assert os.readlink(launcher) == str(root / "active" / "runtime" / "bin" / "agent-orch")
    assert (root / "active").resolve() == root / new_commit


def test_rollout_lock_refuses_second_run_with_running_stamp(tmp_path: Path) -> None:
    lock_path = tmp_path / "rollout.lock"
    first = installer.RolloutLock(lock_path, "rollout-running")
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="rollout-running"):
            installer.RolloutLock(lock_path, "rollout-second").acquire()
    finally:
        first.release()


def test_main_serializes_overlapping_rollouts_and_reports_running_stamp(monkeypatch, tmp_path: Path, capsys) -> None:
    started = threading.Event()
    release = threading.Event()
    first_result: list[int] = []
    monkeypatch.setenv("PENTACLE_RELEASE_ROLLOUT_LOCK", str(tmp_path / "rollout.lock"))
    monkeypatch.setattr(installer, "_rollout_stamp", lambda *_args, **_kwargs: "rollout-running")

    def blocked_archive(*_args, **_kwargs):
        started.set()
        assert release.wait(timeout=5)
        return {"stamp": "rollout-running"}

    monkeypatch.setattr(installer, "_archive", blocked_archive)
    monkeypatch.setattr(installer, "stage", lambda *_args, **_kwargs: "staged_verified")
    monkeypatch.setattr(installer, "_read_pointer", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(installer, "activate", lambda *_args, **_kwargs: {"active": None, "previous": None})
    monkeypatch.setattr(installer.sys, "argv", ["install", "--commit", "a" * 40, "--hosts", "local"])

    worker = threading.Thread(target=lambda: first_result.append(installer.main()))
    worker.start()
    assert started.wait(timeout=5)

    assert installer.main() == 1
    release.set()
    worker.join(timeout=10)

    assert first_result == [0]
    captured = capsys.readouterr()
    assert "rollout_in_progress:rollout-running" in captured.out
    assert all(json.loads(line)["ok"] in {True, False} for line in captured.out.splitlines() if line)


def test_progress_lines_flush_as_they_are_emitted(monkeypatch) -> None:
    stream = io.StringIO()
    flushes = 0

    def flush() -> None:
        nonlocal flushes
        flushes += 1

    monkeypatch.setattr(stream, "flush", flush)
    monkeypatch.setattr(installer, "_PROGRESS_STREAM", stream)

    installer._progress("stage_start", host="hostb", bytes=42)

    assert "stage_start" in stream.getvalue()
    assert "hostb" in stream.getvalue()
    assert flushes == 1


def test_sftp_timeout_is_configurable_and_names_target(monkeypatch, tmp_path: Path) -> None:
    target = installer.Target("hostb", "hostb", str(tmp_path / "releases"))
    (tmp_path / "pentacle.tar").write_bytes(b"archive")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(installer, "_verify_release", lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.CalledProcessError(1, ["verify"])))
    monkeypatch.setattr(installer, "_remote", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(installer, "_read_pointer", lambda *_args, **_kwargs: None)

    def blocked_runner(_argv: list[str], **kwargs: object) -> None:
        calls.append(kwargs)
        raise subprocess.TimeoutExpired("scp", 5)

    with pytest.raises(RuntimeError, match="sftp_timeout:hostb:5s"):
        installer.stage(
            target,
            tmp_path,
            "a" * 40,
            {"archive_sha256": "b" * 64, "stamp": "rollout-test"},
            dry_run=False,
            runner=blocked_runner,
            sftp_timeout=5,
        )
    assert calls[0]["timeout"] == 5


def test_runtime_prepare_is_self_contained() -> None:
    command = installer._runtime_prepare_command("/tmp/release", ">=3.11")
    assert "--system-site-packages" not in command
    assert "pip', 'install'" not in command
    assert "runtime-deps" in command
    assert "runtime_python.py" in command
    assert "websockets" in installer._runtime_verify_command("/tmp/release", {"runtime_package": {"sha256": "a" * 64, "requires_python": ">=3.11", "interpreter_selector": {"path": installer.RUNTIME_SELECTOR_PATH, "sha256": "b" * 64}}, "catalog_version": "spawn-catalog-v2"})


def test_runtime_requires_python_gate_rejects_version_skew() -> None:
    assert runtime_python.version_satisfies((3, 9, 7), ">=3.11") is False
    assert runtime_python.version_satisfies((3, 11, 0), ">=3.11") is True


def test_runtime_prepare_rejects_python_3_9_for_requires_python_3_11(tmp_path: Path) -> None:
    release = tmp_path / ("a" * 40)
    services = release / "app" / "services"
    shutil.copytree(SCRIPT.parents[1], services / "agent-orch")
    shutil.copytree(SCRIPT.parents[2] / "_shared", services / "_shared")
    _bundle, _dependency = installer._runtime_dependency_bundle(release / "app")
    old_python = tmp_path / "python3.9"
    old_python.write_text("#!/bin/sh\nprintf '3.9.7\\n'\n", encoding="utf-8")
    old_python.chmod(0o755)

    rejected = subprocess.run(
        ["sh", "-lc", installer._runtime_prepare_command(str(release), ">=3.11")],
        text=True,
        capture_output=True,
        env={**os.environ, "PENTACLE_RUNTIME_PYTHON": str(old_python)},
    )

    assert rejected.returncode != 0
    assert "runtime_python_unsatisfied:>=3.11" in rejected.stderr
    assert not (release / "runtime").exists()


def test_runtime_prepare_builds_a_launchable_final_release(tmp_path: Path) -> None:
    release = tmp_path / ("a" * 40)
    services = release / "app" / "services"
    shutil.copytree(SCRIPT.parents[1], services / "agent-orch")
    shutil.copytree(SCRIPT.parents[2] / "_shared", services / "_shared")
    _bundle, dependency = installer._runtime_dependency_bundle(release / "app")

    prepared = subprocess.run(["sh", "-lc", installer._runtime_prepare_command(str(release), ">=3.11")], text=True, capture_output=True)
    assert prepared.returncode == 0, prepared.stderr
    runtime_python = release / "runtime" / "bin" / "python"
    verified = subprocess.run(
        [str(runtime_python), "-I", "-c", "import agent_orch, websockets; from _shared import spawn_profiles"],
        text=True,
        capture_output=True,
    )
    assert verified.returncode == 0, verified.stderr
    launcher = release / "runtime" / "bin" / "agent-orch"
    launched = subprocess.run([str(launcher), "--help"], text=True, capture_output=True)
    assert launched.returncode == 0, launched.stderr
    assert ".staging" not in launcher.read_text(encoding="utf-8")
    manifest = {
        "catalog_version": "spawn-catalog-v2",
        "runtime_package": {
            "sha256": installer._tree_sha256(services / "agent-orch" / "agent_orch"),
            "requires_python": ">=3.11",
            "interpreter_selector": {
                "path": installer.RUNTIME_SELECTOR_PATH,
                "sha256": installer._sha256(services / "agent-orch" / "deploy" / "runtime_python.py"),
            },
            "asset_schema": {"sha256": installer._sha256(services / "_shared" / "asset_schema.py")},
            "dependencies": {"websockets": dependency},
        },
    }
    verified_release = subprocess.run(["sh", "-lc", installer._runtime_verify_command(str(release), manifest)], text=True, capture_output=True)
    assert verified_release.returncode == 0, verified_release.stderr
    canonical_asset_schema = services / "_shared" / "asset_schema.py"
    canonical_bytes = canonical_asset_schema.read_bytes()
    canonical_asset_schema.write_bytes(canonical_bytes + b"\n# tampered\n")
    canonical_tampered = subprocess.run(["sh", "-lc", installer._runtime_verify_command(str(release), manifest)], text=True, capture_output=True)
    assert canonical_tampered.returncode != 0
    canonical_asset_schema.write_bytes(canonical_bytes)
    dependency_init = next((release / "runtime").glob("lib/python*/site-packages/websockets/__init__.py"))
    dependency_init.write_text(dependency_init.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    tampered = subprocess.run(["sh", "-lc", installer._runtime_verify_command(str(release), manifest)], text=True, capture_output=True)
    assert tampered.returncode != 0


def test_localhost_ssh_stage_activate_concurrent_launch_and_rollback(tmp_path: Path) -> None:
    root_base = Path(str(tmp_path).removeprefix("/private"))
    root = root_base / "home" / ".local" / "share" / "pentacle" / "releases"
    target = installer.Target("hosta", "localhost", str(root))
    old_commit, new_commit = "a" * 40, "b" * 40
    old_bundle, old_manifest = _localhost_bundle(tmp_path, old_commit)
    new_bundle, new_manifest = _localhost_bundle(tmp_path, new_commit)

    stage_result = installer.stage(target, old_bundle, old_commit, old_manifest, dry_run=False, runner=_localhost_runner)
    assert stage_result == "staged_verified"
    old_release = f"{root}/{old_commit}"
    installer.activate(target, old_commit, old_manifest, None, dry_run=False, runner=_localhost_runner)
    assert installer.stage(target, new_bundle, new_commit, new_manifest, dry_run=False, runner=_localhost_runner) == "staged_verified"
    new_release = f"{root}/{new_commit}"

    tamper_cases = {
        "archive": Path(new_release) / "pentacle.tar",
        "manifest": Path(new_release) / "manifest.json",
        "contract": Path(new_release) / "app" / "services" / "_shared" / "spawn_profiles.py",
        "catalog": Path(new_release) / "app" / "services" / "_shared" / "spawn_profiles.py",
        "launcher": Path(new_release) / "runtime" / "bin" / "agent-orch",
        "runtime_package": next((Path(new_release) / "runtime").glob("lib/python*/site-packages/agent_orch/cli.py")),
    }
    for path in tamper_cases.values():
        original = path.read_bytes()
        path.write_bytes(original + b"\n# tampered\n")
        try:
            installer.activate(target, new_commit, new_manifest, old_release, dry_run=False, runner=_localhost_runner)
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError(f"tampered release activated: {path}")
        assert (root / "active").resolve() == Path(old_release).resolve()
        path.write_bytes(original)
        installer._verify_release(target, new_commit, new_manifest, dry_run=False, runner=_localhost_runner)

    installer.activate(target, new_commit, new_manifest, old_release, dry_run=False, runner=_localhost_runner)

    launcher = Path(installer._stable_launcher(target))
    observed: list[str] = []
    failures: list[str] = []
    stop = threading.Event()

    def invoke() -> None:
        while not stop.is_set():
            try:
                result = subprocess.run([str(launcher), "--help"], text=True, capture_output=True)
                if result.returncode:
                    failures.append(result.stderr)
                else:
                    observed.append(str(launcher.resolve()))
            except OSError as exc:
                failures.append(str(exc))

    worker = threading.Thread(target=invoke)
    worker.start()
    try:
        installer.activate(target, old_commit, old_manifest, new_release, dry_run=False, runner=_localhost_runner)
        installer.activate(target, new_commit, new_manifest, old_release, dry_run=False, runner=_localhost_runner)
    finally:
        stop.set()
        worker.join(timeout=10)

    assert observed
    assert not failures
    assert set(observed) <= {
        str(Path(old_release).resolve() / "runtime/bin/agent-orch"),
        str(Path(new_release).resolve() / "runtime/bin/agent-orch"),
    }
    rollback = installer.rollback(target, old_release, dry_run=False, runner=_localhost_runner)
    assert rollback["active"] == old_release
    assert rollback["commit"] == old_commit


def test_rollback_reports_verified_previous_release_identity(monkeypatch, tmp_path: Path) -> None:
    commit = "b" * 40
    target = installer.Target("test", "example", str(tmp_path / "releases"))
    previous = str(tmp_path / "releases" / commit)
    manifest = {
        "commit": commit,
        "catalog_version": "spawn-catalog-v2",
        "runtime_package": {"sha256": "a" * 64},
    }
    verified = []

    def fake_remote(_target, command, **_kwargs):
        if "manifest.json" in command:
            return json.dumps(manifest)
        if "os.path.realpath" in command:
            return previous + "/runtime/bin/agent-orch"
        if "readlink" in command:
            return previous
        return ""

    monkeypatch.setattr(installer, "_remote", fake_remote)
    monkeypatch.setattr(installer, "_verify_release", lambda *_args, **_kwargs: verified.append(_args[1:3]))

    state = installer.rollback(target, previous, dry_run=False)

    assert state["commit"] == commit
    assert state["catalog_version"] == "spawn-catalog-v2"
    assert verified == [(commit, manifest)]


def test_stage_does_not_migrate_live_legacy_state(monkeypatch, tmp_path: Path) -> None:
    target = installer.Target("test", "local", str(tmp_path / "releases"))
    old_release = tmp_path / "releases" / "old"
    old_release.mkdir(parents=True)
    (tmp_path / "releases" / "active").symlink_to(old_release)

    monkeypatch.setattr(installer, "_verify_release", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(installer, "_snapshot_legacy_residue", lambda *_args, **_kwargs: None)
    migrated: list[str] = []
    monkeypatch.setattr(
        installer,
        "_migrate_legacy_residue",
        lambda _target, stamp, **_kwargs: migrated.append(stamp),
    )

    installer.stage(target, tmp_path, "a" * 40, {"archive_sha256": "b" * 64, "stamp": "rollout-test"}, dry_run=True)
    assert (tmp_path / "releases" / "active").resolve() == old_release
    assert migrated == []


def test_stage_migrates_legacy_residue_and_rollback_restores_launcher(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "home" / ".local" / "share" / "pentacle" / "releases"
    target = installer.Target("test", "localhost", str(root))
    old_commit, new_commit = "a" * 40, "b" * 40
    old_bundle, old_manifest = _localhost_bundle(tmp_path, old_commit)
    new_bundle, new_manifest = _localhost_bundle(tmp_path, new_commit)
    user_base = tmp_path / "python-user-base"
    monkeypatch.setenv("PYTHONUSERBASE", str(user_base))

    assert installer.stage(target, old_bundle, old_commit, old_manifest, dry_run=False, runner=_localhost_runner) == "staged_verified"
    old_release = root / old_commit
    (root / "active").symlink_to(old_release)
    launcher = Path(installer._stable_launcher(target))
    legacy_root = root.parents[1] / "agent-orch-releases"
    legacy_launcher = legacy_root / "current" / "bin" / "agent-orch"
    legacy_launcher.parent.mkdir(parents=True)
    legacy_launcher.write_text("#!/bin/sh\\nexit 0\\n", encoding="utf-8")
    legacy_launcher.chmod(0o755)
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(legacy_launcher)
    pth = user_base / "legacy-agent-orch.pth"
    pth.parent.mkdir(parents=True)
    pth.write_text("/Users/example/repos/pentacle/services/agent-orch\\n", encoding="utf-8")

    assert installer.stage(target, new_bundle, new_commit, new_manifest, dry_run=False, runner=_localhost_runner) == "staged_verified"
    assert pth.exists()
    assert legacy_root.is_dir()
    assert os.readlink(launcher) == str(legacy_launcher)
    assert not (root / "rollouts" / f"{new_manifest['stamp']}.agent-orch-releases").exists()

    installer.activate(target, new_commit, new_manifest, str(old_release), dry_run=False, runner=_localhost_runner)
    assert not pth.exists()
    assert not os.path.lexists(legacy_root)
    assert (root / "rollouts" / f"{new_manifest['stamp']}.agent-orch-releases").is_dir()
    rollback = installer.rollback(target, str(old_release), stamp=str(new_manifest["stamp"]), dry_run=False, runner=_localhost_runner)

    assert rollback["active"] == str(old_release)
    assert os.readlink(launcher) == str(legacy_launcher)
    assert legacy_root.is_dir()


@pytest.mark.parametrize(
    ("needle", "replacement"),
    (
        (
            "os.replace(legacy_root, legacy_backup)",
            "os.replace(legacy_root, legacy_backup); raise RuntimeError('injected_after_legacy_move')",
        ),
        (
            "path.unlink()",
            "path.unlink(); raise RuntimeError('injected_after_pth_remove')",
        ),
    ),
)
def test_stage_migration_failure_restores_full_legacy_state(
    monkeypatch, tmp_path: Path, needle: str, replacement: str
) -> None:
    root = tmp_path / "home" / ".local" / "share" / "pentacle" / "releases"
    target = installer.Target("test", "localhost", str(root))
    old_commit, new_commit = "a" * 40, "b" * 40
    old_bundle, old_manifest = _localhost_bundle(tmp_path, old_commit)
    new_bundle, new_manifest = _localhost_bundle(tmp_path, new_commit)
    user_base = tmp_path / "python-user-base"
    monkeypatch.setenv("PYTHONUSERBASE", str(user_base))

    assert installer.stage(target, old_bundle, old_commit, old_manifest, dry_run=False, runner=_localhost_runner) == "staged_verified"
    old_release = root / old_commit
    (root / "active").symlink_to(old_release)
    launcher = Path(installer._stable_launcher(target))
    legacy_root = root.parents[1] / "agent-orch-releases"
    legacy_launcher = legacy_root / "current" / "bin" / "agent-orch"
    legacy_launcher.parent.mkdir(parents=True)
    legacy_launcher.write_text("#!/bin/sh\\nexit 0\\n", encoding="utf-8")
    legacy_launcher.chmod(0o755)
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(legacy_launcher)
    pth = user_base / "legacy-agent-orch.pth"
    pth.parent.mkdir(parents=True)
    pth_text = "/Users/example/repos/pentacle/services/agent-orch\\n"
    pth.write_text(pth_text, encoding="utf-8")

    injected = False

    def failing_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal injected
        if (
            argv[0] == "ssh"
            and not injected
            and needle in argv[-1]
            and shlex.split(argv[-1])[-1].endswith(" 1")
        ):
            argv = [*argv[:-1], argv[-1].replace(needle, replacement, 1)]
            injected = True
        return _localhost_runner(argv, **kwargs)

    assert installer.stage(target, new_bundle, new_commit, new_manifest, dry_run=False, runner=failing_runner) == "staged_verified"
    with pytest.raises(subprocess.CalledProcessError):
        installer.activate(target, new_commit, new_manifest, str(old_release), dry_run=False, runner=failing_runner)

    assert injected
    installer.rollback(target, str(old_release), stamp=str(new_manifest["stamp"]), dry_run=False, runner=_localhost_runner)
    assert (root / "active").resolve() == old_release
    assert os.readlink(launcher) == str(legacy_launcher)
    assert legacy_launcher.is_file()
    assert pth.read_text(encoding="utf-8") == pth_text
    assert not os.path.lexists(root / "rollouts" / f"{new_manifest['stamp']}.agent-orch-releases")


def test_stage_preserves_checkout_backed_launcher_before_activation(tmp_path: Path) -> None:
    root = tmp_path / "home" / ".local" / "share" / "pentacle" / "releases"
    target = installer.Target("test", "localhost", str(root))
    commit = "a" * 40
    bundle, manifest = _localhost_bundle(tmp_path, commit)
    checkout_launcher = tmp_path / "checkout" / "services" / "agent-orch" / "agent-orch"
    checkout_launcher.parent.mkdir(parents=True)
    checkout_launcher.write_text("#!/bin/sh\\nexit 0\\n", encoding="utf-8")
    checkout_launcher.chmod(0o755)
    launcher = Path(installer._stable_launcher(target))
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(checkout_launcher)

    assert installer.stage(target, bundle, commit, manifest, dry_run=False, runner=_localhost_runner) == "staged_verified"

    assert os.readlink(launcher) == str(checkout_launcher)


def test_stage_migrates_homebrew_user_site_checkout_topology(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "home" / ".local" / "share" / "pentacle" / "releases"
    target = installer.Target("hostb", "localhost", str(root))
    old_commit, new_commit = "a" * 40, "b" * 40
    old_bundle, old_manifest = _localhost_bundle(tmp_path, old_commit)
    new_bundle, new_manifest = _localhost_bundle(tmp_path, new_commit)
    assert installer.stage(target, old_bundle, old_commit, old_manifest, dry_run=False, runner=_localhost_runner) == "staged_verified"
    old_release = root / old_commit
    (root / "active").symlink_to(old_release)
    homebrew_launcher = tmp_path / "opt" / "homebrew" / "bin" / "agent-orch"
    checkout_launcher = tmp_path / "home" / "Library" / "Python" / "3.13" / "bin" / "agent-orch"
    checkout_launcher.parent.mkdir(parents=True)
    checkout_launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    checkout_launcher.chmod(0o755)
    homebrew_launcher.parent.mkdir(parents=True)
    homebrew_launcher.symlink_to(checkout_launcher)
    pth = tmp_path / "home" / "Library" / "Python" / "3.13" / "lib" / "python" / "site-packages" / "__editable__.agent_orch-0.1.0.pth"
    pth.parent.mkdir(parents=True)
    pth.write_text("/Users/example/deploy/pentacle/services/agent-orch\n", encoding="utf-8")
    monkeypatch.setattr(
        installer,
        "_path_launchers",
        lambda _target: [installer._stable_launcher(target), str(homebrew_launcher)],
        raising=False,
    )

    assert installer.stage(target, new_bundle, new_commit, new_manifest, dry_run=False, runner=_localhost_runner) == "staged_verified"

    assert os.readlink(homebrew_launcher) == str(checkout_launcher)
    assert pth.exists()

    installer.activate(target, new_commit, new_manifest, str(old_release), dry_run=False, runner=_localhost_runner)
    assert not pth.exists()
    assert os.readlink(homebrew_launcher) == str(root / "active" / "runtime" / "bin" / "agent-orch")

    installer.rollback(target, str(old_release), stamp=str(new_manifest["stamp"]), dry_run=False, runner=_localhost_runner)
    assert os.readlink(homebrew_launcher) == str(checkout_launcher)
    assert pth.exists()


def test_homebrew_migration_failure_restores_bootstrap_cli(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "home" / ".local" / "share" / "pentacle" / "releases"
    target = installer.Target("hostb", "localhost", str(root))
    old_commit, new_commit = "a" * 40, "b" * 40
    old_bundle, old_manifest = _localhost_bundle(tmp_path, old_commit)
    new_bundle, new_manifest = _localhost_bundle(tmp_path, new_commit)
    assert installer.stage(target, old_bundle, old_commit, old_manifest, dry_run=False, runner=_localhost_runner) == "staged_verified"
    (root / "active").symlink_to(root / old_commit)
    homebrew_launcher = tmp_path / "opt" / "homebrew" / "bin" / "agent-orch"
    checkout_launcher = tmp_path / "home" / "Library" / "Python" / "3.13" / "bin" / "agent-orch"
    checkout_launcher.parent.mkdir(parents=True)
    checkout_launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    checkout_launcher.chmod(0o755)
    homebrew_launcher.parent.mkdir(parents=True)
    homebrew_launcher.symlink_to(checkout_launcher)
    pth = tmp_path / "home" / "Library" / "Python" / "3.13" / "lib" / "python" / "site-packages" / "__editable__.agent_orch-0.1.0.pth"
    pth.parent.mkdir(parents=True)
    pth_text = "/Users/example/deploy/pentacle/services/agent-orch\n"
    pth.write_text(pth_text, encoding="utf-8")
    monkeypatch.setattr(installer, "_path_launchers", lambda _target: [installer._stable_launcher(target), str(homebrew_launcher)])

    injected = False

    def failing_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal injected
        if argv[0] == "ssh" and not injected and "path.unlink()" in argv[-1] and shlex.split(argv[-1])[-1].endswith(" 1"):
            argv = [*argv[:-1], argv[-1].replace("path.unlink()", "path.unlink(); raise RuntimeError('injected_homebrew_pth_remove')", 1)]
            injected = True
        return _localhost_runner(argv, **kwargs)

    assert installer.stage(target, new_bundle, new_commit, new_manifest, dry_run=False, runner=failing_runner) == "staged_verified"
    with pytest.raises(subprocess.CalledProcessError):
        installer.activate(target, new_commit, new_manifest, str(root / old_commit), dry_run=False, runner=failing_runner)

    assert injected
    installer.rollback(target, str(root / old_commit), stamp=str(new_manifest["stamp"]), dry_run=False, runner=_localhost_runner)
    assert (root / "active").resolve() == root / old_commit
    assert os.readlink(homebrew_launcher) == str(checkout_launcher)
    assert pth.read_text(encoding="utf-8") == pth_text


def test_pointer_replacement_does_not_follow_directory_symlink(tmp_path: Path) -> None:
    root = tmp_path / "releases"
    old_release = root / "old"
    new_release = root / "new"
    old_release.mkdir(parents=True)
    new_release.mkdir()
    (root / "active").symlink_to(old_release)
    command = installer._replace_pointer_command(
        installer.Target("test", "local", str(root)),
        "active",
        str(new_release),
    )
    result = subprocess.run(["sh", "-lc", command], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert (root / "active").resolve() == new_release
    assert not (old_release / "active.new").exists()


def test_activation_replaces_existing_active_and_previous_symlinks(tmp_path: Path) -> None:
    commit = "b" * 40
    stamp = "rollout-test"
    root = tmp_path / "releases"
    release = root / commit
    profile = release / "app" / "services" / "_shared" / "spawn_profiles.py"
    profile.parent.mkdir(parents=True)
    profile.write_bytes(b"profile contract")
    archive = release / "pentacle.tar"
    archive.write_bytes(b"release archive")
    manifest = {
        "commit": commit,
        "stamp": stamp,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "contract": {"sha256": hashlib.sha256(profile.read_bytes()).hexdigest()},
        "catalog_version": "spawn-catalog-v2",
        "provider_packages": {"codex": {"binary": "codex"}},
    }
    (release / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    old_release = root / "old"
    old_release.mkdir()
    for pointer in ("active", "previous", f"rollouts/{stamp}.previous", f"rollouts/{stamp}.release"):
        path = root / pointer
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(old_release)

    def local_runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(shlex.split(argv[-1]), text=True, capture_output=True, check=False)

    state = installer.activate(
        installer.Target("test", "local", str(root)),
        commit,
        manifest,
        str(old_release),
        dry_run=False,
        runner=local_runner,
    )
    assert state == {"active": str(release), "previous": str(old_release)}
    assert (root / "active").resolve() == release
    assert (root / "previous").resolve() == old_release
    assert (root / "rollouts" / f"{stamp}.previous").resolve() == old_release
    assert (root / "rollouts" / f"{stamp}.release").resolve() == release
    assert not any(old_release.glob("*.new"))
