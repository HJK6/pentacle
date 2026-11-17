"""Real-process loopback cap admission and delivery, without model calls."""
import os
from pathlib import Path
import shutil
import subprocess
import sys


def test_cap_warning_spawn_refusal_and_grace_over_socket(tmp_path):
    python=shutil.which(sys.executable)
    assert python
    environment=os.environ.copy()
    environment['HOME']=str(tmp_path/'home')
    result=subprocess.run([python,str(Path(__file__).with_name('usage_caps_socket_fixture.py')),str(tmp_path)],
                          env=environment,text=True,capture_output=True,timeout=45)
    (tmp_path/'socket-proof.log').write_text(result.stdout+result.stderr)
    assert result.returncode==0,result.stdout+result.stderr
    assert '"warning_submitted": true' in result.stdout
    print(result.stdout)
