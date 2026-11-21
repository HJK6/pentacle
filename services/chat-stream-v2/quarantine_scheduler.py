"""Run the quarantine reconciliation as a bounded daemon-owned child."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import signal
import sys

log = logging.getLogger(__name__)
INTERVAL_S = 300
PASS_TIMEOUT_S = 900
_SEAT_IDENTITY_ENV = ('AGENT_ORCH_STREAM_TOKEN', 'AGENT_ORCH_STREAM_TOKEN_FILE',
                      'AGENT_ORCH_STREAM_ID', 'AGENT_ORCH_INTERNAL_LEADER_STREAM_ID',
                      'PENTACLE_STREAM_ID', 'TMUX', 'TMUX_PANE')


async def _stop(child):
    if child.returncode is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(child.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await child.wait()


async def run_child(command, env, timeout=PASS_TIMEOUT_S):
    child = await asyncio.create_subprocess_exec(*command, env=env, start_new_session=True,
                                                 stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)
    try:
        return await asyncio.wait_for(child.wait(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        await _stop(child)
        raise


async def _launch_alert(reason, env):
    clean = {k: v for k, v in env.items() if k not in _SEAT_IDENTITY_ENV}
    try:
        code = await run_child(['agent-orch', 'notify', '--producer', 'flaky-test-quarantine',
                                '--severity', 'warning', '--title', 'Flaky test quarantine needs attention',
                                '--message', 'Quarantine worker incomplete: ' + reason,
                                '--dedup-key', 'flaky-test-quarantine:worker:' + reason], clean, timeout=30)
        if code:
            log.error('quarantine worker alert failed')
    except (OSError, asyncio.TimeoutError):
        log.error('quarantine worker alert unavailable')


async def run_forever(config_path, *, interval=INTERVAL_S, timeout=PASS_TIMEOUT_S):
    env = dict(os.environ)
    package_root = str(Path(__file__).resolve().parents[1] / 'agent-orch')
    env['PYTHONPATH'] = os.pathsep.join(filter(None, (package_root, env.get('PYTHONPATH', ''))))
    command = [sys.executable, '-m', 'agent_orch.quarantine_run', '--config', config_path]
    while True:
        try:
            code = await run_child(command, env, timeout=timeout)
            if code:
                # The worker emits a detailed deduplicated alert when possible.
                # A second generic alert covers early interpreter/import failure.
                await _launch_alert('nonzero-exit', env)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            await _launch_alert('timeout', env)
        except OSError:
            await _launch_alert('launch-failed', env)
        await asyncio.sleep(interval)
