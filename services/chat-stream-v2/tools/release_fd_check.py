#!/usr/bin/env python3
"""Observe the front desk's normal published reply to one release probe."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
import uuid

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from tools.spawn_fleet_smoke import (  # noqa: E402
    DEFAULT_TIMEOUT, DEFAULT_URL, DEFAULT_TOKEN_PATH, _operator_connection,
)


def run_check(url: str, token_path: Path, target_sha: str,
              timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Admission is insufficient: require a canonical answer to this input ID."""
    nonce = uuid.uuid4().hex
    message = f'[release-check {target_sha[:8]} {nonce}] Release check. Reply with exactly: PONG {nonce}'
    result = {'outcome': 'failed', 'reason': 'reply_not_observed'}
    deadline = time.monotonic() + timeout
    try:
        with _operator_connection(url, token_path, timeout) as (rpc, *_):
            # Resolve the operator chat, not the peer-only assistant alias or
            # its current physical backend, so ordinary operator intake runs.
            binding = rpc({'type': 'assistant.binding'}, 'assistant.binding', deadline=deadline)
            target = binding.get('composite_stream_id')
            if not isinstance(target, str) or not target:
                return {**result, 'reason': 'input_identity_missing'}
            sent = rpc({'type': 'send', 'to_stream_id': target, 'message': message,
                        'optimistic_id': nonce},
                       'send', deadline=deadline)
            accepted = sent.get('assistant_composite') or {}
            message_id = accepted.get('message_id')
            if not isinstance(message_id, str) or not message_id:
                return {**result, 'reason': 'input_identity_missing'}
            result['message_id'] = message_id
            while time.monotonic() < deadline:
                history = rpc({'type': 'request_stream_events', 'stream_id': target,
                               'limit': 10}, 'request_stream_events', deadline=deadline)
                for event in history.get('events', []):
                    raw = event.get('raw') or {}
                    if (event.get('stream_id') == target
                            and event.get('kind') == 'ASSIST_TEXT'
                            and raw.get('assistant_composite') is True
                            and event.get('message_id')
                            and event.get('reply_to_message_id') == message_id
                            and raw.get('reply_to_message_id') == message_id
                            and event.get('text') == f'PONG {nonce}'):
                        return {'outcome': 'passed', 'message_id': message_id,
                                'reply_message_id': event['message_id']}
                time.sleep(min(2.0, max(0, deadline - time.monotonic())))
    except Exception as exc:
        # Do not include arbitrary transport text, tokens or unrelated replies.
        result['reason'] = ('fd_execution_forbidden' if 'fd_execution_forbidden' in str(exc)
                            else 'reply_not_observed' if isinstance(exc, TimeoutError)
                            else 'operator_request_failed')
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=DEFAULT_URL)
    parser.add_argument('--token-path', type=Path, default=DEFAULT_TOKEN_PATH)
    parser.add_argument('--target-sha', required=True)
    args = parser.parse_args(argv)
    result = run_check(args.url, args.token_path, args.target_sha)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['outcome'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
