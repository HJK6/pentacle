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
from tools.provider_wrapper_probe import assert_notification_journey

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


@pytest.mark.parametrize('compact', [False, True])
def test_tool_result_and_assistant_content_are_not_unwrapped(compact):
    raw = FIXTURE['record']['message']['content']
    if compact:
        raw = raw[2:-1]
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
@pytest.mark.parametrize('compact', [False, True])
def test_wrapped_peer_delivery_keeps_provenance_and_landing(anchor, compact):
    body = f'[from hostb:peer] [{anchor}:id-9]\nplease review'
    raw = wrapped(body)[2:-1] if compact else wrapped(body)
    event = event_for(raw)
    assert event['kind'] == 'TELL'
    assert event['text'] == 'please review'
    assert event['raw']['tell_id'] == 'id-9'
    assert event['raw']['provider_content'] == raw
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


def ingress_record(text, route):
    record = copy.deepcopy(FIXTURE['queued_capture']['record'])
    if route == 'queued':
        record['attachment']['prompt'] = text
    else:
        record['type'] = 'user'
        record.pop('attachment')
        record['message'] = {'content': ([{'type': 'text', 'text': part}
            for part in (text[:text.index('>\n') + 1], text[text.index('>\n') + 2:])] if route == 'joined' else text)}
    return record


@pytest.mark.parametrize('case', FIXTURE['grammar_cases'])
def test_shared_finite_grammar(case):
    assert normalize_provider_user_text(case['text'], provider='claude', authenticated=True) == (
        case['display_text'], {'kind': 'claude_pasted_content', 'id': case['id'], 'provenance': 'grammar'})
    assert normalize_provider_user_text(case['text'], provider='claude') == (case['text'], None)
    assert normalize_provider_user_text(case['text'], provider='codex', authenticated=True) == (case['text'], None)


@pytest.mark.parametrize('route', ['ordinary', 'joined', 'queued'])
@pytest.mark.parametrize('padded', [False, True])
def test_sanitized_queued_notice_contract(route, padded):
    capture = FIXTURE['queued_capture']
    raw = capture['record']['attachment']['prompt']
    assert hashlib.sha256(raw.encode()).hexdigest() == capture['content_sha256']
    if padded:
        raw = '\n\n' + raw + '\n'
    event = normalize_claude_jsonl_records([ingress_record(raw, route)], host=HOST, session_name=NAME)[0]
    assert event['kind'] == 'USER'
    assert event['text'] == capture['display_text']
    assert event['provider_wrapper'] == capture['wrapper']
    assert event['raw']['provider_content'] == raw
    assert event['raw']['jsonl_record_uuid'] == 'queued-fixture'
    assert event['raw']['jsonl_event_index'] == 0
    if route == 'queued':
        assert event['raw']['subtype'] == 'queued-command'
        assert event['raw']['queued_at'] == capture['record']['timestamp']
    assert submission_text_matches(event, capture['display_text'])
    assert Comms._tail_event_matches_submission(event, STREAM, capture['display_text'])


@pytest.mark.parametrize('body', ['[from hostb:peer] [tell:id-9]\nplease review',
    '[from hostb:peer] [send:id-9]\nplease review', '<system-reminder>fixture</system-reminder>'])
@pytest.mark.parametrize('padded', [False, True])
def test_queued_peer_and_synthetic_keep_user_identity(body, padded):
    raw = wrapped(body) if padded else wrapped(body)[2:-1]
    event = normalize_claude_jsonl_records([ingress_record(raw, 'queued')], host=HOST, session_name=NAME)[0]
    assert event['kind'] == 'USER'
    assert event['text'] == body
    assert event['raw']['subtype'] == 'queued-command'
    assert event['raw']['jsonl_event_index'] == 0
    assert event['raw']['provider_content'] == raw


