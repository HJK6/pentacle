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


# --- provenance (metadata only; never token admission) ----------------------
# Wire contract: docs/usage_accounting.md § Provenance. Every item has exactly
# PROVENANCE_ITEM_FIELDS; ``identity`` is null or exactly IDENTITY_FIELDS; and
# ``data`` is exactly PROVENANCE_DATA_FIELDS[kind].
PROVENANCE_ITEM_FIELDS = frozenset({
    'kind', 'provider', 'native_session_id', 'source_file_identity_digest', 'identity', 'data',
})
IDENTITY_FIELDS = frozenset({'account_id', 'account_source', 'conflict', 'cli_version'})
PROVENANCE_DATA_FIELDS = {
    'claude_record': frozenset({'record_key', 'observed_at', 'model'}),
    'codex_response': frozenset({
        'response_id', 'observed_at', 'model', 'input', 'cached_input',
        'cache_write_input', 'output', 'reasoning_output',
    }),
    'rate_limit': frozenset({
        'account_id', 'window_kind', 'window_minutes', 'pct', 'resets_at', 'observed_at',
    }),
}
PROVENANCE_KIND_PROVIDER = {'claude_record': 'claude', 'codex_response': 'codex', 'rate_limit': 'codex'}
_CODEX_RESPONSE_USAGE = (
    ('input', 'input_tokens'), ('cached_input', 'cached_input_tokens'),
    ('cache_write_input', 'cache_write_input_tokens'), ('output', 'output_tokens'),
    ('reasoning_output', 'reasoning_output_tokens'),
)


def _format_utc(moment, *, fraction_ms: int) -> str:
    stamp = moment.strftime('%Y-%m-%dT%H:%M:%S')
    return f'{stamp}.{fraction_ms:03d}Z' if fraction_ms else f'{stamp}Z'


def iso_from_ms(value: Any) -> str | None:
    """Exact UTC ISO-8601 (``Z``) for epoch milliseconds; None when not a positive int."""
    from datetime import datetime, timezone

    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    seconds, millis = divmod(value, 1000)
    try:
        moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return _format_utc(moment, fraction_ms=millis)


