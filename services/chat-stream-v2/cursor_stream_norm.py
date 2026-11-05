"""Cursor stream-json to one stable event per source line.

State spans chunks, but is committed by ingest only when its byte offset advances.
Deltas remain pane-only. Duplicate finals are compared to the last model flush.
"""
from __future__ import annotations
import json
from claude_jsonl_norm import _stamp_jsonl_event_identity


def _text(record):
    message = record.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    return ''.join(str(part.get('text', '')) for part in content
                   if isinstance(part, dict) and part.get('type') == 'text') if isinstance(content, list) else ''


def normalize_cursor_stream_records(records, *, host, session_name, session_id, state=None):
    state = {} if state is None else state
    events = []
    for line_number, record in records:
        if not isinstance(record, dict) or state.get('halted'):
            continue
        kind, text, extra = None, '', {}
        typ = record.get('type')
        if typ == 'user':
            state['last_flush'] = None
            text, kind = _text(record), 'USER'
        elif typ == 'assistant':
            text = _text(record)
            if record.get('model_call_id'):
                state['last_flush'] = text
                kind = 'ASSIST_TEXT'
            elif 'timestamp_ms' not in record and text != state.get('last_flush'):
                kind = 'ASSIST_TEXT'
        elif typ == 'tool_call':
            calls = record.get('tool_call')
            calls = calls if isinstance(calls, dict) else {}
            call = next(((k, v) for k, v in calls.items() if k.endswith('ToolCall') and isinstance(v, dict)), None)
            if call:
                key, value = call
                name = {'readToolCall':'read', 'shellToolCall':'shell', 'writeToolCall':'write'}.get(key, key)
                call_id = record.get('call_id')
                if record.get('subtype') == 'started':
                    args = value.get('args')
                    args = args if isinstance(args, dict) else {}
                    allowed = {k:str(v)[:400] for k,v in args.items() if k in {'path','command','workingDirectory'}}
                    # Bound the whole display/input payload, including multiple paths.
                    text = f'{name}({json.dumps(allowed, ensure_ascii=False)[:400]})'
                    kind = 'TOOL_USE'
                    extra = {'tool_name':name, 'tool_use_id':call_id, 'tool_input':allowed}
                elif record.get('subtype') == 'completed':
                    result = value.get('result')
                    result = result if isinstance(result, dict) else {}
                    success = result.get('success')
                    if isinstance(success, dict):
                        content = success.get('content', success.get('output', success.get('stdout', '')))
                        text = '\n'.join(str(content).splitlines()[:20])[:4000]
                        if not text:
                            text = json.dumps({k:v for k,v in success.items() if k in {'totalLines','fileSize','exitCode','linesAdded','linesRemoved'}}, ensure_ascii=False)[:400]
                        text = text or 'completed'
                    else:
                        text = 'tool rejected' if result.get('rejected') or 'rejected' in str(result).lower() else 'tool failed'
                    kind = 'TOOL_RESULT'
                    extra = {'tool_use_id':call_id, 'tool_content':text}
        elif typ == 'result':
            # Usage remains raw, with no accounting registration in v1.
            if record.get('is_error') or record.get('subtype') != 'success':
                kind, text = 'SYSTEM', 'Cursor turn failed'
                extra = {'subtype':'api-error', 'usage':record.get('usage')}
        elif typ == 'pentacle_repl':
            event = record.get('event')
            if event in {'interrupted','identity_mismatch','turn_failed','exit'}:
                text = {'interrupted':'Cursor turn interrupted', 'identity_mismatch':'Cursor chat identity mismatch',
                        'turn_failed':'Cursor model unavailable on this plan' if record.get('stderr_class') == 'ActionRequiredError' else 'Cursor turn failed',
                        'exit':'Cursor seat exited'}[event]
                kind = 'SYSTEM'
                extra = {'subtype':event}
                if event == 'identity_mismatch':
                    state['halted'] = True
        if not kind or not text:
            continue
        payload = {'host':host, 'provider':'cursor', 'session_name':session_name,
                   'session_id':session_id, 'stream_id':f'{host}:{session_name}',
                   'timestamp':'', 'kind':kind, 'text':text,
                   'raw':{'source':'structured', 'transport':'cursor-stream-json', 'provider':'cursor',
                          'host':host, 'session_name':session_name, 'source_session_identity':session_id,
                          **extra}}
        events.extend(_stamp_jsonl_event_identity([payload], f'cursor:{session_id}:{line_number}'))
    return events