def test_nonhuman_queued_attachment_is_unchanged():
    record = copy.deepcopy(FIXTURE['queued_capture']['record'])
    record['attachment']['origin']['kind'] = 'agent'
    assert normalize_claude_jsonl_records([record], host=HOST, session_name=NAME) == []


def test_only_exact_boolean_image_metadata_is_suppressed(caplog):
    import logging
    record = copy.deepcopy(FIXTURE['image_meta_capture'])
    with caplog.at_level(logging.INFO, logger='chat_streamd_v2.provider_wrappers'):
        assert normalize_claude_jsonl_records([record], host=HOST, session_name=NAME) == []
    assert any(r.subsystem == 'provider_wrapper' and
        r.bug_ref == 'spec_pentacle__claude_queued_notice_and_image_meta_2026_09' for r in caplog.records)
    assert record['message']['content'] not in caplog.text
    for key in ('isMeta', 'turnCompanion'):
        for value in (False, None, 1, 'true'):
            control = copy.deepcopy(record)
            if value is None:
                control.pop(key)
            else:
                control[key] = value
            assert normalize_claude_jsonl_records([control], host=HOST, session_name=NAME)[0]['text'] == record['message']['content']
    for text in (record['message']['content'] + '\n', 'prefix ' + record['message']['content'],
                 record['message']['content'].replace('1.31', '.31'), '[Image: unrelated metadata]'):
        control = {**record, 'message': {'content': text}}
        assert normalize_claude_jsonl_records([control], host=HOST, session_name=NAME)[0]['text'] == text
    for scale in ('1', '2.50'):
        control = {**record, 'message': {'content': f'[Image: original 42x80, displayed at 21x40. Multiply coordinates by {scale} to map to original image.]'}}
        assert normalize_claude_jsonl_records([control], host=HOST, session_name=NAME) == []
    tool = {**record, 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'read-fixture',
        'content': [{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'fixture'}}]}]}}
    assert normalize_claude_jsonl_records([tool], host=HOST, session_name=NAME)[0]['kind'] == 'TOOL_RESULT'
    operator = {**record, 'isMeta': False, 'turnCompanion': False,
        'message': {'content': [{'type': 'text', 'text': 'real operator image'},
            {'type': 'image', 'source': {'type': 'base64', 'data': 'fixture'}}]}}
    assert normalize_claude_jsonl_records([operator], host=HOST, session_name=NAME)[0]['text'] == 'real operator image'


def test_queued_wrapper_telemetry_omits_body(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger='chat_streamd_v2.provider_wrappers'):
        normalize_claude_jsonl_records([FIXTURE['queued_capture']['record']], host=HOST, session_name=NAME)
    assert any(r.bug_ref == 'spec_pentacle__claude_queued_notice_and_image_meta_2026_09'
        and 'queued' in r.getMessage() for r in caplog.records)
    assert FIXTURE['queued_capture']['display_text'] not in caplog.text


def test_journey_probe_requires_durable_proof_and_actual_queued_route():
    from message_envelopes import annotate_message_envelope
    capture = FIXTURE['queued_capture']
    event = annotate_message_envelope(normalize_claude_jsonl_records(
        [capture['record']], host=HOST, session_name=NAME)[0])
    event['daemon_seq'] = 10
    notification = {'notification_id': 'notification-fixture',
                    'resolution': {'by': 'operator', 'delivery_status': 'delivered'}}
    args = dict(stream=STREAM, watermark=9, queued=True)
    assert assert_notification_journey([event], notification, **args) == event
    for changed in [
        {**event, 'daemon_seq': 9}, {**event, 'kind': 'TELL'},
        {**event, 'message_envelope': {**event['message_envelope'], 'kind': 'claude_pasted_content'}},
        {**event, 'raw': {**event['raw'], 'subtype': None}},
    ]:
        with pytest.raises(AssertionError):
            assert_notification_journey([changed], notification, **args)
    with pytest.raises(AssertionError):
        assert_notification_journey([event, event], notification, **args)
    with pytest.raises(AssertionError, match='durable'):
        assert_notification_journey([event], {**notification,
            'resolution': {'by': 'operator', 'delivery_status': 'pending'}}, **args)


