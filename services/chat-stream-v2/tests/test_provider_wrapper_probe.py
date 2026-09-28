"""Harness controls; recordings are replayed separately outside public source."""
import base64
import copy
import json

import pytest

from tools import provider_wrapper_probe as probe
from tools import mobile_probe_oracle as mobile


def test_native_source_requires_exact_record_and_display():
    body = 'Reply exactly PROBE_READY and do nothing else.'
    event = {'kind': 'USER', 'provider': 'claude', 'text': body,
             'raw': {'jsonl_record_uuid': 'owned-user'}}
    record = {'uuid': 'owned-user', 'type': 'user',
              'message': {'role': 'user', 'content': body}}
    assert probe.assert_provider_source(event, body=body, record=record)['mode'] == 'native-one-line'
    for key, value in [('uuid', 'other'), ('isMeta', True), ('type', 'assistant')]:
        with pytest.raises(AssertionError):
            probe.assert_provider_source(event, body=body, record={**record, key: value})
    for changed in [
        {**event, 'text': 'other'},
        {**event, 'provider_wrapper': {'kind': 'claude_pasted_content'}},
        {**event, 'raw': {**event['raw'], 'envelope_source': 'other'}},
    ]:
        with pytest.raises(AssertionError):
            probe.assert_provider_source(changed, body=body, record=record)
    with pytest.raises(AssertionError):
        probe.assert_provider_source(event, body=body + '\nsecond line', record=record)


def test_xml_source_retains_strict_grammar_and_exact_source():
    body = 'line one\nline two'
    content = '<pasted_content id="ab12">\n' + body + '\n</pasted_content id="ab12">'
    event = {'text': body, 'provider_wrapper': {'kind': 'claude_pasted_content',
             'provenance': 'grammar', 'id': 'ab12'},
             'raw': {'jsonl_record_uuid': 'owned', 'provider_content': content}}
    record = {'type': 'user', 'uuid': 'owned', 'message': {'content': content}}
    assert probe.assert_provider_source(event, body=body, record=record)['mode'] == 'strict-XML'
    for text in [content.replace('ab12', 'bad'), content[:-1], body]:
        with pytest.raises(AssertionError):
            probe.assert_provider_source(event, body=body, record={**record, 'message': {'content': text}})


def test_initial_prompt_declares_all_steps_without_overwriting_nonce():
    marker = 'PENTACLE_FLEET_SMOKE_TEST'
    seed = 'Reply exactly ' + marker + ' and do nothing else.'
    prompt = probe.initial_probe_prompt(seed, foreground_job='python3 /scratch/job.py probe')
    assert prompt.endswith(seed)
    for step in ['finite', 'python3 /scratch/job.py probe', 'foreground', 'notification', 'Read',
                 'coordinate', 'PNG', 'operator', 'scratch']:
        assert step in prompt
    assert 'sleep' not in prompt and 'FIFO' not in prompt and 'Monitor' not in prompt


def test_queued_job_admission_precedes_mutations():
    calls = []
    with pytest.raises(RuntimeError, match='upfront-declared'):
        probe.notification_journey('host', 'host:owned', queued=True,
            rpc=lambda *args: calls.append(args), wait_event=lambda *args: None, timeout=1)
    assert calls == []
    for command in ['sleep 45', 'python3 /tmp/job.py probe; sleep 45', 'Monitor wait',
                    'python3 relative.py probe']:
        with pytest.raises(RuntimeError):
            probe.finite_job_path(command)


@pytest.mark.parametrize('fault', ['wrong-tool', 'background', 'returned', 'unowned-process', 'race'])
def test_queued_foreground_controls_never_resolve(monkeypatch, fault):
    command = 'python3 /scratch/job.py probe'
    tool = {'kind': 'TOOL_USE', 'daemon_seq': 1, 'raw': {'tool_name': 'Bash',
            'tool_use_id': 'job', 'tool_input': {'command': command, 'timeout': 240000,
                                               'run_in_background': False}}}
    if fault == 'wrong-tool': tool['raw']['tool_name'] = 'Monitor'
    if fault == 'background': tool['raw']['tool_input']['run_in_background'] = True
    calls, reads = [], []
    def rpc(payload, prefix):
        calls.append(payload['type'])
        if payload['type'] == 'prompt.status': return {'question': {'notification_id': 'owned'}}
        if payload['type'] == 'notification.list': return {'notifications': [{
            'actions': [{'action_id': 'done', 'kind': 'yes_no', 'choice': True,
                         'value': {'answer': 'done'}}]}]}
        if payload['type'] == 'list_sessions': return {'active': [{'stream_id': 'host:owned', 'pane_pid': 123}]}
        if payload['type'] == 'request_stream_events':
            reads.append(True)
            result = {'kind': 'TOOL_RESULT', 'daemon_seq': 2, 'raw': {'tool_use_id': 'job'}}
            return {'events': [tool] + ([result] if fault == 'returned' or
                (fault == 'race' and len(reads) > 1) else [])}
        return {}
    monkeypatch.setattr(probe, '_host_command', lambda *args: json.dumps({
        'pending_foreground_processes': [{'owned_pane_in_ancestry': fault != 'unowned-process',
                                         'descendants': [{'command': 'python3 -m pytest tests'}]}]}))
    with pytest.raises((AssertionError, RuntimeError)):
        probe.notification_journey('host', 'host:owned', queued=True, foreground_job=command,
            rpc=rpc, wait_event=lambda *args: None, timeout=0)
    assert 'notification.resolve' not in calls


