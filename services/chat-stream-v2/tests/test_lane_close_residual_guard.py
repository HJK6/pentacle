"""U1 read-only parsing, lane completion and real local/remote close guards."""
from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
from unittest.mock import AsyncMock
from pathlib import Path

import pytest

from sessions import Sessions, VerbError
from test_work_lanes import run, FD
from test_assistant_prose_mirror import ASSISTANT

MEMBER = 'spec_alpha__member'
FOLLOW = 'spec_alpha__follow'
OTHER = 'spec_alpha__other'
UNCHECKED = '- [ ] **Finish** — a concrete criterion'
CHECKED = '- [x] **Finish** — complete'
WAIVED = UNCHECKED + ' (waived: fixture decision)'
TRACKED = UNCHECKED + ' tracked: spec_alpha__follow (new_scope)'
RECEIPT = '- deferred_from: spec_alpha__member — Finish: a concrete fixture reason'
CASES = [
    ('checked', None, None), ('undisposed', 'lane_residuals_undisposed', 'undisposed'),
    ('waived', None, None), ('tracked', None, None),
    ('tracked_external', None, None), ('tracked_operator', None, None),
    ('terminal', 'lane_residuals_undisposed', 'follow_up_terminal'),
    ('deprecated', 'lane_residuals_undisposed', 'follow_up_terminal'),
    ('self', 'lane_residuals_undisposed', 'follow_up_is_self'),
    ('self_alias', 'lane_residuals_undisposed', 'follow_up_is_self'),
    ('bad_class', 'lane_residuals_undisposed', 'class_invalid'),
    ('missing_follow', 'lane_residuals_undisposed', 'follow_up_not_found'),
    ('unlinked', 'lane_residuals_undisposed', 'follow_up_unlinked'),
    ('empty_reason', 'lane_residuals_undisposed', 'follow_up_unlinked'),
    ('wrong_member', 'lane_residuals_undisposed', 'follow_up_unlinked'),
    ('wrong_criterion', 'lane_residuals_undisposed', 'follow_up_unlinked'),
    ('receipt_outside', 'lane_residuals_undisposed', 'follow_up_unlinked'),
    ('unowned', 'lane_residuals_undisposed', 'follow_up_unowned'),
    ('empty_owner', 'lane_residuals_undisposed', 'follow_up_unowned'),
    ('empty_custom', None, None), ('missing_custom', None, None),
    ('outside_checkbox', None, None),
    ('plain_unchecked', 'lane_residuals_undisposed', 'undisposed'),
    ('missing_member', 'member_spec_unreadable', 'member_spec_unreadable'),
    ('unreadable_member', 'member_spec_unreadable', 'member_spec_unreadable'),
    ('two_members', 'lane_residuals_undisposed', 'undisposed'),
    ('one_of_two', 'lane_residuals_undisposed', 'undisposed'),
]


def write_spec(root, spec_id=MEMBER, status='in_progress', custom=UNCHECKED, *, owner='fixture-owner', tracking=''):
    stem = spec_id.removeprefix('spec_')
    path = root / 'work' / status / stem / 'spec.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    front = f'---\nid: {spec_id}\n' + (f'owner: {owner}\n' if owner is not None else '') + '---\n'
    acceptance = '## Acceptance Criteria\n' + (f'### Custom\n{custom}\n' if custom is not None else '')
    path.write_text(front + acceptance + f'## Tracking\n{tracking}\n')
    return path