@pytest.mark.parametrize('queued', [False, True])
def test_prepared_journey_uses_saved_action_and_cleans_owned_image(monkeypatch, queued):
    from tools import provider_wrapper_probe as probe
    from message_envelopes import annotate_message_envelope
    requests, source, rows, host_commands = [], [], [], []
    notification = {'notification_id': 'notification-fixture', 'actions': [
        {'action_id': 'saved-done-action', 'kind': 'yes_no', 'choice': True,
         'value': {'answer': 'done'}}]}
    phase = {'resolved': False, 'image': None, 'timed': None}
    def host_command(host, *argv):
        host_commands.append(argv)
        return ''
    def rpc(payload, prefix):
        requests.append(payload)
        kind = payload['type']
        if kind == 'request_stream_events':
            return {'events': list(rows)}
        if kind == 'list_sessions':
            return {'active': [{'stream_id': STREAM, 'working': False}]}
        if kind == 'prompt.status':
            return {'question': {'question_id': payload['question_id'], 'notification_id': notification['notification_id']}}
        if kind == 'notification.list':
            return {'notifications': [notification]}
        if kind == 'send':
            if 'sleep 45;' in payload['text']:
                timed = payload['text'].split('command now: ')[1].split('. After')[0]
                phase['timed'] = timed
                rows.append({'kind': 'TOOL_USE', 'raw': {'tool_name': 'Bash',
                    'tool_input': {'command': timed}, 'tool_use_id': 'timed-tool'}, 'daemon_seq': 1})
            if 'Use Read on exactly' in payload['text']:
                image = payload['text'].split('exactly ')[1].split('. Then')[0]
                phase['image'] = image
                source.extend([
                    {'type': 'assistant', 'uuid': 'read-use', 'message': {'content': [
                        {'type': 'tool_use', 'id': 'read-tool', 'name': 'Read', 'input': {'file_path': image}}]}},
                    {'type': 'user', 'uuid': 'read-result-fixture', 'message': {'content': [
                        {'type': 'tool_result', 'tool_use_id': 'read-tool', 'content': 'image'}]}},
                    copy.deepcopy(FIXTURE['image_meta_capture']),
                ])
                rows.append({'kind': 'TOOL_RESULT', 'raw': {'tool_use_id': 'read-tool'}, 'daemon_seq': 3})
        if kind == 'notification.resolve':
            assert not phase['resolved'], 'probe must never resolve twice'
            phase['resolved'] = True
            notification['resolution'] = {'delivery_status': 'delivered', 'by': 'operator'}
            capture = FIXTURE['queued_capture']
            record = ingress_record(capture['record']['attachment']['prompt'], 'queued' if queued else 'ordinary')
            source.append(record)
            event = annotate_message_envelope(normalize_claude_jsonl_records([record], host=HOST, session_name=NAME)[0])
            event['daemon_seq'] = 2
            rows.append(event)
            return {'notification': notification}
        return {'type': prefix + '.ok'}
    monkeypatch.setattr(probe, '_host_command', host_command)
    monkeypatch.setattr(probe, '_owned_source_records', lambda host, sid: source)
    result = probe.notification_journey(HOST, STREAM, queued=queued, rpc=rpc,
        wait_event=lambda *args: None, timeout=1)
    resolves = [r for r in requests if r['type'] == 'notification.resolve']
    assert len(resolves) == 1
    assert resolves[0]['action_id'] == 'saved-done-action'
    assert all('_auth_context' not in r for r in requests)
    assert result['image_cleanup']['removed'] is True
    assert result['image']['meta_record']['uuid'] == 'image-meta-fixture'
    assert phase['image'] == host_commands[-1][-1]
    assert 'unlink' in host_commands[-1][-2]
