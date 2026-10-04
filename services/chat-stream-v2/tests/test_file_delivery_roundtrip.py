"""Exact CLI -> in-process daemon -> fetch journey using synthetic principals.

The connection adapter replaces networking/hello only. Real JSON dispatch,
CLI RPC framing, blob upload, SQLite publication and fetch handlers execute.
Authentication handshake/scoped credential gates have separate tests.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import pytest
from test_managed_attachment_publish import publication_fixture, ASSISTANT, ROOT


@pytest.mark.parametrize('filename,body',[
    ('fixture.png',b'\x89PNG synthetic'),
    ('fixture.jpg',b'\xff\xd8\xff synthetic'),
    ('fixture.jpeg',b'\xff\xd8\xff synthetic'),
    ('fixture.pdf',b'%PDF synthetic end-to-end'),
    ('fixture.zip',b'PK\x03\x04 synthetic end-to-end'),
    ('fixture.3mf',b'PK\x03\x04 synthetic end-to-end'),
    ('fixture.stl',b'solid synthetic'),('fixture.step',b'ISO synthetic'),
    ('fixture.stp',b'ISO synthetic'),('fixture.scad',b'cube(1);'),
])
def test_exact_upload_handoff_retry_fetch_commands(tmp_path,monkeypatch,capsys,filename,body):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]/'agent-orch'))
    import agent_orch.cli as cli
    import agent_orch.wsclient as client
    monkeypatch.setattr(cli,'load_config',lambda:object())
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            server.handlers.update(blobs.wire_handlers())
            loop=asyncio.get_running_loop()
            producer=await store.open_session('fixture-producer','seat',provider='codex')
            current={'auth':dict(token_verified=True,stream_id='fixture-producer:seat',session_generation=producer['session_generation'])}
            async def auth(peer,wire): return dict(peer.auth)
            monkeypatch.setattr(server,'_auth_context',auth)
            class Peer:
                remote_address=('192.0.2.1',1234)
                def __init__(self): self.auth=dict(current['auth']);self.frames=[]
                async def send(self,raw):
                    async def dispatch():
                        frames=await server._dispatch(raw,websocket=self)
                        if hasattr(frames,'__aiter__'): frames=[f async for f in frames]
                        self.frames.extend(json.dumps(f) for f in frames if f is not None)
                    await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(dispatch(),loop))
                async def recv(self): return self.frames.pop(0)
                async def close(self):
                    await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(blobs.abort_connection(self),loop))
            async def connect(config,**kw): return Peer()
            monkeypatch.setattr(client,'_connect_rpc_ready',connect)
            path=tmp_path/filename;path.write_bytes(body)
            assert await asyncio.to_thread(cli.main,['send-file',str(path),'--upload-request-id','stable-upload'])==0
            receipt=json.loads(capsys.readouterr().out)
            assert receipt['blob_sha']==hashlib.sha256(body).hexdigest()
            assert not [e for e in await store.fetch_session_event_tail(ASSISTANT,limit=10) if e['kind']=='ASSIST_TEXT']
            argv=['send-file','--upload-id',receipt['upload_id'],'--to',ASSISTANT,
                '--dispatch-id','dispatch-1','--reply-to-message-id','input-1','--publish-kind','prose']
            # A producer's upload receipt and --to do not grant publisher authority.
            assert await asyncio.to_thread(cli.main,argv)==1
            denied=json.loads(capsys.readouterr().out)
            assert denied['error_code']=='publish_not_authorized'
            current['auth']=dict(msg['_auth_context'])
            assert await asyncio.to_thread(cli.main,argv)==0
            first=json.loads(capsys.readouterr().out)
            assert first['duplicate'] is False
            # Carrier fields are ignored; repeat has exactly the same ID/card.
            carrier=dict(receipt,uploader='FORGED',blob_sha='0'*64)
            retry=['send-file','--from-receipt',json.dumps(carrier),*argv[3:]]
            assert await asyncio.to_thread(cli.main,retry)==0
            second=json.loads(capsys.readouterr().out)
            assert second['duplicate'] and second['event_id']==first['event_id']
            events=[e for e in await store.fetch_session_event_tail(ASSISTANT,limit=10) if e['kind']=='ASSIST_TEXT']
            assert len(events)==1
            result=await asyncio.to_thread(lambda:asyncio.run(client.fetch_blob_once(object(),receipt['blob_sha'])))
            assert result==body and hashlib.sha256(result).hexdigest()==receipt['blob_sha']
    asyncio.run(run())
