#!/usr/bin/env python3
"""Exercise the shipped Electron UI against an isolated daemon and native provider fixture."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    tmux = shutil.which('tmux')
    if not tmux or not shutil.which('lsof'):
        raise SystemExit('Install tmux and lsof before running the desktop smoke.')
    root = Path(tempfile.mkdtemp(prefix='pentacle-public-smoke-')).resolve()
    root.chmod(0o700)
    projects = root / '.claude' / 'projects'
    projects.mkdir(parents=True)
    provider = REPO / 'services/chat-stream-v2/tests/smoke/ingest_provider_stub.py'
    launcher = root / 'claude-fixture'
    launcher.write_text(f'#!{sys.executable}\nimport os,runpy\nos.environ["CHAT_STREAM_STUB_ROOT"]={str(projects)!r}\nrunpy.run_path({str(provider)!r},run_name="__main__")\n')
    launcher.chmod(0o755)
    terminal = root / 'tmux-fixture'
    terminal.write_text(f'#!/bin/sh\nexec {shlex.quote(tmux)} -L {shlex.quote(root.name)} "$@"\n')
    terminal.chmod(0o755)
    sys.path.insert(0, str(REPO / 'services'))
    from _shared.operator_auth import OperatorCredentialRegistry
    registry = OperatorCredentialRegistry(root / '.config/pentacle-stream/operator-credentials.json')
    registry.initialize()
    credential = root / 'desktop.token'
    credential.write_text(registry.issue('pentacle', label='public-smoke')[1])
    credential.chmod(0o600)
    env = {**os.environ, 'HOME': str(root), 'PENTACLE_MACHINES_JSON': json.dumps({'machines': [{
        'name': 'local', 'ssh_target': None, 'claude_bin': str(launcher),
        'tmux_bin': str(terminal), 'projects_root': str(projects), 'cwd': str(root),
    }]}), 'PENTACLE_INGEST_INTERVAL_S': '0.1'}
    args = [sys.executable, 'services/chat-stream-v2/main.py', '--port', '0', '--local-host', 'local',
        '--db', str(root / 'sessions.db'), '--notifications-db', str(root / 'notifications.db'),
        '--assets-db', str(root / 'assets.db'), '--blob-root', str(root / 'blobs'),
        '--claude-bin', str(launcher), '--spawn-cwd', str(root), '--projects-root', str(projects), '--tmux-bin', str(terminal)]
    daemon = None
    try:
        with (root / 'daemon.log').open('w') as output:
            daemon = subprocess.Popen(args, cwd=REPO, env=env, stdout=output, stderr=output, start_new_session=True)
        deadline = time.monotonic() + 20
        port = None
        while time.monotonic() < deadline:
            match = re.search(r'chat_streamd_v2 listening on 127\.0\.0\.1:(\d+)', (root / 'daemon.log').read_text())
            if match:
                port = int(match[1]); break
            if daemon.poll() is not None:
                raise RuntimeError('Daemon exited during startup; see daemon.log')
            time.sleep(0.05)
        if port is None:
            raise RuntimeError('Daemon did not bind before the startup deadline')
        config = {'appName': 'Pentacle public smoke', 'agents': {'claude': {}, 'codex': {}},
            'features': {'chatUi': True}, 'tmux': str(terminal), 'chatStream': {
                'url': f'ws://127.0.0.1:{port}', 'hosts': ['local'], 'localHost': 'local', 'tokenPath': str(credential)}}
        (root / 'pentacle.config.js').write_text('module.exports = ' + json.dumps(config) + ';\n')
        electron_env = {**os.environ, 'PENTACLE_SMOKE_ROOT': str(root), 'PENTACLE_SMOKE_PID': str(daemon.pid)}
        with (root / 'electron.log').open('w') as output:
            result = subprocess.run([str(REPO / 'node_modules/.bin/electron'), str(REPO / 'tools/public_desktop_smoke.cjs')],
                cwd=REPO, env=electron_env, stdout=output, stderr=output, timeout=100)
        if result.returncode:
            raise RuntimeError('Desktop smoke failed; see electron.log')
        proof = json.loads((root / 'proof.json').read_text())
        assert proof['assistantRendered'] and proof['disconnectedRejected']
        print(f'PASS: real spawn, terminal attach, send, assistant render, disconnected rejection. Evidence: {root}')
        return 0
    finally:
        if daemon is not None and daemon.poll() is None:
            os.killpg(daemon.pid, signal.SIGTERM)
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(daemon.pid, signal.SIGKILL); daemon.wait()
        subprocess.run([str(terminal), 'kill-server'], capture_output=True)
        credential.unlink(missing_ok=True)
        shutil.rmtree(root / '.config', ignore_errors=True)
        print(f'Smoke artifacts: {root}')


if __name__ == '__main__':
    raise SystemExit(main())
