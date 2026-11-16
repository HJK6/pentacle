#!/usr/bin/env python3
"""Host-configured launchd recovery.

A responding service is never restarted. A loaded job whose /status stays
unanswered, and whose process is old enough to have finished starting, is
restarted once. Every step shares one deadline inside the web starter's limit.
"""
import argparse
import fcntl
import json
import os
import plistlib
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

DEADLINE_SECS = 70          # web starter kills the helper at 75 s
PROBE_TIMEOUT_SECS = 3
UNANSWERED_PROBES = 3
MIN_RESTART_AGE_SECS = 30   # measured auto-start lock holds are 3-8 s
LOCK_PATH = Path.home()/'Library'/'Application Support'/'Pentacle'/'mic-recover.lock'
PORT = 7780
clock = time.monotonic
sleep = time.sleep


def ready(status):
    if not status:
        return False
    if status.get('mode') == 'off':
        return True
    return (status.get('mode') == 'on' and status.get('asr', {}).get('loaded')
            and status.get('asr', {}).get('model') == 'large-v3'
            and status.get('audio', {}).get('health_state') == 'ok'
            and status.get('audio_buffer', {}).get('ready'))


def status(timeout=PROBE_TIMEOUT_SECS):
    try:
        with urlopen(f'http://127.0.0.1:{PORT}/status', timeout=timeout) as response:
            return json.load(response)
    except Exception:
        return None


def remaining(deadline):
    left = deadline - clock()
    if left <= 0:
        raise TimeoutError('Microphone service did not become ready')
    return left


def probe(deadline):
    return status(min(PROBE_TIMEOUT_SECS, remaining(deadline)))


def launchctl(deadline, *args):
    return subprocess.run(['/bin/launchctl', *args], capture_output=True, text=True,
                          timeout=remaining(deadline))


def process_age(pid, deadline):
    """Seconds since the job's process started, from ps etime ([[dd-]hh:]mm:ss)."""
    out = subprocess.run(['/bin/ps', '-o', 'etime=', '-p', str(pid)], capture_output=True,
                         text=True, timeout=remaining(deadline)).stdout.strip()
    if not out:
        return None
    days, _, rest = out.rpartition('-')
    seconds = 0
    for part in rest.split(':'):
        seconds = seconds*60 + int(part)
    return seconds + int(days or 0)*86400


def job_pid(printed):
    for line in printed.splitlines():
        key, _, value = line.strip().partition(' = ')
        if key == 'pid' and value.isdigit():
            return int(value)
    return None


def hold_lock(deadline):
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, 'a')
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            sleep(min(.25, remaining(deadline)))


def wait_ready(deadline):
    while True:
        if ready(probe(deadline)):
            return
        sleep(min(.5, remaining(deadline)))


def recover(plist, deadline=None):
    deadline = clock() + DEADLINE_SECS if deadline is None else deadline
    config = plistlib.loads(Path(plist).read_bytes())
    label = config['Label']
    if not isinstance(label, str) or not label or any(c.isspace() for c in label) or '/' in label:
        raise ValueError('Invalid microphone service label')
    with hold_lock(deadline):
        current = probe(deadline)
        if ready(current):
            return
        if current is not None:
            # Answering but not ready (loading, or an audio error): never restart it.
            return wait_ready(deadline)
        target = f'gui/{os.getuid()}/{label}'
        printed = launchctl(deadline, 'print', target)
        if printed.returncode != 0:
            launchctl(deadline, 'bootstrap', f'gui/{os.getuid()}', str(Path(plist).absolute())).check_returncode()
            return wait_ready(deadline)
        for _ in range(UNANSWERED_PROBES - 1):
            current = probe(deadline)
            if current is not None:
                return wait_ready(deadline)
        pid = job_pid(printed.stdout)
        age = process_age(pid, deadline) if pid else None
        if age is not None and age >= MIN_RESTART_AGE_SECS:
            launchctl(deadline, 'kickstart', '-k', target).check_returncode()
        # A younger or absent process is still starting under launchd: wait for it.
        wait_ready(deadline)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--plist', required=True)
    parser.add_argument('--port', type=int, default=PORT)
    args = parser.parse_args()
    PORT = args.port
    recover(args.plist)
    print(json.dumps({'ok': True, 'ready': True}))
