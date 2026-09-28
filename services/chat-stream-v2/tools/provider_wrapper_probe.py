#!/usr/bin/env python3
"""Claude fleet cells plus independent tell/USER wrapper assertions.

Run only in the coordinator's runtime window. Reuses the fleet smoke's nonce
authentication, generation-bound ownership, measurements and verified cleanup.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import uuid

SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_DIR))
from tools import spawn_fleet_smoke as smoke  # noqa: E402
from tools.live_window import OwnedSessionRegistry  # noqa: E402


class OwnedImageCleanupError(RuntimeError):
    """The harness could not remove its own remote/local test PNG."""


def assert_wrapper_receipt(
    receipt: dict, events: list[dict], *, stream_id: str, body: str, watermark: int,
    source_record: dict | None = None,
) -> dict:
    """Independent oracle: assistant replies alone cannot satisfy this gate."""
    assert receipt.get("submission_confirmed") is True, "tell submission not confirmed"
    matches = [event for event in events if (
        event.get("stream_id") == stream_id and event.get("provider") == "claude"
        and event.get("kind") == "USER" and event.get("text") == body
        and int(event.get("daemon_seq", 0)) > watermark
    )]
    assert len(matches) == 1, "expected one post-watermark USER with exact display text"
    event = matches[0]
    if source_record is None:
        _assert_wrapper_event(event, body=body)
    else:
        assert_provider_source(event, body=body, record=source_record)
    return event


def _assert_wrapper_event(event: dict, *, body: str) -> None:
    """Check display/source agreement without manufacturing a receipt."""
    wrapper = event.get("provider_wrapper") or {}
    assert wrapper.get("kind") == "claude_pasted_content", "missing wrapper kind"
    assert wrapper.get("provenance") == "grammar", "missing grammar provenance"
    identifier = wrapper.get("id")
    assert isinstance(identifier, str) and re.fullmatch(r"[0-9a-f]+", identifier), "invalid wrapper ID"
    expected_raw = (
        f'\n\n<pasted_content id="{identifier}">\n{body}'
        f'\n</pasted_content id="{identifier}">\n'
    )
    compact_raw = expected_raw[2:-1]
    assert (event.get("raw") or {}).get("provider_content") in (expected_raw, compact_raw), "raw envelope not retained"


def assert_provider_source(event: dict, *, body: str, record: dict) -> dict:
    """L2 oracle: native one-line source or the unchanged strict XML grammar."""
    raw = event.get('raw') or {}
    assert record.get('uuid') and record['uuid'] == raw.get('jsonl_record_uuid'), 'source/event UUID mismatch'
    assert record.get('type') in ('user', 'attachment') and not record.get('isMeta'), 'source is not operator content'
    container = record.get('attachment') if record['type'] == 'attachment' else record.get('message')
    assert isinstance(container, dict), 'source content container missing'
    if record['type'] == 'attachment':
        assert container.get('type') == 'queued_command' and (container.get('origin') or {}).get('kind') == 'human', 'queued source is not human operator content'
    else:
        assert container.get('role', 'user') == 'user', 'source message role is not user'
    content = container.get('prompt', container.get('content'))
    if isinstance(content, list):
        assert all(isinstance(block, dict) for block in content), 'malformed source blocks'
        content = ''.join(block.get('text', '') for block in content if block.get('type') == 'text')
    assert isinstance(content, str), 'owned source content is not text'
    assert event.get('text') == body, 'source/body/display mismatch'
    if '<pasted_content' in content or '</pasted_content' in content:
        _assert_wrapper_event(event, body=body)
        assert content == raw.get('provider_content'), 'actual XML source differs from retained provider content'
        mode = 'strict-XML'
    else:
        assert '\n' not in body and '\r' not in body, 'plain-source exception is one-line only'
        assert content == body, 'plain source/body/display mismatch'
        if 'envelope_source' in raw:
            assert raw['envelope_source'] == body, 'retained envelope/body mismatch'
        assert not event.get('provider_wrapper'), 'plain source unexpectedly annotated as wrapper'
        mode = 'native-one-line'
    return {'mode': mode, 'daemon_seq': event.get('daemon_seq'), 'source_uuid': record['uuid'],
            'source_sha256': hashlib.sha256(content.encode()).hexdigest()}


# Existing L2/L4 process oracle: bind the pending pytest descendant to the owned pane.
FOREGROUND_PROCESS = r'''import subprocess,json,sys,shlex; job,pane=sys.argv[1:]; rows=[]
for line in subprocess.check_output(['ps','-axo','pid=,ppid=,stat=,args='],text=True).splitlines():
 p=line.strip().split(None,3)
 if len(p)==4:rows.append({'pid':int(p[0]),'ppid':int(p[1]),'state':p[2],'command':p[3]})
byid={r['pid']:r for r in rows}; found=[]
for r in rows:
 try:a=shlex.split(r['command'])
 except ValueError:continue
 if len(a)==3 and a[1]==job and a[2]=='probe':
  chain=[];cur=r
  for _ in range(30):
   chain.append(cur['pid'])
   if cur['ppid'] not in byid:break
   cur=byid[cur['ppid']]
  descendants=[]
  for candidate in rows:
   cur=candidate
   for _ in range(30):
    if cur['ppid']==r['pid']:descendants.append(candidate);break
    if cur['ppid'] not in byid:break
    cur=byid[cur['ppid']]
  found.append(dict(r,ancestor_pids=chain,owned_pane_in_ancestry=int(pane) in chain,descendants=descendants))
print(json.dumps({'job_path':job,'pane_pid':int(pane),'pending_foreground_processes':found}))'''

def initial_probe_prompt(seed: str, *, foreground_job: str | None = None) -> str:
    """Declare the entire finite probe contract before any later instruction."""
    job = (f'Run the useful finite scratch validation command {foreground_job} with Bash in the '
           'foreground, timeout=240000 and run_in_background=false when instructed. '
           if foreground_job else 'The idle cell has no foreground validation task. ')
    return ('You are a disposable Pentacle ingress PRODUCT-TEST-TARGET. No production edits. '
            'This initial contract authorizes all following finite steps: readiness and tell nonce replies; '
            'create the disposable notification question using the supplied exact command; process its '
            'authenticated operator answer; ' + job +
            'use Read on the supplied owned PNG and reply with its marker; accept typed literal image '
            'coordinate text and an actual PNG attachment as operator content and acknowledge their supplied '
            'markers. Follow-up instructions supply exact paths, question ids and nonces for these steps. '
            'Do not invent additional work. ' + seed)


def finite_job_path(command: str) -> str:
    """Accept the existing scratch job invocation, never a wait/busy substitute."""
    argv = shlex.split(command)
    if len(argv) != 3 or Path(argv[0]).name not in ('python3', 'python3.13') or argv[2] != 'probe':
        raise RuntimeError('HARNESS_ERROR: expected python3 <finite-scratch-job.py> probe')
    if not Path(argv[1]).is_absolute() or not argv[1].endswith('.py'):
        raise RuntimeError('HARNESS_ERROR: finite scratch job must name an absolute Python script')
    return argv[1]


def assert_operator_controls(events: list[dict], records: list[dict], *, stream: str,
                             literal: str, caption: str) -> list[dict]:
    """L4 positive controls need native operator source, including real image blocks."""
    proofs = []
    for body in (literal, caption):
        matches = [event for event in events if event.get('stream_id') == stream
                   and event.get('kind') == 'USER' and event.get('provider') == 'claude'
                   and event.get('text') == body]
        assert len(matches) == 1, 'expected exactly one owned operator control'
        event = matches[0]
        sources = [record for record in records if record.get('uuid') ==
                   (event.get('raw') or {}).get('jsonl_record_uuid')]
        assert len(sources) == 1, 'operator control source UUID missing or ambiguous'
        record = sources[0]
        assert record.get('type') == 'user' and (record.get('message') or {}).get('role') == 'user', 'control is not native operator USER'
        proof = assert_provider_source(event, body=body, record=record)
        content = record['message']['content']
        image_blocks = [block for block in content if isinstance(block, dict) and block.get('type') == 'image'] if isinstance(content, list) else []
        if body == caption:
            assert event.get('attachments') and image_blocks, 'real PNG attachment absent from daemon/provider control'
            hashes = []
            for block in image_blocks:
                source = block.get('source') or {}
                assert source.get('type') == 'base64' and source.get('media_type') == 'image/png', 'provider control image is not a base64 PNG'
                try:
                    image = base64.b64decode(source.get('data', ''), validate=True)
                except (binascii.Error, ValueError, TypeError) as exc:
                    raise AssertionError('malformed provider PNG control') from exc
                assert image.startswith(b'\x89PNG\r\n\x1a\n'), 'provider control bytes are not PNG'
                hashes.append(hashlib.sha256(image).hexdigest())
            proof['provider_png_sha256'] = hashes
        proofs.append({**proof, 'daemon_event_id': event['daemon_seq'], 'native_operator_user': True,
                       'image_blocks': len(image_blocks)})
    return proofs


def _host_command(host: str, *argv: str) -> str:
    """Use the fleet's existing per-host transport for owned probe files."""
    from hosts import Hosts, HostsConfig
    from machines import load_machines, get_local_machine_name
    machines = load_machines(os.environ)
    local = get_local_machine_name(machines)
    if not local or not any(m.name == host for m in machines):
        raise RuntimeError("probe host is absent from the machine configuration")
    transport = Hosts(local_host=local, peers={m.name: m for m in machines if not m.is_local},
                      config=HostsConfig.from_env())
    code, output = asyncio.run(transport.run_command(host, *argv, timeout=20))
    if code:
        raise RuntimeError(f"owned probe command failed on {host}: exit {code}")
    return output


