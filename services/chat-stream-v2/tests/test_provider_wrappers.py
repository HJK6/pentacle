"""Captured provider paste replay through ingest, landing proof and receipt projection."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest

from claude_jsonl_norm import normalize_claude_jsonl_records
from comms import Comms
from ingest import append_ingested_event
from store import Store
from submission_events import provider_text_digest, submission_text_matches
from submission_events import normalize_submission_text
from provider_wrappers import normalize_provider_user_text
from tools.provider_wrapper_probe import assert_wrapper_receipt

FIXTURE = json.loads((Path(__file__).resolve().parents[3] / 'pentacle-chat-core/tests/fixtures/provider-wrapper.json').read_text())
HOST, NAME = 'hosta', 'wrapper-target'
STREAM = f'{HOST}:{NAME}'


def test_shared_normalization_and_wrapper_contract():
    assert FIXTURE['schema_version'] == 1
    raw = FIXTURE['record']['message']['content']
    assert normalize_provider_user_text(raw, provider='claude', authenticated=True) == (
        FIXTURE['display_text'], FIXTURE['wrapper'])
    assert normalize_provider_user_text(raw, provider='claude') == (raw, None)
    assert normalize_provider_user_text(raw, provider='codex', authenticated=True) == (raw, None)
    for case in FIXTURE['normalization_cases']:
        assert normalize_submission_text(case['text']) == case['normalized']


def test_wrapper_identifier_is_preserved_and_extra_final_newline_rejected():
    raw = FIXTURE['record']['message']['content'].replace('8769', '0008769')
    assert event_for(raw)['provider_wrapper']['id'] == '0008769'
    assert event_for(raw + '\n')['text'] == raw + '\n'


@pytest.mark.parametrize('capture', FIXTURE['additional_captures'])
@pytest.mark.parametrize('blocks', [False, True])
def test_live_failure_capture_proves_submission_and_probe(capture, blocks):
    raw = capture['record']['message']['content']
    assert hashlib.sha256(raw.encode()).hexdigest() == capture['captured_content_sha256']
    record = copy.deepcopy(capture['record'])
    if blocks:
        record['message']['content'] = [{'type': 'text', 'text': raw}]
    event = normalize_claude_jsonl_records([record], host=HOST, session_name=NAME)[0]
    assert event['text'] == capture['display_text']
    assert event['provider_wrapper'] == capture['wrapper']
    assert event['raw']['provider_content'] == raw
    assert submission_text_matches(event, capture['display_text'])
    assert Comms._tail_event_matches_submission(event, STREAM, capture['display_text'])
    assert_wrapper_receipt({'submission_confirmed': True}, [{**event, 'daemon_seq': 10}],
                           stream_id=STREAM, body=capture['display_text'], watermark=9)


@pytest.mark.parametrize('identifier', FIXTURE['positive_ids'])
def test_lowercase_hex_identifier_preserves_length_and_leading_zeroes(identifier):
    raw = FIXTURE['record']['message']['content'].replace('8769', identifier)
    event = event_for(raw)
    assert event['text'] == FIXTURE['display_text']
    assert event['provider_wrapper']['id'] == identifier


def test_tool_result_and_assistant_content_are_not_unwrapped():
    raw = FIXTURE['record']['message']['content']
    record = {**FIXTURE['record'], 'message': {'content': [
        {'type': 'tool_result', 'tool_use_id': 'tool-one', 'content': raw}]}}
    result = normalize_claude_jsonl_records([record], host=HOST, session_name=NAME)[0]
    assert result['kind'] == 'TOOL_RESULT' and result['text'] == raw
    assert 'provider_wrapper' not in result
    record.update(type='assistant', message={'content': [{'type': 'text', 'text': raw}]})
    result = normalize_claude_jsonl_records([record], host=HOST, session_name=NAME)[0]
    assert result['kind'] == 'ASSIST_TEXT' and result['text'] == raw
    assert 'provider_wrapper' not in result


def event_for(text=None, *, blocks=False):
    record = copy.deepcopy(FIXTURE['record'])
    if text is None:
        text = record['message']['content']
    record['message']['content'] = [{'type': 'text', 'text': text}] if blocks else text
    return normalize_claude_jsonl_records([record], host=HOST, session_name=NAME)[0]


@pytest.mark.parametrize('blocks', [False, True])
def test_captured_wrapper_ingest_preserves_raw_and_displays_body(blocks):
    event = event_for(blocks=blocks)
    assert event['kind'] == 'USER'
    assert event['text'] == FIXTURE['display_text']
    assert event['provider_wrapper'] == FIXTURE['wrapper']
    assert event['raw']['provider_content'] == FIXTURE['record']['message']['content']
    assert event['raw']['jsonl_record_uuid'] == FIXTURE['record']['uuid']


def test_captured_wrapper_proves_submission():
    assert submission_text_matches(event_for(), FIXTURE['display_text'])


def test_captured_wrapper_proves_comms_tail():
    assert Comms._tail_event_matches_submission(event_for(), STREAM, FIXTURE['display_text'])


@pytest.mark.parametrize('text', FIXTURE['negative_texts'])
def test_similar_operator_text_is_untouched(text):
    event = event_for(text)
    assert event['text'] == text
    assert 'provider_wrapper' not in event
    assert 'provider_content' not in event['raw']


def wrapped(body):
    return f'\n\n<pasted_content id="8769">\n{body}\n</pasted_content id="8769">\n'


def test_only_outer_layer_removed():
    literal = wrapped('literal example')
    assert event_for(wrapped(literal))['text'] == literal


def test_wrapped_synthetic_text_keeps_its_existing_classification():
    event = event_for(wrapped('<system-reminder>fixture</system-reminder>'))
    assert event['kind'] == 'SYSTEM'
    assert event['raw']['subtype'] == 'synthetic-user'


def test_wrapper_telemetry_contains_tags_not_operator_text(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger='chat_streamd_v2.provider_wrappers'):
        event_for()
    assert caplog.records[-1].subsystem == 'provider_wrapper'
    assert caplog.records[-1].bug_ref == 'spec_pentacle__claude_pasted_content_envelope_2026_09'
    assert FIXTURE['display_text'] not in caplog.text


@pytest.mark.parametrize('anchor', ['tell', 'send'])
def test_wrapped_peer_delivery_keeps_provenance_and_landing(anchor):
    body = f'[from hostb:peer] [{anchor}:id-9]\nplease review'
    event = event_for(wrapped(body))
    assert event['kind'] == 'TELL'
    assert event['text'] == 'please review'
    assert event['raw']['tell_id'] == 'id-9'
    assert event['raw']['provider_content'] == wrapped(body)
    assert Comms._tail_event_matches_submission(event, STREAM, body)


def test_digest_branch_still_proves_provider_content_not_caption():
    event = {**event_for(), 'text': 'caption', 'provider_text_digest': provider_text_digest('original')}
    assert submission_text_matches(event, 'original')
    assert not submission_text_matches(event, 'caption')
    assert not submission_text_matches({**event, 'provider_text_digest': 'bad'}, 'original')


def test_live_probe_requires_receipt_post_watermark_user_tag_and_raw():
    event = {**event_for(), 'daemon_seq': 10}
    args = dict(stream_id=STREAM, body=FIXTURE['display_text'], watermark=9)
    assert assert_wrapper_receipt({'submission_confirmed': True}, [event], **args) == event
    with pytest.raises(AssertionError, match='not confirmed'):
        assert_wrapper_receipt({'submission_confirmed': False}, [event], **args)
    for invalid in [
        {**event, 'daemon_seq': 9}, {**event, 'kind': 'ASSIST_TEXT'},
        {**event, 'stream_id': 'hostb:other'}, {**event, 'text': 'different'},
        {**event, 'provider_wrapper': None}, {**event, 'raw': {}},
        {**event, 'provider_wrapper': {**FIXTURE['wrapper'], 'id': 'wrong'}},
        {**event, 'provider_wrapper': {**FIXTURE['wrapper'], 'id': '9999'}},
    ]:
        with pytest.raises(AssertionError):
            assert_wrapper_receipt({'submission_confirmed': True}, [invalid], **args)


def test_wrapped_user_receipt_projects_and_persists_display_text():
    async def go():
        store = Store(':memory:')
        store.start()
        broadcasts = []
        async def broadcast(frame):
            broadcasts.append(frame)
        try:
            await store.open_session(HOST, NAME, provider='claude')
            await store.append_send_receipt(to_stream_id=STREAM, request_id='send-wrapper', receipt_id='receipt-wrapper',
                state='accepted', optimistic_id='optimistic-wrapper', wire_text=FIXTURE['display_text'],
                display_text=FIXTURE['display_text'], attachments=[], delivery='accepted', submission_confirmed=False)
            seq = await append_ingested_event(store, broadcast, event_for(), recent_limit=500)
            assert seq is not None
            persisted = (await store.fetch_session_event_tail(STREAM, limit=500))[0]
            assert persisted['text'] == FIXTURE['display_text']
            assert persisted['provider_wrapper'] == FIXTURE['wrapper']
            assert persisted['optimistic_id'] == 'optimistic-wrapper'
            assert broadcasts[0]['event']['text'] == FIXTURE['display_text']
            assert persisted['raw']['provider_content'] == FIXTURE['record']['message']['content']
            assert submission_text_matches(persisted, FIXTURE['display_text'])
        finally:
            store.stop()
    asyncio.run(go())
