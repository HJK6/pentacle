"""The running deploy must retain its modules across a real checkout switch."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from .test_deploy_script import DEPLOY_PATH


@pytest.mark.parametrize("skewed_module", ["log_scan", "managed_logs"])
def test_new_deploy_scans_after_checkout_switch_to_old_tree(tmp_path, skewed_module):
    repo = tmp_path / "repository"
    service = repo / "services/chat-stream-v2"
    service.mkdir(parents=True)
    shutil.copytree(DEPLOY_PATH.parent, service / "deploy", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(DEPLOY_PATH.parents[1] / "managed_logs.py", service / "managed_logs.py")

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "-q")
    git("config", "user.name", "Test Agent")
    git("config", "user.email", "test@example.invalid")
    scanner = service / "deploy/packet/log_scan.py"
    current = scanner.read_text()
    # The older module ends before generation attribution was added. Retain
    # its real scanner implementation without requiring historical Git objects
    # (CI may use a shallow clone).
    previous, boundary, _ = current.partition("class AttributionAmbiguous")
    assert boundary, "the checkout-switch fixture must remove the newer API"
    assert "class AttributionAmbiguous" not in previous
    managed = service / "managed_logs.py"
    managed_current = managed.read_text()
    if skewed_module == "log_scan":
        scanner.write_text(previous)
    else:
        assert "class Config:" in managed_current
        managed.write_text(managed_current.replace("class Config:", "class PreviousConfig:"))
    git("add", ".")
    git("commit", "-qm", "Old deploy tree")
    old_sha = git("rev-parse", "HEAD")
    scanner.write_text(current)
    managed.write_text(managed_current)
    git("add", ".")
    git("commit", "-qm", "New deploy tree")
    log = tmp_path / "boot.log"
    probe = """
import json, runpy, subprocess, sys
from pathlib import Path
repo, script, log, old_sha = sys.argv[1:]
sys.path.insert(0, str(Path(script).parent))
module = runpy.run_path(script, run_name='deploy_switch_probe')
subprocess.run(['git', '-C', repo, 'checkout', '--detach', old_sha], check=True, capture_output=True)
log = Path(log)
log.write_text(module['V2_BOOT_LINE'] + '\\n')
scan = module['_scan_slow_consumer_window']
scan.__globals__['_daemon_log_paths'] = lambda _: (log,)
scan.__globals__['_launchd_environment'] = lambda _: {}
result = scan(module['SERVICES']['chat-streamd-v2'], 0,
              runner=lambda argv, cwd: subprocess.CompletedProcess(argv, 1, '', ''))
assert result['scan_complete'], result
assert result['boot_marker_seen'], result
print(json.dumps(result))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(repo), str(service / "deploy/deploy.py"), str(log), old_sha],
        text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["scan_complete"]
