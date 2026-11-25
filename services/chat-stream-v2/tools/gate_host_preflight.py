#!/usr/bin/env python3
"""Qualify the local child environment using only owned probe resources."""
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from machines import resolve_ssh_control_path


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=3)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f'exit {result.returncode}')
    return result.stdout


def main():
    operation = 'process enumeration'
    try:
        with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)', 'gate-owned-probe']) as child:
            try:
                table = command(['ps', '-axo', 'pid='])
                output = command(['ps', '-ww', '-p', str(child.pid), '-o', 'lstart=', '-o', 'args='])
                if str(child.pid) not in table.split() or not re.match(r'\S+\s+\S+\s+\d{1,2}\s+\d\d:\d\d:\d\d\s+\d{4}\s+', output.strip()) or 'gate-owned-probe' not in output:
                    raise RuntimeError('owned child start/argv unavailable')
            finally:
                child.terminate()
                child.wait(timeout=3)
        operation = 'ssh executable'
        command(['ssh', '-V'])
        operation = 'ssh control socket'
        target = resolve_ssh_control_path(f'probe-{uuid.uuid4().hex}', required=True)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(target))
            try:
                pass
            finally:
                target.unlink()
        operation = 'tmux socket/session'
        with tempfile.TemporaryDirectory(prefix='gp-', dir='/tmp') as folder:
            target = str(Path(folder) / 's')
            args = ['tmux', '-f', os.devnull, '-S', target]
            try:
                command(args + ['new-session', '-d', '-s', 'probe', 'sleep', '30'])
                command(args + ['has-session', '-t', '=probe'])
            finally:
                if Path(target).exists():
                    command(args + ['kill-server'])
                check = subprocess.run(args + ['has-session', '-t', '=probe'], capture_output=True, text=True, timeout=3)
                if check.returncode == 0:
                    raise RuntimeError('owned tmux session remains after cleanup')
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        print(f'[v2-gate-preflight] FAIL: {operation}: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 2
    print('[v2-gate-preflight] PASS: owned process, ssh socket and tmux')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
