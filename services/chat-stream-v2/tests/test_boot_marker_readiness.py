"""Readiness consumers must be able to connect at the flushed listening line."""
import json
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sys

from websockets.sync.client import connect


def test_listening_line_accepts_immediate_connection_before_next_loop_turn(tmp_path, isolated_tmux_env):
    script = r'''
import json, os, sys
from pathlib import Path
import main, server, local_admin
root = Path(sys.argv[1])
main._spec_catalog = lambda _sessions: None
registry_init = server.operator_auth.OperatorCredentialRegistry.__init__
server.operator_auth.OperatorCredentialRegistry.__init__ = lambda self, path=None: registry_init(self, root/'registry.json')
server.operator_auth.OperatorCredentialRegistry().initialize()
admin_init = local_admin.initialize
local_admin.initialize = lambda: admin_init(root/'admin.token')
emit = main._emit_boot_marker
def marker(host, port):
    emit(host, port)
    (root/'markers-written').touch()
    # Hold the event loop after the real flushed line. The external reader
    # connects before releasing this synchronous measurement barrier.
    assert sys.stdin.readline() == 'continue\n'
main._emit_boot_marker = marker
handle = server.Server._handle_client
async def handler(self, websocket):
    assert (root/'markers-written').exists(), 'handler ran before paired markers'
    (root/'handler-ran').touch()
    return await handle(self, websocket)
server.Server._handle_client = handler
flags = ['hosts','mirror','ingest','reconciler','retention','nudges','notification-expiry',
         'remote-presence','event-push-ingest','routing-integrity','outbound-notices',
         'awaiter-resolution','window-schedule','usage-state-publisher']
args = ['--bind','127.0.0.1','--port','0','--local-host','fixture',
        '--tmux-bin',sys.argv[2], '--db',str(root/'sessions.db'),
        '--assets-db',str(root/'assets.db'),'--notifications-db',str(root/'notifications.db'),
        '--blob-root',str(root/'blobs')]
raise SystemExit(main.main(args + ['--disable-'+flag for flag in flags]))
'''
    env = {'PATH': os.environ['PATH'],
           'PYTHONPATH': str(Path(__file__).resolve().parents[1]),
           'PENTACLE_MACHINES_JSON': json.dumps([{'name': 'fixture', 'cwd': str(tmp_path)}]),
           'PENTACLE_MEMORY_ROOT': str(tmp_path/'memory'),
           'PENTACLE_ASSISTANT_COMPOSITE_ENABLED': '0',
           'PENTACLE_ASSISTANT_DAFF_COMPOSITE_ENABLED': '0'}
    with (tmp_path/'stderr').open('w') as errors:
        proc = subprocess.Popen([sys.executable, '-u', '-c', script, str(tmp_path), isolated_tmux_env],
                                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=errors, text=True, start_new_session=True)
        try:
            with selectors.DefaultSelector() as reader:
                reader.register(proc.stdout, selectors.EVENT_READ)
                assert reader.select(15), (tmp_path/'stderr').read_text()
                line = proc.stdout.readline()
            assert 'chat_streamd_v2 listening on ' in line, line
            port = int(line.rsplit(':', 1)[1])
            # No retries: a listening announcement is a connectable port.
            with socket.create_connection(('127.0.0.1', port), timeout=2):
                assert not (tmp_path/'handler-ran').exists()
            proc.stdin.write('continue\n')
            proc.stdin.flush()
            with connect(f'ws://127.0.0.1:{port}', open_timeout=5) as peer:
                assert json.loads(peer.recv(timeout=5))['type'] == 'welcome'
            assert (tmp_path/'handler-ran').exists()
            stderr = (tmp_path/'stderr').read_text()
            assert line.strip() in stderr
            assert stderr.index(line.strip()) < stderr.index('conn_diag ')
        finally:
            # Release the barrier on a failing control as well, then shut down
            # only this owned daemon. The tmux fixture owns its server cleanup.
            if proc.poll() is None:
                try:
                    proc.stdin.write('continue\n')
                    proc.stdin.flush()
                except BrokenPipeError:
                    pass
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            proc.stdin.close()
            proc.stdout.close()
