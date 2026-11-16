"""Timed directives retain their exact-generation owner through handoff."""
import asyncio
from test_lifecycle_continuity import environment, HOST, SOURCE


def test_handoff_wake_survives_retiring_owner():
    async def run():
        async with environment() as (store,sessions,ctl,server,tmux,opened):
            source=await store.fetch_session(HOST,'source')
            successor=await opened('successor')
            payload={'request_id':'directive','due_at':200,'note':'Carry these exact words','urgent':False}
            wake=await store.register_watch_wake('wake',SOURCE,source['session_generation'],payload,now=100)
            result=await ctl._finish_handoff({'handoff_from_stream_id':SOURCE},HOST+':successor')
            assert result['state']=='complete'
            rows=await store.list_watch_wake('wake',HOST+':successor',successor['session_generation'])
            assert len(rows)==1 and rows[0]['id']==wake['id'] and rows[0]['state']=='active'
            assert await store.evaluate_watch_wake({},now=200)==1
            notices=await store.submit(lambda c:[dict(r) for r in c.execute("SELECT * FROM v2_outbound_notices WHERE kind='wake'")])
            assert len(notices)==1 and notices[0]['recipient_stream_id']==HOST+':successor'
            assert 'Carry these exact words' in notices[0]['body']
    asyncio.run(run())


def test_reparent_moves_child_watch_custody_and_leaves_parent_wake():
    async def run():
        async with environment() as (store,sessions,ctl,server,tmux,opened):
            source=await store.fetch_session(HOST,'source')
            target=await opened('target')
            child=await opened('child',parent_stream_id=SOURCE,no_watch=True)
            sibling=await opened('sibling',parent_stream_id=SOURCE,no_watch=True)
            gen=source['session_generation']
            payload={'request_id':'child-watch','child_stream_id':HOST+':child','triggers':['idle'],'repeat':False}
            watch=await store.register_watch_wake('watch',SOURCE,gen,payload,now=100)
            cancelled=await store.register_watch_wake('watch',SOURCE,gen,{**payload,'request_id':'cancelled'},now=100)
            await store.cancel_watch_wake('watch',SOURCE,gen,cancelled['id'],request_id='cancel-receipt')
            sibling_watch=await store.register_watch_wake('watch',SOURCE,gen,{**payload,'request_id':'sibling-watch','child_stream_id':HOST+':sibling'},now=100)
            wake=await store.register_watch_wake('wake',SOURCE,gen,{'request_id':'parent-wake','due_at':200,'note':'stay here','urgent':False},now=100)
            await store.evaluate_watch_wake({HOST+':child':{'session_generation':child['session_generation'],'working':False}},now=100)
            await store.evaluate_watch_wake({HOST+':child':{'session_generation':child['session_generation'],'working':False}},now=1001)
            facts=await store.submit(lambda c:[dict(r) for r in c.execute('SELECT * FROM v2_watch_facts WHERE owner=? AND child=?',(SOURCE,HOST+':child'))])
            assert facts
            await sessions.reparent(HOST,'child',HOST+':target',auth_context={'token_verified':True,'stream_id':SOURCE})
            moved=await store.list_watch_wake('watch',HOST+':target',target['session_generation'])
            assert {r['id'] for r in moved}=={watch['id'],cancelled['id']}
            assert (await store.list_watch_wake('wake',SOURCE,gen))[0]['id']==wake['id']
            assert (await store.list_watch_wake('watch',SOURCE,gen))[0]['id']==sibling_watch['id']
            rows=await store.submit(lambda c:[dict(r) for r in c.execute('SELECT * FROM v2_watch_cancellations')])
            assert rows[0]['owner']==HOST+':target' and rows[0]['owner_generation']==target['session_generation']
            for fact in facts:
                moved_fact=await store.submit(lambda c:dict(c.execute('SELECT * FROM v2_watch_facts WHERE id=?',(fact['id'],)).fetchone()))
                assert moved_fact['owner']==HOST+':target' and moved_fact['owner_generation']==target['session_generation']
                notice=await store.submit(lambda c:dict(c.execute('SELECT * FROM v2_outbound_notices WHERE notice_id=?',(fact['notice_id'],)).fetchone()))
                assert notice['recipient_stream_id']==HOST+':target' and notice['terminal_at'] is None
            # Raw parent removal still retires watches; no unrelated owner custody moves.
            await store.update_session(HOST,'sibling',parent_stream_id=None)
            assert (await store.list_watch_wake('watch',SOURCE,gen))[0]['state']=='cancelled'
    asyncio.run(run())


