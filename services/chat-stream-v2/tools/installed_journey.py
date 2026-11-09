#!/usr/bin/env python3
"""Finite installed-client composition. No service control or load generation.

The private manifest identifies an authorized installed release and credentials
by path, never by value. This command qualifies its named checks, not the whole
product's observation period. See docs/E2E_HARNESS.md.
"""
from __future__ import annotations

import argparse
import ast
import base64
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from urllib.request import urlopen
import uuid

SERVICE = Path(__file__).resolve().parents[1]
ROOT = SERVICE.parents[1]
for directory in (SERVICE, SERVICE.parent):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


class Blocked(RuntimeError):
    """A required prerequisite is unavailable; no successful journey claimed."""


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def served_build(url):
    """Read the actual injected config, before any fixture or credential write."""
    with urlopen(url, timeout=20) as response:
        document = response.read(4 * 1024 * 1024).decode('utf-8')
    marker = 'window.__PENTACLE_CONFIG__='
    if marker not in document:
        raise Blocked('served page has no installed web configuration')
    config, _end = json.JSONDecoder().raw_decode(document.split(marker, 1)[1])
    if not isinstance(config.get('buildId'), str) or not config['buildId']:
        raise Blocked('served page has no build ID')
    return config['buildId']


def validate_manifest(value):
    if not isinstance(value, dict) or value.get('schema') != 'pentacle.installed-journey.v1':
        raise Blocked('manifest schema must be pentacle.installed-journey.v1')
    for name in ('web_url', 'expected_build_id', 'source_sha', 'ws_url', 'operator_token_path',
                 'operator_registry_path', 'local_host', 'machine_cwd', 'cli', 'daemon', 'provider', 'spec_hashes'):
        if not value.get(name):
            raise Blocked('manifest missing ' + name)
    if not re.fullmatch('[0-9a-f]{40}', value['source_sha']):
        raise Blocked('manifest source_sha must be a full Git SHA')
    for key in ('alias', 'generation', 'executable', 'executable_sha256', 'version', 'model', 'effort'):
        if not value['provider'].get(key):
            raise Blocked('manifest provider missing ' + key)
    for section, keys in (('daemon', ('pid', 'birth', 'boot', 'checkout', 'executable')),
                          ('cli', ('executable', 'sha256', 'version', 'manifest_path', 'manifest_sha256', 'modules'))):
        if not isinstance(value[section], dict) or any(not value[section].get(key) for key in keys):
            raise Blocked('manifest ' + section + ' identity is incomplete')
    # Values belong in secure stores. Reject the usual accidental inline forms.
    def inspect(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {'token', 'stream_token', 'operator_token', 'access_token', 'api_key', 'secret',
                           'proof_key', 'password', 'auth_v2'}:
                    raise Blocked('inline credentials are forbidden')
                inspect(child)
        elif isinstance(item, list):
            for child in item:
                inspect(child)
    inspect(value)
    if value.get('remote') and any(not value['remote'].get(k) for k in ('host', 'cwd', 'provider')):
        raise Blocked('remote identity requires host, cwd and provider tuple')
    return value


def error_text(error):
    # Transport/auth exceptions can interpolate frames. Keep only our own
    # assertion/prerequisite messages; credentials never enter evidence.
    return str(error)[:1000] if isinstance(error, (AssertionError, Blocked)) else type(error).__name__


class Journey:
    def __init__(self, manifest, evidence):
        self.m = manifest
        self.out = evidence
        self.run_id = uuid.uuid4().hex
        self.rows = []
        self.fixtures = []
        self.credential_ids = []
        self.assets = []
        self.questions = []
        self.pending_spawns = []
        self.cleanup = {'expected': 0, 'closed': 0, 'credentials_revoked': [], 'errors': []}
        self.active_row = 'build'
        self.rpc = None
        self.registry = None
        self.timeout = 30
        self.failure = None

    def row(self, name, result='PASS', **facts):
        self.active_row = name
        record = {'row': name, 'run_id': self.run_id, 'result': result, 'primary_error': None, 'harness_error': None,
                  'fixture_ids': [f['stream_id'] for f in self.fixtures], 'cleanup': None, **facts}
        self.rows.append(record)
        save(self.out / 'rows.json', self.rows)
        return record

    def record_manifest(self):
        save(self.out / 'run-manifest.json', {'run_id': self.run_id, 'release': self.m,
             'fixtures': self.fixtures, 'credential_ids': self.credential_ids,
             'pending_spawns': self.pending_spawns,
             'helper_sha256': digest(__file__), 'cleanup': self.cleanup})

    def call(self, payload):
        kind = payload['type']
        response = self.rpc(payload, kind)
        if response.get('error_code') or response.get('type', '').endswith(('.error', '.failed')):
            raise AssertionError(kind + ' refused: ' + str(response.get('error_code') or response['type']))
        return response

    def inspect(self, stream):
        return self.call({'type': 'inspect_stream', 'stream_id': stream, 'event_tail': 500})

    def history(self, stream):
        return self.inspect(stream)['recent_events']

    def child(self, argv, *, env, stdout, stderr, timeout):
        """Keep the existing authenticated receiver drained during owned work."""
        with subprocess.Popen(argv, env=env, stdout=stdout, stderr=stderr) as process:
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    require(time.monotonic() < deadline, 'owned composition child exceeded its finite bound')
                    self.call({'type': 'list_sessions'})
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
            return process.returncode

    def target_state(self, stream):
        inspected = self.inspect(stream)
        row = inspected['session']
        events = [{k: event.get(k) for k in ('daemon_seq', 'message_id', 'kind', 'text', 'attachments')}
                  for event in inspected['recent_events']
                  if event.get('kind') in ('USER', 'ASSIST', 'ASSIST_TEXT')]
        return {'status': row['status'], 'generation': row['session_generation'], 'events': events,
                'binding': self.call({'type': 'assistant.binding'}).get('generation')}

    def spawn(self, host, register, wait_ready, catalog, *, parent=None, initial_prompt=None, visibility='nested'):
        provider = self.m['provider']
        key = uuid.uuid4().hex
        payload = {'type': 'spawn', 'schema': 'SpawnRequestV2', 'host': host,
                   'session_name': 'v2-installed-' + key[:12], 'request_id': str(uuid.uuid4()),
                   'idempotency_key': key, 'provider': 'codex', 'account': provider['alias'],
                   'spawn_profile': 'desktop_manual', 'catalog_version': catalog,
                   'resolution_source': 'explicit_override', 'model': provider['model'],
                   'effort': provider['effort'], 'visibility': visibility,
                   'objective': 'Disposable installed journey fixture; no business actions'}
        if parent:
            payload['parent_stream_id'] = parent
        if initial_prompt is not None:
            payload['initial_prompt'] = initial_prompt
        register.prepare_owned_spawn(payload)
        self.pending_spawns.append(payload)
        self.record_manifest()
        response = self.call(payload)
        # Registration precedes all provider/output assertions and teardown uses
        # the library's exact immutable request/generation ownership binding.
        register(payload, response)
        self.pending_spawns.remove(payload)
        owned = self.registry._owned[response['stream_id']]
        fixture = asdict(owned)
        fixture['spawn_ordinal'] = len(self.fixtures)
        self.fixtures.append(fixture)
        self.cleanup['expected'] = len(self.fixtures)
        fixture['spawn_payload'] = payload
        fixture['spawn_reply'] = {k: response.get(k) for k in ('stream_id', 'state', 'initial_prompt_delivery')}
        self.record_manifest()
        inspected = self.inspect(owned.stream_id)
        require(inspected['session']['session_generation'] == owned.session_generation, 'owned admission generation drift')
        fixture['stream_token_sha256'] = inspected['session'].get('token_hash')
        self.record_manifest()
        wait_ready(owned.stream_id)
        inspected = self.inspect(owned.stream_id)
        require(inspected['session']['session_generation'] == owned.session_generation, 'owned ready generation drift')
        binding = inspected['session'].get('account_binding') or {}
        expected = provider if host == self.m['local_host'] else self.m['remote']['provider']
        require(all(binding.get(key) == expected.get(key) for key in
                    ('generation', 'executable', 'executable_sha256', 'version')), 'native provider tuple drift')
        require(binding.get('alias') == provider['alias'] and binding.get('provider') == 'codex', 'wrong provider/account')
        fixture['account_binding'] = binding
        fixture['stream_token_sha256'] = inspected['session'].get('token_hash')
        self.record_manifest()
        return fixture

    def browser(self, phase, *, allow_failure=False, **extra):
        descriptor = {'phase': phase, 'run_id': self.run_id, 'url': self.m['web_url'],
                      'expected_build_id': self.m['expected_build_id'],
                      'fixtures': self.fixtures[:3], **extra}
        path = self.out / ('browser-' + phase + '.json')
        save(path, descriptor)
        directory = self.out / ('browser-' + phase)
        env = dict(os.environ, PENTACLE_WEB_GATE_REPORT_ROOT=str(directory))
        log = self.out / ('browser-' + phase + '.log')
        try:
            with log.open('w') as handle:
                returncode = self.child(['node', str(ROOT / 'test/e2e/lib/installed_journey_scenario.js'), str(path)],
                                        env=env, stdout=handle, stderr=subprocess.STDOUT, timeout=480)
        except Exception:
            receipts = list(directory.glob('*/web_gate/verdict.json'))
            if not (len(receipts) == 1 and json.loads(receipts[0].read_text()).get('chrome_cleanup', {}).get('exited') is True):
                self.cleanup['errors'].append('owned browser cleanup unproved: ' + phase)
            raise
        # The web gate's logs contain only our selected scenario; keep its
        # actual primary failure and owned-browser cleanup verdict intact.
        observations = list(directory.glob('*/web_gate/installed-observation.json'))
        verdicts = list(directory.glob('*/web_gate/verdict.json'))
        require(len(observations) == len(verdicts) == 1, 'browser proof/cleanup receipt missing')
        observation = json.loads(observations[0].read_text())
        verdict = json.loads(verdicts[0].read_text())
        require(verdict.get('chrome_cleanup', {}).get('exited') is True, 'owned browser cleanup unproved')
        if not (allow_failure and returncode == 1 and observation.get('status') == 'FAIL'):
            require(returncode == 0 and verdict.get('status') == 'PASS',
                    'installed browser journey failed; retain browser-' + phase + ' evidence')
        return observation

    def identity(self):
        from tools.provider_wrapper_probe import runtime_binding
        daemon_code = runtime_binding(Path(self.m['daemon']['checkout']), self.m['source_sha'], self.m['daemon']['pid'])
        require(daemon_code['command'].startswith(self.m['daemon']['executable'] + ' '), 'installed daemon executable drift')
        pid = str(self.m['daemon']['pid'])
        env = dict(os.environ, TZ='UTC0', LC_ALL='C')
        birth = subprocess.check_output(['ps', '-p', pid, '-o', 'lstart='], env=env, text=True, timeout=10).strip()
        require(birth == self.m['daemon']['birth'], 'installed daemon process birth drift')
        if sys.platform == 'darwin':
            boot = subprocess.check_output(['/usr/sbin/sysctl', '-n', 'kern.boottime'], text=True, timeout=10).strip()
            boot = re.search(r'sec\s*=\s*(\d+)', boot).group(1)
        else:
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        require(boot == self.m['daemon']['boot'], 'installed daemon boot identity drift')
        for name in ('cli', 'provider'):
            executable = self.m[name]['executable']
            require(digest(executable) == self.m[name].get('sha256', self.m[name].get('executable_sha256')),
                    'installed ' + name + ' executable drift')
        version = subprocess.check_output([self.m['provider']['executable'], '--version'], text=True, timeout=10).strip()
        require(version == self.m['provider']['version'], 'native provider executable version drift')
        cli = self.m['cli']
        require(digest(cli['manifest_path']) == cli['manifest_sha256'], 'installed CLI release manifest drift')
        require(json.loads(Path(cli['manifest_path']).read_text()).get('commit') == cli['version'] == self.m['source_sha'],
                'installed CLI release version mismatch')
        launcher = Path(cli['executable']).read_text().splitlines()[0]
        require(launcher.startswith('#!'), 'installed CLI interpreter binding missing')
        python = shlex.split(launcher[2:])[0]
        require(Path(python).is_absolute(), 'installed CLI interpreter must be absolute')
        program = ('import agent_orch.cli as cli, agent_orch.wsclient as wsclient, hashlib,json,pathlib; '
                   'print(json.dumps({k:{"file":m.__file__,"sha256":hashlib.sha256(pathlib.Path(m.__file__).read_bytes()).hexdigest()} '
                   'for k,m in [("cli",cli),("wsclient",wsclient)]}))')
        modules = json.loads(subprocess.check_output([python, '-I', '-c', program], text=True, timeout=15))
        require(modules == cli['modules'], 'actual imported CLI modules differ from the accepted artifact')
        for path, expected in self.m['spec_hashes'].items():
            require(digest(path) == expected, 'acceptance spec changed since manifest freeze')
        return {'source_sha': self.m['source_sha'], 'pid': int(pid), 'birth': birth, 'boot': boot,
                'cli': self.m['cli'], 'native_executable_sha256': digest(self.m['provider']['executable']),
                'native_version': version, 'daemon_code':daemon_code, 'spec_hashes':self.m['spec_hashes']}

    def actor_cli(self, fixture, *args):
        from launch import stream_token_file_for
        path = Path(stream_token_file_for(SimpleNamespace(name=fixture['host'], cwd=self.m['machine_cwd']),
                                          fixture['session_name']))
        info = path.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0,
                'fixture seat credential is not private')
        require(hashlib.sha256(path.read_bytes().strip()).hexdigest() ==
                self.inspect(fixture['stream_id'])['session']['token_hash'], 'fixture credential generation mismatch')
        env = {k: v for k, v in os.environ.items() if not k.startswith(('AGENT_ORCH_', 'PENTACLE_STREAM_'))}
        env.update(AGENT_ORCH_STREAM_ID=fixture['stream_id'], AGENT_ORCH_STREAM_TOKEN_FILE=str(path),
                   AGENT_ORCH_HOST_ID=self.m['local_host'], AGENT_ORCH_WS_URL=self.m['ws_url'])
        with tempfile.TemporaryFile(mode='w+') as output, tempfile.TemporaryFile(mode='w+') as errors:
            code = self.child([self.m['cli']['executable'], *args], env=env, stdout=output, stderr=errors, timeout=90)
            output.seek(0)
            errors.seek(0)
            body, diagnostic = output.read(), errors.read()
        try:
            value = json.loads(body)
        except ValueError:
            value = {}
        receipt = {'command':list(args[:2]), 'returncode':code,
                   'response_type':value.get('type'), 'error_code':value.get('error_code'),
                   'stderr_sha256':hashlib.sha256(diagnostic.encode()).hexdigest()}
        receipts = self.out / 'cli-receipts.json'
        save(receipts, (json.loads(receipts.read_text()) if receipts.exists() else []) + [receipt])
        require(code == 0, 'owned fixture CLI refused ' + ' '.join(args[:2]) + ': ' + str(value.get('error_code') or 'see cli-receipts.json'))
        require(bool(value), 'owned fixture CLI returned no JSON response')
        require(not value.get('error_code'), 'owned fixture CLI returned an error')
        return value

    def scope_controls(self, actor, other, *, question_id=None):
        from _shared.operator_auth import OperatorCredentialRegistry
        from tools.live_window.core import _OperatorConnection, LiveWindowError
        self.active_row = 'P1.scope'
        registry = OperatorCredentialRegistry(Path(self.m['operator_registry_path']))
        cid, envelope = registry.issue('pentacle', label='installed-' + self.run_id,
                                       scope={'stream': other['stream_id']})
        self.credential_ids.append(cid)
        self.record_manifest()
        token = self.out / 'scoped-credential'
        descriptor = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(envelope)
        envelope = None
        connection = _OperatorConnection(self.m['ws_url'], token, 20, None)
        denied = []
        try:
            before = self.target_state(actor['stream_id'])
            question_before = self.call({'type': 'prompt.status', 'question_id': question_id})['question'] if question_id else None
            connection.__enter__()
            own = connection.rpc({'type': 'request_stream_events', 'stream_id': other['stream_id'], 'limit': 1})
            require(own.get('type') == 'request_stream_events.ok', 'scoped own-history read failed')
            requests = [{'type': 'send', 'to_stream_id': actor['stream_id'], 'text': 'forbidden-' + self.run_id,
                         'optimistic_id': 'denied-' + self.run_id},
                        {'type': 'assistant.rebind', 'to_stream_id': actor['stream_id']}]
            if question_id:
                requests.append({'type': 'prompt.answer', 'question_id': question_id, 'selections': ['fixture']})
            for payload in requests:
                reply = connection.rpc(payload)
                require(reply.get('error_code') == 'scope_denied', 'foreign scoped ' + payload['type'] + ' was not refused')
                denied.append(payload['type'])
            # Use the lifecycle library to construct even the negative close.
            # The scoped connection must reject it before any close side effect.
            close_reply = []
            def forbidden_close(payload):
                value = connection.rpc(payload)
                close_reply.append(value)
                return value
            try:
                self.registry.close(actor['stream_id'],
                    inventory=lambda _deadline: self.call({'type': 'list_sessions'})['active'],
                    send_close=forbidden_close, wait_gone=lambda _owned, _deadline: False, timeout=1)
            except Exception:
                require(close_reply and close_reply[0].get('error_code') == 'scope_denied',
                        'foreign scoped close refusal missing')
            else:
                raise AssertionError('foreign scoped close unexpectedly completed')
            denied.append('close')
            require(self.target_state(actor['stream_id']) == before, 'foreign scope attempt changed target state')
            if question_id:
                require(self.call({'type': 'prompt.status', 'question_id': question_id})['question'] == question_before,
                        'foreign scoped answer changed the question')
        finally:
            connection.close()
            registry.revoke(cid)
            self.cleanup['credentials_revoked'].append(cid)
        revoked = _OperatorConnection(self.m['ws_url'], token, 5, None)
        try:
            try:
                revoked.__enter__()
            except LiveWindowError as error:
                require('nonce proof rejected' in str(error), 'revoked authentication lacked an explicit server refusal')
                denied.append('revoked-authentication')
            else:
                raise AssertionError('revoked credential authenticated')
        finally:
            revoked.close()
            token.unlink()
        self.row('P1.scope', refusals=denied, credential_id=cid, target_unchanged=True)

    def native_proof(self, fixture, expected, responses, image=None, requests=None):
        inspected = self.inspect(fixture['stream_id'])
        transcript = inspected['session'].get('observer_binding', {}).get('transcript') or {}
        path = Path(transcript.get('path') or '')
        if not path.is_file():
            raise Blocked('native transcript is unavailable for owned fixture')
        info = path.stat()
        require([info.st_dev, info.st_ino] == transcript.get('file_id'), 'native transcript file identity drift')
        captured = path.read_bytes()
        records = [json.loads(line) for line in captured.decode().splitlines() if line.strip()]
        snapshot = self.out / ('native-' + hashlib.sha256(fixture['stream_id'].encode()).hexdigest()[:16] + '.jsonl')
        snapshot.write_bytes(captured)
        snapshot.chmod(0o600)
        users = [r for r in records if r.get('type') == 'response_item'
                 and r.get('payload', {}).get('type') == 'message' and r['payload'].get('role') == 'user']
        proofs = []
        for text in expected:
            matches = [r for r in users if text in json.dumps(r['payload'].get('content'), ensure_ascii=False)]
            require(len(matches) == 1, 'native input was missing or submitted more than once')
            events = [e for e in inspected['recent_events'] if e.get('kind') == 'USER' and e.get('text') == text]
            require(len(events) == 1, 'durable logical USER identity was missing or duplicated')
            sends = [r for r in requests or [] if r['text'] == text]
            if requests is not None:
                require(bool(sends) and all(r['requestId'] == events[0].get('request_id')
                        and r['optimisticId'] == events[0].get('optimistic_id') for r in sends),
                        'browser client IDs do not match the unique durable input')
            if image and text == expected[-1]:
                # The installed Codex attachment route submits the owned file
                # path, then the native view_image result carries PNG bytes.
                # Inspect that exact turn, never another session's tool output.
                start = records.index(matches[0])
                end = next((i for i in range(start + 1, len(records)) if records[i] in users), len(records))
                images = [b for b in matches[0]['payload'].get('content', []) if b.get('type') == 'input_image']
                for record in records[start + 1:end]:
                    payload = record.get('payload') or {}
                    if payload.get('type') not in ('custom_tool_call_output', 'function_call_output'):
                        continue
                    output = payload.get('output')
                    if isinstance(output, str):
                        try:
                            output = ast.literal_eval(output)
                        except (ValueError, SyntaxError):
                            continue
                    if isinstance(output, list):
                        images.extend(b for b in output if isinstance(b, dict) and b.get('type') == 'input_image')
                require(bool(images), 'native image turn lacks loaded PNG image content')
                payloads = []
                for block in images:
                    url = block.get('image_url') or block.get('url') or ''
                    if isinstance(url, str) and url.startswith('data:image/png;base64,'):
                        payloads.append(base64.b64decode(url.split(',', 1)[1], validate=True))
                require(any(hashlib.sha256(data).hexdigest() == image['sha256']
                            and len(data) == image['bytes'] for data in payloads),
                        'native PNG bytes differ from the committed attachment')
            proofs.append({'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
                           'daemon_seq': events[0]['daemon_seq'], 'message_id': events[0].get('message_id'),
                           'native_record_sha256': hashlib.sha256(json.dumps(matches[0], sort_keys=True).encode()).hexdigest()})
        assistant_records = [r for r in records if r.get('type') == 'response_item'
                             and r.get('payload', {}).get('type') == 'message'
                             and r['payload'].get('role') == 'assistant']
        for response in responses:
            matches = [r for r in assistant_records if response['text'].strip() in
                       json.dumps(r['payload'].get('content'), ensure_ascii=False)]
            require(len(matches) == 1, 'rendered assistant response lacks one native source record')
        return {'path': str(path), 'captured_path':str(snapshot), 'sha256': hashlib.sha256(captured).hexdigest(), 'file_id': transcript['file_id'],
                'inputs': proofs, 'responses': responses}

    def replay(self):
        """Historical real transcript replay is explicitly separate from live use."""
        source = self.m.get('claude_replay')
        if not source:
            self.row('provider.claude.replay', 'BLOCKED', primary_error='captured Claude replay receipt missing')
            return
        require(digest(source['path']) == source['sha256'], 'captured transcript hash mismatch')
        records = [json.loads(line) for line in Path(source['path']).read_text().splitlines() if line.strip()]
        require(any(r.get('type') == 'user' for r in records) and any(r.get('type') == 'assistant' for r in records),
                'captured Claude transcript lacks actual user/assistant records')
        from claude_jsonl_norm import normalize_claude_jsonl_records
        # The real adapter's parser, with its actual captured input, supplies
        # replay only. A separate row always preserves the live-provider limit.
        parsed = normalize_claude_jsonl_records(records, host=source['host'], session_name=source['session_name'])
        require(parsed and any(p.get('kind') == 'USER' for p in parsed)
                and any(p.get('kind') in ('ASSIST', 'ASSIST_TEXT') for p in parsed),
                'captured transcript produced no adapter input/response pair')
        self.row('provider.claude.replay', source=source, historical_only=True)

    def run(self):
        expected = self.m['expected_build_id']
        self.active_row = 'build'
        actual = served_build(self.m['web_url'])
        require(actual == expected, 'served build mismatch: expected ' + expected + ', got ' + actual)
        self.row('build', actual=actual, mutations=0)
        from tools import spawn_fleet_smoke as smoke
        from tools.live_window import OwnedSessionRegistry
        self.registry = OwnedSessionRegistry(self.out / 'owned.json')
        self.active_row = 'identity'
        before_identity = self.identity()
        self.row('identity', runtime=before_identity)
        self.record_manifest()
        with smoke._operator_connection(self.m['ws_url'], Path(self.m['operator_token_path']), self.timeout,
                                         self.registry) as (rpc, ready, wait_event, register, close):
            self.rpc = rpc
            try:
                self.active_row = 'P2.spawn'
                catalog = self.call({'type': 'spawn_catalog_get'})['catalog_version']
                receiver = self.spawn(self.m['local_host'], register, ready, catalog)
                actor = self.spawn(self.m['local_host'], register, ready, catalog, parent=receiver['stream_id'],
                                   visibility='default')
                sentinel = self.spawn(self.m['local_host'], register, ready, catalog)
                self.fixtures[:] = [actor, receiver, sentinel]
                self.record_manifest()
                sentinel_before = self.target_state(sentinel['stream_id'])
                require(all(not [e for e in self.history(f['stream_id']) if e.get('kind') in ('USER', 'ASSIST_TEXT')]
                            for f in self.fixtures), 'fixture chat is dirty before browser input')
                self.row('P2.spawn', admitted=[f['account_binding'] for f in self.fixtures])
                self.active_row = 'P3/P4.browser'
                observation = self.browser('chat')
                for index, fixture in enumerate(self.fixtures[:2]):
                    proof = self.native_proof(fixture, observation['inputs'][str(index)],
                                              observation['messages'][str(index)]['replies'],
                                              image=observation.get('image') if index == 0 else None,
                                              requests=observation['messages'][str(index)]['requests'])
                    self.row('P3.input.' + str(index), native=proof, browser=observation['messages'][str(index)])
                self.active_row = 'P4.history'
                history = self.browser('history', allow_failure=True,
                                       before=observation['history'], inputs=observation['inputs'])
                self.row('P4.history', history['status'], ordered_events=history['history'],
                         primary_error=history.get('primary_error'), sentinel_isolated=True)
                self.active_row = 'P5.question'
                question = 'installed-q-' + self.run_id
                self.questions.append((actor, question))
                asked = self.actor_cli(actor, 'prompt', 'ask', '--question-id', question,
                    '--title', 'Disposable installed journey fixture', '--body', 'Select the fixture answer. No authority or business action is requested.',
                    '--option', 'Fixture=fixture')
                require(asked.get('type') == 'prompt.ask.ok', 'fixture question was held or refused')
                self.scope_controls(actor, receiver, question_id=question)
                self.active_row = 'P5.question'
                self.browser('question', question_id=question, notification_id=asked['question']['notification_id'])
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    status = self.call({'type': 'prompt.status', 'question_id': question})
                    saved_question = status.get('question') or {}
                    notification_id = saved_question.get('notification_id')
                    notices = self.call({'type': 'notification.list', 'notification_ids': [notification_id]})
                    notice = (notices.get('notifications') or [{}])[0]
                    if (saved_question.get('state') == 'consumed'
                            and notice.get('resolution', {}).get('delivery_status') == 'delivered'):
                        break
                    time.sleep(1)
                else:
                    raise AssertionError('question decision did not reach the intended provider generation')
                generation = (saved_question.get('envelope') or {}).get('producer_session_generation')
                require(generation == actor['session_generation'], 'question decision belongs to another generation')
                delivered = [e for e in self.history(actor['stream_id']) if e.get('kind') == 'USER'
                             and notification_id in e.get('text', '')]
                require(len(delivered) == 1, 'question answer reached the provider more or less than once')
                self.row('P5.question', question_id=question, status=status,
                         delivery_daemon_seq=delivered[0]['daemon_seq'])
                self.active_row = 'P5.report'
                summary = 'Installed journey fixture ' + self.run_id + ' complete'
                reported = self.actor_cli(actor, 'report', '--msg-id', '0', '--status', 'done', '--result',
                    json.dumps({'summary': summary, 'findings': [], 'next_action': 'fixture cleanup'}))
                report_id = reported['report_id']
                metadata = self.call({'type': 'work.report.get', 'report_id': report_id})
                require(metadata.get('report_id') == report_id and metadata.get('status') == 'done', 'durable report metadata mismatch')
                notice = reported.get('notice_delivery') or {}
                ledger = self.call({'type': 'ledger_get', 'tell_id': notice.get('tell_id')})
                require(summary in ledger.get('tell', {}).get('text', '') and ledger.get('read_only') is True,
                        'durable report summary/notice mismatch')
                asset = 'installed-report-' + self.run_id
                body = {'schema_version': 1, 'title': summary, 'sections': [{'id': 'fixture', 'title': report_id,
                        'status': 'reference', 'blocks': [{'id': 'summary', 'type': 'para',
                        'runs': [{'type': 'text', 'text': summary}]}]}]}
                asset_path = self.out / 'report-asset.json'
                save(asset_path, body)
                self.actor_cli(actor, 'asset', 'publish', '--asset-id', asset, '--title', summary,
                               '--type', 'report', '--content-file', str(asset_path))
                self.assets.append((actor['stream_id'], asset))
                self.browser('report', asset_id=asset, report_id=report_id, summary=summary)
                self.row('P5.report', report_id=report_id, metadata=metadata, ledger_tell_id=notice['tell_id'], asset_id=asset)
                if self.m.get('remote'):
                    self.active_row = 'P10.remote'
                    marker = 'REMOTE_' + self.run_id
                    prompt = 'Disposable remote fixture. Do not use tools, message other chats or manage any seat. Reply exactly ' + marker + ' and do nothing else.'
                    remote = self.spawn(self.m['remote']['host'], register, ready, catalog, initial_prompt=prompt)
                    wait_event(remote['stream_id'], marker)
                    events = self.history(remote['stream_id'])
                    require(len([e for e in events if e.get('kind') == 'USER' and e.get('text') == prompt]) == 1,
                            'remote initial brief missing or duplicated')
                    replies = [e for e in events if e.get('kind') in ('ASSIST', 'ASSIST_TEXT')
                               and e.get('text', '').strip() == marker]
                    require(len(replies) == 1, 'remote actual provider response missing or duplicated')
                    self.row('P10.remote', fixture=remote, response_daemon_seq=replies[0]['daemon_seq'])
                self.active_row = 'P8.ordinary-state'
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    states = [self.inspect(f['stream_id'])['session'] for f in self.fixtures[:2]]
                    if all(row.get('working') is False and not row.get('queued_messages') for row in states):
                        break
                    time.sleep(1)
                else:
                    raise AssertionError('ordinary queue/working state did not settle')
                require(self.target_state(sentinel['stream_id']) == sentinel_before, 'unrelated sentinel changed')
                require(self.identity() == before_identity and served_build(self.m['web_url']) == expected,
                        'installed artifact identity changed during the journey')
                self.row('P8.ordinary-state', settled=True, full_soak_qualified=False)
                self.replay()
                self.row('provider.claude.live', 'NOT TESTED', required=False,
                         primary_error=self.m.get('live_claude_limit', 'live provider fixture is not authorized'))
            except Exception as primary:
                self.failure = (self.active_row, primary)
                self.row(self.active_row, 'BLOCKED' if isinstance(primary, (Blocked, OSError, ImportError)) else 'FAIL',
                         primary_error=error_text(primary))
                raise
            finally:
                from _shared.operator_auth import OperatorCredentialRegistry
                for cid in self.credential_ids:
                    if cid not in self.cleanup['credentials_revoked']:
                        try:
                            OperatorCredentialRegistry(Path(self.m['operator_registry_path'])).revoke(cid)
                            self.cleanup['credentials_revoked'].append(cid)
                        except Exception as error:
                            self.cleanup['errors'].append('credential revocation: ' + error_text(error))
                (self.out / 'scoped-credential').unlink(missing_ok=True)
                try:
                    cleanup_connection = smoke._operator_connection(self.m['ws_url'], Path(self.m['operator_token_path']),
                                                                    30, self.registry)
                    with cleanup_connection as (cleanup_rpc, _ready, _event, _register, cleanup_close):
                        self.rpc = cleanup_rpc
                        close = cleanup_close
                        for fixture, question_id in self.questions:
                            try:
                                status = self.rpc({'type': 'prompt.status', 'question_id': question_id}, 'prompt.status')
                                if status.get('error_code') == 'question_not_found':
                                    continue  # Attempted create was refused before persistence.
                                require(status.get('type') == 'prompt.status.ok', 'owned question cleanup status refused')
                                current = status['question']
                                if current.get('state') == 'open':
                                    self.actor_cli(fixture, 'prompt', 'cancel', question_id)
                            except Exception as error:
                                self.cleanup['errors'].append('question: ' + error_text(error))
                        for stream, asset in self.assets:
                            try:
                                self.call({'type': 'asset.delete', 'stream_id': stream, 'asset_id': asset})
                            except Exception as error:
                                self.cleanup['errors'].append('asset: ' + error_text(error))
                        # Creation order is parent-before-child; display order differs.
                        for fixture in sorted(self.fixtures, key=lambda f: f['spawn_ordinal'], reverse=True):
                            try:
                                close(fixture['stream_id'])
                                inspected = self.inspect(fixture['stream_id'])
                                require(inspected['session']['status'] == 'closed', 'owned durable closed row missing')
                                require(inspected['session']['session_generation'] == fixture['session_generation'],
                                        'owned cleanup generation changed')
                                # Admission can fail before its first inspect.
                                # The freshly read, same-generation closed row
                                # remains authoritative for its token hash.
                                token_hash = fixture.get('stream_token_sha256') or inspected['session'].get('token_hash')
                                require(isinstance(token_hash, str) and re.fullmatch('[0-9a-f]{64}', token_hash),
                                        'owned cleanup credential binding missing')
                                remote = fixture['host'] != self.m['local_host']
                                tmux = self.m['remote'].get('tmux_bin', 'tmux') if remote else self.m.get('tmux_bin', 'tmux')
                                command = [tmux, 'has-session', '-t', '=' + fixture['session_name']]
                                if remote:
                                    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                                               fixture['host'], shlex.join(command)]
                                physical = subprocess.run(command, capture_output=True, text=True, timeout=10)
                                require(physical.returncode == 1, 'owned pane absence unproved')
                                if not remote:
                                    from launch import stream_token_file_for
                                    token = Path(stream_token_file_for(SimpleNamespace(name=fixture['host'], cwd=self.m['machine_cwd']),
                                                                       fixture['session_name']))
                                    if token.exists():
                                        require(not token.is_symlink() and stat.S_ISREG(token.lstat().st_mode),
                                                'owned token file was replaced; refusing removal')
                                        require(hashlib.sha256(token.read_bytes().strip()).hexdigest() == token_hash,
                                                'owned token file was replaced; refusing removal')
                                        token.unlink()
                                else:
                                    from launch import stream_token_file_for
                                    token = stream_token_file_for(SimpleNamespace(name=fixture['host'], cwd=self.m['remote']['cwd']),
                                                                  fixture['session_name'])
                                    program = ('import hashlib,os,pathlib,stat,sys; p=pathlib.Path(sys.argv[1]); assert not p.is_symlink(); '
                                               's=p.lstat() if p.exists() else None; '
                                               'assert s is None or (stat.S_ISREG(s.st_mode) and s.st_uid==os.getuid() and not s.st_mode & 0o077); '
                                               'assert s is None or hashlib.sha256(p.read_bytes().strip()).hexdigest()==sys.argv[2]; '
                                               'p.unlink(missing_ok=True)')
                                    removed = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', fixture['host'],
                                        shlex.join(['python3', '-c', program, token, token_hash])],
                                        capture_output=True, text=True, timeout=15)
                                    require(removed.returncode == 0, 'owned remote credential-file cleanup unproved')
                                self.cleanup['closed'] += 1
                            except Exception as error:
                                self.cleanup['errors'].append(fixture['stream_id'] + ': ' + error_text(error))
                        self.record_manifest()
                except Exception as cleanup_error:
                    self.cleanup['errors'].append('cleanup connection: ' + error_text(cleanup_error))
                self.record_manifest()



