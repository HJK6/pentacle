"""Opt-in real-process coverage for bounded nested-session cleanup."""
import json
import os
import signal
import sys

import pytest

import scheduled_admission as sa


@pytest.mark.soak
def test_bounded_kills_term_resistant_separate_session(tmp_path):
    descendant = 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)'
    child = (
        'import signal,subprocess,sys,time; '
        'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
        f'subprocess.Popen([sys.executable,"-c",{descendant!r}],start_new_session=True); '
        'time.sleep(30)'
    )
    receipt = tmp_path / 'bound.json'
    try:
        assert sa.bounded([sys.executable, '-c', child], 6, receipt) == 2
        value = json.loads(receipt.read_text())
        owned = value['owned_at_stop']
        killed = {str(pid) for pid in value['killed_owned_pids']}
        assert value['terminated_at_bound'] and value['finished']
        assert len(owned) >= 2 and len(killed) >= 2
        assert not value.get('cleanup_errors')
        table = sa.process_table(5)
        assert all(int(pid) not in table or table[int(pid)]['birth'] != row['birth']
                   for pid, row in owned.items())
    finally:
        # Cleanup only identities recorded by the supervisor, never bare PIDs.
        value = json.loads(receipt.read_text()) if receipt.exists() else {}
        owned = value.get('owned_at_stop', {})
        if owned:
            table = sa.process_table(5)
            for pid, row in owned.items():
                current = table.get(int(pid))
                if current and current['birth'] == row['birth']:
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
