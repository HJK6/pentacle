"""Exercise authority restore initialization through the actual daemon entrypoint."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time


def test_enabled_composite_starts_authority_restore_after_store(tmp_path):
    root = Path(__file__).resolve().parents[3]
    (tmp_path / 'voice-answers-fixture.json').write_text(json.dumps({
        'stream_id': 'local:web-gate-assistant',
        'producer_stream_id': 'local:web-gate-voice-producer',
        'producer_generation': 'fixture-generation',
    }))
    database = tmp_path / 'daemon.db'
    log_path = tmp_path / 'daemon.log'
    command = [sys.executable, str(root / 'test/e2e/lib/web_gate_daemon.py'), str(tmp_path),
               '--host', '127.0.0.1', '--port', '0', '--local-host', 'local',
               '--db', str(database), '--notifications-db', str(tmp_path / 'notifications.db'),
               '--assets-db', str(tmp_path / 'assets.db'), '--blob-root', str(tmp_path / 'blobs'),
               '--disable-hosts', '--disable-mirror', '--disable-nudges',
               '--disable-outbound-notices', '--disable-remote-presence', '--disable-reconciler']
    env = os.environ.copy()
    env.pop('PENTACLE_WEB_GATE_COSMO_URL', None)
    with log_path.open('w') as log:
        process = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 20
            initialized = False
            while time.monotonic() < deadline and process.poll() is None:
                if database.exists():
                    try:
                        with sqlite3.connect(database) as conn:
                            initialized = bool(conn.execute(
                                "SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name='v2_authority_restore_episode'").fetchone())
                    except sqlite3.Error:
                        pass
                    # The daemon opens its client bootstrap gate just after
                    # SHA resolution, after store and composite recovery.
                    if initialized and 'startup phase=sha_resolution' in log_path.read_text():
                        break
                time.sleep(0.05)
            assert initialized and 'startup phase=sha_resolution' in log_path.read_text() \
                and process.poll() is None, log_path.read_text()
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