def prepare(root, case, monkeypatch):
    custom = {'checked': CHECKED, 'waived': WAIVED, 'empty_custom': 'No checkboxes.',
              'missing_custom': None, 'outside_checkbox': CHECKED,
              'plain_unchecked': '- [ ] not a named criterion'}.get(case, UNCHECKED)
    tracking_cases = {'tracked','tracked_external','tracked_operator','terminal','deprecated','self','self_alias',
                      'bad_class','missing_follow','unlinked','empty_reason','wrong_member','wrong_criterion',
                      'receipt_outside','unowned','empty_owner'}
    if case in tracking_cases:
        custom = TRACKED
        if case == 'tracked_external': custom = custom.replace('new_scope', 'external_event')
        if case == 'tracked_operator': custom = custom.replace('new_scope', 'operator_action')
        if case == 'bad_class': custom = custom.replace('new_scope', 'unfinished')
        if case == 'self': custom = custom.replace(FOLLOW, MEMBER)
        if case == 'self_alias': custom = custom.replace(FOLLOW, 'alpha__member')
        if case not in {'missing_follow','self','self_alias'}:
            receipt = RECEIPT
            if case == 'unlinked': receipt = ''
            if case == 'empty_reason': receipt = receipt.split(': a concrete')[0] + ':'
            if case == 'wrong_member': receipt = receipt.replace(MEMBER, OTHER)
            if case == 'wrong_criterion': receipt = receipt.replace('Finish:', 'Another:')
            if case == 'receipt_outside': receipt = '## Elsewhere\n' + receipt
            write_spec(root, FOLLOW, status={'terminal':'completed','deprecated':'deprecated'}.get(case,'backlog'),
                       custom=CHECKED, owner=None if case == 'unowned' else '""' if case == 'empty_owner' else 'fixture-receiver',
                       tracking=receipt)
    path = write_spec(root, custom=custom)
    if case == 'missing_member': path.unlink(); path.parent.rmdir()
    if case == 'outside_checkbox':
        path.write_text(path.read_text() + '\n- [ ] malformed outside Custom\n')
    if case == 'unreadable_member':
        original = Path.read_text
        def denied(self, *args, **kwargs):
            if self == path: raise PermissionError('synthetic deterministic member read denial')
            return original(self,*args,**kwargs)
        monkeypatch.setattr(Path,'read_text',denied)
    members = [MEMBER]
    if case in {'two_members','one_of_two'}:
        write_spec(root, OTHER, custom=UNCHECKED)
        if case == 'one_of_two': write_spec(root, MEMBER, custom=CHECKED)
        members = [OTHER, MEMBER]
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(root))
    return members, path


def assert_refusal(exc, code, reason, case, path):
    assert getattr(exc, 'code', None) == code
    items = exc.details['items']
    expected = [MEMBER, OTHER] if case == 'two_members' else [OTHER] if case == 'one_of_two' else [MEMBER]
    assert [item['spec_id'] for item in items] == expected
    assert all(item['reason'] == reason for item in items)
    if code == 'lane_residuals_undisposed':
        name = 'not a named criterion' if case == 'plain_unchecked' else 'Finish'
        assert all(item['criterion'] == name for item in items)
    if code == 'member_spec_unreadable': assert exc.details['retriable'] is True


@pytest.mark.parametrize('case,code,reason', CASES, ids=[row[0] for row in CASES])
def test_parser_cells(tmp_path, monkeypatch, case, code, reason):
    members,path = prepare(tmp_path,case,monkeypatch)
    api = importlib.import_module('_shared.custom_ac')
    if code:
        with pytest.raises(api.CompletionRefusal) as exc: api.check_lane_members(tmp_path,members)
        assert_refusal(exc.value,code,reason,case,path)
    else:
        result = api.check_lane_members(tmp_path,members)
        if case.startswith('tracked'):
            disposition = result['dispositions'][0]
            assert disposition['class'] == {'tracked':'new_scope','tracked_external':'external_event',
                                            'tracked_operator':'operator_action'}[case]
            assert disposition['reason'] == 'a concrete fixture reason'
            assert disposition['owner'] == 'fixture-receiver'


@pytest.mark.parametrize('case,code,reason', CASES, ids=[row[0] for row in CASES])
def test_real_lane_cells(tmp_path,monkeypatch,case,code,reason):
    members,path = prepare(tmp_path,case,monkeypatch)
    async def body(env):
        lane = (await env.adopt(members=members))['lane']
        before = await env.store.get_work_lane(lane['lane_id'])
        if code:
            with pytest.raises(ValueError) as exc:
                await env.op('set_state',{'to':'done','outcome':'Fixture complete'},lane=lane['lane_id'],version=1)
            assert_refusal(exc.value,code,reason,case,path)
            assert await env.store.get_work_lane(lane['lane_id']) == before
        else:
            done = await env.op('set_state',{'to':'done','outcome':'Fixture complete'},lane=lane['lane_id'],version=1)
            assert done['lane']['work_state'] == 'done'
    run(body)


class QuietTmux:
    def __init__(self): self.kills = 0; self.live = False; self.has_calls = 0; self.capture_calls = 0; self.capture_text = 'ready\n'
    async def has_session(self,name): self.has_calls += 1; return self.live
    async def session_state(self,name): return 'alive' if self.live else 'gone'
    async def kill_session(self,name): self.kills += 1; self.live = False
    async def pane_pid(self,name): return ''
    async def capture_checked(self,name,**kwargs):
        self.capture_calls += 1
        return True,self.capture_text


class Peers:
    def __init__(self,tmux,online=True): self.tmux=tmux; self.online=online; self.probes=0
    def is_local(self,host): return host == 'local-fixture'
    def known(self,host): return host == 'host-a'
    def tmux_for(self,host): return self.tmux
    async def probe_once(self,host): self.probes += 1; return self.online


async def close_context(env,lead,*,remote=False,online=True):
    tmux=QuietTmux()
    sessions=Sessions(env.store,tmux=tmux,local_host='local-fixture' if remote else 'host-a',
                      hosts=Peers(tmux,online) if remote else None)
    await sessions.refresh()
    return sessions,tmux


