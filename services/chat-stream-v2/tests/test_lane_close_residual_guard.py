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


@pytest.fixture
def tmp_path(tmp_path):
    """Use the owned canonical root on platforms whose temp path is an alias."""
    return tmp_path.resolve()


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
                write_spec(tmp_path, status=status, custom=CHECKED)
                done=await env.op('set_state',{'to':'done','outcome':'U1 tracked separately'},lane=lane['lane_id'],version=1)
                assert done['lane']['work_state']=='done'
                write_spec(tmp_path, status=status, custom=UNCHECKED)
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


@pytest.mark.parametrize('route', ['lane', 'local', 'remote', 'offline_operator', 'offline_manager'])
def test_restored_completion_refuses_undisposed(tmp_path, monkeypatch, route):
    """RED at the pinned guard-absent base through the actual public routes."""
    root = tmp_path.resolve()
    write_spec(root)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(root))
    async def body(env):
        lead = await env.seat('guard-lead', role='lead', parent_stream_id=FD)
        lane = (await env.adopt(lead=lead, members=[MEMBER]))['lane']
        before = await env.store.get_work_lane(lane['lane_id'])
        with pytest.raises((ValueError, VerbError)) as caught:
            if route == 'lane':
                await env.op('set_state', {'to': 'done', 'outcome': 'Synthetic incomplete'},
                             lane=lane['lane_id'], version=1)
            else:
                sessions, tmux = await close_context(env, lead, remote=route != 'local',
                                                     online=not route.startswith('offline'))
                kwargs = ({'operator_confirm': True} if route == 'offline_operator' else
                          {'close_kind': 'manager_close'} if route == 'offline_manager' else {})
                await sessions.close(*lead[0].split(':', 1), **kwargs)
        assert caught.value.code == 'lane_residuals_undisposed'
        details = getattr(caught.value, 'details', None) or caught.value.extra
        assert details['items'] == [
            {'spec_id': MEMBER, 'line': 7, 'criterion': 'Finish', 'reason': 'undisposed'}]
        after = await env.store.get_work_lane(lane['lane_id'])
        assert after['lane'] == before['lane']
        assert after['events'] == before['events']
        assert (await env.store.fetch_session(*lead[0].split(':', 1)))['status'] == 'open'
    run(body)


def test_generic_operation_hook_is_harness_error(tmp_path, monkeypatch):
    """A bare-name hook fires on authentication, before any preparation."""
    path = write_spec(tmp_path.resolve(), custom=CHECKED)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path.resolve()))
    async def body(env):
        lane = (await env.adopt(members=[MEMBER]))['lane']
        submit = env.store.submit
        receipt = []
        async def invalid_hook(callback):
            if callback.__name__ == '_op':
                path.write_text(path.read_text().replace('[x]', '[ ]'))
                receipt.append({'callback': callback.__qualname__, 'mutation': 'before_preparation'})
                raise AssertionError('HARNESS_ERROR: generic operation hook mutated before preparation')
            return await submit(callback)
        monkeypatch.setattr(env.store, 'submit', invalid_hook)
        with pytest.raises(AssertionError, match='^HARNESS_ERROR:'):
            await env.op('set_state', {'to': 'done', 'outcome': 'Invalid seam'},
                         lane=lane['lane_id'], version=1)
        assert len(receipt) == 1
        assert receipt[0]['callback'] != '_WorkLanesStoreMixin.apply_work_lane_operation.<locals>._op'
        print('HARNESS_ERROR_RECEIPT', json.dumps(receipt, sort_keys=True))
        monkeypatch.setattr(env.store, 'submit', submit)
        assert (await env.store.get_work_lane(lane['lane_id']))['lane']['work_state'] == 'active'
    run(body)


LANE_CALLBACK = '_WorkLanesStoreMixin.apply_work_lane_operation.<locals>._op'
ADMISSION_CALLBACK = '_WorkLanesStoreMixin.revalidate_lead_lane_completion.<locals>._op'
CLOSE_CALLBACK = 'Store.mark_closed.<locals>.transaction'
CLOSE_ENVELOPE = '_PreparedLaneCloseStore.submit.<locals>._op'


