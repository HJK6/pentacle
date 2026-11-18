"""Pure brief contract and isolated gather failures."""
import pytest
from assistant_brief import compose, Section, Brief

@pytest.mark.parametrize('reason', ['bind','rebind','restore','compaction','daemon_start','requested'])
def test_compose_contract(reason):
    sections = {name: Section(name + ': 30', [name + ':' + '界'*180 for _ in range(30)])
                for name in ('owed','questions','rulings','wakes','usage','windows','lanes','seats')}
    text = compose({'at':'2026-11-18T00:00:00Z','reason':reason,'sections':sections})
    assert text.startswith('[front desk brief] 2026-11-18T00:00:00Z reason=' + reason)
    assert len(text.encode()) <= 6000
    for name in sections: assert name + ': 30' in text
    assert 'Commands:' in text
    assert 'seats:' + '界'*180 not in text
    assert 'owed:' + '界'*180 in text

@pytest.mark.asyncio
async def test_gather_isolates_failure():
    brief = Brief(None, None)
    async def fail(binding, now): raise RuntimeError('private details')
    async def ok(binding, now): return Section('ok: 1', ['ok'])
    brief.builders = {name:ok for name in brief.builders}
    brief.builders['owed'] = fail
    facts = await brief.gather('requested', binding={})
    text = compose(facts)
    assert 'Owed: unavailable (RuntimeError)' in text
    assert 'private details' not in text
    assert 'ok' in text


def test_fresh_store_builders_and_authorization(tmp_path):
    from test_assistant_owed import run, auth, ROOT
    from types import SimpleNamespace
    from sessions import VerbError
    from server import Server
    async def body(h):
        brief=Brief(h.store,h.composite)
        facts=await brief.gather('requested')
        for name in ('owed','wakes','windows','rulings','seats'):
            assert 'unavailable' not in facts['sections'][name].head,(name,facts['sections'][name])
        server=SimpleNamespace(store=h.store,_composite_for_message=lambda m:h.composite)
        assert (await Server._on_assistant_brief(server,{'_auth_context':auth(h)}))['type']=='assistant.brief.ok'
        assert (await Server._on_assistant_brief(server,{'_auth_context':{'operator_authenticated':True,'operator_principal':'operator:test'}}))['type']=='assistant.brief.ok'
        for bad in ({},dict(auth(h),session_generation='stale'),dict(auth(h),scoped_principal='scope')):
            with pytest.raises(VerbError,match='brief requires'):
                await Server._on_assistant_brief(server,{'_auth_context':bad})
        server.lane_rulings=SimpleNamespace(binding_conn=lambda c:dict(state='ready',stream_id=ROOT,generation=h.generation))
        # Bind a different FD; the original seat may read as ruling advisor only.
        h.composite.owed.binding=lambda c:dict(stream_id='other:desk',generation='other')
        assert (await Server._on_assistant_brief(server,{'_auth_context':auth(h)}))['type']=='assistant.brief.ok'
        server.lane_rulings=None
        with pytest.raises(VerbError):await Server._on_assistant_brief(server,{'_auth_context':auth(h)})
    run(tmp_path,body)


def test_wake_cancel_predicate(tmp_path):
    from test_assistant_owed import run,ROOT
    import json
    async def body(h):
        brief=Brief(h.store,h.composite,clock=lambda:1000)
        binding=await h.composite.binding()
        async def old(_):return dict(stream_id=ROOT,generation='old')
        brief.previous_binding=old
        def seed(c):
            for ident,kind,state,gen,due,note in [('ended','wake','cancelled','old',2000,'ended'),('manual','wake','cancelled','old',2000,'manual'),('rearmed','wake','cancelled','old',2000,'same'),('new','wake','active',h.generation,3000,'same'),('past','wake','cancelled','old',500,'past'),('watch','watch','cancelled','old',2000,'watch'),('wrong','wake','cancelled','older',2000,'wrong')]:
                c.execute('INSERT INTO v2_watch_wake(id,kind,owner,owner_generation,request_id,fingerprint,state,data) VALUES(?,?,?,?,?,?,?,?)',(ident,kind,ROOT,gen,ident,ident,state,json.dumps(dict(due_at=due,note=note))))
            c.execute('INSERT INTO v2_watch_cancellations VALUES(?,?,?,?,?)',(ROOT,'old','cancel','wake','manual'))
            c.commit()
        await h.store.submit(seed)
        section=await brief.wakes(binding,1000);text='\n'.join(section.render())
        assert '1 armed' in text and 'ended with previous binding: ended' in text and 'new due' in text
        for excluded in ('manual','rearmed','past','watch','wrong'):assert excluded not in text
    run(tmp_path,body)

@pytest.mark.asyncio
async def test_usage_cap_weekly_and_missing():
    from types import SimpleNamespace
    pools=[dict(provider='claude',pool_id='a',cap=dict(stop_pct=80,window='five_hour'),fresh=True,observation=dict(windows=dict(five_hour=dict(pct=60)))),dict(provider='codex',pool_id='b',cap=dict(blocked=True),fresh=False,observation=dict(windows={'codex:10080':dict(pct=70)})),dict(provider='claude',pool_id='c',fresh=False)]
    async def run(fn): return fn(SimpleNamespace(readback=lambda **kw:dict(quota_pools=pools)))
    brief=Brief(None,None,SimpleNamespace(account_runtime=SimpleNamespace(run=run,clock=lambda:1000)))
    text='\n'.join((await brief.usage({},1000)).render())
    assert 'cap 80%; five_hour 60%; fresh=True' in text
    assert 'cap blocked; codex:10080 70%; fresh=False' in text
    assert 'cap none; weekly unknown%; fresh=False' in text

