"""Advisory holds are transactional, bounded and authenticated."""
import asyncio
from datetime import datetime, timezone, timedelta
import pytest
from coordination_holds import Holds, initialize
from test_assistant_owed import run, auth

OP = {'operator_authenticated': True, 'operator_principal': 'operator:test'}

def expiry(seconds=60):
    return (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()


def test_lifecycle_and_authority(tmp_path):
    async def body(h):
        holds=Holds(h.store,h.composite)
        await h.store.submit(initialize)
        call=lambda action, args={}, who=OP: holds.request(action,args,who)
        for value,code in [(None,'hold_expiry_required'),(expiry(86410),'hold_expiry_too_long'),(expiry(-1),'hold_expiry_invalid'),('bad','hold_expiry_invalid')]:
            with pytest.raises(ValueError,match=code):
                await call('acquire',dict(resource='daemon',reason='test',expires_at=value))
        with pytest.raises(ValueError,match='hold_unauthorized'):
            await call('acquire',dict(resource='daemon',reason='test',expires_at=expiry()),{})
        held=(await call('acquire',dict(resource='daemon',reason='test',expires_at=expiry(),owner='host:worker')))['hold']
        assert len(held['id'])==37 and held['owner_stream']=='host:worker'
        with pytest.raises(ValueError,match='hold_taken.*'+held['id']+'.*host:worker'):
            await call('acquire',dict(resource='daemon',reason='again',expires_at=expiry()))
        assert (await call('list'))['holds']==[held]
        assert (await call('list',{'resource':'other'}))['holds']==[]
        with pytest.raises(ValueError,match='hold_force_required'):
            await call('release',{'hold_id':held['id']})
        released=(await call('release',{'resource':'daemon','force':True}))['hold']
        assert released['release_cause']=='forced'
        assert (await call('list'))['holds']==[]
        with pytest.raises(ValueError,match='hold_not_live'):
            await call('release',{'hold_id':held['id'],'force':True})
        own=(await call('acquire',dict(resource='daemon',reason='test',expires_at=expiry()),auth(h)))['hold']
        assert (await call('release',{'hold_id':own['id']},auth(h)))['hold']['release_cause']=='released'
    run(tmp_path,body)


def test_expired_rows_and_fresh_store(tmp_path):
    async def body(h):
        holds=Holds(h.store,h.composite)
        await h.store.submit(lambda c:(c.execute("INSERT INTO holds VALUES ('old','r','owner','reason','2000-01-01T00:00:00Z','2000-01-02T00:00:00Z',NULL,NULL)"),c.commit()))
        assert (await holds.request('list',{},OP))['holds']==[]
        with pytest.raises(ValueError,match='hold_not_live'):
            await holds.request('release',{'resource':'r','force':True},OP)
        assert (await holds.request('acquire',{'resource':'r','reason':'new','expires_at':expiry()},OP))['hold']['id']!='old'
        with pytest.raises(ValueError,match='hold_unauthorized'):
            await holds.request('list',{}, {})
    run(tmp_path,body)


def test_ordinary_owner_and_invalid_arguments(tmp_path):
    async def body(h):
        holds=Holds(h.store,h.composite)
        await h.store.submit(initialize)
        seat=await h.store.open_session('host','worker',provider='claude',pane_pid='4243')
        worker={'token_verified':True,'stream_id':'host:worker','session_generation':seat['session_generation']}
        args={'resource':'r','reason':'test','expires_at':expiry(),'owner':'host:worker'}
        with pytest.raises(ValueError,match='hold_unauthorized'):
            await holds.request('acquire',args,worker)
        row=(await holds.request('acquire',args,OP))['hold']
        assert (await holds.request('list',{},worker))['holds']==[row]
        assert (await holds.request('release',{'hold_id':row['id']},worker))['hold']['release_cause']=='released'
        args['owner']='host:someone_else'
        row=(await holds.request('acquire',args,OP))['hold']
        with pytest.raises(ValueError,match='hold_unauthorized'):
            await holds.request('release',{'hold_id':row['id'],'force':True},worker)
        for bad in ({}, {'hold_id':row['id'],'resource':'r'}):
            with pytest.raises(ValueError,match='hold_arguments_invalid'):
                await holds.request('release',bad,OP)
        for overrides in ({'resource':''},{'reason':''},{'owner':' '}):
            with pytest.raises(ValueError,match='hold_arguments_invalid'):
                await holds.request('acquire',{**args,**overrides},OP)
        for bad in ({**worker,'session_generation':'stale'},{**worker,'scoped_principal':'restricted'},{**worker,'dot_principal':'dot'}):
            with pytest.raises(ValueError,match='hold_unauthorized'):
                await holds.request('acquire',args,bad)
    run(tmp_path,body)


@pytest.mark.parametrize('identity', ['token_verified','operator_authenticated','service_authenticated','scoped_principal','dot_principal'])
def test_list_any_authenticated_identity(tmp_path, identity):
    async def body(h):
        holds=Holds(h.store,h.composite)
        await h.store.submit(initialize)
        assert (await holds.request('list',{}, {identity:True}))['holds']==[]
    run(tmp_path,body)


def test_acquire_serializes_and_normalizes_iso_expiry(tmp_path):
    async def body(h):
        holds=Holds(h.store,h.composite)
        await h.store.submit(initialize)
        args={'resource':'shared','reason':'test','expires_at':expiry()}
        results=await asyncio.gather(*(holds.request('acquire',args,OP) for _ in range(2)),return_exceptions=True)
        assert sum(isinstance(r,dict) for r in results)==1
        assert sum(isinstance(r,ValueError) and str(r).startswith('hold_taken') for r in results)==1
        row=(await holds.request('list',{},OP))['holds'][0]
        assert row['expires_at'].endswith('Z')
        assert row['owner_stream']=='operator:test'
        assert (await holds.request('release',{'resource':'shared'},OP))['hold']['release_cause']=='released'
        with pytest.raises(ValueError,match='hold_expiry_invalid'):
            await holds.request('acquire',{**args,'expires_at':'2099-01-01T12:00:00'},OP)
    run(tmp_path,body)