class PreparedRace:
    """Exact qualified-name seam, with captured bytes and monotonic order proof."""
    def __init__(self, env, monkeypatch, path, mutate, *, route='lane', callback=None):
        import itertools
        import functools
        self.order, self.prepared, self.fingerprints = [], None, {}
        self.before_bytes = path.read_bytes()
        self.path, self.callback = path, callback or (LANE_CALLBACK if route == 'lane' else CLOSE_CALLBACK)
        self.sequence = itertools.count(1)
        self.original_submit = env.store.submit
        self.prepare_name = 'prepare_work_lane_completion' if route == 'lane' else 'prepare_lead_lane_completion'
        prepare_call = getattr(env.store, self.prepare_name)
        async def preparation(*args, **kwargs):
            result = await prepare_call(*args, **kwargs)
            assert self.prepared is None, 'HARNESS_ERROR: preparation ran more than once'
            self.prepared = result
            self.mark('prepared')
            return result
        def mutate_before_submission(call):
            assert call.__qualname__ == self.callback, 'HARNESS_ERROR: wrong completion callback'
            assert self.prepared is not None, 'HARNESS_ERROR: mutation before preparation'
            assert not any(name == 'mutation' for _, name in self.order), 'HARNESS_ERROR: repeated callback'
            consumed = {item.path: item for item in self.prepared.inputs.files}
            assert path in consumed and consumed[path].content == self.before_bytes
            assert self.prepared.inputs.refusal is None
            assert any(item['disposition'] in {'checked', 'tracked'}
                       for item in self.prepared.inputs.dispositions)
            from _shared.specs_service import SpecsSubsystem
            self.fingerprints['before'] = consumed[path].fingerprint
            return mutate(self.prepared)

        def mutated():
            from _shared.specs_service import SpecsSubsystem
            self.mark('mutation')
            self.fingerprints['after'] = SpecsSubsystem._identity_fingerprint(path)

        async def enqueue(call):
            self.mark('enqueue')
            @functools.wraps(call)
            def callback(conn):
                self.mark('callback')
                return call(conn)
            return await self.original_submit(callback)

        async def submit(call):
            if self.callback == CLOSE_CALLBACK and call.__qualname__ == CLOSE_ENVELOPE:
                closure = dict(zip(call.__code__.co_freevars, [cell.cell_contents for cell in call.__closure__]))
                assert closure['transaction'].__qualname__ == CLOSE_CALLBACK
                assert sum(name == 'mutation' for _, name in self.order) == 1
                return await enqueue(call)
            if call.__qualname__ != self.callback:
                if call.__qualname__ == ADMISSION_CALLBACK:
                    @functools.wraps(call)
                    def admitted(conn):
                        result = call(conn)
                        self.mark('admitted')
                        return result
                    return await self.original_submit(admitted)
                return await self.original_submit(call)
            changed = mutate_before_submission(call)
            if hasattr(changed, '__await__'):
                await changed
            mutated()
            return await enqueue(call)

        self.original_close_submit = None
        if self.callback == CLOSE_CALLBACK:
            from store_work_lanes import _PreparedLaneCloseStore
            self.original_close_submit = _PreparedLaneCloseStore.submit
            async def close_submission(view, transaction):
                if view.store is not env.store or view.prepared is not self.prepared:
                    return await self.original_close_submit(view, transaction)
                changed = mutate_before_submission(transaction)
                if hasattr(changed, '__await__'):
                    await changed
                mutated()
                return await self.original_close_submit(view, transaction)
            monkeypatch.setattr(_PreparedLaneCloseStore, 'submit', close_submission)
        monkeypatch.setattr(env.store, self.prepare_name, preparation)
        monkeypatch.setattr(env.store, 'submit', submit)
        self.monkeypatch = monkeypatch
        self.prepare_call = prepare_call
        self.env = env

    def mark(self, name):
        self.order.append((next(self.sequence), name))

    def verify(self, label):
        core = [(seq, name) for seq, name in self.order if name in {'prepared', 'mutation', 'enqueue', 'callback'}]
        assert [name for _, name in core] == ['prepared', 'mutation', 'enqueue', 'callback'], (
            'HARNESS_ERROR: unmatched callback or incorrect order', self.order)
        assert [seq for seq, _ in core] == sorted(seq for seq, _ in core)
        print('RACE_SEAM_RECEIPT', json.dumps({
            'cell': label, 'callback': self.callback, 'order': self.order,
            'original_bytes_hex': self.before_bytes.hex(), 'fingerprints': self.fingerprints,
            'consumed': [{'path': str(item.path), 'bytes_hex': item.content.hex(),
                          'fingerprint': item.fingerprint} for item in self.prepared.inputs.files],
            'directories': [(str(path), value) for path, value in self.prepared.inputs.directories],
        }, sort_keys=True))
        self.monkeypatch.setattr(self.env.store, 'submit', self.original_submit)
        self.monkeypatch.setattr(self.env.store, self.prepare_name, self.prepare_call)
        if self.original_close_submit is not None:
            from store_work_lanes import _PreparedLaneCloseStore
            self.monkeypatch.setattr(_PreparedLaneCloseStore, 'submit', self.original_close_submit)