def iso_utc(value: Any) -> str | None:
    """Normalize epoch seconds or an offset-bearing ISO string to UTC ``Z`` form.

    Precision is truncated to milliseconds; a naive or unparseable value is None,
    so a receipt time is never substituted for an unknown observation time.
    """
    from datetime import datetime, timezone

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        try:
            moment = datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith(('Z', 'z')):
            text = text[:-1] + '+00:00'
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
        if moment.tzinfo is None:
            return None
        moment = moment.astimezone(timezone.utc)
    else:
        return None
    return _format_utc(moment, fraction_ms=moment.microsecond // 1000)


def _text(value: Any, limit: int = 256) -> str | None:
    return value if isinstance(value, str) and value.strip() and len(value) <= limit else None


def _identity(accounts: list[str], cli_version: str | None, *, complete: bool) -> dict[str, Any] | None:
    """Transcript identity: one id, two ids (sticky conflict), or none seen."""
    if len(accounts) > 1:
        return {'account_id': None, 'account_source': 'transcript', 'conflict': 1, 'cli_version': cli_version}
    if accounts:
        return {'account_id': accounts[0], 'account_source': 'transcript', 'conflict': 0, 'cli_version': cli_version}
    if complete:
        return {'account_id': None, 'account_source': 'unknown', 'conflict': 0, 'cli_version': cli_version}
    return None


def _claude_provenance(records: list[dict[str, Any]], *, digest: str | None, complete: bool) -> list[dict[str, Any]]:
    sessions: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        native = _text(record.get('sessionId') or record.get('session_id'))
        if native is None:
            continue
        session = sessions.setdefault(native, {'accounts': [], 'cli_version': None, 'records': {}})
        if session['cli_version'] is None:
            session['cli_version'] = _text(record.get('version'), 64)
        attachment = record.get('attachment')
        if record.get('type') == 'attachment' and isinstance(attachment, dict) and attachment.get('type') == 'credential_org':
            org = _text(attachment.get('organizationUuid'), 128)
            if org is not None and org not in session['accounts']:
                session['accounts'].append(org)
            continue
        message = record.get('message')
        if record.get('type') != 'assistant' or record.get('isSidechain') or not isinstance(message, dict) or 'usage' not in message:
            continue
        key = _text(message.get('id'))
        if key is None:
            continue
        usage = message.get('usage')
        output = usage.get('output_tokens') if isinstance(usage, dict) else None
        output = output if type(output) is int else -1
        previous = session['records'].get(key)
        # One API response is written once per content block; keep the copy
        # with the largest output count (the first on a tie).
        if previous is None or output > previous[0]:
            session['records'][key] = (output, iso_utc(record.get('timestamp')), _text(message.get('model'), 128))
    items = []
    for native, session in sessions.items():
        identity = _identity(session['accounts'], session['cli_version'], complete=complete)
        for key, (_output, observed_at, model) in session['records'].items():
            items.append({
                'kind': 'claude_record', 'provider': 'claude', 'native_session_id': native,
                'source_file_identity_digest': digest, 'identity': identity,
                'data': {'record_key': key, 'observed_at': observed_at, 'model': model},
            })
    return items


def _pct(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    pct = int(round(value))
    return pct if 0 <= pct <= 100 else None


def _codex_provenance(records: list[dict[str, Any]], *, native_session_id: str | None,
                      digest: str | None, complete: bool,
                      state: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Per-response detail and rate-limit windows from one rollout span.

    ``state`` carries a live tail's cross-span context (session identity, turn
    models); a complete transcript needs none.
    """
    state = state if state is not None else {}
    state.setdefault('turn_models', {})
    accounts: list[str] = list(state.get('accounts') or [])
    meta_seen = bool(state.get('meta_seen'))
    native = native_session_id or state.get('native_session_id')
    responses: list[tuple[str, dict[str, Any]]] = []
    seen_responses: set[str] = set()
    limits: list[dict[str, Any]] = []
    seen_limits: set[tuple] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        kind = record.get('type')
        payload = record.get('payload') if isinstance(record.get('payload'), dict) else {}
        if kind == 'session_meta':
            meta_id = _text(payload.get('id') or payload.get('session_id'))
            if native is None:
                native = meta_id
            if meta_id is None or meta_id != native:
                continue
            meta_seen = True
            if state.get('cli_version') is None:
                state['cli_version'] = _text(payload.get('cli_version'), 64)
            account = _text(payload.get('creator_account_id'), 128)
            if account is not None and account not in accounts:
                accounts.append(account)
        elif kind == 'turn_context':
            model = _text(payload.get('model'), 128)
            turn = _text(payload.get('turn_id'))
            if model is not None:
                state['last_model'] = model
                if turn is not None:
                    state['turn_models'][turn] = model
        elif kind == 'token_usage_record':
            response_id = _text(payload.get('response_id'))
            usage = payload.get('usage')
            if response_id is None or response_id in seen_responses or not isinstance(usage, dict):
                continue
            values = {target: usage.get(source) for target, source in _CODEX_RESPONSE_USAGE}
            if any(type(value) is not int or value < 0 for value in values.values()):
                continue
            turn = _text(payload.get('turn_id'))
            model = state['turn_models'].get(turn) if turn is not None else None
            seen_responses.add(response_id)
            responses.append((response_id, {
                'response_id': response_id, 'observed_at': iso_utc(record.get('timestamp')),
                'model': model or state.get('last_model'), **values,
            }))
        elif kind == 'event_msg' and payload.get('type') == 'token_count':
            rate_limits = payload.get('rate_limits')
            observed_at = iso_utc(record.get('timestamp'))
            if not isinstance(rate_limits, dict) or observed_at is None:
                continue
            window_kind = _text(rate_limits.get('limit_id'), 64) or 'codex'
            for position in ('primary', 'secondary'):
                window = rate_limits.get(position)
                if not isinstance(window, dict):
                    continue
                minutes = window.get('window_minutes')
                pct = _pct(window.get('used_percent'))
                resets_at = window.get('resets_at')
                if type(minutes) is not int or minutes <= 0 or pct is None or iso_utc(resets_at) is None:
                    continue
                limits.append({'window_kind': window_kind, 'window_minutes': minutes,
                               'pct': pct, 'resets_at': resets_at, 'observed_at': observed_at})
    state['accounts'] = accounts
    state['meta_seen'] = meta_seen
    if native is not None:
        state['native_session_id'] = native
    account_id = accounts[0] if len(accounts) == 1 else None
    if meta_seen:
        identity = _identity(accounts, state.get('cli_version'), complete=True)
    else:
        identity = _identity(accounts, state.get('cli_version'), complete=complete)
    items = []
    if native is not None:
        for _response_id, data in responses:
            items.append({
                'kind': 'codex_response', 'provider': 'codex', 'native_session_id': native,
                'source_file_identity_digest': digest, 'identity': identity, 'data': data,
            })
    for limit in limits:
        data = {'account_id': account_id, **limit}
        key = (data['account_id'], data['window_kind'], data['window_minutes'],
               iso_utc(data['resets_at']), data['pct'])
        if key in seen_limits:
            continue
        seen_limits.add(key)
        items.append({
            'kind': 'rate_limit', 'provider': 'codex', 'native_session_id': None,
            'source_file_identity_digest': None, 'identity': None, 'data': data,
        })
    return items


def native_provenance(provider: str, records: list[dict[str, Any]], *,
                      native_session_id: str | None = None,
                      source_file_identity_digest: str | None = None,
                      complete: bool = False,
                      state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Typed provenance items for one transcript span (wire schema version 1).

    ``complete`` means the span is the whole transcript, so a missing identity
    record is a definitive ``unknown`` rather than "not in this span".
    """
    if provider == 'claude':
        return _claude_provenance(records, digest=source_file_identity_digest, complete=complete)
    if provider == 'codex':
        return _codex_provenance(records, native_session_id=native_session_id,
                                 digest=source_file_identity_digest, complete=complete, state=state)
    return []
