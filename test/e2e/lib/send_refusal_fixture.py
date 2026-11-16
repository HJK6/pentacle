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
    print('READY\n⏵⏵ bypass permissions on (synthetic counterpart)\n❯ ', flush=True)
    for line in sys.stdin:
        text = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z~]', '', line).strip('\r\n')
        if not text:
            continue
        # This file is external to the daemon and survives both daemon instances.
        with (out / 'submissions.jsonl').open('a') as target:
            target.write(json.dumps({'index': sum(1 for _ in (out / 'submissions.jsonl').open()) + 1,
                                     'text': text, 'at': time.time()}) + '\n')
        index = sum(1 for _ in (out / 'submissions.jsonl').open())
        record('user', text)
        print('⏺ Input received; response withheld by counterpart\n❯ ', flush=True)
        threading.Thread(target=response, args=(index, text), daemon=True).start()


if __name__ == '__main__':
    out = Path(sys.argv[2]).resolve()
    seed(out) if sys.argv[1] == 'seed' else provider(out)
