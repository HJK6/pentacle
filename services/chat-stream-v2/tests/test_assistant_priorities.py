"""Observable priority list persistence and source validation."""
import pytest
from test_assistant_owed import run, auth


def test_priority_revisions_and_validation(tmp_path):
    async def body(h):
        await h.route('request-a')
        service = h.composite.priorities
        async def call(action, **args):
            return await service.request(action, args, auth(h))
        a = await call('add', rank=1, label='First', words='operator  QUESTION\nrequest-a', source='message:request-a')
        first = a['items'][0]['id']
        assert a['revision'] == 1 and a['items'][0]['words_checked'] is True
        b = await call('add', rank=1, label='Second', words='Operator question', source='message:request-a')
        assert [x['rank'] for x in b['items']] == [1, 2]
        c = await call('move', id=first, rank=1, source='message:request-a')
        assert c['items'][0]['id'] == first and c['revision'] == 3
        await call('owner', id=first, owner='lane:test')
        with pytest.raises(ValueError, match='assistant_priorities_words_not_in_source'):
            await call('add', rank=1, label='Bad', words='invented', source='message:request-a')
        with pytest.raises(ValueError, match='assistant_priorities_source_invalid'):
            await call('remove', id=first, source='message:missing')
        assert (await call('list'))['revision'] == 4
        d = await call('remove', id=first, done=True)
        assert d['revision'] == 5 and len(d['items']) == 1
        assert (await call('list', revision=1))['items'][0]['id'] == first
    run(tmp_path, body)


def test_seat_source_authorization_and_restart(tmp_path):
    from assistant_priorities import snapshot
    from test_assistant_owed import ROOT
    from types import SimpleNamespace
    async def body(h):
        service=h.composite.priorities
        result=await service.request('add', {'rank':1,'label':'Seat source','words':'unverified words','source':'seat:'+ROOT},auth(h))
        assert result['items'][0]['words_checked'] is False
        with pytest.raises(ValueError,match='assistant_owed_unauthorized'):
            await service.request('list',{}, {})
        service.composite.owed.server=SimpleNamespace(lane_rulings=SimpleNamespace(binding_conn=lambda c:dict(state='ready',stream_id=ROOT,generation=h.generation)))
        service.composite.owed.binding=lambda c:dict(stream_id='other:desk',generation='other')
        assert (await service.request('list',{},auth(h)))['revision']==1
        with pytest.raises(ValueError,match='assistant_owed_unauthorized'):
            await service.request('owner',{'id':result['items'][0]['id'],'owner':'lane:x'},auth(h))
        import sqlite3
        def reopened():
            conn=sqlite3.connect(tmp_path/'store.db');conn.row_factory=sqlite3.Row
            try: return snapshot(conn,h.composite.config.stream_id,1)
            finally: conn.close()
        assert reopened()==result
    run(tmp_path,body)
