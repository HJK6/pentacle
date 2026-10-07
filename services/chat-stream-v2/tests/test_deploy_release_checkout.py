from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest


from .test_deploy_script import deploy_mod


V2_SERVICE = deploy_mod.SERVICES["chat-streamd-v2"]


def test_v2_release_layout_uses_the_live_checkout_and_dependencies() -> None:
    repo = Path("/release")

    assert deploy_mod.DEFAULT_RELEASE_CHECKOUT == Path.home() / "repos/pentacle-v2"
    assert V2_SERVICE.requirements_path == "services/chat-stream-v2/requirements.txt"
    assert deploy_mod._venv_python(repo, V2_SERVICE) == repo / "services/chat-stream-v2/.venv/bin/python"
    assert deploy_mod._gate_path(repo, V2_SERVICE).split(":", 1)[0] == str(
        repo / "services/chat-stream-v2/.venv/bin"
    )


def test_v2_launchd_program_outside_release_is_refused_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "release"
    template = repo / deploy_mod.USAGE_COLLECTOR_TEMPLATE
    template.parent.mkdir(parents=True)
    template.write_bytes(
        (Path(__file__).parents[2] / deploy_mod.USAGE_COLLECTOR_TEMPLATE.relative_to("services")).read_bytes()
    )
    daemon_plist = tmp_path / "com.pentacle.chat-streamd-v2.plist"
    original = plistlib.dumps(
        {
            "Label": deploy_mod.V2_DAEMON_LABEL,
            "ProgramArguments": [
                "/Users/example/deploy/pentacle/services/chat-stream-v2/.venv/bin/python",
                "/Users/example/deploy/pentacle/services/chat-stream-v2/main.py",
                "--claude-bin",
                "/shim",
            ],
        }
    )
    daemon_plist.write_bytes(original)
    collector_plist = tmp_path / "com.pentacle.usage-state-collector.plist"
    calls: list[tuple[str, ...]] = []

    def runner(command, _cwd):
        calls.append(tuple(str(part) for part in command))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(deploy_mod, "_launchd_plist_path", lambda _label: daemon_plist)
    monkeypatch.setattr(deploy_mod, "_usage_collector_plist_path", lambda: collector_plist)

    with pytest.raises(deploy_mod.DeployError, match=r"ProgramArguments\[0\].*release checkout"):
        deploy_mod._ensure_v2_usage_probe_launchd(repo, runner)

    assert daemon_plist.read_bytes() == original
    assert not collector_plist.exists()
    assert calls == []


def test_daemon_log_falls_back_to_absolute_standard_out_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "launchd.log"
    plist_path = tmp_path / "com.pentacle.chat-streamd-v2.plist"
    plist_path.write_bytes(
        plistlib.dumps(
            {
                "EnvironmentVariables": {},
                "StandardOutPath": str(log_path),
            }
        )
    )
    monkeypatch.setattr(deploy_mod, "_launchd_plist_path", lambda _label: plist_path)

    assert deploy_mod._daemon_log_path(V2_SERVICE) == log_path


def test_daemon_log_refuses_relative_standard_out_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist_path = tmp_path / "com.pentacle.chat-streamd-v2.plist"
    plist_path.write_bytes(plistlib.dumps({"StandardOutPath": "relative/launchd.log"}))
    monkeypatch.setattr(deploy_mod, "_launchd_plist_path", lambda _label: plist_path)

    with pytest.raises(deploy_mod.DeployError, match="StandardOutPath must be absolute"):
        deploy_mod._daemon_log_path(V2_SERVICE)


def test_deploy_plist_templates_have_no_stale_release_checkout_literal() -> None:
    repo = Path(__file__).resolve().parents[3]
    stale = b"/Users/example/deploy/pentacle"
    offenders = [
        path.relative_to(repo)
        for path in repo.glob("services/*/deploy/*.plist")
        if stale in path.read_bytes()
    ]

    assert offenders == []


def test_fleet_smoke_template_renders_the_activated_v2_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "release"
    template = repo / deploy_mod.SPAWN_FLEET_SMOKE_TEMPLATE
    template.parent.mkdir(parents=True)
    source = Path(__file__).parents[2] / deploy_mod.SPAWN_FLEET_SMOKE_TEMPLATE.relative_to("services")
    template.write_bytes(source.read_bytes())

    rendered = plistlib.loads(deploy_mod._render_v2_spawn_fleet_smoke_plist(repo))
    command = rendered["ProgramArguments"][2]
    assert f"cd {repo}" in command
    assert str(repo / "services/chat-stream-v2/.venv/bin/python") in command
    assert str(repo / "services/chat-stream-v2/tools/spawn_fleet_smoke.py") in command
    assert rendered["StartInterval"] == 43200
    assert rendered["EnvironmentVariables"]["PENTACLE_MACHINES_FILE"] == str(
        Path.home() / ".config/pentacle-public/machines.json"
    )


def test_fleet_smoke_helper_runs_from_the_gate_tools_worktree_not_the_runtime_checkout(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    repo = tmp_path / "release"
    template = repo / deploy_mod.SPAWN_FLEET_SMOKE_TEMPLATE
    template.parent.mkdir(parents=True)
    source = Path(__file__).parents[2] / deploy_mod.SPAWN_FLEET_SMOKE_TEMPLATE.relative_to("services")
    template.write_bytes(source.read_bytes())

    command = plistlib.loads(deploy_mod._render_v2_spawn_fleet_smoke_plist(repo))["ProgramArguments"][2]
    helper = home / "repos/pentacle-gate-tools/services/chat-stream-v2/tools/gate_at_fire.py"
    py = repo / "services/chat-stream-v2/.venv/bin/python"
    # The gate helper lives in its own worktree; the candidate and the wrapped command are the runtime checkout.
    assert f"{py} {helper} run --job spawn-fleet-smoke" in command
    assert f"--candidate-repo {repo} -- {py} {repo}/services/chat-stream-v2/tools/spawn_fleet_smoke.py" in command
    assert str(repo / "services/chat-stream-v2/tools/gate_at_fire.py") not in command


def test_a_render_for_a_runtime_checkout_reproduces_the_hand_installed_live_command(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "synthetic-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    repo = home / "repos/pentacle-public-runtime"
    source = (Path(__file__).parents[2] / deploy_mod.SPAWN_FLEET_SMOKE_TEMPLATE.relative_to("services")).read_bytes()
    root = tmp_path / "r"  # render against a throwaway root, then swap in the runtime path
    (root / deploy_mod.SPAWN_FLEET_SMOKE_TEMPLATE).parent.mkdir(parents=True)
    (root / deploy_mod.SPAWN_FLEET_SMOKE_TEMPLATE).write_bytes(source)
    rendered = deploy_mod._render_v2_spawn_fleet_smoke_plist(root).replace(str(root).encode(), str(repo).encode())
    command = plistlib.loads(rendered)["ProgramArguments"][2]
    rt = str(repo)
    assert command.startswith(
        f"cd {rt} && {rt}/services/chat-stream-v2/.venv/bin/python "
        f"{home}/repos/pentacle-gate-tools/services/chat-stream-v2/tools/gate_at_fire.py run --job spawn-fleet-smoke "
    )
    assert command.endswith(f"--candidate-repo {rt} -- {rt}/services/chat-stream-v2/.venv/bin/python "
                            f"{rt}/services/chat-stream-v2/tools/spawn_fleet_smoke.py")