@pytest.mark.parametrize('mutation', ['criterion', 'follow_up', 'none', 'accepted_duplicate_gap'])
def test_prepared_lane_race_cells(tmp_path, monkeypatch, mutation):
    from _shared.custom_ac import CompletionRefusal, check_lane_members, _directory_fingerprint
    root = tmp_path.resolve()
    path = write_spec(root, custom=TRACKED if mutation == 'follow_up' else CHECKED)
    follow = write_spec(root, FOLLOW, status='backlog', custom=CHECKED, tracking=RECEIPT)
    unrelated = write_spec(root, OTHER, custom=CHECKED)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(root))
    async def body(env):
        lane = (await env.adopt(state='paused', owner='operator', members=[MEMBER]))['lane']
        payload = {'to': 'done', 'outcome': 'Prepared synthetic completion',
                   'operator_confirmation': env.confirm('race-confirm', lane['lane_id'], 'set_state:done')}
        before = await env.store.get_work_lane(lane['lane_id'])
        original = path.read_bytes(), follow.read_bytes()
        def mutate(prepared):
            if mutation == 'criterion':
                path.write_bytes(path.read_bytes().replace(b'[x]', b'[ ]'))
            elif mutation == 'follow_up':
                follow.write_bytes(follow.read_bytes().replace(b'fixture-owner', b'changed-owner'))
            elif mutation == 'accepted_duplicate_gap':
                captured = dict(prepared.inputs.directories)
                unrelated.write_text(unrelated.read_text().replace(OTHER, MEMBER))
                assert {directory: _directory_fingerprint(directory) for directory in captured} == captured
        seam = PreparedRace(env, monkeypatch, path, mutate)
        if mutation in {'criterion', 'follow_up'}:
            with pytest.raises(CompletionRefusal) as caught:
                await env.op('set_state', payload, lane=lane['lane_id'], version=1, request_id='race-done')
            assert caught.value.code == 'member_spec_unreadable'
            assert caught.value.details == {'retriable': True, 'items': [
                {'spec_id': MEMBER, 'criterion': None, 'reason': 'member_spec_unreadable'}]}
            assert await env.store.get_work_lane(lane['lane_id']) == before
            assert not any(event.get('consumed_question_id') for event in before['events'])
            seam.verify(mutation)
            path.write_bytes(original[0]); follow.write_bytes(original[1])
            result = await env.op('set_state', payload, lane=lane['lane_id'], version=1, request_id='race-done')
        else:
            result = await env.op('set_state', payload, lane=lane['lane_id'], version=1, request_id='race-done')
            seam.verify(mutation)
        assert result['lane']['work_state'] == 'done'
        if mutation == 'accepted_duplicate_gap':
            with pytest.raises(CompletionRefusal) as caught:
                check_lane_members(root, [MEMBER])
            assert caught.value.code == 'member_spec_unreadable'
        # Replay remains committed even if a new preparation finds bad input.
        path.write_bytes(path.read_bytes().replace(b'[x]', b'[ ]'))
        assert (await env.op('set_state', payload, lane=lane['lane_id'], version=1,
                             request_id='race-done'))['duplicate'] is True
        events = (await env.store.get_work_lane(lane['lane_id']))['events']
        assert sum(event.get('consumed_question_id') == 'race-confirm' for event in events) == 1
        assert sum(event.get('update_kind') == 'lane_completed' for event in events) == 1
    run(body)


