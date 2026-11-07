"""Clock permutations use the existing tick/receipt/notice paths, never live quota."""
import asyncio
from datetime import datetime,timezone
from types import SimpleNamespace

import pytest

from account_cap_monitor import CapMonitor,REMINDER,FLAG
from test_account_pool_control import seed,OPERATOR
from test_account_runtime import setup
from test_account_rotation import NOW,binding,observation


class Notices:
    def __init__(self): self.notices={}; self.guards={}
    def register_kind(self,kind,**kwargs): self.guards[kind]=kwargs['guard']
    async def enqueue(self,**row):
        previous=self.notices.setdefault(row['tell_id'],row)
        assert previous==row  # The durable substrate rejects changed same-ID notices.


async def fixture():
    store,runtime=await setup(initialize_pin=False)
    clock=[NOW]; runtime.clock=lambda:clock[0]
    await seed(runtime)
    worker=await runtime.sessions.open('host-a','worker',provider='codex',session_generation='worker-one',
                                       account_binding=binding().public(),pane_status='pane_alive')
    notices=Notices();runtime.cap_monitor.attach(notices)
    queued=[False]
    async def queue_probe(*args,**kwargs): return queued[0]
    runtime.comms=SimpleNamespace(_prompt_in_native_queue=queue_probe)
    def sample(working=True,*,stamp=None,online=True,generation=None):
        return runtime.sessions.apply_live('host-a:worker',online=online,working=working,
            capture_generation=generation or worker['session_generation'],capture_liveness='idle',
            capture_observed_at=clock[0] if stamp is None else stamp)
    sample()
    return store,runtime,clock,worker,notices,sample,queued


def iso(stamp): return datetime.fromtimestamp(stamp,timezone.utc).isoformat()


async def submit_reminder(runtime,notices,clock):
    reminder=next(n for n in notices.notices.values() if n['kind']==REMINDER)
    await runtime.store.put_tell_delivery(reminder['tell_id'],{'payload_digest':'fixture',
        'reply':{'type':'tell.ok','submission_confirmed':True,'delivery_status':'delivered','delivery_ack_at':iso(clock[0])},
        'delivery':{'text':reminder['body'],'to_stream_id':reminder['recipient_stream_id'],'delivered_at':iso(clock[0])}})
    await runtime.tick()
    return reminder


async def operator_input(runtime,worker,clock,*,key='operator-one',confirmed=True,kind='tell'):
    message={'_auth_context':OPERATOR,'tell_id':key} if kind=='tell' else {'_auth_context':OPERATOR,'request_id':key}
    async with runtime.input_guard(worker,message=message,claim=True): pass
    if kind=='tell':
        await runtime.store.put_tell_delivery(key,{'payload_digest':key,
            'reply':{'submission_confirmed':confirmed,'delivery_ack_at':iso(clock[0]) if confirmed else None},
            'delivery':{'text':'operator order','to_stream_id':worker['stream_id'],
                        'delivered_at':iso(clock[0]) if confirmed else None}})
    else:
        import json
        await runtime.store.append_send_receipt(to_stream_id=worker['stream_id'],request_id=key,receipt_id=key,
            state='landed' if confirmed else 'accepted',wire_text='operator order',display_text='operator order',attachments=[],
            delivery='landed' if confirmed else 'committed_pending_proof',submission_confirmed=confirmed,
            created_at=iso(clock[0]),meta_json=json.dumps({'pool_input':{'reason':'direct_operator_order','wire_text':'operator order'}}))


