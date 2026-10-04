"""Synthetic send-file contract tests; no fleet connections or credentials."""
import hashlib
import json
from pathlib import Path
import pytest
import agent_orch.cli as cli

PDF = b'%PDF synthetic fixture'


def args(*argv):
    return cli.build_parser().parse_args(['send-file', *map(str, argv)])


def receipt(data=PDF, filename='sample.pdf', mime='application/pdf'):
    return dict(type='upload_blob.ok', upload_id='synthetic-upload',
        blob_sha=hashlib.sha256(data).hexdigest(), bytes=len(data),
        media_type=mime, filename=filename, uploader='fixture:seat', generation='g1',
        uploaded_at='2026-01-01T00:00:00+00:00', auth_kind='seat',
        seat_stream_id='fixture:seat', seat_generation='g1', credential_id=None,
        assistant_scope=None)


@pytest.fixture
def wire(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, 'load_config', lambda: object())
    async def upload(config, data, **kw):
        calls.append(('upload', data, kw))
        return receipt(data, kw['filename'])
    async def publish(config, payload, **kw):
        calls.append(('publish', payload, kw))
        return {'type': 'assistant.publish.ok', 'request_id': payload['request_id']}
    monkeypatch.setattr(cli, 'upload_blob_once', upload)
    monkeypatch.setattr(cli, 'assistant_once', publish)
    return calls


def test_step_a_uploads_managed_and_prints_server_receipt(tmp_path, wire, capsys):
    p = tmp_path / 'sample.pdf'; p.write_bytes(PDF)
    assert cli.send_file(args(p)) == 0
    assert len(wire) == 1
    assert wire[0][2]['purpose'] == 'chat_attachment'
    result = json.loads(capsys.readouterr().out)
    assert result['upload_id'] == 'synthetic-upload'
    assert result['uploader'] == 'fixture:seat'
    assert result['bytes'] == len(PDF)


def test_step_b_reuses_explicit_correlation(wire):
    assert cli.send_file(args('--upload-id','synthetic-upload','--to','bart:assistant',
        '--dispatch-id','real-dispatch','--reply-to-message-id','real-input',
        '--publish-kind','result','--request-id','existing-request')) == 0
    assert len(wire) == 1
    payload = wire[0][1]
    assert payload == dict(type='assistant.publish', request_id='existing-request',
        composite_stream_id='bart:assistant', dispatch_id='real-dispatch',
        reply_to_message_id='real-input', publish_kind='result', message='',
        attachment_ids=['synthetic-upload'])


def publish_args(*source, **kw):
    argv = [*source, '--to','bart:assistant','--dispatch-id','real-dispatch',
        '--reply-to-message-id','real-input','--publish-kind',kw.get('kind','result')]
    if kw.get('request', True):
        argv += ['--request-id','existing-request']
    return args(*argv)


@pytest.mark.parametrize('suffix,data,mime', [
    ('png',b'\x89PNG synthetic','image/png'), ('jpg',b'\xff\xd8\xff synthetic','image/jpeg'),
    ('jpeg',b'\xff\xd8\xff synthetic','image/jpeg'), ('pdf',PDF,'application/pdf'),
    ('zip',b'PK\x03\x04 synthetic','application/zip'), ('3mf',b'PK\x03\x04 synthetic','model/3mf'),
    ('stl',b'solid synthetic','model/stl'), ('step',b'ISO synthetic','model/step'),
    ('stp',b'ISO synthetic','model/step'), ('scad',b'cube(1);','application/x-openscad'),
])
def test_closed_type_map(tmp_path, monkeypatch, wire, capsys, suffix, data, mime):
    p=tmp_path/f'fixture.{suffix}';p.write_bytes(data)
    async def upload(config, body, **kw):
        assert body == data
        return receipt(body, kw['filename'], mime)
    monkeypatch.setattr(cli,'upload_blob_once',upload)
    assert cli.send_file(args(p)) == 0
    assert json.loads(capsys.readouterr().out)['media_type'] == mime
    assert wire == []


