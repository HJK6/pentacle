from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

from .test_deploy_script import deploy_mod


def test_collector_template_renders_the_activated_v2_checkout(tmp_path: Path) -> None:
    template = Path(__file__).parents[2] / "chat-stream-v2/deploy/com.pentacle.usage-state-collector.plist"
    repo = tmp_path / "release"
    destination = repo / "services/chat-stream-v2/deploy/com.pentacle.usage-state-collector.plist"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(template.read_bytes())

    rendered = plistlib.loads(deploy_mod._render_v2_usage_collector_plist(repo))
    arguments = rendered["ProgramArguments"]
    assert arguments[:2] == [
        str(repo / "services/chat-stream-v2/.venv/bin/python"),
        str(repo / "services/chat-stream-v2/tools/collect_usage_state.py"),
    ]
    assert rendered["EnvironmentVariables"]["PATH"].startswith(
        str(repo / "services/chat-stream-v2/deploy/usage-probe-bin") + ":"
    )


def test_collector_template_requires_release_checkout_placeholders(tmp_path: Path) -> None:
    repo = tmp_path / "release"
    template = repo / deploy_mod.USAGE_COLLECTOR_TEMPLATE
    template.parent.mkdir(parents=True)
    template.write_bytes(plistlib.dumps({"Label": deploy_mod.USAGE_COLLECTOR_LABEL}))

    try:
        deploy_mod._render_v2_usage_collector_plist(repo)
    except deploy_mod.DeployError as exc:
        assert "release-checkout placeholders" in str(exc)
    else:
        raise AssertionError("collector template without release-checkout placeholders rendered")


def test_v2_deploy_installs_collector_and_removes_daemon_claude_shim(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "release"
    daemon_plist = tmp_path / "com.pentacle.chat-streamd-v2.plist"
    daemon_plist.write_bytes(plistlib.dumps({
        "Label": deploy_mod.V2_DAEMON_LABEL,
        "ProgramArguments": [
            str(repo / "services/chat-stream-v2/.venv/bin/python"),
            str(repo / "services/chat-stream-v2/main.py"),
            "--claude-bin",
            "/shim",
            "--db",
            "/state",
        ],
    }))
    template = repo / "services/chat-stream-v2/deploy/com.pentacle.usage-state-collector.plist"
    template.parent.mkdir(parents=True)
    source_template = Path(__file__).parents[2] / "chat-stream-v2/deploy/com.pentacle.usage-state-collector.plist"
    template.write_bytes(source_template.read_bytes())
    installed = tmp_path / "com.pentacle.usage-state-collector.plist"
    calls: list[tuple[tuple[str, ...], Path]] = []

    def runner(command: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append((command, cwd))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(deploy_mod, "_launchd_plist_path", lambda _label: daemon_plist)
    monkeypatch.setattr(deploy_mod, "_usage_collector_plist_path", lambda: installed)

    assert deploy_mod._ensure_v2_usage_probe_launchd(repo, runner) is True
    args = plistlib.loads(daemon_plist.read_bytes())["ProgramArguments"]
    assert "--claude-bin" not in args
    assert "/shim" not in args
    assert installed.read_bytes() == deploy_mod._render_v2_usage_collector_plist(repo)
    assert all(cwd == repo for _command, cwd in calls)
    assert any("com.pentacle.usage-state-collector" in " ".join(command) for command, _cwd in calls)


def test_usage_probe_launchd_rollback_restores_original_bytes(tmp_path: Path, monkeypatch) -> None:
    daemon_plist = tmp_path / "com.pentacle.chat-streamd-v2.plist"
    daemon_bytes = plistlib.dumps({"ProgramArguments": ["python", "--claude-bin", "/shim"]})
    daemon_plist.write_bytes(daemon_bytes)
    collector = tmp_path / "com.pentacle.usage-state-collector.plist"
    collector_bytes = b"original collector"
    collector.write_bytes(collector_bytes)
    calls: list[tuple[tuple[str, ...], Path]] = []
    repo = tmp_path / "release"

    def runner(command: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append((command, cwd))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(deploy_mod, "_launchd_plist_path", lambda _label: daemon_plist)
    monkeypatch.setattr(deploy_mod, "_usage_collector_plist_path", lambda: collector)

    rollback = deploy_mod._v2_usage_probe_rollback(repo, runner)
    daemon_plist.write_bytes(plistlib.dumps({"ProgramArguments": ["python"]}))
    collector.write_bytes(b"new collector")
    rollback()

    assert daemon_plist.read_bytes() == daemon_bytes
    assert collector.read_bytes() == collector_bytes
    assert all(cwd == repo for _command, cwd in calls)
    assert any("bootout" in command for command, _cwd in calls)
    assert any("bootstrap" in command for command, _cwd in calls)