def test_handoff_moves_fired_facts_cancellations_and_exact_generation_only():
    async def run():
        async with environment() as (store,sessions,ctl,server,tmux,opened):
            source=await store.fetch_session(HOST,'source');gen=source['session_generation']
            target=await opened('successor')
            first=await store.register_watch_wake('wake',SOURCE,gen,{'request_id':'fired','due_at':101,'note':'verbatim','urgent':False},now=100)
            cancelled=await store.register_watch_wake('wake',SOURCE,gen,{'request_id':'cancelled','due_at':200,'note':'cancel','urgent':False},now=100)
            await store.cancel_watch_wake('wake',SOURCE,gen,cancelled['id'],request_id='cancel-receipt')
            await store.evaluate_watch_wake({},now=101)
            assert (await ctl._finish_handoff({'handoff_from_stream_id':SOURCE},HOST+':successor'))['state']=='complete'
            rows=await store.submit(lambda c:[dict(r) for r in c.execute('SELECT owner,owner_generation FROM v2_watch_facts UNION ALL SELECT owner,owner_generation FROM v2_watch_cancellations')])
            assert rows and all(r['owner']==HOST+':successor' and r['owner_generation']==target['session_generation'] for r in rows)
            notice=await store.submit(lambda c:dict(c.execute("SELECT * FROM v2_outbound_notices WHERE kind='wake'").fetchone()))
            assert notice['recipient_stream_id']==HOST+':successor' and notice['terminal_at'] is None
    asyncio.run(run())


def test_reparent_preserves_one_default_watch_episode():
    async def run():
        async with environment() as (store,sessions,ctl,server,tmux,opened):
            source=await store.fetch_session(HOST,'source');target=await opened('target')
            await opened('child',parent_stream_id=SOURCE)
            before=await store.list_watch_wake('watch',SOURCE,source['session_generation'])
            assert len(before)==1
            await sessions.reparent(HOST,'child',HOST+':target',auth_context={'token_verified':True,'stream_id':SOURCE})
            after=await store.list_watch_wake('watch',HOST+':target',target['session_generation'])
            assert len(after)==1 and after[0]['id']==before[0]['id'] and after[0]['state']=='active'
    asyncio.run(run())


def test_custody_collision_rolls_back_and_handoff_keeps_source_open():
    async def run():
        async with environment() as (store,sessions,ctl,server,tmux,opened):
            source=await store.fetch_session(HOST,'source');target=await opened('successor')
            payload={'request_id':'same-key','due_at':200,'note':'owned','urgent':False}
            old=await store.register_watch_wake('wake',SOURCE,source['session_generation'],payload,now=100)
            new=await store.register_watch_wake('wake',HOST+':successor',target['session_generation'],payload,now=100)
            result=await ctl._finish_handoff({'handoff_from_stream_id':SOURCE},HOST+':successor')
            assert result['state']=='incomplete' and result['stage']=='watch_custody'
            assert (await store.fetch_session(HOST,'source'))['status']=='open'
            assert (await store.list_watch_wake('wake',SOURCE,source['session_generation']))[0]['id']==old['id']
            assert (await store.list_watch_wake('wake',HOST+':successor',target['session_generation']))[0]['id']==new['id']
    asyncio.run(run())


def test_closed_owner_retains_all_custody_for_manual_recovery():
    async def run():
        async with environment() as (store,sessions,ctl,server,tmux,opened):
            source=await store.fetch_session(HOST,'source');gen=source['session_generation']
            target=await opened('successor')
            await store.register_watch_wake('wake',SOURCE,gen,{'request_id':'fired','due_at':101,'note':'verbatim','urgent':False},now=100)
            cancelled=await store.register_watch_wake('wake',SOURCE,gen,{'request_id':'cancelled','due_at':200,'note':'cancel','urgent':False},now=100)
            await store.cancel_watch_wake('wake',SOURCE,gen,cancelled['id'],request_id='cancel-receipt')
            await store.evaluate_watch_wake({},now=101)
            await store.update_session(HOST,'source',status='closed')
            def custody(conn):
                return {table:[dict(r) for r in conn.execute('SELECT * FROM '+table)]
                        for table in ('v2_watch_wake','v2_watch_facts','v2_watch_cancellations')}
            before=await store.submit(custody)
            assert await store.transfer_watch_custody(SOURCE,gen,HOST+':successor',target['session_generation'])==0
            assert await store.submit(custody)==before
    asyncio.run(run())
