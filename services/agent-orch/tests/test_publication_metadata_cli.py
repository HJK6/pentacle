"""One-door metadata shape checks and forwarding; every transport is a fake."""
import hashlib

import pytest

from agent_orch import cli


PNG = b'\x89PNG\r\n\x1a\nfixture'


@pytest.fixture
def wire(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, 'load_config', lambda: object())

    async def upload(config, data, **kwargs):
        calls.append(('upload', kwargs))
        return dict(type='upload_blob.ok', upload_id='fixture-upload',
                    blob_sha=hashlib.sha256(data).hexdigest(), bytes=len(data),
                    media_type='image/png', filename=kwargs['filename'],
                    uploader='fixture:seat', generation='g1',
                    uploaded_at='2026-01-01T00:00:00+00:00', auth_kind='seat',
                    seat_stream_id='fixture:seat', seat_generation='g1',
                    credential_id=None, assistant_scope=None)

    async def publish(config, payload, **kwargs):
        calls.append(('publish', payload))
        return {'type': 'assistant.publish.ok', 'event_id': 1}

    monkeypatch.setattr(cli, 'upload_blob_once', upload)
    monkeypatch.setattr(cli, 'assistant_once', publish)
    return calls


def arguments(command, tmp_path, *extra):
    if command == 'publish':
        argv = ['assistant', 'publish', '--composite-stream-id', 'home:assistant',
                '--request-id', 'fixture', '--publish-kind', 'prose', '--message', 'done']
    else:
        path = tmp_path / 'fixture.png'
        path.write_bytes(PNG)
        argv = [command, str(path), '--to', 'home:assistant']
    return cli.build_parser().parse_args([*argv, '--proactive', *extra])


@pytest.mark.parametrize('command', ['publish', 'send-file', 'send-image'])
@pytest.mark.parametrize('note_type,ref', [
    ('result', 'report:one'), ('problem', 'lane:two'), ('reminder', 'wake:three'),
    ('relay', 'tell:four'), ('result', 'message:operator:id'),
])
def test_proactive_metadata_forwarded(command, note_type, ref, tmp_path, wire):
    args = arguments(command, tmp_path, '--note-type', note_type, '--ref', ref)
    assert args.func(args) == 0
    payload = wire[-1][1]
    assert wire[-1][0] == 'publish'
    assert payload['note_type'] == note_type and payload['ref'] == ref
    assert 'dispatch_id' not in payload


@pytest.mark.parametrize('command', ['publish', 'send-file', 'send-image'])
@pytest.mark.parametrize('flags', [[], ['--note-type', 'reminder'], ['--ref', 'report:one'],
                                   ['--note-type', 'relay', '--ref', 'lane:one']])
def test_requiredness_and_type_ref_relationship_belong_to_daemon(command, flags, tmp_path, wire):
    args = arguments(command, tmp_path, *flags)
    assert args.func(args) == 0
    payload = wire[-1][1]
    assert ('note_type' in payload) == ('--note-type' in flags)
    assert ('ref' in payload) == ('--ref' in flags)


@pytest.mark.parametrize('command', ['publish', 'send-file', 'send-image'])
@pytest.mark.parametrize('flags', [
    ['--note-type', 'invalid'], ['--ref', 'lane:'], ['--ref', 'unknown:id'],
    ['--ref', 'message'], ['--ref', 'tell: bad'], ['--ref', 'tell:x\ny'],
    ['--ref', 'message:two words'],
])
def test_bad_supplied_shape_refused_before_transport(command, flags, tmp_path, wire):
    args = arguments(command, tmp_path, *flags)
    assert args.func(args) == 2
    assert wire == []


@pytest.mark.parametrize('flags', [['--proactive'], ['--note-type', 'result'], ['--ref', 'report:x']])
def test_send_image_publication_flags_require_target(tmp_path, wire, flags):
    path = tmp_path / 'fixture.png'
    path.write_bytes(PNG)
    args = cli.build_parser().parse_args(['send-image', str(path), *flags])
    assert args.func(args) == 2
    assert wire == []