async def audits(env,close=False):
    table='v2_close_audit' if close else 'v2_lifecycle_authority_audit'
    return await env.store.submit(lambda conn:[dict(row) for row in conn.execute('SELECT * FROM '+table)])


@pytest.mark.parametrize('role,status',[('worker','in_progress'),('qa','analysis'),('planner','in_progress'),('helper','in_progress')])
def test_other_roles_close_with_residuals(tmp_path,monkeypatch,role,status):
    write_spec(tmp_path,status=status); monkeypatch.setenv('PENTACLE_MEMORY_ROOT',str(tmp_path))
    async def body(env):
        lane=(await env.adopt(members=[MEMBER]))['lane']
        for stage in ('before','after'):
            if stage=='after':
                with pytest.raises(ValueError) as exc:
                    await env.op('set_state',{'to':'done','outcome':'Incomplete'},lane=lane['lane_id'],version=1)
                assert exc.value.code=='lane_residuals_undisposed'
            seat=await env.seat(role+'-'+stage,role=role,spec_id=MEMBER)
            sessions,tmux=await close_context(env,seat)
            result=await sessions.close(*seat[0].split(':',1))
            assert result['session']['status']=='closed' and not result['failed']
    run(body)


@pytest.mark.parametrize('remote',[False,True],ids=['local','remote'])
def test_open_lane_lead_close_refuses_and_audits(tmp_path,monkeypatch,remote):
    write_spec(tmp_path);monkeypatch.setenv('PENTACLE_MEMORY_ROOT',str(tmp_path))
    async def body(env):
        lead=await env.seat('lead',role='lead',parent_stream_id=FD)
        lane=(await env.adopt(lead=lead,members=[MEMBER]))['lane']
        sessions,tmux=await close_context(env,lead,remote=remote)
        before=await audits(env,close=True)
        with pytest.raises(VerbError) as exc: await sessions.close(*lead[0].split(':',1))
        assert exc.value.code=='lane_residuals_undisposed'
        assert exc.value.extra['items'][0]['spec_id']==MEMBER
        assert (await env.store.fetch_session(*lead[0].split(':',1)))['status']=='open'
        rows=await audits(env,close=True)
        assert len(rows)==len(before)+1
        assert rows[-1]['disposition']=='fenced' and 'lane_residuals_undisposed' in rows[-1]['reason']
        assert tmux.kills==0
    run(body)


def test_lane_refusal_audit_is_committed(tmp_path,monkeypatch):
    write_spec(tmp_path);monkeypatch.setenv('PENTACLE_MEMORY_ROOT',str(tmp_path))
    async def body(env):
        lane=(await env.adopt(members=[MEMBER]))['lane']
        before=await env.store.get_work_lane(lane['lane_id'])
        prior=await audits(env)
        try: await env.op('set_state',{'to':'done','outcome':'Incomplete'},lane=lane['lane_id'],version=1)
        except ValueError: pass
        rows=await audits(env)
        assert len(rows)==len(prior)+1, 'completion refusal audit row missing'
        assert rows[-1]['refusal_code']=='lane_residuals_undisposed'
        assert MEMBER in rows[-1]['reason'] and 'Finish' in rows[-1]['reason']
        assert await env.store.get_work_lane(lane['lane_id'])==before
    run(body)


@pytest.mark.parametrize('option',['requires_hidden','requires_idle','defer_if_working'])
@pytest.mark.parametrize('remote',[False,True],ids=['local','remote'])
@pytest.mark.parametrize('allows',[False,True],ids=['preserve_decision','residual_refuses'])
def test_current_lead_close_decision_order(tmp_path,monkeypatch,option,remote,allows):
    write_spec(tmp_path);monkeypatch.setenv('PENTACLE_MEMORY_ROOT',str(tmp_path))
    async def body(env):
        lead=await env.seat('lead',role='lead',parent_stream_id=FD)
        await env.adopt(lead=lead,members=[MEMBER])
        sessions,tmux=await close_context(env,lead,remote=remote)
        probe=AsyncMock(wraps=sessions._probe_capture_liveness)
        monkeypatch.setattr(sessions,'_probe_capture_liveness',probe)
        tmux.capture_text='ready\n' if allows else 'Working (esc to interrupt)\n'
        if option=='requires_hidden' and allows:
            await sessions.set_visibility(*lead[0].split(':',1),'hidden')
        if allows:
            with pytest.raises(VerbError) as exc: await sessions.close(*lead[0].split(':',1),**{option:True})
            assert exc.value.code=='lane_residuals_undisposed'
        else:
            result=await sessions.close(*lead[0].split(':',1),**{option:True})
            if option=='requires_hidden': assert result['visibility_changed'] is True
            elif option=='requires_idle': assert result['reason'].startswith('reap_fenced')
            else:
                assert result['deferred'] is True
                assert (await env.store.latest_close_audit(lead[0]))['disposition']=='deferred'
        assert probe.await_count==(0 if option=='requires_hidden' else 1)
        assert tmux.capture_calls==probe.await_count
        assert tmux.kills==0
        assert (await env.store.fetch_session(*lead[0].split(':',1)))['status']=='open'
    run(body)