def test_duplicate_already_present_refuses_without_spend(tmp_path, monkeypatch):
    from _shared.custom_ac import CompletionRefusal
    root = tmp_path.resolve()
    write_spec(root, custom=CHECKED)
    other = write_spec(root, OTHER, custom=CHECKED)
    other.write_text(other.read_text().replace(OTHER, MEMBER))
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(root))
    async def body(env):
        lane = (await env.adopt(state='paused', owner='operator', members=[MEMBER]))['lane']
        payload = {'to': 'done', 'outcome': 'Ambiguous',
                   'operator_confirmation': env.confirm('duplicate-confirm', lane['lane_id'], 'set_state:done')}
        before = await env.store.get_work_lane(lane['lane_id'])
        with pytest.raises(CompletionRefusal) as caught:
            await env.op('set_state', payload, lane=lane['lane_id'], version=1)
        assert caught.value.code == 'member_spec_unreadable'
        assert await env.store.get_work_lane(lane['lane_id']) == before
        assert not any(event.get('consumed_question_id') for event in before['events'])
    run(body)


@pytest.mark.parametrize('drift', ['version', 'members', 'actor_generation', 'binding_generation'])
def test_prepared_lane_drift_preserves_authority_and_conflict(tmp_path, monkeypatch, drift):
    root = tmp_path.resolve(); path = write_spec(root, custom=CHECKED)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(root))
    async def body(env):
        lane = (await env.adopt(state='paused', owner='operator', members=[MEMBER]))['lane']
        payload = {'to': 'done', 'outcome': 'Drift',
                   'operator_confirmation': env.confirm('drift-confirm', lane['lane_id'], 'set_state:done')}
        before = await env.store.get_work_lane(lane['lane_id'])
        async def mutate(prepared):
            def update(conn):
                if drift == 'version':
                    conn.execute('UPDATE v2_assistant_composite_lanes SET version=version+1 WHERE lane_id=?',
                                 (lane['lane_id'],))
                elif drift == 'members':
                    conn.execute('UPDATE v2_assistant_composite_lanes SET members_json=? WHERE lane_id=?',
                                 (json.dumps([OTHER]), lane['lane_id']))
                elif drift == 'actor_generation':
                    conn.execute('UPDATE v2_session_generations SET generation=? WHERE host=? AND session_name=?',
                                 ('new-generation', *FD.split(':', 1)))
                else:
                    conn.execute('INSERT OR REPLACE INTO v2_assistant_direct_binding '
                                 '(name,stream_id,generation,revision,updated_at) VALUES (?,?,?,?,?)',
                                 (env.composite.config.name, FD, 'new-binding-generation', 1, '2026-01-01T00:00:00Z'))
                conn.commit()
            await env.store.submit(update)
        seam = PreparedRace(env, monkeypatch, path, mutate)
        expected = 'assistant_lane_version_conflict' if drift in {'version', 'members'} else 'work_lane_actor_unverified'
        with pytest.raises(ValueError, match='^' + expected + '$'):
            await env.op('set_state', payload, lane=lane['lane_id'], version=1)
        seam.verify(drift)
        after = await env.store.get_work_lane(lane['lane_id'])
        assert after['events'] == before['events'] and after['lane']['work_state'] == 'paused'
        assert not any(event.get('consumed_question_id') for event in after['events'])
    run(body)


class OrderedTmux(QuietTmux):
    """Synthetic transport drives the real termination ladder, without a process."""
    def __init__(self, seam, *, failed_kill=False):
        super().__init__()
        self.live, self.seam, self.failed_kill = True, seam, failed_kill
        self.recorded_death = False

    def observation(self):
        if self.kills and not self.live and not self.recorded_death:
            self.seam.mark('confirmed_dead')
            self.recorded_death = True
        return self.live

    async def has_session(self, name):
        self.has_calls += 1
        return self.observation()

    async def session_state(self, name):
        return 'alive' if self.observation() else 'gone'

    async def kill_session(self, name):
        assert any(name == 'admitted' for _, name in self.seam.order), 'HARNESS_ERROR: kill before admission'
        self.seam.mark('terminate')
        self.kills += 1
        if not self.failed_kill:
            self.live = False


async def ordered_close_context(env, lead, seam, monkeypatch, route, *, failed_kill=False):
    sessions, _ = await close_context(env, lead, remote=route != 'local',
                                      online=not route.startswith('offline'))
    transport = OrderedTmux(seam, failed_kill=failed_kill)
    sessions.tmux = transport
    if sessions.hosts is not None:
        sessions.hosts.tmux = transport
    # Invented process inventory: retain the actual termination/control flow.
    monkeypatch.setattr(importlib.import_module('sessions').prockill, 'process_records', AsyncMock(return_value={}))
    monkeypatch.setattr(importlib.import_module('sessions'), 'CLOSE_GRACEFUL_CONFIRM_S', 0)
    kwargs = ({'operator_confirm': True} if route == 'offline_operator' else
              {'close_kind': 'manager_close'} if route == 'offline_manager' else {})
    return sessions, transport, kwargs


