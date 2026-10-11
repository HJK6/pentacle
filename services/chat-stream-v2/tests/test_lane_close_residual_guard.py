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
                done=await env.op('set_state',{'to':'done','outcome':'U1 tracked separately'},lane=lane['lane_id'],version=1)
                assert done['lane']['work_state']=='done'
            seat=await env.seat(role+'-'+stage,role=role,spec_id=MEMBER)
            sessions,tmux=await close_context(env,seat)
            result=await sessions.close(*seat[0].split(':',1))
            assert result['session']['status']=='closed' and not result['failed']
    run(body)


@pytest.mark.parametrize('option',['requires_hidden','requires_idle','defer_if_working'])
@pytest.mark.parametrize('remote',[False,True],ids=['local','remote'])
@pytest.mark.parametrize('allows',[False],ids=['preserve_decision'])
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


@pytest.mark.parametrize('status',['backlog','analysis','ready_for_dev','in_progress','needs_qa','blocked'])
def test_each_nonterminal_followup_status(tmp_path,monkeypatch,status):
    write_spec(tmp_path,custom=TRACKED)
    write_spec(tmp_path,FOLLOW,status=status,custom=CHECKED,tracking=RECEIPT)
    api=importlib.import_module('_shared.custom_ac')
    assert api.check_lane_members(tmp_path,[MEMBER])['dispositions'][0]['owner']=='fixture-owner'


@pytest.mark.parametrize('prefix',['1. ', '1) '])
@pytest.mark.parametrize('through_lane',[False],ids=['parser'])
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