def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--evidence-dir', required=True, type=Path)
    parser.add_argument('--expect-build-id')
    args = parser.parse_args(argv)
    out = args.evidence_dir.resolve()
    if out.exists() and any(out.iterdir()):
        # Retire a dirty run; do not overwrite an earlier primary failure.
        print('BLOCKED: evidence directory must be fresh', file=sys.stderr)
        return 2
    out.mkdir(parents=True, mode=0o700, exist_ok=True)
    journey = None
    code = 0
    try:
        manifest = validate_manifest(json.loads(args.manifest.read_text()))
        if args.expect_build_id is not None:
            manifest = {**manifest, 'expected_build_id': args.expect_build_id}
        journey = Journey(manifest, out)
        journey.run()
    except Exception as error:
        primary = journey.failure[1] if journey and journey.failure else error
        code = 2 if journey is None or isinstance(primary, (Blocked, OSError, ImportError)) else 1
        if journey is None:
            save(out / 'rows.json', [{'row': 'setup', 'result': 'BLOCKED', 'primary_error': error_text(error),
                                    'harness_error': None, 'fixture_ids': [], 'cleanup': {'expected': 0, 'closed': 0}}])
        else:
            if journey.failure is None:
                journey.row(journey.active_row, 'BLOCKED' if code == 2 else 'FAIL', primary_error=error_text(error))
            elif error is not primary:
                journey.cleanup['errors'].append('connection shutdown: ' + error_text(error))
    finally:
        if journey:
            cleanup = journey.cleanup
            if cleanup['errors'] or cleanup['closed'] != cleanup['expected']:
                code = 1
            if journey.pending_spawns:
                cleanup['errors'].append('ambiguous spawn outcome retained by immutable request ID; FD reconciliation required')
                code = 1
            for row in journey.rows:
                row['cleanup'] = cleanup
                if cleanup['errors']:
                    row['harness_error'] = '; '.join(cleanup['errors'])
            if any(r['result'] == 'FAIL' for r in journey.rows):
                code = 1
            if code == 0 and any(r['result'] == 'BLOCKED' for r in journey.rows):
                code = 2
            save(out / 'rows.json', journey.rows)
            journey.record_manifest()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