def _owned_source_records(host: str, source_session_id: str) -> list[dict]:
    from machines import load_machines
    machine = next(m for m in load_machines(os.environ) if m.name == host)
    # Read only the transcript whose UUID was observed on this owned seat.
    if not re.fullmatch(r"[0-9a-f-]{36}", source_session_id):
        raise RuntimeError("owned Claude transcript UUID is unavailable")
    program = (
        "import pathlib,json,sys; "
        "paths=list(pathlib.Path(sys.argv[1]).expanduser().rglob(sys.argv[2]+'.jsonl')); "
        "assert len(paths)==1, 'owned source transcript must be unique'; "
        "print(json.dumps([json.loads(line) for line in paths[0].read_text().splitlines() if line.strip()]))"
    )
    return json.loads(_host_command(host, "python3", "-c", program, machine.projects_root, source_session_id))


def assert_notification_journey(events: list[dict], notification: dict, *, stream: str,
                                watermark: int, queued: bool) -> dict:
    """Independent route/tag/proof oracle, also exercised by non-live tests."""
    nid = notification['notification_id']
    assert notification['resolution']['delivery_status'] == 'delivered', "answer lacks durable delivery proof"
    assert notification['resolution'].get('by') == 'operator', "answer is not authenticated operator delivery"
    matches = [e for e in events if e.get('stream_id') == stream and e.get('provider') == 'claude'
        and e.get('kind') == 'USER'
        and int(e.get('daemon_seq', 0)) > watermark
        and (e.get('message_envelope') or {}).get('kind') == 'notification_answer'
        and (e.get('message_envelope') or {}).get('id') == 'notification-answer-' + nid]
    assert len(matches) == 1, "expected exactly one post-watermark notification answer"
    event = matches[0]
    assert ('notification_id=' + nid) in event['text'].splitlines()
    assert ('answer=done') in event['text'].splitlines()
    assert ('by=operator') in event['text'].splitlines()
    assert ((event.get('raw') or {}).get('subtype') == 'queued-command') is queued, "wrong provider ingress route"
    _assert_wrapper_event(event, body=event['text'])
    return event


