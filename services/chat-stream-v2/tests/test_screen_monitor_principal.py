"""One fixed service may acquire only short leases on its configured host."""
import asyncio
import json
import pytest
from server import Server
from store import Store
from screen_lease import ScreenLease
from test_system_notification_producer import Peer

ACTOR='codex-reset-monitor'
TOKEN='synthetic-dedicated-screen-token'

def frame(kind,**fields):return {'type':kind,'request_id':'screen-test','from_stream_id':ACTOR,'stream_token':TOKEN,**fields}

@pytest.fixture
def credentials(monkeypatch,tmp_path):
    token=tmp_path/'token';token.write_text(TOKEN);token.chmod(0o600)
    monkeypatch.setenv('PENTACLE_CODEX_RESET_MONITOR_TOKEN_FILE',str(token))
    monkeypatch.setenv('PENTACLE_CODEX_RESET_MONITOR_HOST','test')
    return token


def test_fixed_principal_real_dispatch_and_store_scope(credentials):
    async def run():
        store=Store(':memory:');store.start()
        try:
            s=Server();s.handlers.update(ScreenLease(store,['test','peer']).wire_handlers());p=Peer()
            async def forbidden(msg):raise AssertionError('forbidden handler reached')
            for verb in ['tell','notification.create','asset.publish','list_sessions','account']:
                s.handlers[verb]=forbidden
            async def call(kind,**kw):return (await s._dispatch(json.dumps(frame(kind,**kw)),websocket=p))[0]
            assert (await call('hello',subscribe={'snapshot':False,'mode':'rpc'}))['type']=='ready'
            lease=await call('screen.lease.acquire',host='test',ttl=120,reason='read-only source')
            assert lease['type']=='screen.lease.acquire.ok',lease
            assert lease['holder_stream_id']==ACTOR
            assert lease['expires_at']-lease['acquired_at']==120
            for ttl in [121,True,'120',0]:
                assert 'error_code' in await call('screen.lease.renew',lease_id=lease['lease_id'],ttl=ttl)
            assert 'error_code' in await call('screen.lease.acquire',host='peer',ttl=120,reason='outside scope')
            assert 'error_code' in await call('screen.lease.release',lease_id=lease['lease_id'],force=True)
            for verb in ['tell','notification.create','asset.publish','list_sessions','screen.lease.status','account']:
                assert (await call(verb))['error_code']=='system_producer_forbidden'
            assert (await call('screen.lease.renew',lease_id=lease['lease_id'],ttl=60))['type'].endswith('.ok')
            assert (await call('screen.lease.release',lease_id=lease['lease_id']))['type'].endswith('.ok')
            assert await store.submit(lambda c:c.execute('SELECT count(*) FROM sessions').fetchone()[0])==0
        finally:store.stop()
    asyncio.run(run())


def test_revoked_or_unreadable_token_never_falls_back_to_local_operator(credentials):
    async def run():
        s=Server();p=Peer('127.0.0.1')
        async def forbidden(msg):raise AssertionError('revoked principal handler reached')
        s.handlers['screen.lease.acquire']=forbidden
        assert (await s._dispatch(json.dumps(frame('hello',subscribe={'snapshot':False,'mode':'rpc'})),websocket=p))[0]['type']=='ready'
        credentials.chmod(0o644)
        denied=(await s._dispatch(json.dumps(frame('screen.lease.acquire',host='test',ttl=120,reason='test')),websocket=p))[0]
        assert denied['error_code']=='system_producer_auth_required'
    asyncio.run(run())