@pytest.mark.parametrize('name,data,error', [
    ('x.svg',b'<svg/>','unsupported_type'), ('x.html',b'<h1>','unsupported_type'),
    ('x.pdf',b'not pdf','type_mismatch'), ('x.png',PDF,'type_mismatch'),
    ('x.zip',PDF,'type_mismatch'), ('x.3mf',PDF,'type_mismatch'),
    ('x.step',b'','empty_file'),
])
def test_refusals_before_config_or_upload(tmp_path, monkeypatch, wire, capsys, name, data, error):
    p=tmp_path/name;p.write_bytes(data)
    monkeypatch.setattr(cli,'load_config',lambda: pytest.fail('must preflight first'))
    assert cli.send_file(args(p)) == 2
    assert json.loads(capsys.readouterr().out)['error_code'] == error
    assert wire == []


def test_real_oversize_refused(tmp_path, wire, capsys):
    p=tmp_path/'large.pdf'
    with p.open('wb') as f:
        f.write(b'%PDF'); f.truncate(25*1024*1024+1)
    assert cli.send_file(args(p)) == 2
    assert json.loads(capsys.readouterr().out)['error_code'] == 'attachment_too_large'
    assert not wire


@pytest.mark.parametrize('relative', ['.ssh/key.pdf','.aws/credentials.pdf','.config/pentacle-stream/token.pdf','.config/dot/token.pdf','.env.pdf'])
def test_secret_location_refused_including_symlink(tmp_path, monkeypatch, wire, capsys, relative):
    home=tmp_path/'home';p=home/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(PDF)
    monkeypatch.setattr(Path,'home',classmethod(lambda cls: home))
    alias=tmp_path/'innocent.pdf';alias.symlink_to(p)
    for source in (p, alias):
        assert cli.send_file(args(source)) == 2
        assert json.loads(capsys.readouterr().out)['error_code'] == 'secret_path_refused'
    assert wire == []


def test_explicit_token_file_refused_without_reading(tmp_path, monkeypatch, wire, capsys):
    p=tmp_path/'looks-like-report.pdf';p.write_bytes(PDF)
    monkeypatch.setenv('AGENT_ORCH_STREAM_TOKEN_FILE',str(p))
    assert cli.send_file(args(p)) == 2
    assert 'secret_path_refused' in capsys.readouterr().out
    assert not wire


@pytest.mark.parametrize('field,value', [('blob_sha','0'*64),('bytes',999),('media_type','text/html'),('filename','other.pdf'),('upload_id',None),('generation',None),('uploaded_at','nope')])
def test_bad_server_receipt_not_published(tmp_path, monkeypatch, wire, capsys, field, value):
    p=tmp_path/'sample.pdf';p.write_bytes(PDF)
    async def upload(*a,**kw):
        result=receipt();result[field]=value;return result
    monkeypatch.setattr(cli,'upload_blob_once',upload)
    assert cli.send_file(publish_args(p)) == 2
    assert 'synthetic-upload' not in capsys.readouterr().out
    assert not wire


@pytest.mark.parametrize('as_file',[False,True])
def test_receipt_carrier_ignores_all_advisory_fields(tmp_path, wire, capsys, as_file):
    carrier=json.dumps({'upload_id':'synthetic-upload','uploader':'FORGED',
        'blob_sha':'FORGED', 'token':'DO_NOT_PRINT', 'bytes':False})
    if as_file:
        p=tmp_path/'receipt.json';p.write_text(carrier);carrier=str(p)
    assert cli.send_file(publish_args('--from-receipt',carrier)) == 0
    assert len(wire)==1 and wire[0][1]['attachment_ids']==['synthetic-upload']
    assert 'FORGED' not in json.dumps(wire)
    assert 'DO_NOT_PRINT' not in capsys.readouterr().out


def test_nonpublisher_to_preserves_upload_receipt(tmp_path, monkeypatch, wire, capsys):
    p=tmp_path/'sample.pdf';p.write_bytes(PDF)
    async def denied(*a,**kw):
        return {'type':'assistant.publish.error','error_code':'publish_not_authorized','message':'DO_NOT_PRINT'}
    monkeypatch.setattr(cli,'assistant_once',denied)
    assert cli.send_file(publish_args(p)) == 1
    output=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[0]['upload_id']=='synthetic-upload'
    assert output[0]['uploader']=='fixture:seat'
    assert output[1]['error_code']=='publish_not_authorized'
    assert 'DO_NOT_PRINT' not in json.dumps(output)


