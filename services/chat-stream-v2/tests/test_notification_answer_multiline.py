"""Producer → authenticated registry → ingest contract for multiline answers."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from ingest import append_ingested_event
from message_envelopes import build_message_envelope, match_message_envelope
from notify import _answer_back_text
from store import Store


FIXTURE_PATH = Path(__file__).resolve().parents[3] / 'pentacle-chat-core/tests/fixtures/notification-answer-multiline.json'


def _wire(**fields):
    return build_message_envelope('notification_answer', notice_id='notification-answer-fixture',
                                  body=_answer_back_text({'notification_id': 'notification-fixture', 'by': 'operator', **fields}))


@pytest.mark.parametrize('field', ['text', 'custom_text', 'selections'])
def test_real_producer_multiline_answer_registers(field):
    value = 'line one\nline two'
    wire = _wire(**{field: [value] if field == 'selections' else value})
    tag = match_message_envelope(wire)
    assert tag is not None, repr(wire)
    assert tag['answer' if field == 'selections' else field] == value


def test_sanitized_multiline_fixture_is_producer_bound():
    fixture = json.loads(FIXTURE_PATH.read_text())
    assert _wire(custom_text=fixture['answer']) == fixture['wire_text']
    assert hashlib.sha256(fixture['wire_text'].encode()).hexdigest() == fixture['wire_sha256']
    assert match_message_envelope(fixture['wire_text']) == {
        key: value for key, value in fixture['message_envelope'].items() if key != 'schema_version'}


def test_real_multiline_producer_answer_survives_authenticated_ingest():
    async def run():
        store = Store(':memory:')
        store.start()
        broadcasts = []
        async def broadcast(frame):
            broadcasts.append(frame)
        try:
            await store.open_session('host', 'child', provider='codex')
            wire = _wire(custom_text='line one\nline two')
            event = {'host': 'host', 'provider': 'codex', 'session_id': 'host:child',
                     'session_name': 'child', 'stream_id': 'host:child', 'kind': 'USER',
                     'timestamp': '2026-09-20T00:00:00Z', 'text': wire,
                     'raw': {'source': 'structured', 'jsonl_record_uuid': 'multiline-fixture'}}
            assert await append_ingested_event(store, broadcast, event, recent_limit=500) is not None
            saved = broadcasts[-1]['event']
            assert saved['message_envelope']['custom_text'] == 'line one\nline two'
            assert saved['raw']['envelope_source'] == wire
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('value', [
    '\nline one\nline two\n', 'line one\r\nline two',
    'line one\n\nline three', 'line one\nby=somebody\ntext=still the answer',
    'line one\\n literal\n"quoted" \\path 🙂',
])
def test_multiline_answer_preserves_whitespace_and_field_like_content(value):
    tag = match_message_envelope(_wire(custom_text=value))
    assert tag is not None
    assert tag['custom_text'] == value
    assert tag['by'] == 'operator'


def test_legacy_one_line_bytes_and_literal_backslash_are_unchanged():
    frozen = json.loads((FIXTURE_PATH.parent / 'message-envelopes.json').read_text())
    case = next(case for case in frozen['cases'] if case['key'] == 'notification-answer')
    assert _wire(selections=['Done'], choice=True) == case['wire_text']
    assert hashlib.sha256(case['wire_text'].encode()).hexdigest() == '50748652cb4e6a42bab21c7dc2085d335ff5fa037515e0e9eb3e04bd5d949bca'
    wire = _wire(custom_text=r'literal\ntext')
    assert 'custom_text=literal\\ntext' in wire
    assert 'custom_text_json=' not in wire
    assert match_message_envelope(wire) is not None


@pytest.mark.parametrize('fields', [
    'custom_text_json="line one\\qline two"\nby=operator',
    'custom_text_json="unterminated\nby=operator',
    'custom_text_json=["line one", "line two"]\nby=operator',
    'custom_text_json=null\nby=operator',
    'custom_text_json=12\nby=operator',
    'custom_text_json=' + '[' * 2000 + '\nby=operator',
    'custom_text_json=""\nby=operator',
    'custom_text_json="ok" trailing\nby=operator',
    'custom_text_json="ok"\ncustom_text=other\nby=operator',
    'custom_text=other\ncustom_text_json="ok"\nby=operator',
    'custom_text_json="ok"\ncustom_text_json="again"\nby=operator',
    'custom_text_json="ok"\nby=operator\nby=other',
    'by=operator\ncustom_text_json="ok"',
    'custom_text_json="ok"\nby=',
    'custom_text_json="ok"',
    'custom_text=line one\nline two\nby=operator',
    'choice_json="True"\nby=operator',
    'custom_text_json="ok"\nby_json="operator"',
    'unknown_json="ok"\nby=operator',
    'custom_text_json="ok"\nby=operator\nextra=tail',
])
def test_malformed_and_ambiguous_fields_fail_closed(fields):
    wire = ('[pentacle-notice:notification-answer-fixture]\n[notification.answer]\n'
            'notification_id=notification-fixture\n' + fields)
    assert match_message_envelope(wire) is None


def test_multiline_encoding_does_not_register_other_kinds_or_mismatched_ids():
    wire = _wire(custom_text='line one\nline two')
    for malformed in (wire.replace('notification_id=notification-fixture', 'notification_id=other'),
                      wire.replace('[notification.answer]', '[child_report_ready]'),
                      wire.replace('notification-answer-fixture]', 'notice-fixture]'),
                      'preamble\n' + wire):
        assert match_message_envelope(malformed) is None


def test_truncated_multiline_answer_stays_unregistered():
    from notify import ANSWER_BACK_MAX_CHARS
    body = _answer_back_text({'notification_id': 'notification-fixture',
                             'custom_text': 'line\n' * ANSWER_BACK_MAX_CHARS, 'by': 'operator'})
    assert len(body) == ANSWER_BACK_MAX_CHARS
    assert body.endswith('\n[answer truncated]')
    assert match_message_envelope(build_message_envelope('notification_answer',
        notice_id='notification-answer-fixture', body=body)) is None


def test_multiline_tag_survives_authenticated_provider_wrapper():
    from message_envelopes import annotate_message_envelope
    wire = _wire(custom_text='line one\nline two')
    wrapper = {'kind': 'claude_pasted_content', 'id': '8d21', 'provenance': 'grammar'}
    source = f'<pasted_content id="8d21">\n{wire}\n</pasted_content id="8d21">'
    event = annotate_message_envelope({'kind': 'USER', 'provider': 'claude',
        'text': wire, 'provider_wrapper': wrapper, 'raw': {'provider_content': source}})
    assert event['message_envelope']['custom_text'] == 'line one\nline two'
    assert event['message_envelope']['schema_version'] == 1
    assert event['provider_wrapper'] == wrapper
    assert event['raw']['envelope_source'] == source


@pytest.mark.parametrize('padding', [708, 712])
def test_encoded_answer_cutoff_inside_actor_fails_closed(padding):
    wire = _wire(custom_text='a' * padding + '\nx')
    assert wire.endswith('\n[answer truncated]')
    assert match_message_envelope(wire) is None, repr(wire[-35:])
    assert match_message_envelope(wire.strip()) is None, 'provider whitespace trimming stays fail-closed'