@pytest.mark.parametrize('kind',['operator','manager'])
def test_remote_offline_logical_close_refuses_residuals(tmp_path,monkeypatch,kind):
    write_spec(tmp_path);monkeypatch.setenv('PENTACLE_MEMORY_ROOT',str(tmp_path))
    async def body(env):
        lead=await env.seat('lead',role='lead',parent_stream_id=FD)
        await env.adopt(lead=lead,members=[MEMBER])
        sessions,tmux=await close_context(env,lead,remote=True,online=False)
        kwargs={'close_kind':'manager_close'} if kind=='manager' else {'operator_confirm':True}
        with pytest.raises(VerbError) as exc: await sessions.close(*lead[0].split(':',1),**kwargs)
        assert exc.value.code=='lane_residuals_undisposed'
        assert (await env.store.fetch_session(*lead[0].split(':',1)))['status']=='open'
        assert await env.store.get_deferred_reap(lead[0]) is None
        assert sessions.hosts.probes==1 and tmux.kills==0
    run(body)


def test_done_lane_lead_closes(tmp_path,monkeypatch):
    write_spec(tmp_path,custom=WAIVED);monkeypatch.setenv('PENTACLE_MEMORY_ROOT',str(tmp_path))
    async def body(env):
        lead=await env.seat('lead',role='lead',parent_stream_id=FD)
        lane=(await env.adopt(lead=lead,members=[MEMBER]))['lane']
        await env.op('set_state',{'to':'done','outcome':'Disposition recorded'},lane=lane['lane_id'],version=1)
        write_spec(tmp_path,custom=UNCHECKED)
        sessions,tmux=await close_context(env,lead)
        assert (await sessions.close(*lead[0].split(':',1)))['session']['status']=='closed'
    run(body)


def test_pause_relead_and_former_lead_close(tmp_path,monkeypatch):
    write_spec(tmp_path);monkeypatch.setenv('PENTACLE_MEMORY_ROOT',str(tmp_path))
    async def body(env):
        lead=await env.seat('old',role='lead',parent_stream_id=FD)
        lane=(await env.adopt(lead=lead,members=[MEMBER]))['lane']
        paused=await env.op('set_state',{'to':'paused'},lane=lane['lane_id'],version=1)
        successor=await env.seat('new',role='lead',parent_stream_id=FD)
        await env.op('set_lead',{'lead':{'stream_id':successor[0],'generation':successor[1]}},lane=lane['lane_id'],version=2)
        sessions,tmux=await close_context(env,lead)
        assert (await sessions.close(*lead[0].split(':',1)))['session']['status']=='closed'
    run(body)


@pytest.mark.parametrize('method',['confirmation','reason'])
def test_operator_precedence_and_confirmation_rollback(tmp_path,monkeypatch,method):
    write_spec(tmp_path);monkeypatch.setenv('PENTACLE_MEMORY_ROOT',str(tmp_path))
    async def body(env):
        lane=(await env.adopt(state='paused',lead=False,owner='operator',members=[MEMBER]))['lane']
        payload={'to':'done','outcome':'Fixture done'}
        with pytest.raises(ValueError,match='^work_lane_override_reason_required$'):
            await env.op('set_state',payload,lane=lane['lane_id'],version=1)
        if method=='confirmation': payload['operator_confirmation']=env.confirm('fixture-confirm',lane['lane_id'],'set_state:done')
        else: payload['reason']='Fixture operator decision'
        before=await env.store.get_work_lane(lane['lane_id'])
        with pytest.raises(ValueError) as exc:
            await env.op('set_state',payload,lane=lane['lane_id'],version=1,request_id='completion-retry')
        assert exc.value.code=='lane_residuals_undisposed'
        assert await env.store.get_work_lane(lane['lane_id'])==before
        write_spec(tmp_path,custom=WAIVED)
        done=await env.op('set_state',payload,lane=lane['lane_id'],version=1,request_id='completion-retry')
        assert done['lane']['work_state']=='done'
        assert (await env.op('set_state',payload,lane=lane['lane_id'],version=1,request_id='completion-retry'))['duplicate'] is True
        if method=='confirmation':
            events=(await env.store.get_work_lane(lane['lane_id']))['events']
            assert sum(e.get('consumed_question_id')=='fixture-confirm' for e in events)==1
    run(body)