@pytest.mark.parametrize('route', ['local', 'remote', 'offline_operator', 'offline_manager'])
@pytest.mark.parametrize('stage', ['admission', 'commit'])
def test_close_prepared_race_order_and_late_source_flag(tmp_path, monkeypatch, route, stage):
    """Accepted amendment: admission first, confirmed-death flag, offline intent."""
    root = tmp_path.resolve(); path = write_spec(root, custom=CHECKED)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(root))
    async def body(env):
        lead = await env.seat('close-race', role='lead', parent_stream_id=FD)
        lane = (await env.adopt(lead=lead, members=[MEMBER]))['lane']
        before = await env.store.get_work_lane(lane['lane_id'])
        def mutation(prepared):
            assert sessions._graph_lock.locked()
            assert sessions._lifecycle_lock(*lead[0].split(':', 1)).locked()
            if stage == 'commit' and not route.startswith('offline'):
                assert any(name == 'confirmed_dead' for _, name in seam.order), 'HARNESS_ERROR: mutation before death'
            else:
                assert not any(name == 'terminate' for _, name in seam.order), 'HARNESS_ERROR: mutation after termination'
            path.write_bytes(path.read_bytes().replace(b'[x]', b'[ ]'))
        seam = PreparedRace(env, monkeypatch, path, mutation, route=route,
                            callback=ADMISSION_CALLBACK if stage == 'admission' else CLOSE_CALLBACK)
        sessions, tmux, kwargs = await ordered_close_context(env, lead, seam, monkeypatch, route)
        refused = stage == 'admission'
        flagged = stage == 'commit' and not route.startswith('offline')
        if refused:
            with pytest.raises(VerbError) as caught:
                await sessions.close(*lead[0].split(':', 1), **kwargs)
            assert caught.value.code == 'member_spec_unreadable'
            assert caught.value.extra == {'items': [
                {'spec_id': MEMBER, 'criterion': None, 'reason': 'member_spec_unreadable'}], 'retriable': True}
            assert tmux.kills == 0 and tmux.live
            assert (await env.store.fetch_session(*lead[0].split(':', 1)))['status'] == 'open'
        else:
            result = await sessions.close(*lead[0].split(':', 1), reason='Synthetic close reason', **kwargs)
            assert result['session']['status'] == 'closed' and not result['failed']
            sequence = {name: seq for seq, name in seam.order}
            if flagged:
                assert result['session']['completion_source_changed'] is True
                assert lead[1] in result['session']['completion_source_warning']
                assert tmux.kills == 1 and not tmux.live
                assert sequence['prepared'] < sequence['admitted'] < sequence['terminate'] < sequence['confirmed_dead']
                assert sequence['confirmed_dead'] < sequence['mutation'] < sequence['enqueue'] < sequence['callback']
            else:
                assert 'completion_source_changed' not in result['session']
                assert tmux.kills == 0 and tmux.live
                assert result['reap_status'] == 'deferred_host_offline'
                assert sequence['prepared'] < sequence['admitted'] < sequence['mutation'] < sequence['enqueue'] < sequence['callback']
                assert 'terminate' not in sequence and 'confirmed_dead' not in sequence
        seam.verify(f'close:{route}:{stage}')
        assert await env.store.get_work_lane(lane['lane_id']) == before  # A killed seat never marks its lane done.
        audit = await env.store.latest_close_audit(lead[0])
        assert audit['disposition'] == ('fenced' if refused else 'closed')
        assert ('u1_source_changed_after_termination:' in audit['reason']) is flagged
        if flagged:
            assert 'Synthetic close reason' in audit['reason'] and lead[1] in audit['reason']
            assert MEMBER in audit['reason']
            assert str(path.relative_to(root)) in audit['reason']
            from server import Server
            server = Server(store=env.store, sessions=sessions, local_host=sessions.local_host)
            inspected = await server._on_inspect_stream({'stream_id': lead[0], 'event_tail': 0})
            assert inspected['close_audit']['reason'] == audit['reason']
            # Existing same-generation replay creates no second close or flag.
            replay = await sessions.close(*lead[0].split(':', 1), expected_generation=lead[1])
            assert replay['already_closed'] is True
            assert await env.store.latest_close_audit(lead[0]) == audit
    run(body)