def test_operator_png_requires_source_image_and_owned_identity():
    literal, caption, stream = mobile.LITERAL, 'Reply exactly PROBE_ATTACHMENT', 'host:owned'
    records = [
        {'uuid': 'literal', 'type': 'user', 'message': {'role': 'user', 'content': literal}},
        {'uuid': 'image', 'type': 'user', 'message': {'role': 'user', 'content': [
            {'type': 'text', 'text': caption},
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()}}]}},
    ]
    events = [{'kind': 'USER', 'provider': 'claude', 'stream_id': stream, 'daemon_seq': i + 1,
               'text': body, 'raw': {'jsonl_record_uuid': record['uuid']}}
              for i, (body, record) in enumerate(zip([literal, caption], records))]
    events[1]['attachments'] = [{'kind': 'image', 'mimeType': 'image/png'}]
    args = dict(stream=stream, literal=literal, caption=caption)
    assert len(probe.assert_operator_controls(events, records, **args)) == 2
    bad_sources = []
    for change in ['image-missing', 'metadata', 'role', 'uuid', 'mime', 'bytes']:
        bad = copy.deepcopy(records)
        if change == 'image-missing': bad[1]['message']['content'].pop()
        if change == 'metadata': bad[1]['isMeta'] = True
        if change == 'role': bad[1]['message']['role'] = 'assistant'
        if change == 'uuid': bad[1]['uuid'] = 'other'
        if change == 'mime': bad[1]['message']['content'][1]['source']['media_type'] = 'image/jpeg'
        if change == 'bytes': bad[1]['message']['content'][1]['source']['data'] = 'invalid-base64'
        bad_sources.append(bad)
    for bad_records in bad_sources:
        with pytest.raises(AssertionError):
            probe.assert_operator_controls(events, bad_records, **args)
    for changed in [events + [events[0]], [events[0], {**events[1], 'attachments': []}],
                    [events[0], {**events[1], 'stream_id': 'other:unowned'}]]:
        with pytest.raises(AssertionError):
            probe.assert_operator_controls(changed, records, **args)


def test_mobile_read_label_is_bound_to_exact_card(tmp_path):
    expected = {'stream': 'host:owned', 'provider_marker': 'PROBE_READ', 'meta_text': 'META_TEXT',
                'notice_seq': 10, 'notification_id': 'owned-notice', 'read_use_ids': [11],
                'owned_read_path': '/tmp/owned.png'}
    model = {'views': [{}, {'detail': {'transcriptItems': [
        {'id': '11', 'kind': 'TOOL_RESULT', 'text': 'image', 'displayRule': 'tool-result'}]}}]}
    common = [{'AXLabel': 'PROBE_READ'}, {'AXLabel': 'Operator answered: Done'}]
    for mode in ['hidden', 'shown']:
        directory = tmp_path / ('mobile-' + mode)
        directory.mkdir()
        (directory / 'navigation-identity.json').write_text(json.dumps({
            'stream': expected['stream'], 'provider_marker': 'PROBE_READ',
            'marker_visible': True, 'session_header_visible': True}))
        rows = common + ([{'AXLabel': 'Tool result. Read /tmp/owned.png',
                           'AXUniqueId': 'tool-result-card-11'}] if mode == 'shown' else [])
        (directory / 'screen.ax.json').write_text(json.dumps(rows))
    assert len(mobile.validate(tmp_path, expected, model)) == 2
    path = tmp_path / 'mobile-shown/screen.ax.json'
    original = json.loads(path.read_text())
    for change in [{'AXUniqueId': 'tool-result-card-99'}, {'AXLabel': 'Tool result. Read /tmp/other.png'}]:
        rows = copy.deepcopy(original)
        rows[-1].update(change)
        path.write_text(json.dumps(rows))
        with pytest.raises(AssertionError):
            mobile.validate(tmp_path, expected, model)
