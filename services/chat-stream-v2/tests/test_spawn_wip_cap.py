"""U2 synthetic conformance and real admission, replay and cleanup contracts."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib
import json
from pathlib import Path

import pytest

from _shared.specs_service import SpecsSubsystem
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from store import Store
from test_spawn_error_no_phantom_row import BootReadyTmux

DATA = json.loads((Path(__file__).parent / 'fixtures/wip_cap_conformance.json').read_text())
ROWS = DATA['rows']
HOST = 'fixture'
VALID_OVERRIDE = '- wip_override: 2026-01-01 fixture-ref — fixture reason'


def materialize(root, row):
    work = root / 'work'
    work.mkdir(parents=True, exist_ok=True)
    if row['policy'] is not None:
        (work / 'wip_policy.json').write_text(json.dumps(row['policy']))
    candidate = row['candidate']
    locations = {stem: 'in_progress' for stem in row['in_progress']}
    locations[candidate['stem']] = candidate['status']
    for stem, status in locations.items():
        folder = work / status / stem
        folder.mkdir(parents=True, exist_ok=True)
        tracking = row['overrides'].get(stem, '')
        if stem == candidate['stem']:
            tracking = candidate.get('tracking', tracking)
        (folder / 'spec.md').write_text(
            f'---\nid: spec_{stem}\nowner: fixture-owner\n---\n'
            f'## Acceptance Criteria\n### Custom\n- [x] **Fixture** — complete\n'
            f'## Tracking\n{tracking}\n')
    return 'spec_' + candidate['stem']


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


class CountingTmux(BootReadyTmux):
    def __init__(self):
        super().__init__()
        self.created = 0
        self.names = set()

    async def new_session(self, *args, **kwargs):
        self.created += 1
        self.names.add(args[0])
        await super().new_session(*args, **kwargs)

    async def has_session(self, name): return name in self.names

    async def session_state(self, name): return "alive" if name in self.names else "gone"

    async def kill_session(self, name):
        self.names.discard(name)
        self.alive = bool(self.names)


async def setup(root, store, monkeypatch, resolved_role=None):
    tmux = CountingTmux()
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    specs = SpecsSubsystem(memory_root=root, session_summaries=lambda: [], changed_callback=lambda _: None)
    ctl = SpawnCtl(store, sessions, tmux=tmux, specs=specs)

    async def launch(msg, host, name):
        return 'run', {}, ({'role': resolved_role} if resolved_role is not None else {})

    monkeypatch.setattr(ctl, '_resolve_launch', launch)
    return ctl, sessions, tmux


def message(spec_id, role='lead', keyed=True):
    msg = {'objective': 'Exercise synthetic lead admission', 'session_name': 'candidate',
           'command': 'run', 'role': role}
    if spec_id is not None:
        msg['spec_id'] = spec_id
    if keyed:
        msg['idempotency_key'] = 'fixture-key'
    return msg


async def cleanup(store, sessions, tmux):
    expected = len(await store.list_sessions('open'))
    for row in await store.list_sessions('open'):
        await sessions.close(row['host'], row['session_name'])
    assert await store.list_sessions('open') == []
    assert not tmux.alive
    print(json.dumps({'cleanup': 'spawn', 'expected_seats': expected, 'closed_seats': expected,
                      'panes_created': tmux.created, 'panes_remaining': int(tmux.alive)}))


@pytest.mark.parametrize('row', ROWS, ids=lambda row: row['id'])
def test_conformance_rows(tmp_path, row):
    spec_id = materialize(tmp_path, row)
    before = files(tmp_path)
    api = importlib.import_module('_shared.wip_cap')
    actual = api.check_lead_spawn(tmp_path, spec_id)
    assert {key: actual[key] for key in row['expected']} == row['expected']
    assert files(tmp_path) == before
    if row['id'] == 'beta_isolation':
        beta_before = api.check_lead_spawn(tmp_path, 'spec_beta__item_000')
        assert beta_before['E_R'] == 10
        api.check_lead_spawn(tmp_path, spec_id)
        assert api.check_lead_spawn(tmp_path, 'spec_beta__item_000') == beta_before


@pytest.mark.parametrize('row', ROWS, ids=lambda row: row['id'])
@pytest.mark.parametrize('keyed', [False, True], ids=['keyless', 'keyed'])
def test_real_spawn_cells(tmp_path, monkeypatch, row, keyed):
    spec_id = materialize(tmp_path, row)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path))
    before = files(tmp_path)

    async def go():
        store = Store(':memory:'); store.start()
        ctl, sessions, tmux = await setup(tmp_path, store, monkeypatch)
        try:
            if row['expected']['error_code']:
                with pytest.raises(VerbError) as exc:
                    await ctl.spawn(message(spec_id, keyed=keyed), HOST)
                assert exc.value.code == row['expected']['error_code']
                assert str(exc.value) == row['expected']['message']
                assert await store.reservations(include_expired=True) == []
                assert await store.list_sessions('open') == []
                assert await store.get_spawn_outcome(HOST, 'candidate') is None
                assert tmux.created == 0
            else:
                reply = await ctl.spawn(message(spec_id, keyed=keyed), HOST)
                assert reply['type'] == 'spawn.ok'
                assert (await store.fetch_session(HOST, 'candidate'))['spec_id'] == spec_id
                assert tmux.created == 1
            assert files(tmp_path) == before
        finally:
            try:
                await cleanup(store, sessions, tmux)
            finally:
                store.stop()
    asyncio.run(go())


@pytest.mark.parametrize('role', ['worker', 'qa', 'planner', 'helper', 'dispatcher', 'nexus', ''])
@pytest.mark.parametrize('tree', ['over_cap', 'malformed_policy'])
def test_nonlead_roles_unaffected(tmp_path, monkeypatch, role, tree):
    row = next(row for row in ROWS if row['id'] == tree)
    spec_id = materialize(tmp_path, row)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path))
    async def go():
        store = Store(':memory:'); store.start()
        ctl, sessions, tmux = await setup(tmp_path, store, monkeypatch)
        try:
            reply = await ctl.spawn(message(spec_id, role), HOST)
            assert reply['type'] == 'spawn.ok' and tmux.created == 1
        finally:
            try: await cleanup(store, sessions, tmux)
            finally: store.stop()
    asyncio.run(go())


@pytest.mark.parametrize('tree', ['over_cap', 'malformed_policy'])
def test_lead_without_spec_unaffected(tmp_path, monkeypatch, tree):
    materialize(tmp_path, next(row for row in ROWS if row['id'] == tree))
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path))
    async def go():
        store = Store(':memory:'); store.start()
        ctl, sessions, tmux = await setup(tmp_path, store, monkeypatch)
        try:
            assert (await ctl.spawn(message(None), HOST))['type'] == 'spawn.ok'
        finally:
            try: await cleanup(store, sessions, tmux)
            finally: store.stop()
    asyncio.run(go())


@pytest.mark.parametrize('requested,resolved,refused', [('worker','lead',True), ('lead','worker',False)])
def test_resolved_role_controls_admission(tmp_path, monkeypatch, requested, resolved, refused):
    spec_id = materialize(tmp_path, ROWS[0])
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path))
    async def go():
        store = Store(':memory:'); store.start()
        ctl, sessions, tmux = await setup(tmp_path, store, monkeypatch, resolved_role=resolved)
        try:
            if refused:
                with pytest.raises(VerbError) as exc: await ctl.spawn(message(spec_id,requested), HOST)
                assert exc.value.code == 'wip_cap_exceeded' and tmux.created == 0
            else:
                assert (await ctl.spawn(message(spec_id,requested), HOST))['type'] == 'spawn.ok'
        finally:
            try: await cleanup(store, sessions, tmux)
            finally: store.stop()
    asyncio.run(go())


@pytest.mark.parametrize('state', ['in_flight', 'delivered'])
@pytest.mark.parametrize('tree', ['over_cap', 'malformed_policy'])
@pytest.mark.parametrize('operation', ['replay', 'conflict', 'fresh'])
def test_recorded_operation_precedes_new_admission(tmp_path, monkeypatch, state, tree, operation):
    row = next(row for row in ROWS if row['id'] == tree)
    spec_id = materialize(tmp_path, row)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path))
    async def go():
        store = Store(':memory:'); store.start()
        ctl, sessions, tmux = await setup(tmp_path, store, monkeypatch)
        msg = message(spec_id)
        try:
            assert (await store.atomic_claim_or_replay(HOST, 'original', idempotency_key='fixture-key',
                request_payload_hash=ctl._spawn_payload_hash(msg), ttl_s=60, request_id='original-request',
                nonce='fixture-nonce'))['status'] == 'claimed'
            if state == 'delivered':
                await store.open_session(HOST, 'original', objective='Synthetic prior admission', role='lead',
                                    spec_id=spec_id, provider='')
                tmux.alive = True
                tmux.names.add('original')
                await sessions.refresh()
                await store.set_spawn_outcome(HOST, 'original', 'delivered', request_id='original-request',
                    idempotency_key='fixture-key', request_payload_hash=ctl._spawn_payload_hash(msg),
                    delivery_receipt={'state':'not_requested'})
            before = await store.reservations(include_expired=True)
            if operation == 'conflict':
                msg['objective'] += ' changed'
                with pytest.raises(VerbError) as exc: await ctl.spawn(msg, HOST)
                assert exc.value.code == 'idempotency_key_conflict'
            elif operation == 'fresh':
                msg['idempotency_key'] = 'fresh-key'
                with pytest.raises(VerbError) as exc: await ctl.spawn(msg, HOST)
                assert exc.value.code == row['expected']['error_code']
                assert str(exc.value) == row['expected']['message']
                assert await store.fetch_session(HOST, 'candidate') is None
                assert await store.get_spawn_outcome(HOST, 'candidate') is None
            else:
                reply = await ctl.spawn(msg, HOST)
                assert reply['stream_id'] == HOST + ':original'
                assert reply.get('replayed') is True if state == 'delivered' else reply['state'] == 'starting'
            assert await store.reservations(include_expired=True) == before
            assert tmux.created == 0
        finally:
            try: await cleanup(store, sessions, tmux)
            finally: store.stop()
    asyncio.run(go())


@pytest.mark.parametrize('expired_payload', [False, True])
def test_expired_key_eligibility_retains_atomic_contract(tmp_path, monkeypatch, expired_payload):
    spec_id = materialize(tmp_path, ROWS[0])
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path))
    async def go():
        store = Store(':memory:'); store.start()
        ctl, sessions, tmux = await setup(tmp_path, store, monkeypatch)
        msg = message(spec_id)
        try:
            assert (await store.atomic_claim_or_replay(HOST, 'original', idempotency_key='fixture-key',
                request_payload_hash=ctl._spawn_payload_hash(msg), ttl_s=60))['status'] == 'claimed'
            if expired_payload:
                assert await store.record_spawn_intent(HOST, 'original', {'open_fields': {}, 'brief': ''})
            def expire(conn):
                conn.execute('UPDATE v2_stream_reservations SET expires_at=0'); conn.commit()
            await store.submit(expire)
            if expired_payload:
                assert (await ctl.spawn(msg, HOST))['stream_id'] == HOST + ':original'
            else:
                with pytest.raises(VerbError) as exc: await ctl.spawn(msg, HOST)
                assert exc.value.code == 'wip_cap_exceeded'
            assert tmux.created == 0
        finally:
            try: await cleanup(store, sessions, tmux)
            finally: store.stop()
    asyncio.run(go())


@pytest.mark.parametrize('tree',['over_cap','malformed_policy'])
@pytest.mark.parametrize('operation',['replay','conflict'])
def test_concurrent_key_claim_precedes_later_policy_refusal(tmp_path,monkeypatch,tree,operation):
    import threading
    import spawnctl
    spec_id=materialize(tmp_path,next(row for row in ROWS if row['id']=='at_cap'))
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT',str(tmp_path))
    async def go():
        store=Store(':memory:');store.start()
        ctl,sessions,tmux=await setup(tmp_path,store,monkeypatch)
        second_started=threading.Event();claimed=threading.Event();lock=threading.Lock()
        checks=0
        original_check=spawnctl.check_lead_spawn
        original_claim=store.atomic_claim_or_replay
        async def claim(*args,**kwargs):
            result=await original_claim(*args,**kwargs)
            if result['status']=='claimed': claimed.set()
            return result
        def check(*args,**kwargs):
            nonlocal checks
            with lock:
                checks+=1
                ordinal=checks
            if ordinal==1:
                result=original_check(*args,**kwargs)
                assert second_started.wait(5), 'second caller did not reach U2'
                return result
            second_started.set()
            assert claimed.wait(5), 'first caller did not claim'
            materialize(tmp_path,next(row for row in ROWS if row['id']==tree))
            return original_check(*args,**kwargs)
        monkeypatch.setattr(store,'atomic_claim_or_replay',claim)
        monkeypatch.setattr(spawnctl,'check_lead_spawn',check)
        first=message(spec_id);second=message(spec_id)
        if operation=='conflict': second['objective']+=' changed'
        try:
            replies=await asyncio.wait_for(asyncio.gather(ctl.spawn(first,HOST),ctl.spawn(second,HOST),
                                                         return_exceptions=True),10)
            # Either concurrent caller may win the existing atomic claim.
            successes=[reply for reply in replies if isinstance(reply,dict)]
            errors=[reply for reply in replies if isinstance(reply,Exception)]
            assert all(reply['type']=='spawn.ok' for reply in successes)
            if operation=='replay':
                assert len(successes)==2 and not errors, f'recorded operation lost replay precedence: {errors}'
                assert successes[0]['stream_id']==successes[1]['stream_id']
            else:
                assert len(successes)==1 and len(errors)==1
                assert isinstance(errors[0],VerbError) and errors[0].code=='idempotency_key_conflict'
            assert checks==2 and tmux.created==1
            assert (await store.spawn_record_for_key(HOST,'fixture-key'))['row']['session_name']=='candidate'
        finally:
            try:
                await ctl.drain_background_spawns()
                await cleanup(store,sessions,tmux)
            finally: store.stop()
    asyncio.run(go())


@pytest.mark.parametrize('prefix',['1. ','1) '])
@pytest.mark.parametrize('valid_also',[False,True],ids=['ignored','with-live-override'])
def test_ordered_override_text_matches_policy_parser(tmp_path,monkeypatch,prefix,valid_also):
    row=deepcopy(ROWS[0])
    row['candidate']['tracking']=(VALID_OVERRIDE+'\n' if valid_also else '')+prefix+VALID_OVERRIDE[2:]
    # Ordered prose is not an override declaration in the policy parser.
    if valid_also:
        row['expected']={'E_R':8,'error_code':None,'message':None,'warnings':[]}
    spec_id=materialize(tmp_path,row)
    api=importlib.import_module('_shared.wip_cap')
    actual=api.check_lead_spawn(tmp_path,spec_id)
    assert {key:actual[key] for key in row['expected']}==row['expected']
    test_real_spawn_cells(tmp_path,monkeypatch,row,True)


@pytest.mark.parametrize('which', ['candidate', 'peer'])
@pytest.mark.parametrize('failure', [OSError, ValueError])
def test_spec_read_failure_refuses_spawn_with_cause(tmp_path, monkeypatch, which, failure):
    """A read race is not a malformed override and cannot create a seat."""
    row = deepcopy(next(row for row in ROWS if row['id'] == 'at_cap'))
    spec_id = materialize(tmp_path, row)
    failing_spec = spec_id if which == 'candidate' else 'spec_' + row['in_progress'][0]
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path))
    api = importlib.import_module('_shared.wip_cap')
    original = api.SpecReader

    class FailingReader(original):
        def read(self, requested):
            if requested == failing_spec:
                raise failure('synthetic read race')
            return super().read(requested)

    monkeypatch.setattr(api, 'SpecReader', FailingReader)
    expected = f'wip_spec_unreadable: spec={failing_spec} exception={failure.__name__}'
    decision = api.check_lead_spawn(tmp_path, spec_id)
    assert decision['error_code'] == 'wip_spec_unreadable'
    assert decision['message'] == expected
    assert decision['E_R'] is None

    async def go():
        store = Store(':memory:'); store.start()
        ctl, sessions, tmux = await setup(tmp_path, store, monkeypatch)
        try:
            with pytest.raises(VerbError) as exc:
                await ctl.spawn(message(spec_id), HOST)
            assert exc.value.code == 'wip_spec_unreadable'
            assert str(exc.value) == expected
            assert await store.reservations(include_expired=True) == []
            assert await store.list_sessions('open') == []
            assert tmux.created == 0
        finally:
            try:
                await cleanup(store, sessions, tmux)
            finally:
                store.stop()
    asyncio.run(go())


def test_malformed_peer_override_still_refuses_spawn(tmp_path, monkeypatch):
    row = deepcopy(next(row for row in ROWS if row['id'] == 'at_cap'))
    peer = row['in_progress'][0]
    row['overrides'][peer] = '- wip_override: invalid'
    row['expected'] = {
        'E_R': None, 'error_code': 'wip_override_malformed',
        'message': f'wip_override_malformed: spec=spec_{peer}', 'warnings': [],
    }
    test_real_spawn_cells(tmp_path, monkeypatch, row, True)