def test_disposable_two_member_journey(tmp_path,monkeypatch):
    for member in (MEMBER,OTHER): write_spec(tmp_path,member)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT',str(tmp_path))
    async def body(env):
        lane=(await env.adopt(members=[OTHER,MEMBER]))['lane']
        for stage in ('before','after'):
            worker=await env.seat('worker-'+stage,role='worker',spec_id=MEMBER)
            sessions,tmux=await close_context(env,worker)
            assert (await sessions.close(*worker[0].split(':',1)))['session']['status']=='closed'
            if stage=='before':
                with pytest.raises(ValueError) as exc:
                    await env.op('set_state',{'to':'done','outcome':'Finish'},lane=lane['lane_id'],version=1)
                assert [item['spec_id'] for item in exc.value.details['items']]==[MEMBER,OTHER]
                print(json.dumps({'refusal':exc.value.code,'details':exc.value.details,'audit':await audits(env)}))
                write_spec(tmp_path,MEMBER,custom=TRACKED)
                write_spec(tmp_path,FOLLOW,status='backlog',custom=CHECKED,tracking=RECEIPT)
                write_spec(tmp_path,OTHER,custom=WAIVED)
                success=await env.op('set_state',{'to':'done','outcome':'Dispositions recorded'},lane=lane['lane_id'],version=1)
                assert success['lane']['work_state']=='done'
                print(json.dumps({'success':success,'audit':await audits(env)}))
    run(body)


def test_all_matching_lanes_and_only_members(tmp_path,monkeypatch):
    write_spec(tmp_path,MEMBER,custom=CHECKED);write_spec(tmp_path,OTHER)
    write_spec(tmp_path,'spec_alpha__unrelated')
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT',str(tmp_path))
    async def body(env):
        lead=await env.seat('lead',role='lead',parent_stream_id=FD,spec_id='spec_alpha__unrelated')
        first=(await env.adopt('stream:first',lead=lead,members=[MEMBER]))['lane']
        second=(await env.adopt('stream:second',lead=lead,members=[OTHER]))['lane']
        sessions,tmux=await close_context(env,lead)
        with pytest.raises(VerbError) as exc: await sessions.close(*lead[0].split(':',1))
        assert exc.value.code=='lane_residuals_undisposed'
        assert [item['spec_id'] for item in exc.value.extra['items']]==[OTHER]
        write_spec(tmp_path,OTHER,custom=WAIVED)
        assert (await sessions.close(*lead[0].split(':',1)))['session']['status']=='closed'
    run(body)


def test_generation_replacement_never_guards_wrong_generation(tmp_path,monkeypatch):
    write_spec(tmp_path);monkeypatch.setenv('PENTACLE_MEMORY_ROOT',str(tmp_path))
    async def body(env):
        lead=await env.seat('lead',role='lead',parent_stream_id=FD)
        await env.adopt(lead=lead,members=[MEMBER])
        await env.store.mark_closed(*lead[0].split(':',1),expected_generation=lead[1],closed_at='2026-01-01T00:00:00Z',pane_status='gone')
        replacement=await env.store.open_session('host-a','lead',role='lead',parent_stream_id=FD)
        sessions,tmux=await close_context(env,lead)
        stale=await sessions.close('host-a','lead',expected_generation=lead[1])
        assert stale['stale_generation'] is True
        assert (await env.store.fetch_session('host-a','lead'))['status']=='open'
        closed=await sessions.close('host-a','lead',expected_generation=replacement['session_generation'])
        assert closed['session']['status']=='closed'
    run(body)


def test_seat_without_open_member_lane_closes(tmp_path,monkeypatch):
    write_spec(tmp_path);monkeypatch.setenv('PENTACLE_MEMORY_ROOT',str(tmp_path))
    async def body(env):
        lead=await env.seat('unbound',role='lead',spec_id=MEMBER)
        sessions,tmux=await close_context(env,lead)
        assert (await sessions.close(*lead[0].split(':',1)))['session']['status']=='closed'
    run(body)


def test_report_driven_refusal_preserves_durable_report(tmp_path,monkeypatch):
    from ledger import Ledger
    write_spec(tmp_path);monkeypatch.setenv('PENTACLE_MEMORY_ROOT',str(tmp_path))
    async def body(env):
        lead=await env.seat('lead',role='lead',parent_stream_id=FD)
        await env.adopt(lead=lead,members=[MEMBER])
        sessions,tmux=await close_context(env,lead)
        reply=await Ledger(env.store,sessions=sessions).report({
            'from_stream_id':lead[0],'report_id':'fixture-durable-report','msg_id':0,'status':'done',
            'summary':'Fixture report remains durable','findings':[],'next_action':'none','terminate':True})
        assert reply['durability_ack'] is True
        assert reply['closed'] is False and 'lane_residuals_undisposed' in reply['close_error']
        report=await env.store.get_report('fixture-durable-report')
        assert report is not None and report['summary']=='Fixture report remains durable'
        assert (await env.store.fetch_session(*lead[0].split(':',1)))['status']=='open'
        assert tmux.kills==0
    run(body)


