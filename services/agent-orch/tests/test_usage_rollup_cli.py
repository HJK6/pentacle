"""`agent-orch usage rollup` passes through to the read-only rollup tool."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

from agent_orch import cli


def test_usage_rollup_tool_path_resolves_in_checkout():
    assert cli.USAGE_ROLLUP_TOOL.is_file()
    assert cli.USAGE_ROLLUP_TOOL.parts[-3:] == ("chat-stream-v2", "tools", "usage_rollup.py")


def test_usage_rollup_passes_flags_verbatim(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv: calls.append(argv) or 0)
    assert cli.main(["usage", "rollup", "--spec", "spec_x", "--json", "--help"]) == 0
    assert calls == [[sys.executable, str(cli.USAGE_ROLLUP_TOOL), "--spec", "spec_x", "--json", "--help"]]


def test_usage_rollup_exit_code_and_override(monkeypatch, tmp_path, capsys):
    tool = tmp_path / "tool.py"
    tool.write_text("import sys; sys.exit(3)\n")
    monkeypatch.setenv("PENTACLE_USAGE_ROLLUP_TOOL", str(tool))
    assert cli.main(["usage", "rollup", "--calibrate"]) == 3
    monkeypatch.setenv("PENTACLE_USAGE_ROLLUP_TOOL", str(tmp_path / "missing.py"))
    assert cli.main(["usage", "rollup"]) == 2
    assert "tool not found" in capsys.readouterr().err


def test_usage_host_readback_unchanged():
    args = cli.build_parser().parse_args(["usage", "--host", "thoth", "--json"])
    assert args.func is cli.usage and args.host == "thoth"


def _release_installer():
    import importlib.util

    script = Path(__file__).parents[1] / "deploy" / "install_fleet_spawn_tooling.py"
    spec = importlib.util.spec_from_file_location("install_fleet_spawn_tooling_rollup", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_usage_rollup_resolves_from_a_release_install(tmp_path):
    # Build the runtime exactly as a fleet release does: the app tree under
    # <release>/app, agent_orch copied into the runtime venv's site-packages.
    installer = _release_installer()
    services_src = Path(__file__).resolve().parents[2]
    release = tmp_path / ("a" * 40)
    services = release / "app" / "services"
    ignore = shutil.ignore_patterns(".venv", "tests", "__pycache__", "node_modules")
    for name in ("agent-orch", "_shared", "chat-stream-v2"):
        shutil.copytree(services_src / name, services / name, ignore=ignore)
    scripts = release / "app" / "scripts" / "lib"
    scripts.mkdir(parents=True)
    shutil.copy2(services_src.parent / "scripts/lib/gate_attempts.py", scripts / "gate_attempts.py")
    installer._runtime_dependency_bundle(release / "app")
    prepared = subprocess.run(
        ["sh", "-lc", installer._runtime_prepare_command(str(release), ">=3.11")],
        text=True,
        capture_output=True,
    )
    assert prepared.returncode == 0, prepared.stderr
    launcher = release / "runtime" / "bin" / "agent-orch"
    env = {k: v for k, v in os.environ.items() if k != "PENTACLE_USAGE_ROLLUP_TOOL"}

    helped = subprocess.run([str(launcher), "usage", "rollup", "--help"], text=True, capture_output=True, env=env)

    assert helped.returncode == 0, helped.stderr
    assert "--spec" in helped.stdout and "--calibrate" in helped.stdout
    # Read a real manifest through the installed CLI in an isolated immutable release.
    import json
    import sqlite3
    data = tmp_path / "ledger"
    data.mkdir()
    conn = sqlite3.connect(data / "sessions.db")
    conn.execute("PRAGMA user_version=1")
    conn.close()
    manifest = tmp_path / "attempts.json"
    manifest.write_text(json.dumps({"schema":"pentacle.test-attempt-manifest.v1","attempts":[]}))
    rolled = subprocess.run([str(launcher),"usage","rollup","--spec","spec_fixture","--attempt-manifest",str(manifest),
                             "--data-dir",str(data),"--work-root",str(tmp_path / "work"),"--json"],
                            text=True,capture_output=True,env=env)
    assert rolled.returncode == 0, rolled.stderr
    assert json.loads(rolled.stdout)["specs"][0]["test_health"]["coverage"]["valid_attempts"] == 0


def test_attempt_manifests_forward_verbatim_without_dispatch_changes(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv: calls.append(argv) or 0)
    args = ["--spec", "spec_x", "--attempt-manifest", "/tmp/first.json", "--attempt-manifest", "/tmp/reviewer.json", "--json"]
    assert cli.main(["usage", "rollup", *args]) == 0
    assert calls == [[sys.executable, str(cli.USAGE_ROLLUP_TOOL), *args]]