@pytest.mark.parametrize('route', ['local', 'remote'])
def test_failed_termination_never_closes_or_flags(tmp_path, monkeypatch, route):
    root = tmp_path.resolve(); path = write_spec(root, custom=CHECKED)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(root))
    async def body(env):
        lead = await env.seat('failed-kill', role='lead', parent_stream_id=FD)
        lane = (await env.adopt(lead=lead, members=[MEMBER]))['lane']
        seam = PreparedRace(env, monkeypatch, path, lambda prepared: None, route=route)
        sessions, tmux, kwargs = await ordered_close_context(env, lead, seam, monkeypatch, route, failed_kill=True)
        result = await sessions.close(*lead[0].split(':', 1), **kwargs)
        assert result['failed'] is True and tmux.kills == 1 and tmux.live
        assert [name for _, name in seam.order] == ['prepared', 'admitted', 'terminate']
        assert (await env.store.fetch_session(*lead[0].split(':', 1)))['status'] == 'open'
        assert await env.store.latest_close_audit(lead[0]) is None
        assert (await env.store.get_work_lane(lane['lane_id']))['lane']['work_state'] == 'active'
        print('CLOSE_ORDER_RECEIPT', json.dumps({'cell': f'failed-kill:{route}', 'order': seam.order}))
    run(body)


@pytest.mark.parametrize('route', ['local', 'remote', 'offline_operator', 'offline_manager'])
def test_close_generation_replaced_after_preparation_never_closes_replacement(tmp_path, monkeypatch, route):
    root = tmp_path.resolve(); path = write_spec(root, custom=CHECKED)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(root))
    async def body(env):
        lead = await env.seat('replaced-close', role='lead', parent_stream_id=FD)
        await env.adopt(lead=lead, members=[MEMBER])
        async def replacement(prepared):
            def update(conn):
                conn.execute('UPDATE v2_session_generations SET generation=? WHERE host=? AND session_name=?',
                             ('replacement-generation', *lead[0].split(':', 1)))
                conn.commit()
            await env.store.submit(update)
            path.write_bytes(path.read_bytes().replace(b'[x]', b'[ ]'))
        seam = PreparedRace(env, monkeypatch, path, replacement, route=route)
        sessions, tmux, kwargs = await ordered_close_context(env, lead, seam, monkeypatch, route)
        result = await sessions.close(*lead[0].split(':', 1), **kwargs)
        seam.verify('close-generation:' + route)
        assert result['stale_generation'] is True
        current = await env.store.fetch_session(*lead[0].split(':', 1))
        assert current['status'] == 'open' and current['session_generation'] == 'replacement-generation'
        assert await env.store.latest_close_audit(lead[0]) is None
    run(body)


def test_report_driven_refusal_keeps_durable_report(tmp_path, monkeypatch):
    from ledger import Ledger
    root = tmp_path.resolve(); write_spec(root)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(root))
    async def body(env):
        lead = await env.seat('report-guard', role='lead', parent_stream_id=FD)
        await env.adopt(lead=lead, members=[MEMBER])
        sessions, tmux = await close_context(env, lead)
        reply = await Ledger(env.store, sessions=sessions).report({
            'from_stream_id': lead[0], 'report_id': 'guard-durable-report', 'msg_id': 0, 'status': 'done',
            'summary': 'Synthetic durable report', 'findings': [], 'next_action': 'none', 'terminate': True})
        assert reply['durability_ack'] is True and reply['closed'] is False
        assert 'lane_residuals_undisposed' in reply['close_error']
        assert (await env.store.get_report('guard-durable-report'))['summary'] == 'Synthetic durable report'
        assert (await env.store.fetch_session(*lead[0].split(':', 1)))['status'] == 'open'
        assert tmux.kills == 0
    run(body)