def test_transport_exception_does_not_leak(tmp_path, monkeypatch, wire, capsys):
    p=tmp_path/'sample.pdf';p.write_bytes(PDF)
    async def fail(*a,**kw):
        raise RuntimeError('DO_NOT_PRINT token or file payload')
    monkeypatch.setattr(cli,'assistant_once',fail)
    assert cli.send_file(publish_args(p)) == 1
    output=capsys.readouterr()
    assert 'DO_NOT_PRINT' not in output.out+output.err
    assert 'synthetic-upload' in output.out  # receipt remains available after upload


@pytest.mark.parametrize('argv', [
    ['--upload-id','synthetic-upload'],
    ['--upload-id','synthetic-upload','--to','bart:assistant'],
    ['--upload-id','synthetic-upload','--to','bart:assistant','--dispatch-id','real',
     '--reply-to-message-id','input','--publish-kind','result'],
])
def test_missing_correlation_fails_before_network(wire, argv):
    assert cli.send_file(args(*argv)) == 2
    assert wire == []


def test_prose_request_uses_existing_convention(wire):
    assert cli.send_file(publish_args('--upload-id','synthetic-upload',kind='prose',request=False)) == 0
    assert wire[0][1]['request_id']=='publish:real-dispatch'


def test_upload_retry_key_preserved(tmp_path, wire):
    p=tmp_path/'sample.pdf';p.write_bytes(PDF)
    assert cli.send_file(args(p,'--upload-request-id','retry-original')) == 0
    assert wire[0][2]['request_id']=='retry-original'


def test_cli_rejects_spoofed_content_type(wire):
    with pytest.raises(SystemExit):
        args('sample.pdf','--content-type','text/plain')
    assert wire == []


def test_direct_prose_defaults_to_final(wire):
    assert cli.send_file(publish_args('--upload-id','synthetic-upload',kind='prose',request=False)) == 0
    assert wire[0][1]['response_state'] == 'final'


def test_upload_transport_uses_managed_purpose_without_altering_generic(monkeypatch):
    import asyncio
    import base64
    import agent_orch.wsclient as ws
    sockets=[]
    class Socket:
        def __init__(self): self.sent=[]; self.closed=False; self.responses=[]
        async def send(self, value):
            item=json.loads(value);self.sent.append(item)
            if item['type']=='upload_blob_init':
                self.responses.append(dict(type='upload_blob.init.ok',request_id=item['request_id']))
            elif item.get('final'):
                self.responses.append(dict(receipt(),request_id=item['request_id']))
        async def recv(self): return json.dumps(self.responses.pop(0))
        async def close(self): self.closed=True
    async def connect(config):
        result=Socket();sockets.append(result);return result
    monkeypatch.setattr(ws,'_connect_rpc_ready',connect)
    managed=asyncio.run(ws.upload_blob_once(object(),PDF,request_id='stable',chunk_size=3,
        purpose='chat_attachment',filename='sample.pdf'))
    generic=asyncio.run(ws.upload_blob_once(object(),PDF,request_id='generic'))
    assert managed['upload_id']=='synthetic-upload'
    assert sockets[0].sent[0]==dict(type='upload_blob_init',request_id='stable',
        size_hint_bytes=len(PDF),purpose='chat_attachment',filename='sample.pdf')
    assert sockets[1].sent[0]==dict(type='upload_blob_init',request_id='generic',size_hint_bytes=len(PDF))
    chunks=sockets[0].sent[1:]
    assert b''.join(base64.b64decode(c['data_b64']) for c in chunks)==PDF
    assert [c['final'] for c in chunks]==[False]*(len(chunks)-1)+[True]
    assert all(s.closed for s in sockets)


def test_path_swap_to_secret_symlink_is_refused(tmp_path, monkeypatch, capsys, wire):
    import agent_orch.file_delivery as delivery
    home=tmp_path/'home';secret=home/'.ssh'/'secret.pdf';secret.parent.mkdir(parents=True);secret.write_bytes(PDF)
    monkeypatch.setattr(Path,'home',classmethod(lambda cls: home))
    folder=tmp_path/'safe';folder.mkdir();source=folder/'secret.pdf';source.write_bytes(PDF)
    original=delivery.guarded_path
    def swapping(value):
        resolved=original(value)
        source.unlink();folder.rmdir();folder.symlink_to(secret.parent,target_is_directory=True)
        return resolved
    monkeypatch.setattr(delivery,'guarded_path',swapping)
    assert cli.send_file(args(source))==2
    assert not wire
    assert 'fixture' not in capsys.readouterr().out


