#!/usr/bin/env python3
"""Check installed note delivery on an owned, authenticated TEST composite."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

from websockets.sync.client import connect

SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SERVICE_DIR), str(SERVICE_DIR.parent)]

from tools.disposable_smoke import disposable_composite
from tools.proactive_note_probe import connect_client, history, note, prepare

LIVE_WINDOW_SECONDS = 3
ASSERTIONS = ('subscribe', 'publish', 'receive', 'reconnect', 'recover', 'merge')


def new_receipt(target_sha):
    return {
        'outcome': 'failed',
        'classification': 'infrastructure',
        'phase': 'precondition',
        'reason': 'probe_not_started',
        'release_sha': target_sha,
        'assertions': dict.fromkeys(ASSERTIONS, 'not_run'),
        'teardown': 'not_run',
    }


def matching_events(events, payload):
    message_id = 'publication:' + payload['request_id']
    return [row for row in events if row.get('message_id') == message_id
            or row.get('text') == payload['message']]


def exact_event(events, payload, cursor, publication_seq):
    return (len(events) == 1
            and events[0].get('stream_id') == payload['composite_stream_id']
            and events[0].get('message_id') == 'publication:' + payload['request_id']
            and events[0].get('text') == payload['message']
            and events[0].get('note_type') == payload['note_type']
            and type(events[0].get('daemon_seq')) is int
            and events[0]['daemon_seq'] == publication_seq
            and publication_seq > cursor)


async def live_events(socket):
    """Observe the whole bounded receive window, including repeated frames."""
    events = []
    try:
        async with asyncio.timeout(LIVE_WINDOW_SECONDS):
            while True:
                frame = json.loads(await socket.recv())
                if frame.get('type') == 'chat.event':
                    events.append(frame['event'])
    except TimeoutError:
        return events


async def exercise(harness, result, correlation):
    target = harness['target']
    await prepare(harness)
    async with AsyncExitStack() as stack:
        socket = await connect_client(stack, harness, 'pentacle')
        initial = await history(socket, target, 'cursor-' + correlation)
        anchor = max(initial, key=lambda row: row['daemon_seq'])
        cursor = anchor['daemon_seq']
        if anchor.get('stream_id') != target or type(cursor) is not int or cursor < 1:
            raise ValueError('invalid history cursor')
        result['saved_cursor'] = cursor
        result['assertions']['subscribe'] = 'passed'

        payload = note(harness, correlation)
        result['phase'] = 'publish'
        ack = await harness['rpc'](payload)
        publication_seq = ack.get('event_id')
        if (ack.get('type') != 'assistant.publish.ok' or ack.get('publication_key') != correlation
                or ack.get('duplicate') is not False or type(publication_seq) is not int
                or publication_seq <= cursor):
            result['reason'] = 'invalid_publication_receipt'
            return
        result.update(publication_seq=publication_seq, message_id='publication:' + correlation)
        result['assertions']['publish'] = 'passed'
        result['phase'] = 'receive'
        live = matching_events(await live_events(socket), payload)
        result['live_count'] = len(live)
        if exact_event(live, payload, cursor, publication_seq):
            result['assertions']['receive'] = 'passed'

    result['phase'] = 'reconnect'
    async with AsyncExitStack() as stack:
        socket = await connect_client(stack, harness, 'pentacle')
        result['assertions']['reconnect'] = 'passed'
        result['phase'] = 'recover'
        recent = await history(socket, target, 'recovery-' + correlation)
        # The client protocol refetches the latest window on reconnect. Use the
        # saved high-water mark locally and require the same anchor in the window.
        replay = matching_events(recent, payload)
        result['recovered_count'] = len(replay)
        if anchor in recent and exact_event(replay, payload, cursor, publication_seq):
            result['assertions']['recover'] = 'passed'

    if result['assertions']['receive'] != 'passed':
        result.update(phase='receive', reason='missing_live_event' if not live else
                      'duplicate_live_event' if len(live) > 1 else 'wrong_live_event')
        return
    if result['assertions']['recover'] != 'passed':
        result.update(phase='recover', reason='missing_recovery_event' if not replay else
                      'duplicate_recovery_event' if len(replay) > 1 else 'wrong_recovery_event_or_anchor')
        return

    result['phase'] = 'merge'
    # Desktop merges the history refetch into its saved live view by daemon_seq.
    merged = {row['daemon_seq']: row for row in live + replay}
    result['merged_count'] = len(merged)
    if len(merged) != 1 or live != replay:
        result['reason'] = 'merged_event_mismatch'
        return
    result['assertions']['merge'] = 'passed'
    result.update(outcome='passed', classification='passed', phase='complete', reason='')


async def run_probe(target_sha):
    result = new_receipt(target_sha)
    correlation = uuid.uuid4().hex
    result.update(correlation_id=correlation, live_window_seconds=LIVE_WINDOW_SECONDS)
    started = time.monotonic()
    harness = None
    try:
        async with disposable_composite(operator_composite='operator-excluded:assistant') as harness:
            result.update(test_stream=harness['target'], teardown='pending')
            try:
                await exercise(harness, result, correlation)
            except Exception as exc:
                # Protocol exceptions may carry frames. Record only their types.
                result['reason'] = type(exc).__name__
    except Exception as exc:
        result['reason'] = type(exc).__name__
        if harness is not None:
            result.update(phase='teardown', teardown='failed', outcome='failed')
    finally:
        if result['teardown'] == 'pending':
            cleaned = not harness['state_path'].exists() and harness['server']._ws_server is None
            result['teardown'] = 'passed' if cleaned else 'failed'
            if not cleaned:
                result.update(phase='teardown', reason='fixture_still_present', outcome='failed')
        result['duration_ms'] = round((time.monotonic() - started) * 1000)
    if result['outcome'] == 'failed':
        if result['phase'] in ASSERTIONS:
            result['assertions'][result['phase']] = 'failed'
        result['classification'] = ('delivery_failure' if result['phase'] in {'publish', 'receive', 'recover', 'merge'}
                                    else 'infrastructure')
    return result


def installed_check(target_sha, active_pid):
    result = new_receipt(target_sha)
    if not active_pid:
        result['reason'] = 'active_pid_missing'
        return result
    try:
        source_sha = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=SERVICE_DIR, text=True, timeout=3).strip()
        dirty = subprocess.check_output(
            ['git', 'status', '--porcelain', '--untracked-files=all', '--',
             'services/chat-stream-v2', 'services/_shared'],
            cwd=SERVICE_DIR.parents[1], text=True, timeout=3)
        if dirty:
            result.update(reason='installed_source_dirty', source_sha=source_sha)
            return result
        with connect('ws://127.0.0.1:7791', open_timeout=2, close_timeout=1) as socket:
            welcome = json.loads(socket.recv(timeout=2))
    except Exception as exc:
        result['reason'] = type(exc).__name__
        return result
    observed_sha = welcome.get('runtime_sha') if welcome.get('type') == 'welcome' else None
    identity = {'sha': observed_sha, 'pid': active_pid}
    if source_sha != target_sha or observed_sha != target_sha:
        result.update(reason='installed_identity_mismatch', source_sha=source_sha, active_instance=identity)
        return result
    return {**asyncio.run(run_probe(target_sha)), 'source_sha': source_sha, 'active_instance': identity}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-sha', required=True)
    parser.add_argument('--active-pid', type=int, required=True)
    args = parser.parse_args(argv)
    result = installed_check(args.target_sha, args.active_pid)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['outcome'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
