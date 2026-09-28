#!/usr/bin/env python3
"""Host-configured launchd recovery; does not restart a responding/running job."""
import argparse
import json
import os
import plistlib
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen


def ready(status):
    if not status:
        return False
    if status.get('mode') == 'off':
        return True
    return (status.get('mode') == 'on' and status.get('asr', {}).get('loaded')
            and status.get('asr', {}).get('model') == 'large-v3'
            and status.get('audio', {}).get('health_state') == 'ok'
            and status.get('audio_buffer', {}).get('ready'))


def status():
    try:
        with urlopen('http://127.0.0.1:7780/status', timeout=2) as response:
            return json.load(response)
    except Exception:
        return None


def recover(plist):
    config = plistlib.loads(Path(plist).read_bytes())
    label = config['Label']
    if not isinstance(label, str) or not label or any(c.isspace() for c in label) or '/' in label:
        raise ValueError('Invalid microphone service label')
    current = status()
    if ready(current):
        return
    # A loaded or unhealthy listener is left alone, including while loading.
    target = f'gui/{os.getuid()}/{label}'
    loaded = subprocess.run(['/bin/launchctl', 'print', target], capture_output=True).returncode == 0
    if not loaded and current is None:
        subprocess.run(['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(Path(plist).absolute())], check=True)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if ready(status()):
            return
        time.sleep(.5)
    raise TimeoutError('Microphone service did not become ready')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--plist', required=True)
    args = parser.parse_args()
    recover(args.plist)
    print(json.dumps({'ok': True, 'ready': True}))
