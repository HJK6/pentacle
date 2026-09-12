"""Pure native-provider token observations; context estimates are not usage."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

FIELDS = {
    'codex': {'input_tokens': 'input_total', 'cached_input_tokens': 'cached_input',
              'output_tokens': 'output', 'reasoning_output_tokens': 'reasoning'},
    'claude': {'input_tokens': 'uncached_input', 'cache_read_input_tokens': 'cache_read',
               'cache_creation_input_tokens': 'cache_write', 'output_tokens': 'output'},
}


@dataclass(frozen=True)
class Observation:
    native_session_id: str
    record_key: str
    tokens: dict[str, int]


def native_usage(provider: str, record: dict[str, Any], native_session_id: str) -> tuple[Observation | None, str | None]:
    """Ignore unrelated records; reject an entire invalid usage vector."""
    if provider == 'codex':
        payload = record.get('payload')
        if record.get('type') != 'event_msg' or not isinstance(payload, dict) or payload.get('type') != 'token_count':
            return None, None
        info = payload.get('info')
        usage = info.get('total_token_usage') if isinstance(info, dict) else None
        key = 'cumulative'
        native = native_session_id
    elif provider == 'claude':
        message = record.get('message')
        if record.get('type') != 'assistant' or record.get('isSidechain') or not isinstance(message, dict) or 'usage' not in message:
            return None, None
        usage = message.get('usage')
        key = message.get('id')
        native = record.get('sessionId') or record.get('session_id')
    else:
        return None, None
    if not isinstance(native, str) or not native.strip() or not isinstance(key, str) or not key.strip():
        return None, 'missing_identity'
    if not isinstance(usage, dict):
        return None, 'invalid_usage'
    tokens = {}
    for source, target in FIELDS[provider].items():
        value = usage.get(source)
        if type(value) is not int or value < 0:
            return None, 'invalid_usage'
        tokens[target] = value
    if provider == 'codex' and (tokens['cached_input'] > tokens['input_total'] or tokens['reasoning'] > tokens['output']):
        return None, 'invalid_usage'
    return Observation(native, key, tokens), None
