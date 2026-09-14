"""Exercise the production entrypoints' logging in fresh non-UTC processes."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest


SERVICE = Path(__file__).resolve().parents[1]
RECORD = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z) (DEBUG|INFO|WARNING|ERROR) (\S+) (.*)$')


def invoke(entrypoint: str, level: str = 'DEBUG', preconfigured: bool = False):
    script = f'''
import asyncio, importlib, logging, time
entry = importlib.import_module({entrypoint!r})
if {preconfigured!r}:
    logging.basicConfig(level=logging.ERROR, format="OWNED %(levelname)s %(message)s")
original_handlers = tuple(logging.getLogger().handlers)
async def probe(*args):
    entry.log.debug("startup probe")
    entry.log.warning("disconnect probe", extra={{"payload": "PRIVATE_PAYLOAD", "token": "PRIVATE_TOKEN"}})
    await asyncio.sleep(0.025)
    entry.log.warning("retry probe")
    try:
        raise ValueError("traceback probe")
    except ValueError:
        entry.log.exception("exception probe")
    return 0
if {entrypoint!r} == 'main':
    entry.run = probe
    result = entry.main(['--log-level', {level!r}])
else:
    entry.Satellite.run_forever = probe
    result = entry.main([])
assert result == 0
assert len(logging.getLogger().handlers) == 1
if {preconfigured!r}:
    assert tuple(logging.getLogger().handlers) == original_handlers
    assert logging.getLogger().level == logging.ERROR
'''
    env = {**os.environ, 'TZ': 'Pacific/Honolulu', 'PYTHONPATH': str(SERVICE),
           'PENTACLE_HOST_ID': 'test-host', 'PENTACLE_SATELLITE_HOST': 'test-host',
           'PENTACLE_SATELLITE_LOG_LEVEL': level}
    return subprocess.run([sys.executable, '-c', script], env=env, text=True,
                          capture_output=True, timeout=20, check=True).stderr


@pytest.mark.parametrize('entrypoint', ['main', 'satellite'])
def test_entrypoint_emits_utc_milliseconds_levels_logger_and_traceback(entrypoint):
    before = datetime.now(timezone.utc)
    output = invoke(entrypoint)
    after = datetime.now(timezone.utc)
    records = [RECORD.fullmatch(line) for line in output.splitlines()]
    records = [match for match in records if match and match[3].startswith('chat_streamd_v2')]
    assert len(records) == 4, output
    times = [datetime.fromisoformat(match[1]) for match in records]
    assert before.timestamp() - 0.001 <= times[0].timestamp() <= after.timestamp()
    assert times[2] > times[1]
    assert [match[2] for match in records] == ['DEBUG', 'WARNING', 'WARNING', 'ERROR']
    expected_logger = 'chat_streamd_v2' + ('.satellite' if entrypoint == 'satellite' else '')
    assert all(match[3] == expected_logger for match in records)
    assert 'Traceback (most recent call last):' in output
    assert 'ValueError: traceback probe' in output
    assert 'PRIVATE_PAYLOAD' not in output and 'PRIVATE_TOKEN' not in output


@pytest.mark.parametrize('entrypoint', ['main', 'satellite'])
def test_entrypoint_respects_configured_level(entrypoint):
    output = invoke(entrypoint, level='ERROR')
    assert 'startup probe' not in output and 'disconnect probe' not in output and 'retry probe' not in output
    assert output.count('exception probe') == 1


@pytest.mark.parametrize('entrypoint', ['main', 'satellite'])
def test_entrypoint_preserves_existing_handler_formatter_and_level(entrypoint):
    output = invoke(entrypoint, preconfigured=True)
    assert output.startswith('OWNED ERROR exception probe\n')
    assert 'disconnect probe' not in output