@pytest.mark.parametrize('kind,principal,credential,scope',[
    ('scoped','credential:scope-1','scope-1','bart:assistant'),
    ('operator','operator:operator-1','operator-1',None),
])
def test_nonseat_receipt_preserves_explicit_null_generation(tmp_path,monkeypatch,wire,capsys,kind,principal,credential,scope):
    p=tmp_path/'sample.pdf';p.write_bytes(PDF)
    async def upload(*a,**kw):
        result=receipt();result.update(auth_kind=kind,uploader=principal,
            generation=None,seat_generation=None,seat_stream_id=None,
            credential_id=credential,assistant_scope=scope)
        return result
    monkeypatch.setattr(cli,'upload_blob_once',upload)
    assert cli.send_file(args(p))==0
    result=json.loads(capsys.readouterr().out)
    assert result['generation'] is None and result['uploader']==principal


def test_fifo_refused_without_blocking(tmp_path,wire):
    import os
    p=tmp_path/'pipe.pdf';os.mkfifo(p)
    assert cli.send_file(args(p))==2
    assert not wire


def test_secret_receipt_file_refused(tmp_path,monkeypatch,wire,capsys):
    home=tmp_path/'home';p=home/'.ssh'/'receipt.json';p.parent.mkdir(parents=True)
    p.write_text(json.dumps(receipt()))
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:home))
    assert cli.send_file(publish_args('--from-receipt',p))==2
    assert 'secret_path_refused' in capsys.readouterr().out
    assert not wire


@pytest.mark.parametrize('value',['{"blob_sha":"forged"}','{"upload_id":42}','{"upload_id":"../x"}'])
def test_invalid_receipt_has_no_publish(value,wire):
    assert cli.send_file(publish_args('--from-receipt',value))==2
    assert not wire


def test_server_receipt_unknown_fields_not_printed(tmp_path,monkeypatch,capsys,wire):
    p=tmp_path/'sample.pdf';p.write_bytes(PDF)
    async def upload(*a,**kw):
        return dict(receipt(),token='DO_NOT_PRINT',payload='DO_NOT_PRINT')
    monkeypatch.setattr(cli,'upload_blob_once',upload)
    assert cli.send_file(args(p))==0
    assert 'DO_NOT_PRINT' not in capsys.readouterr().out


def test_nonprose_operation_evidence_passes_through(wire):
    ns=publish_args('--upload-id','synthetic-upload')
    ns.evidence_refs_json='["real-operation-receipt"]'
    assert cli.send_file(ns)==0
    assert wire[0][1]['evidence_refs']==['real-operation-receipt']


def test_publication_receipt_retains_event_and_duplicate(monkeypatch,wire,capsys):
    async def publish(*a,**kw):
        return {'type':'assistant.publish.ok','event_id':123,'duplicate':True,'publication_key':'existing-request'}
    monkeypatch.setattr(cli,'assistant_once',publish)
    assert cli.send_file(publish_args('--upload-id','synthetic-upload'))==0
    result=json.loads(capsys.readouterr().out)
    assert result['event_id']==123 and result['duplicate'] is True


def test_kubeconfig_path_list_each_entry_and_symlink_refused(tmp_path,monkeypatch,wire,capsys):
    import os
    first=tmp_path/'first.pdf';second=tmp_path/'second.pdf'
    first.write_bytes(PDF);second.write_bytes(PDF)
    alias=tmp_path/'alias.pdf';alias.symlink_to(second)
    monkeypatch.setenv('KUBECONFIG',os.pathsep.join([str(first),'',str(second)]))
    for p in (first,second,alias):
        assert cli.send_file(args(p))==2
        assert json.loads(capsys.readouterr().out)['error_code']=='secret_path_refused'
    assert not wire


@pytest.mark.parametrize('code',['scope_denied','assistant_publish_unauthorized'])
def test_existing_server_scope_refusals_report_publish_not_authorized(monkeypatch,wire,capsys,code):
    async def denied(*a,**kw): return {'type':'assistant.publish.error','error_code':code}
    monkeypatch.setattr(cli,'assistant_once',denied)
    assert cli.send_file(publish_args('--upload-id','synthetic-upload'))==1
    assert json.loads(capsys.readouterr().out)['error_code']=='publish_not_authorized'