@pytest.mark.asyncio
async def test_previous_binding_chooses_latest_source():
    import json
    from types import SimpleNamespace
    brief=Brief(None,SimpleNamespace(config=SimpleNamespace(name='primary')))
    data=[dict(old_binding_json=json.dumps(dict(stream_id='old',generation='g1')),created_at='2026-11-18T01:00:00Z')]
    restore=[dict(stream_id='current',generation='g2',updated_at='2026-11-18T02:00:00Z')]
    transfer=[dict(source='transfer',source_generation='g3',committed_at='2026-11-18T03:00:00Z')]
    async def rows(sql,args):
        if 'rebind' in sql:return data
        if 'restore' in sql:return restore
        return transfer
    brief.rows=rows
    binding=dict(stream_id='current',generation='new')
    assert (await brief.previous_binding(binding))['generation']=='g3'
    transfer.clear()
    assert (await brief.previous_binding(binding))['generation']=='g2'
    restore.clear()
    assert (await brief.previous_binding(binding))['generation']=='g1'


def test_order_caps_and_line_removal():
    from assistant_brief import ORDER,CAPS,COMMANDS
    sections={n:Section(n+': 99',[f'  {n}-{i}' for i in range(99)]) for n in ORDER}
    facts=dict(at='2026-11-18T00:00:00Z',reason='requested',sections=sections)
    text=compose(facts)
    assert [text.index(n+': 99') for n in ORDER]==sorted(text.index(n+': 99') for n in ORDER)
    for n in ORDER:
        if CAPS[n] is not None:assert f'  {n}-{CAPS[n]}\n' not in text
    assert text.endswith(COMMANDS)
    assert len(sections['seats'].lines)==99  # pure renderer


def test_noop_rebind_does_not_hide_predecessor(tmp_path):
    from test_assistant_owed import run
    import json
    async def body(h):
        brief=Brief(h.store,h.composite)
        binding=await h.composite.binding()
        def seed(c):
            for old,at in [(dict(stream_id='old:desk',generation='g-old'),'2026-11-18T01:00:00Z'),(binding,'2026-11-18T02:00:00Z')]:
                c.execute('INSERT INTO v2_assistant_rebind_audit(request_id,payload_digest,actor_stream_id,actor_generation,old_binding_json,new_binding_json,outcome,created_at) VALUES(?,?,?,?,?,?,?,?)',(at,at,'operator','operator',json.dumps(old),json.dumps(binding),'ok',at))
            c.commit()
        await h.store.submit(seed)
        assert (await brief.previous_binding(binding))['generation']=='g-old'
    run(tmp_path,body)


def test_missing_section_table_is_unavailable(tmp_path):
    from test_assistant_owed import run
    async def body(h):
        await h.store.submit(lambda c:c.execute('DROP TABLE holds'))
        facts=await Brief(h.store,h.composite).gather('requested')
        assert facts['sections']['windows'].head=='Windows: unavailable (OperationalError)'
        assert facts['sections']['owed'].head=='Owed: 0 open, 0 overdue'
    run(tmp_path,body)

@pytest.mark.asyncio
async def test_lanes_bounded_counts_and_generation_fenced_presence(monkeypatch):
    from types import SimpleNamespace
    brief=Brief(None,None,SimpleNamespace(sessions=SimpleNamespace(list_open=lambda:[dict(stream_id='h:a',session_generation='new',online=True)])))
    async def rows(sql,args=()):
        if 'GROUP BY' in sql:return [dict(work_state='active',n=20,parked=0),dict(work_state='paused',n=4,parked=2)]
        if 'LIMIT 13' in sql:return []
        assert 'LIMIT 12' in sql
        return [dict(lane_id='a',version=1,title='current',work_state='active',bound_stream_id='h:a',bound_generation='new',blocker=None),dict(lane_id='b',version=1,title='stale',work_state='blocked',bound_stream_id='h:a',bound_generation='old',blocker='waiting')]
    brief.rows=rows
    text='\n'.join((await brief.lanes({},1000)).render())
    assert '20 active/blocked; 4 paused; 2 parked' in text
    assert 'current active lead=h:a presence=idle online=True' in text
    assert 'stale blocked lead=h:a presence=transport_unknown online=False blocker=waiting' in text


@pytest.mark.parametrize('legacy_count', [1, 13])
def test_legacy_owner_loss_is_active_or_explicitly_unavailable(tmp_path, legacy_count):
    """Keep canonical owner-loss state without an unbounded board enrichment."""
    from types import SimpleNamespace
    from test_assistant_owed import run

    async def body(h):
        def seed(conn):
            for index in range(legacy_count):
                conn.execute(
                    "INSERT INTO v2_assistant_composite_lanes "
                    "(lane_id,stream_id,phase,created_at,updated_at,title,work_state,work_state_reason) "
                    "VALUES(?,?,'execution',?,?,'Lost owner','paused','lead_lost')",
                    (f'legacy-{index}', h.composite.config.stream_id,
                     '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
                )
            conn.commit()

        await h.store.submit(seed)
        server = SimpleNamespace(sessions=SimpleNamespace(list_open=lambda: []))
        facts = await Brief(h.store, h.composite, server).gather('requested')
        section = facts['sections']['lanes']
        if legacy_count == 1:
            assert section.head == 'Lanes: 1 active/blocked; 0 paused; 0 parked'
            assert 'Lost owner active' in '\n'.join(section.lines)
        else:
            assert section.head == 'Lanes: unavailable (RuntimeError)'
            assert section.lines == []
        assert facts['sections']['owed'].head == 'Owed: 0 open, 0 overdue'

    run(tmp_path, body)
