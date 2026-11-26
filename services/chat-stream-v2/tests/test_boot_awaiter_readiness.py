"""Boot readiness must not wait for the child-report sweep."""
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time

import pytest
from websockets.sync.client import connect


@pytest.mark.parametrize("sweep", ["held", "real"])
def test_recovery_job_owns_first_pass_and_does_not_block_inventory(tmp_path, isolated_tmux_env, sweep):
    script = r'''
import asyncio, json, sys
from pathlib import Path
import main, server, local_admin, ledger, spawnctl
root = Path(sys.argv[1])
main._spec_catalog = lambda _sessions: None
registry_init = server.operator_auth.OperatorCredentialRegistry.__init__
server.operator_auth.OperatorCredentialRegistry.__init__ = lambda self, path=None: registry_init(self, root/'registry.json')
server.operator_auth.OperatorCredentialRegistry().initialize()
admin_init = local_admin.initialize
local_admin.initialize = lambda: admin_init(root/'admin.token')
real_sweep = ledger.Ledger.sweep_awaited_unreported
async def recorded_sweep(self, *, limit=500):
    # Record who ran the pass; the held variant never returns.
    with (root/'sweep-callers').open('a') as out:
        out.write(asyncio.current_task().get_name() + '\n')
    if sys.argv[3] == 'held':
        await asyncio.Event().wait()
    result = await real_sweep(self, limit=limit)
    with (root/'sweep-results').open('a') as out:
        out.write(json.dumps(result) + '\n')
    return result
ledger.Ledger.sweep_awaited_unreported = recorded_sweep
if sys.argv[3] == 'real':
    # A waiter left by a crash: its child closed while the ledger callback never ran.
    from store import Store
    from sessions import Sessions
    async def seed():
        store = Store(str(root/'sessions.db'))
        store.start()
        try:
            child = await Sessions(store, local_host='fixture').open(
                'fixture', 'crashed', role='worker', visibility='hidden', parent_stream_id='fixture:parent')
            await store.register_awaiter(child['stream_id'], 9)
            await store.mark_closed('fixture', 'crashed', closed_at='2026-08-09T00:00:02Z',
                                    pane_status='pane_dead', expected_generation=child['session_generation'])
        finally:
            store.stop()
    asyncio.run(seed())
async def held_reconcile(self, *args, **kwargs):
    # Optional startup work after readiness; the first pass must not wait on it.
    await asyncio.Event().wait()
spawnctl.SpawnCtl.reconcile_spawn_intents = held_reconcile
flags = ['hosts','mirror','ingest','reconciler','retention','nudges','notification-expiry',
         'remote-presence','event-push-ingest','routing-integrity','outbound-notices',
         'window-schedule','usage-state-publisher']
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
        proc = subprocess.Popen([sys.executable, '-u', '-c', script, str(tmp_path), isolated_tmux_env, sweep],
                                env=env, stdout=subprocess.PIPE, stderr=errors, text=True,
                                start_new_session=True)
        try:
            with selectors.DefaultSelector() as reader:
                reader.register(proc.stdout, selectors.EVENT_READ)
                assert reader.select(15), (tmp_path/'stderr').read_text()
                line = proc.stdout.readline()
            assert 'chat_streamd_v2 listening on ' in line, line
            port = int(line.rsplit(':', 1)[1])
            with connect(f'ws://127.0.0.1:{port}', open_timeout=5) as peer:
                # The timeout only catches a boot stuck behind the held sweep.
                assert json.loads(peer.recv(timeout=15))['type'] == 'welcome', (tmp_path/'stderr').read_text()
                peer.send(json.dumps({'type': 'list_sessions', 'request_id': 'first'}))
                while True:
                    reply = json.loads(peer.recv(timeout=15))
                    if reply.get('request_id') == 'first':
                        break
            assert reply['type'] == 'list_sessions.ok', reply
            # The one pass that ran is the recovery job's, not a boot step.
            assert (tmp_path/'sweep-callers').read_text().split() == ['awaiter-resolution']
            if sweep == 'real':
                # The job's first pass settles the crash waiter while later startup is held.
                for _ in range(100):
                    if (tmp_path/'sweep-results').exists():
                        break
                    time.sleep(0.05)
                first = json.loads((tmp_path/'sweep-results').read_text().splitlines()[0])
                assert first['resolved'] == 1 and first['closed_without_report'] == 1
        finally:
            if proc.poll() is None:
                # Boot never finishes behind the held reconcile, so stop is not graceful.
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            proc.stdout.close()