@pytest.mark.parametrize('invalid', ['producer', 'lane', 'action', 'answer', 'actor_class', 'spent'])
def test_confirmation_validation_still_precedes_prepared_refusal(tmp_path, monkeypatch, invalid):
    write_spec(tmp_path)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path))
    async def body(env):
        lane = (await env.adopt(state='paused', owner='operator', members=[MEMBER]))['lane']
        question = env.confirm('invalid-confirm', lane['lane_id'], 'set_state:done')
        stored = env.confirmations['invalid-confirm']
        if invalid == 'producer': stored['producer_stream_id'] = 'fixture:other'
        elif invalid == 'lane': stored['work_lane_confirmation']['lane_id'] = 'another-lane'
        elif invalid == 'action': stored['work_lane_confirmation']['action'] = 'set_state:paused'
        elif invalid == 'answer': stored['answer'] = 'Decline'
        elif invalid == 'actor_class': stored['actor_class'] = 'seat'
        else:
            # Existing append-only consumed-question fact, not a new confirmation mechanism.
            await env.store.submit(lambda conn: (conn.execute(
                'UPDATE v2_work_lane_events SET consumed_question_id=? WHERE lane_id=?',
                ('invalid-confirm', lane['lane_id'])), conn.commit()))
        before = await env.store.get_work_lane(lane['lane_id'])
        expected = 'work_lane_operator_confirmation_consumed' if invalid == 'spent' else 'work_lane_operator_confirmation_mismatch'
        with pytest.raises(ValueError, match='^' + expected + '$'):
            await env.op('set_state', {'to': 'done', 'outcome': 'Invalid approval', 'operator_confirmation': question},
                         lane=lane['lane_id'], version=1)
        assert await env.store.get_work_lane(lane['lane_id']) == before
        assert await audits(env) == []
    run(body)


@pytest.mark.parametrize('route',['local','remote'])
@pytest.mark.parametrize('criterion',['placement','flag_marker','flag_path'])
def test_post_termination_metadata_and_warning_contract(tmp_path,monkeypatch,route,criterion):
 import functools,threading
 from _shared.custom_ac import PreparedCompletion
 from store import Store
 trace=[]; audits=[]; workers=set(); local=threading.local(); main=threading.get_ident()
 original_submit=Store.submit; original_revalidate=PreparedCompletion.revalidate; original_audit=Store.latest_close_audit
 async def submit(owner,call):
  workers.add(owner._thread.ident)
  if call.__qualname__ not in {ADMISSION_CALLBACK, '_PreparedLaneCloseStore.submit.<locals>._op'}:
   return await original_submit(owner,call)
  @functools.wraps(call)
  def invoke(conn):
   local.conn=conn;local.worker=owner._thread.ident
   try:return call(conn)
   finally:del local.conn;del local.worker
  return await original_submit(owner,invoke)
 def revalidate(owner):
  trace.append({'thread':threading.get_ident(),'store_workers':sorted(workers),'main_thread':main,'in_transaction':local.conn.in_transaction if hasattr(local,'conn') else False,'files':[str(item.path) for item in owner.files]})
  return original_revalidate(owner)
 async def latest_audit(owner,*a,**kw):
  value=await original_audit(owner,*a,**kw)
  if value:audits.append(value)
  return value
 monkeypatch.setattr(Store,'submit',submit);monkeypatch.setattr(PreparedCompletion,'revalidate',revalidate);monkeypatch.setattr(Store,'latest_close_audit',latest_audit)
 test_close_prepared_race_order_and_late_source_flag(tmp_path,monkeypatch,route,'commit')
 assert len(trace)==3,'HARNESS_ERROR: expected preparation, admission and exactly one post-death validation'
 post=trace[-1]
 row={'route':route,'criterion':criterion,'trace':trace,'audits':audits,'matched_callback':CLOSE_CALLBACK,'inner_callback':'Store.mark_closed.<locals>.transaction','real_fixture_passed':True}
 print('POST_TERMINATION_METADATA_RECEIPT',json.dumps(row,sort_keys=True))
 if criterion=='placement':
  assert post['thread'] not in post['store_workers'] and post['thread'] != main, 'Post-death source revalidation ran on Store worker'
  assert not post['in_transaction'], 'Post-death source revalidation ran inside write transaction'
 else:
  reason=audits[0]['reason']
  if criterion=='flag_marker':
   assert 'u1_source_changed_after_termination' in reason,'Published audit flag absent'
  else:
   relative_path=str(Path(post['files'][0]).relative_to(tmp_path))
   assert relative_path.startswith('work/'),'HARNESS_ERROR: consumed path not fixture-relative'
   assert relative_path in reason,'Affected source relative path absent'