def notification_journey(host: str, stream: str, *, queued: bool, rpc, wait_event,
                         timeout: float, evidence: dict | None = None,
                         foreground_job: str | None = None) -> dict:
    """Create and resolve only this seat's disposable question, once."""
    result = evidence if evidence is not None else {}
    if queued and not foreground_job:
        raise RuntimeError('HARNESS_ERROR: queued cell requires an upfront-declared finite scratch job')
    job_path = finite_job_path(foreground_job) if queued else None
    nonce = uuid.uuid4().hex
    qid, marker = 'q-wrapper-' + nonce, 'PENTACLE_ASKED_' + nonce
    def events():
        return rpc({'type': 'request_stream_events', 'stream_id': stream, 'limit': 500},
                   'request_stream_events')['events']
    def until(read, predicate):
        deadline = time.monotonic() + timeout
        while True:
            value = read()
            if predicate(value):
                return value
            if time.monotonic() >= deadline:
                raise RuntimeError('owned journey observation timed out')
            time.sleep(0.2)
    rpc({'type': 'set_visibility', 'stream_id': stream, 'visibility': 'visible'}, 'set_visibility')
    command = shlex.join(['agent-orch', 'prompt', 'ask', '--question-id', qid,
        '--title', 'Disposable wrapper probe', '--body', 'Has the disposable probe completed?',
        '--option', 'Done=done', '--option', 'Not yet=not_yet'])
    rpc({'type': 'send', 'host': host, 'session_name': stream.partition(':')[2],
         'text': f'Run this exact command using Bash: {command}. Then reply exactly {marker} and do nothing else.',
         'optimistic_id': uuid.uuid4().hex}, 'send')
    wait_event(stream, marker)
    status = rpc({'type': 'prompt.status', 'question_id': qid}, 'prompt.status')['question']
    nid = status['notification_id']
    def notification():
        reply = rpc({'type': 'notification.list', 'notification_ids': [nid]}, 'notification.list')
        return reply['notifications'][0]
    saved = notification()
    action = next(a for a in saved['actions'] if a.get('value', {}).get('answer') == 'done')
    result.update(question=status, saved_action=action)
    busy_tool = None
    if not queued:
        def seat():
            inventory = rpc({'type': 'list_sessions'}, 'list_sessions')
            return next(r for r in inventory['active'] if r['stream_id'] == stream)
        until(seat, lambda row: row.get('working') is False)
    if queued:
        timed_command = foreground_job
        rpc({'type': 'send', 'host': host, 'session_name': stream.partition(':')[2],
             'text': f'Run the declared finite scratch validation command now: {timed_command}. '
                     'Use Bash in the foreground with timeout=240000 and run_in_background=false. '
                     'After it completes, report the result briefly and process the queued operator answer normally.',
             'optimistic_id': uuid.uuid4().hex}, 'send')
        active = until(events, lambda rows: any(e.get('kind') == 'TOOL_USE'
            and (e.get('raw') or {}).get('tool_name') == 'Bash'
            and (e.get('raw') or {}).get('tool_input', {}).get('command') == timed_command for e in rows))
        busy_tool = next(e for e in active if e.get('kind') == 'TOOL_USE'
            and (e.get('raw') or {}).get('tool_name') == 'Bash'
            and (e.get('raw') or {}).get('tool_input', {}).get('command') == timed_command)
        tool_input = busy_tool['raw']['tool_input']
        assert int(tool_input.get('timeout', 0)) >= 180000 and tool_input.get('run_in_background', False) is False, 'finite foreground tool contract mismatch'
        assert not any(e.get('kind') == 'TOOL_RESULT' and (e.get('raw') or {}).get('tool_use_id') ==
                       busy_tool['raw']['tool_use_id'] for e in active), 'timed tool already completed'
        inventory = rpc({'type': 'list_sessions'}, 'list_sessions')
        seat = next(row for row in inventory['active'] if row['stream_id'] == stream)
        proof = json.loads(_host_command(host, 'python3', '-c', FOREGROUND_PROCESS, job_path, str(seat['pane_pid'])))
        pending = proof['pending_foreground_processes']
        if not pending or not all(row['owned_pane_in_ancestry'] for row in pending) or not any(
            'pytest' in child['command'] for row in pending for child in row['descendants']
        ):
            raise RuntimeError('HARNESS_ERROR: owned pending finite scratch pytest process absent')
        result['foreground_job'] = {'command': foreground_job, 'job_path': job_path,
                                    'pre_resolve_process_proof': proof, 'active_tool': busy_tool}
    before = events()
    if queued:
        assert not any(e.get('kind') == 'TOOL_RESULT' and (e.get('raw') or {}).get('tool_use_id') ==
                       busy_tool['raw']['tool_use_id'] for e in before), 'foreground task completed before resolve'
    watermark = max((int(e.get('daemon_seq', 0)) for e in before), default=0)
    result.update(watermark=watermark, busy_tool=busy_tool)
    resolved = rpc({'type': 'notification.resolve', 'notification_id': nid,
        'action_id': action['action_id'], 'action_kind': action['kind'],
        'choice': action['choice'], 'selections': [action['value']['answer']]}, 'notification.resolve')
    delivered = until(notification, lambda n: n.get('resolution', {}).get('delivery_status') == 'delivered')
    result.update(resolve=resolved, notification=delivered)
    rows = until(events, lambda rows: any((e.get('message_envelope') or {}).get('id') ==
        'notification-answer-' + nid for e in rows))
    event = assert_notification_journey(rows, delivered, stream=stream, watermark=watermark, queued=queued)
    source_id = event.get('session_id')
    source = _owned_source_records(host, source_id)
    record = next(r for r in source if r.get('uuid') == event['raw']['jsonl_record_uuid'])
    result['source_proof'] = assert_provider_source(event, body=event['text'], record=record)
    result.update(event=event, source_record=record, source_record_sha256=hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
    if queued:
        assert record.get('type') == 'attachment' and record.get('attachment', {}).get('type') == 'queued_command'
        assert record['attachment']['origin']['kind'] == 'human'
    else:
        assert record.get('type') == 'user', 'idle answer was not an ordinary provider USER'
    # Oversized owned image forces Claude's genuine coordinate companion.
    image = '/tmp/pentacle-wrapper-image-' + nonce + '.png'
    program = ("import pathlib,struct,zlib,sys; w,h=1800,2400; "
        "chunk=lambda t,d:struct.pack('>I',len(d))+t+d+struct.pack('>I',zlib.crc32(t+d)); "
        "data=b'\\x89PNG\\r\\n\\x1a\\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',w,h,8,2,0,0,0))"
        "+chunk(b'IDAT',zlib.compress((b'\\0'+b'\\x20\\x60\\xa0'*w)*h))+chunk(b'IEND',b''); "
        "pathlib.Path(sys.argv[1]).write_bytes(data)")
    try:
        _host_command(host, 'python3', '-c', program, image)
        read_marker = 'PENTACLE_READ_' + nonce
        rpc({'type': 'send', 'host': host, 'session_name': stream.partition(':')[2],
             'text': f'Use Read on exactly {image}. Then reply exactly {read_marker} and do nothing else.',
             'optimistic_id': uuid.uuid4().hex}, 'send')
        wait_event(stream, read_marker)
        source = _owned_source_records(host, source_id)
        use = next(r for r in source if any(b.get('type') == 'tool_use' and b.get('name') == 'Read'
            and b.get('input', {}).get('file_path') == image for b in (r.get('message', {}).get('content') or [])
            if isinstance(b, dict)))
        tool_id = next(b['id'] for b in use['message']['content'] if b.get('name') == 'Read' and
                       b.get('input', {}).get('file_path') == image)
        read = next(r for r in source if any(b.get('type') == 'tool_result' and b.get('tool_use_id') == tool_id
            for b in (r.get('message', {}).get('content') or []) if isinstance(b, dict)))
        meta = next(r for r in source if r.get('parentUuid') == read['uuid'] and r.get('type') == 'user'
                    and r.get('isMeta') is True and r.get('turnCompanion') is True
                    and re.fullmatch(r'\[Image: original [0-9]+x[0-9]+, displayed at [0-9]+x[0-9]+\. Multiply coordinates by [0-9]+(?:\.[0-9]+)? to map to original image\.\]',
                                     str(r.get('message', {}).get('content') or '')))
        rows = events()
        result['image'] = {'owned_path': image, 'read_record': read, 'meta_record': meta,
                           'events': rows}
        assert any(e.get('kind') == 'TOOL_RESULT' and (e.get('raw') or {}).get('tool_use_id') == tool_id for e in rows)
        assert not any((e.get('raw') or {}).get('jsonl_record_uuid') == meta['uuid'] for e in rows), 'phantom operator metadata row'
    finally:
        try:
            _host_command(host, 'python3', '-c', 'import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.unlink(missing_ok=True); assert not p.exists()', image)
        except Exception as exc:
            raise OwnedImageCleanupError('owned image cleanup failed') from exc
        result['image_cleanup'] = {'path': image, 'removed': True}
    return result


def runtime_binding(checkout: Path, sha: str, pid: int) -> dict:
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    command = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True).strip()
    assert actual == sha, "installed artifact SHA mismatch"
    assert str(checkout / "services/chat-stream-v2/main.py") in command, "PID is not the installed daemon"
    paths = ["provider_wrappers.py", "claude_jsonl_norm.py", "message_envelopes.py", "ingest.py", "submission_events.py", "comms.py"]
    digests = {}
    for name in paths:
        rel = f"services/chat-stream-v2/{name}"
        installed = (checkout / rel).read_bytes()
        committed = subprocess.check_output(["git", "show", f"{sha}:{rel}"], cwd=checkout)
        assert installed == committed, f"installed bytes differ: {rel}"
        digests[rel] = hashlib.sha256(installed).hexdigest()
    return {"candidate_sha": sha, "daemon_pid": pid, "command": command, "source_sha256": digests}


