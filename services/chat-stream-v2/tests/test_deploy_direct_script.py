"""Exercise scanner imports with the same sys.path as direct deploy.py execution."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from .test_deploy_script import DEPLOY_PATH, _u2_line, deploy_mod


@pytest.mark.parametrize("abnormal_close", [False, True])
def test_direct_script_scanner_refuses_unattributed_window(tmp_path, abnormal_close):
    log = tmp_path / "daemon.log"
    log.write_text(
        deploy_mod.V2_BOOT_LINE + "\n"
        + (_u2_line(close_code=1011, close_sent_code=1011) + "\n" if abnormal_close else "")
    )
    probe = """
import json, runpy, sys
from pathlib import Path
script, log = map(Path, sys.argv[1:])
# Direct script execution puts deploy/ before the service root. A fresh process
# prevents pytest's preloaded deploy namespace from hiding that collision.
sys.path.insert(0, str(script.parent))
module = runpy.run_path(str(script), run_name='deploy_import_probe')
scan = module['_scan_slow_consumer_window']
scan.__globals__['_daemon_log_paths'] = lambda _: (log,)
scan.__globals__['_launchd_environment'] = lambda _: {}
result = scan(module['SERVICES']['chat-streamd-v2'], 0)
result['scanner_module'] = sys.modules['deploy.packet.log_scan'].__file__
print(json.dumps(result))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(DEPLOY_PATH), str(log)],
        cwd=tmp_path, text=True, capture_output=True, timeout=15, check=True,
    )
    result = json.loads(completed.stdout)
    assert result["scan_complete"] and result["boot_marker_seen"]
    assert result["bytes_read"] == log.stat().st_size
    assert Path(result["scanner_module"]).resolve() == DEPLOY_PATH.parent / "packet" / "log_scan.py"
    assert result["outcome"] == "failed"
    assert result["attribution"]["outcome"] == "attribution_ambiguous"
    assert result["failure_count"] == int(abnormal_close)
    assert [item["kind"] for item in result["failures"]] == (["1011_close"] if abnormal_close else [])
