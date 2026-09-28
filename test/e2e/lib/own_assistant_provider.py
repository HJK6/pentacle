#!/usr/bin/env python3
"""Native-format Claude counterpart. Only this backend publishes its dispatch.

The --cli branch isolates home resolution before loading the candidate CLI;
the daemon-issued stream-token file is inherited unchanged by that subprocess.
"""
from __future__ import annotations
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[3]


def cli():
    home = Path(os.environ['PENTACLE_FIXTURE_HOME'])
    Path.home = classmethod(lambda cls: home)
    original = os.path.expanduser
    os.path.expanduser = lambda value: str(home / os.fspath(value)[2:]) if os.fspath(value).startswith('~/') else (str(home) if value == '~' else original(value))
    sys.path[:0] = [str(ROOT / 'services/agent-orch'), str(ROOT / 'services')]
    from agent_orch.cli import main
    sys.argv = ['agent-orch', *sys.argv[2:]]
    raise SystemExit(main())


def run():
    native_id = sys.argv[sys.argv.index('--session-id') + 1]
    scratch = Path(os.environ['PENTACLE_FIXTURE_ROOT'])
    workspace = os.path.realpath(os.getcwd())
    slug = workspace.replace('/', '-').replace('_', '-').replace('.', '-')
    transcript_path = scratch / '.claude/projects' / slug / (native_id + '.jsonl')
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    token_file = Path(os.environ['AGENT_ORCH_STREAM_TOKEN_FILE'])
    assert token_file.resolve().is_relative_to(scratch)
    assert os.environ['AGENT_ORCH_STREAM_ID'].startswith('local:assistant-backend-')
    transcript = transcript_path.open('a', buffering=1)
    def record(kind, text):
        message = {'role': kind, 'content': text if kind == 'user' else [{'type': 'text', 'text': text}]}
        transcript.write(json.dumps({'type': kind, 'sessionId': native_id, 'uuid': str(uuid.uuid4()),
                'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'message': message}) + '\n')
    def command(arguments):
        environment = dict(os.environ)
        environment['AGENT_ORCH_WS_URL'] = (scratch / 'endpoint').read_text().strip()
        environment['AGENT_ORCH_HOST_ID'] = 'local'
        environment['AGENT_ORCH_RUNTIME_DIR'] = str(scratch / 'cli-runtime')
        environment['PENTACLE_FIXTURE_HOME'] = str(scratch / 'home')
        completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--cli', *arguments],
                                   env=environment, capture_output=True, text=True, timeout=15)
        return completed
    print('READY\n⏵⏵ bypass permissions on (synthetic counterpart)\n❯ ', flush=True)
    buffered = []
    for line in sys.stdin:
        text = line.rstrip('\r\n')
        if buffered or '[canonical assistant direct dispatch' in text:
            buffered.append(text)
            if '</assistant-original-input-json>' not in text:
                continue
            wire = '\n'.join(buffered)
            buffered = []
            fields = dict(re.findall(r'^(origin|dispatch_id|reply_to_message_id|target_stream_id|target_generation): (.+)$', wire, re.M))
            original = json.loads(wire.split('<assistant-original-input-json>\n', 1)[1].split('\n</assistant-original-input-json>', 1)[0])
            display = json.loads(re.search(r'^assistant_display_name_json: (.+)$', wire, re.M)[1])
            assert fields['target_stream_id'] == os.environ['AGENT_ORCH_STREAM_ID']
            record('user', wire)
            response = display + ': ' + original['text']
            argv = ['assistant', 'publish', '--request-id', 'publish:' + fields['dispatch_id'],
                '--composite-stream-id', fields['origin'], '--dispatch-id', fields['dispatch_id'],
                '--reply-to-message-id', fields['reply_to_message_id'], '--publish-kind', 'prose',
                '--response-state', 'final', '--message', response]
            published = command(argv)
            duplicate = command(argv)
            stale = command(['assistant', 'rebind', '--request-id', 'stale:' + fields['dispatch_id'],
                '--expected-revision', '0', '--target', fields['target_stream_id'],
                '--generation', fields['target_generation'] + '-stale'])
            result = {'dispatch': fields, 'display_name': display, 'original_input': original,
                'publish': {'exit': published.returncode, 'stdout': published.stdout, 'stderr': published.stderr},
                'duplicate': {'exit': duplicate.returncode, 'stdout': duplicate.stdout, 'stderr': duplicate.stderr},
                'stale_generation': {'exit': stale.returncode, 'stdout': stale.stdout, 'stderr': stale.stderr},
                'token_source': 'daemon-issued stream-token file', 'publisher': 'backend counterpart'}
            (scratch / 'provider-proof.json').write_text(json.dumps(result, indent=2))
            record('assistant', response)
            print(response + '\n⏵⏵ bypass permissions on (synthetic counterpart)\n❯ ', flush=True)
        elif text:
            record('user', text)
            record('assistant', 'Synthetic backend initialized.')
            print('Synthetic backend initialized.\n⏵⏵ bypass permissions on (synthetic counterpart)\n❯ ', flush=True)


if __name__ == '__main__':
    cli() if sys.argv[1:2] == ['--cli'] else run()