def probe_cell(host: str, mode: str, args, output: Path) -> dict:
    registry_path = output / "owned.json"
    registry = OwnedSessionRegistry(registry_path)
    evidence: dict = {"host": host, "provider": "claude", "prompt_mode": mode, "rpc": []}
    closed: list[str] = []
    registered: list[str] = []
    try:
        jobs = getattr(args, 'foreground_jobs', {})
        if args.journeys and mode == 'promptless':
            if host not in jobs:
                raise RuntimeError('HARNESS_ERROR: queued cell has no declared finite scratch job')
            finite_job_path(jobs[host])
        with smoke._operator_connection(args.url, args.token_path, args.timeout, registry) as (
            rpc, wait_ready, wait_event, register_owned, close_owned,
        ):
            def capture_rpc(payload, prefix):
                response = rpc(payload, prefix)
                evidence["rpc"].append({"request": payload, "response": response})
                return response

            def close(stream):
                close_owned(stream)
                closed.append(stream)

            def register(payload, response):
                register_owned(payload, response)
                registered.append(response["stream_id"])

            def validate(stream, marker):
                metrics = close_owned.validate(stream, marker)
                # Preserve every existing fleet-smoke predicate before tell.
                assert all(value.get("passed", True) for value in metrics.values()
                           if isinstance(value, dict)), "fleet smoke predicate failed"
                before = rpc({"type": "request_stream_events", "stream_id": stream, "limit": 500}, "request_stream_events")
                watermark = max((int(e.get("daemon_seq", 0)) for e in before["events"]), default=0)
                tell_marker = f"PENTACLE_WRAPPER_{uuid.uuid4().hex}"
                body = f"Reply exactly {tell_marker} and do nothing else."
                evidence.update(watermark=watermark, tell_body=body)
                receipt = capture_rpc({"type": "tell", "stream_id": stream, "message": body,
                                       "tell_id": uuid.uuid4().hex}, "tell")
                # General tell can confirm from the pane before transcript ingest.
                # Keep this owned session alive until its exact USER is observable;
                # never turn a pending receipt into a confirmed one or resend it.
                assert receipt.get("submission_confirmed") is True, "tell submission not confirmed"
                started = time.monotonic()
                deadline = started + args.timeout
                polls = 0
                while True:
                    replay = rpc({"type": "request_stream_events", "stream_id": stream, "limit": 500}, "request_stream_events")
                    polls += 1
                    evidence["events"] = replay["events"]
                    now = time.monotonic()
                    evidence["event_wait"] = {"polls": polls, "elapsed_ms": (now - started) * 1000,
                                              "timeout_seconds": args.timeout}
                    if any(event.get("stream_id") == stream and event.get("provider") == "claude"
                           and event.get("kind") == "USER" and event.get("text") == body
                           and int(event.get("daemon_seq", 0)) > watermark for event in replay["events"]):
                        break
                    assert now < deadline, "post-watermark USER ingest deadline exceeded"
                    time.sleep(min(0.1, deadline - now))
                matched = next(event for event in replay['events'] if event.get('stream_id') == stream
                    and event.get('provider') == 'claude' and event.get('kind') == 'USER'
                    and event.get('text') == body and int(event.get('daemon_seq', 0)) > watermark)
                source = _owned_source_records(host, matched['session_id'])
                record = next(r for r in source if r.get('uuid') == matched['raw']['jsonl_record_uuid'])
                evidence['wrapper_source_record'] = record
                evidence["wrapper_event"] = assert_wrapper_receipt(
                    receipt, replay["events"], stream_id=stream, body=body, watermark=watermark,
                    source_record=record,
                )
                inventory = rpc({"type": "list_sessions"}, "list_sessions")
                row = next(r for r in inventory["active"] if r["stream_id"] == stream)
                evidence["provider_binding"] = {key: row.get(key) for key in (
                    "host", "stream_id", "session_generation", "pane_pid", "observer_binding",
                )}
                assert row.get("pane_pid"), "provider live PID missing"
                wait_event(stream, tell_marker)
                if args.journeys:
                    notification_journey(host, stream, queued=(mode == 'promptless'),
                        rpc=capture_rpc, wait_event=wait_event, timeout=args.timeout,
                        evidence=evidence.setdefault('journey', {}),
                        foreground_job=getattr(args, 'foreground_jobs', {}).get(host))
                metrics["provider_wrapper"] = {"passed": True}
                return metrics

            def prepare(payload):
                payload['initial_prompt'] = initial_probe_prompt(
                    payload.get('initial_prompt', 'Reply exactly PENTACLE_PROBE_READY and await the declared steps.'),
                    foreground_job=getattr(args, 'foreground_jobs', {}).get(host))
                register_owned.prepare_owned_spawn(payload)

            evidence["cell"] = smoke.run_cell(
                host, "claude", mode, rpc=capture_rpc, wait_ready=wait_ready, wait_event=wait_event,
                verify_teardown=close, register_owned=register, close_owned=close,
                prepare_owned_spawn=prepare, validate_session=validate,
                rescue_teardown=lambda stream: smoke._rescue_teardown(
                    args.url, args.token_path, args.timeout, stream, registry),
            )
            evidence["outcome"] = "PASS"
    except Exception as exc:
        # run_cell preserves the predicate exception as the cause.
        cause = exc
        cleanup_failure = False
        while cause.__cause__ is not None:
            cleanup_failure |= isinstance(cause, OwnedImageCleanupError)
            cause = cause.__cause__
        evidence.update(outcome="FAIL", classification=(
            "CLEANUP_FAIL" if cleanup_failure or str(exc).startswith("teardown:") else
            "PRODUCT_FAIL" if isinstance(cause, AssertionError) else "HARNESS_ERROR"
        ), error=str(exc))
    finally:
        ownership = json.loads(registry_path.read_text()) if registry_path.exists() else {"owned": []}
        evidence["cleanup"] = {"closed_streams": closed, "remaining_owned": ownership["owned"],
                               "expected_count": len(registered),
                               "closed_count": len(registered) - len(ownership["owned"])}
        if ownership["owned"]:
            evidence.update(outcome="FAIL", classification="CLEANUP_FAIL")
        (output / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hosts", nargs="+", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--daemon-pid", type=int, required=True)
    parser.add_argument("--runtime-checkout", type=Path, required=True)
    parser.add_argument("--url", default=smoke.DEFAULT_URL)
    parser.add_argument("--token-path", type=Path, default=smoke.DEFAULT_TOKEN_PATH)
    parser.add_argument("--timeout", type=float, default=smoke.DEFAULT_TIMEOUT)
    parser.add_argument("--journeys", action="store_true",
                        help="Also run idle/queued notification and owned-image Read cells in the runtime window")
    parser.add_argument('--foreground-job', action='append', default=[], metavar='HOST=COMMAND',
                        help='Upfront-declared existing finite scratch pytest job for each queued host')
    args = parser.parse_args()
    args.foreground_jobs = {}
    for declaration in args.foreground_job:
        host, separator, command = declaration.partition('=')
        if not separator or host not in args.hosts or host in args.foreground_jobs:
            parser.error('foreground-job must uniquely declare a selected HOST=COMMAND')
        try:
            finite_job_path(command)
        except RuntimeError as exc:
            parser.error(str(exc))
        args.foreground_jobs[host] = command
    if args.journeys and set(args.foreground_jobs) != set(args.hosts):
        parser.error('journeys requires a finite foreground-job declaration for every selected host')
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    result = {"runtime": runtime_binding(args.runtime_checkout, args.candidate_sha, args.daemon_pid), "cells": []}
    for host in args.hosts:
        for mode in smoke.PROMPT_MODES:
            output = args.evidence_dir / f"{host}-{mode}"
            output.mkdir(exist_ok=True)
            cell = probe_cell(host, mode, args, output)
            result["cells"].append(cell)
            (args.evidence_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
            if cell["outcome"] != "PASS":
                return 1  # preserve first failure; no retry or further live mutations
    result["runtime_after"] = runtime_binding(args.runtime_checkout, args.candidate_sha, args.daemon_pid)
    (args.evidence_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