def test_server_maps_structured_completion_refusal(tmp_path,monkeypatch):
    from server import Server
    write_spec(tmp_path);monkeypatch.setenv('PENTACLE_MEMORY_ROOT',str(tmp_path))
    async def body(env):
        lane=(await env.adopt(members=[MEMBER]))['lane']
        sessions=Sessions(env.store,tmux=None,local_host='fixture-root')
        server=Server(store=env.store,sessions=sessions,local_host='fixture-root')
        server.assistant_composite=env.composite
        msg={'type':'assistant.operation','request_id':'wire-refusal','composite_stream_id':ASSISTANT,
             'dispatch_id':'none','operation':'work_lane.set_state','lane_id':lane['lane_id'],
             'expected_lane_version':1,'payload':{'to':'done','outcome':'Fixture complete'},
             '_auth_context':{'token_verified':True,'stream_id':FD,'session_generation':env.gen}}
        try:
            with pytest.raises(VerbError) as exc: await server._on_assistant_operation(msg)
            assert exc.value.code=='lane_residuals_undisposed'
            assert exc.value.extra['items'][0]['spec_id']==MEMBER
            assert exc.value.extra['items'][0]['criterion']=='Finish'
        finally: await server.close()
    run(body)


@pytest.mark.parametrize('status',['backlog','analysis','ready_for_dev','in_progress','needs_qa','blocked'])
def test_each_nonterminal_followup_status(tmp_path,monkeypatch,status):
    write_spec(tmp_path,custom=TRACKED)
    write_spec(tmp_path,FOLLOW,status=status,custom=CHECKED,tracking=RECEIPT)
    api=importlib.import_module('_shared.custom_ac')
    assert api.check_lane_members(tmp_path,[MEMBER])['dispositions'][0]['owner']=='fixture-owner'


