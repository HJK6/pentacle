#!/usr/bin/env python3
"""Owned ordinary-session seed and counted native-format provider counterpart.

Only the provider boundary is synthetic. Admission, send, receipt persistence,
restart, ingestion, main client and browser remain candidate implementations.
"""
from __future__ import annotations
import asyncio
import datetime
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'services/chat-stream-v2'), str(ROOT / 'services')]


def seed(out):
    from store import Store
    async def run():
        store = Store(str(out / 'sessions.db'))
        store.start()
        try:
            await store.open_session('local', 'send-refusal-fixture', visibility='default',
                                     role='worker', provider='claude', objective='Counted send acceptance')
            await store.update_session('local', 'send-refusal-fixture',
                jsonl_path=str(out / 'native.jsonl'), claude_session_id='send-refusal-native', pane_status='pane_alive',
                pane_pid=subprocess.check_output([str(out / 'tmux'), 'display-message', '-p', '-t', '=send-refusal-fixture:', '#{pane_pid}'], text=True).strip())
            row = await store.fetch_session('local', 'send-refusal-fixture')
            (out / 'backend.json').write_text(json.dumps({'generation': row['session_generation']}))
        finally:
            store.stop()
    asyncio.run(run())


def provider(out):
    lock = threading.Lock()
    def record(kind, text):
        entry = {'type': kind, 'sessionId': 'send-refusal-native', 'uuid': str(uuid.uuid4()),
                 'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        if kind == 'system':
            entry.update(subtype='turn_duration', durationMs=100, messageCount=1)
        else:
            entry['message'] = {'role': kind, 'content': text if kind == 'user' else [{'type': 'text', 'text': text}]}
        with lock, (out / 'native.jsonl').open('a') as target:
            target.write(json.dumps(entry) + '\n')
    def response(index, text):
        while not (out / f'release-{index}').exists():
            time.sleep(.05)
        record('assistant', 'Synthetic reply: ' + text)
        record('system', '')
        print('Synthetic reply: ' + text + '\n⏵⏵ bypass permissions on (synthetic counterpart)\n❯ ', flush=True)
    # A real native-format input boundary: bracketed multiline paste is one
    # submission only after the trailing Enter. The counter survives restarts.
    import select
    import codecs
    import termios
    import tty
    descriptor = sys.stdin.fileno()
    old = termios.tcgetattr(descriptor)
    tty.setraw(descriptor)
    decoder = codecs.getincrementaldecoder('utf-8')()
    pending = ''
    body = ''
    pasted = False
    print('\x1b[?2004hREADY\r\n⏵⏵ bypass permissions on (synthetic counterpart)\r\n❯ ', flush=True)
    try:
        while True:
            readable, _, _ = select.select([descriptor], [], [], .1)
            if readable:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                pending += decoder.decode(chunk)
            while pending:
                if pending.startswith('\x1b[200~'):
                    pasted = True; pending = pending[6:]; continue
                if pending.startswith('\x1b[201~'):
                    pasted = False; pending = pending[6:]; continue
                if pending[0] == '\x1b':
                    if readable and any(marker.startswith(pending) for marker in ['\x1b[200~', '\x1b[201~']):
                        break
                    with (out / 'escapes.jsonl').open('a') as target:
                        target.write(json.dumps({'at': time.time()}) + '\n')
                    pending = pending[1:]; continue
                char, pending = pending[0], pending[1:]
                if char in '\r\n' and not pasted:
                    text, body = body.strip('\r\n'), ''
                    if not text:
                        continue
                    counter = out / 'submissions.jsonl'
                    index = len(counter.read_text().splitlines()) + 1 if counter.exists() else 1
                    with counter.open('a') as target:
                        target.write(json.dumps({'index': index, 'text': text, 'at': time.time()}) + '\n')
                    record('user', text)
                    print('\r\n⏺ Input received; response withheld by counterpart\r\n❯ ', flush=True)
                    threading.Thread(target=response, args=(index, text), daemon=True).start()
                else:
                    body += char
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, old)


if __name__ == '__main__':
    out = Path(sys.argv[2]).resolve()
    seed(out) if sys.argv[1] == 'seed' else provider(out)