def test_one_reminder_then_flag_only_after_120_seconds_and_fresh_later_working_sample():
    async def run():
        store,runtime,clock,worker,notices,sample,_=await fixture()
        try:
            await runtime.tick(); await submit_reminder(runtime,notices,clock)
            clock[0]+=119; sample(); await runtime.tick()
            assert len(notices.notices)==1
            clock[0]+=1; sample(stamp=NOW); await runtime.tick()  # Stale evidence cannot flag.
            assert len(notices.notices)==1
            sample(); await runtime.tick(); await runtime.tick()
            assert [n['kind'] for n in notices.notices.values()]==[REMINDER,FLAG]
            # A restarted monitor reuses the original episode/receipt without another reminder or flag.
            runtime.cap_monitor=CapMonitor(runtime);runtime.cap_monitor.attach(notices)
            await runtime.tick();assert len(notices.notices)==2
            flag=next(n for n in notices.notices.values() if n['kind']==FLAG)
            assert flag['recipient_stream_id']=='host-a:front-desk'
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('cell',['unknown','offline','stale_capture','old_generation','idle'])
def test_unknown_offline_and_stale_working_samples_send_no_notices(cell):
    async def run():
        store,runtime,clock,worker,notices,sample,_=await fixture()
        try:
            if cell=='unknown':
                obs=observation(binding());obs['collection']='failed'
                await runtime.record_observation(binding(),obs)
            if cell=='offline': sample(online=False)
            if cell=='stale_capture': sample(stamp=NOW-31)
            if cell=='old_generation': sample(generation='previous')
            if cell=='idle': sample(False)
            await runtime.tick();assert notices.notices=={}
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('stage',['before_cap','before_reminder','after_reminder','after_deadline'])
@pytest.mark.parametrize('kind',['tell','send'])
def test_operator_to_working_seat_suppresses_pending_notice_until_proved_idle(stage,kind):
    async def run():
        store,runtime,clock,worker,notices,sample,queued=await fixture()
        try:
            if stage=='before_cap': await seed(runtime,pct=20)
            if stage in {'after_reminder','after_deadline'}:
                await runtime.tick();await submit_reminder(runtime,notices,clock)
            if stage=='after_deadline':
                clock[0]+=121;sample();await runtime.tick()
            await operator_input(runtime,worker,clock,kind=kind)
            await seed(runtime)
            sample();await runtime.tick()
            before=len(notices.notices)
            clock[0]+=1;sample();await runtime.tick()
            assert len(notices.notices)==before
            if stage=='after_deadline':
                flag=next(n for n in notices.notices.values() if n['kind']==FLAG)
                row=dict(flag,recipient_stream_id=flag['recipient_stream_id'])
                assert (await runtime.cap_monitor.guard(row)).action=='defer'
            # An idle sample older than submission, or one with a still-native-queued prompt, cannot clear.
            sample(False,stamp=NOW-1);await runtime.tick()
            assert await runtime.cap_monitor.suppressed(runtime.sessions.get(worker['stream_id']))
            queued[0]=True;sample(False);await runtime.tick()
            assert await runtime.cap_monitor.suppressed(runtime.sessions.get(worker['stream_id']))
            queued[0]=False;clock[0]+=1;sample(False);await runtime.tick()
            assert not await runtime.cap_monitor.suppressed(runtime.sessions.get(worker['stream_id']))
            clock[0]+=1;sample();await runtime.tick()
            assert any(n['kind']==REMINDER for n in notices.notices.values())
        finally: store.stop()
    asyncio.run(run())


def test_uncertain_operator_submission_stays_suppressed_through_idle_restart_and_offline():
    async def run():
        store,runtime,clock,worker,notices,sample,_=await fixture()
        try:
            await operator_input(runtime,worker,clock,confirmed=False)
            clock[0]+=121;sample(False);await runtime.tick();assert notices.notices=={}
            runtime.cap_monitor=CapMonitor(runtime);runtime.cap_monitor.attach(notices)
            sample(online=False);await runtime.tick();sample();await runtime.tick()
            assert notices.notices=={}
            assert (await runtime.cap_monitor.state(worker['stream_id'],worker['session_generation']))['suppression_reason']
        finally: store.stop()
    asyncio.run(run())


def test_fresh_below_cap_ends_episode_and_a_new_operator_receipt_renews_suppression():
    async def run():
        store,runtime,clock,worker,notices,sample,_=await fixture()
        try:
            await runtime.tick();await submit_reminder(runtime,notices,clock)
            await seed(runtime,pct=20);await runtime.tick();await seed(runtime);await runtime.tick()
            assert len(notices.notices)==2 and all(n['kind']==REMINDER for n in notices.notices.values())
            await operator_input(runtime,worker,clock,key='first')
            clock[0]+=1;sample(False);await runtime.tick()
            assert not await runtime.cap_monitor.suppressed(runtime.sessions.get(worker['stream_id']))
            await operator_input(runtime,worker,clock,key='second')
            sample();assert await runtime.cap_monitor.suppressed(runtime.sessions.get(worker['stream_id']))
        finally: store.stop()
    asyncio.run(run())