@pytest.mark.parametrize('kind',['lane','close'])
def test_refusal_survives_store_restart(tmp_path,monkeypatch,kind):
    root=tmp_path/'memory';write_spec(root);monkeypatch.setenv('PENTACLE_MEMORY_ROOT',str(root))
    database=tmp_path/'fixture.db'
    record={}
    async def body(env):
        lead=await env.seat('lead',role='lead',parent_stream_id=FD)
        lane=(await env.adopt(lead=lead,members=[MEMBER]))['lane']
        record.update(lane_id=lane['lane_id'],lead=lead[0])
        before={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
        if kind=='lane':
            with pytest.raises(ValueError) as exc:
                await env.op('set_state',{'to':'done','outcome':'Incomplete'},lane=lane['lane_id'],version=1)
        else:
            sessions,tmux=await close_context(env,lead)
            with pytest.raises(VerbError) as exc: await sessions.close(*lead[0].split(':',1))
        assert exc.value.code=='lane_residuals_undisposed'
        assert {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}==before
    run(body,str(database))
    with sqlite3.connect(database) as conn:
        table='v2_lifecycle_authority_audit' if kind=='lane' else 'v2_close_audit'
        assert conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==1
        assert conn.execute('SELECT work_state FROM v2_assistant_composite_lanes WHERE lane_id=?',
                            (record['lane_id'],)).fetchone()[0]=='active'
        assert conn.execute("SELECT status FROM sessions WHERE host='host-a' AND session_name='lead'").fetchone()[0]=='open'


@pytest.mark.parametrize('prefix',['1. ', '1) '])
@pytest.mark.parametrize('through_lane',[False,True],ids=['parser','lane'])
def test_ordered_checkbox_remains_undisposed(tmp_path,monkeypatch,prefix,through_lane):
    line=prefix+'[ ] Finish the remaining work'
    path=write_spec(tmp_path,custom=line)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT',str(tmp_path))
    def check(exc):
        assert exc.code=='lane_residuals_undisposed'
        assert exc.details['items'][0]['criterion']=='Finish the remaining work'
        assert exc.details['items'][0]['line']==path.read_text().splitlines().index(line)+1
    if not through_lane:
        api=importlib.import_module('_shared.custom_ac')
        with pytest.raises(api.CompletionRefusal) as exc: api.check_lane_members(tmp_path,[MEMBER])
        check(exc.value)
    else:
        async def body(env):
            lane=(await env.adopt(members=[MEMBER]))['lane']
            with pytest.raises(ValueError) as exc:
                await env.op('set_state',{'to':'done','outcome':'Incomplete'},lane=lane['lane_id'],version=1)
            check(exc.value)
            assert (await env.store.get_work_lane(lane['lane_id']))['lane']['work_state']=='active'
        run(body)


LAYOUTS = ['Ship the outcome', '**Ship** with no dash', '**Ship** (evidence) — outcome',
           '**Ship — west** outcome', 'Ship `tool` output', 'Ship "(waived: example)" output',
           'Ship `(waived: example)` and `tracked: spec_alpha__follow (new_scope)`']


@pytest.mark.parametrize('body', LAYOUTS)
@pytest.mark.parametrize('mark', [' ', 'x', 'X'])
def test_plain_and_existing_layout_states(tmp_path, body, mark):
    api=importlib.import_module('_shared.custom_ac')
    write_spec(tmp_path,custom=f'- [{mark}] {body}')
    if mark==' ':
        with pytest.raises(api.CompletionRefusal) as exc: api.check_lane_members(tmp_path,[MEMBER])
        assert exc.value.code=='lane_residuals_undisposed'
        assert len(exc.value.details['items'])==1
    else:
        assert api.check_lane_members(tmp_path,[MEMBER])['dispositions'][0]['disposition']=='checked'


@pytest.mark.parametrize('prefix',['- ','* ','+ ','1. ','12) ','  - '])
def test_checked_task_list_variants(tmp_path,prefix):
    write_spec(tmp_path,custom=prefix+'[x] plain complete')
    importlib.import_module('_shared.custom_ac').check_lane_members(tmp_path,[MEMBER])


@pytest.mark.parametrize('text,allowed',[
    ('- [ ] Ship (waived: reason)',True),
    ('- [ ] Ship (waived: )',False),
    ('- [ ] Ship (waived: reason',False),
    ('- [ ] Ship (waived: one) (waived: two)',False),
    ('- [ ] Ship tracked: spec_alpha__follow (new_scope) (waived: reason)',False),
    ('- [ ]',False),('- [ ] (waived: reason)',False),
    ('- [ ] Ship `(waived: example)`',False),
    ('- [ ] Ship ``(waived: example)`` (waived: real)',True),
    ('- [ ] Ship "(waived: example)"',False),
    ("- [ ] Ship '(waived: example)' then proceed (waived: authorized)",True),
    ('- [ ] Ship “(waived: example)” (waived: real)',True),
    ('- [ ] Ship ‘(waived: example)’ (waived: real)',True),
    ("- [ ] Don't hide the real suffix (waived: real)",True),
    ('- [ ] Ship "unmatched example (waived: real)',True),
    ('- [ ] Ship "example \\"(waived: ignored)\\" text" (waived: real)',True),
])
def test_unchecked_dispositions_and_examples(tmp_path,text,allowed):
    write_spec(tmp_path,custom=text)
    api=importlib.import_module('_shared.custom_ac')
    if allowed: api.check_lane_members(tmp_path,[MEMBER])
    else:
        with pytest.raises(api.CompletionRefusal) as exc: api.check_lane_members(tmp_path,[MEMBER])
        assert exc.value.code=='lane_residuals_undisposed'
        assert len(exc.value.details['items'])==1


@pytest.mark.parametrize('custom',[
    '```text\n- [ ] example\n## Outside\n```\n- [x] Actual',
    '~~~\n- [ ] example\n~~~\n- [x] Actual',
    'Prose [ ] is not a task.\n`- [ ] example`\n- prose\n[ ] prose\n- [x] Actual',
    '- [x] Actual\n### Common\n- [ ] Common task\n#### Closure\n- [ ] closure',
    '- [x] Actual\n## Other\n- [ ] other task',
    '- [x] Actual\n# Other\n- [ ] other task',
])
def test_only_live_custom_task_criteria(tmp_path,custom):
    write_spec(tmp_path,custom=custom)
    importlib.import_module('_shared.custom_ac').check_lane_members(tmp_path,[MEMBER])


def test_custom_scope_excludes_common_before_and_keeps_nested_tasks(tmp_path):
    path=write_spec(tmp_path,custom='#### Nested\n- [ ] Real nested task')
    path.write_text(path.read_text().replace('### Custom','### Common\n- [ ] Common task\n#### Closure\n- [ ] closure\n   ### Custom ###'))
    api=importlib.import_module('_shared.custom_ac')
    with pytest.raises(api.CompletionRefusal) as exc: api.check_lane_members(tmp_path,[MEMBER])
    assert [x['criterion'] for x in exc.value.details['items']]==['Real nested task']


@pytest.mark.parametrize('label,identity',[
    ('Ship the outcome','Ship the outcome'),('Ship   the outcome','Ship the outcome'),
    ('**Ship** (proof) — outcome','Ship'),('Ship: west','Ship: west'),
    ('**Ship: west** outcome','Ship: west'),('Ship `tool` output','Ship `tool` output'),
])
def test_named_and_plain_receipt_identity(tmp_path,label,identity):
    write_spec(tmp_path,custom=f'- [ ] {label} tracked: {FOLLOW} (new_scope)')
    write_spec(tmp_path,FOLLOW,status='backlog',tracking=f'- deferred_from: {MEMBER} — {identity}: reason: external dependency')
    result=importlib.import_module('_shared.custom_ac').check_lane_members(tmp_path,[MEMBER])
    assert result['dispositions'][0]['criterion']==identity
    assert result['dispositions'][0]['reason']=='reason: external dependency'


@pytest.mark.parametrize('custom,receipt,reason',[
    (f'- [ ] Ship tracked: {FOLLOW} (new_scope)\n- [ ] Ship tracked: {FOLLOW} (new_scope)',
     f'- deferred_from: {MEMBER} — Ship: reason','criterion_ambiguous'),
    (f'- [ ] Ship tracked: {FOLLOW} (new_scope)\n- [ ] Ship: west tracked: {FOLLOW} (new_scope)',
     f'- deferred_from: {MEMBER} — Ship: west: delayed','follow_up_unlinked'),
    (f'- [x] Ship\n- [ ] Ship: west tracked: {FOLLOW} (new_scope)',
     f'- deferred_from: {MEMBER} — Ship: west: delayed','follow_up_unlinked'),
    (f'- [ ] Ship tracked: {FOLLOW} (new_scope)',
     f'```text\n- deferred_from: {MEMBER} — Ship: reason\n```','follow_up_unlinked'),
    (f'- [ ] Ship tracked: {FOLLOW} (new_scope)',
     f'- deferred_from: {MEMBER} — Other: reason','follow_up_unlinked'),
    ('- [ ] Ship deferred: waiting for external action','', 'undisposed'),
])
def test_receipts_never_dispose_ambiguous_or_unlinked_criteria(tmp_path,custom,receipt,reason):
    write_spec(tmp_path,custom=custom)
    write_spec(tmp_path,FOLLOW,status='backlog',tracking=receipt)
    api=importlib.import_module('_shared.custom_ac')
    with pytest.raises(api.CompletionRefusal) as exc:api.check_lane_members(tmp_path,[MEMBER])
    assert exc.value.code=='lane_residuals_undisposed'
    assert all(x['reason']==reason for x in exc.value.details['items'])


@pytest.mark.parametrize('route',['lane','local','remote','offline_operator','offline_manager'])
@pytest.mark.parametrize('case',['plain_checked','plain_unchecked','missing','unreadable'])
def test_compatible_criteria_and_missing_members_on_completion_paths(tmp_path,monkeypatch,route,case):
    path=write_spec(tmp_path,custom='- [x] Ship plain outcome' if case=='plain_checked' else '- [ ] Ship plain outcome')
    if case=='missing':path.unlink()
    if case=='unreadable':
        original=Path.read_text
        def denied(self,*a,**kw):
            if self==path:raise PermissionError('fixture member read denied')
            return original(self,*a,**kw)
        monkeypatch.setattr(Path,'read_text',denied)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT',str(tmp_path))
    expected='member_spec_unreadable' if case in {'missing','unreadable'} else 'lane_residuals_undisposed'
    async def body(env):
        lead=await env.seat('lead',role='lead',parent_stream_id=FD)
        lane=(await env.adopt(lead=lead,members=[MEMBER]))['lane']
        sessions,tmux=await close_context(env,lead,remote=route in {'remote','offline_operator','offline_manager'},online=not route.startswith('offline'))
        before=await env.store.get_work_lane(lane['lane_id'])
        async def action():
            if route=='lane':return await env.op('set_state',{'to':'done','outcome':'Checked work'},lane=lane['lane_id'],version=1)
            kw={'operator_confirm':True} if route=='offline_operator' else {'close_kind':'manager_close'} if route=='offline_manager' else {}
            return await sessions.close(*lead[0].split(':',1),**kw)
        if case=='plain_checked':
            result=await action()
            if route=='lane':assert result['lane']['work_state']=='done'
            else:assert result['session']['status']=='closed'
        else:
            with pytest.raises((ValueError,VerbError)) as exc:await action()
            assert exc.value.code==expected
            assert await env.store.get_work_lane(lane['lane_id'])==before
            assert (await env.store.fetch_session(*lead[0].split(':',1)))['status']=='open'
            assert tmux.kills==0
            assert len(await audits(env,close=route!='lane'))==1
    run(body)
