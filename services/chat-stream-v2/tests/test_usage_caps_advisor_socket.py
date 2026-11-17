"""Real-pane advisor restore contract, blocked explicitly when tmux is absent."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest


def test_blocked_pool_advisor_restore_over_socket(tmp_path,request):
    if not shutil.which('tmux'):
        pytest.skip('BLOCKED: tmux unavailable; fleet must run real-pane advisor restore proof')
    wrapper=request.getfixturevalue('isolated_tmux_env')
    python=shutil.which(sys.executable)
    assert python
    environment=os.environ.copy();environment['HOME']=str(tmp_path/'home')
    result=subprocess.run([python,str(Path(__file__).with_name('usage_caps_advisor_fixture.py')),str(tmp_path),wrapper],
        env=environment,text=True,capture_output=True,timeout=90)
    (tmp_path/'advisor-proof.log').write_text(result.stdout+result.stderr)
    assert result.returncode==0,result.stdout+result.stderr
    assert '"restore_admitted": true' in result.stdout
    print(result.stdout)
