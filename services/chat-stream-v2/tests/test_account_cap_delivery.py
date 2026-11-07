"""Owned daemon fixtures drive refusal, physical delivery, replay and revocation."""
import asyncio
import json

import pytest

from comms import Comms
from spawnctl import SpawnCtl
from outbound_notices import OutboundNoticeQueue
from test_account_runtime import setup,AUTH
from test_account_rotation import binding,observation,NOW
from test_account_pool_control import control,PEER,OPERATOR
from test_send_semantics import FakeTmux,FakeBlobStore,SHA


async def fixture(tmp_path):
    store,runtime=await setup(initialize_pin=False)
    account=binding(provider='claude')
    await runtime.run(lambda a:a.register(account))
    tmux=FakeTmux()
    ctl=SpawnCtl(store,runtime.sessions,tmux=tmux)
    comms=Comms(store,runtime.sessions,ctl,blob_store=FakeBlobStore(),attachment_root=tmp_path/'attachments')
    runtime.attach(runtime.server,ctl,comms)
    worker=await runtime.sessions.open('host-a','worker',provider='claude',session_generation='worker-one',
                                       account_binding=account.public(),pane_status='pane_alive')
    async def meter(pct):
        obs=observation(account);obs['windows']['weekly']['pct']=pct
        await runtime.record_observation(account,obs)
    await meter(21)
    def cap(a):
        a.conn.execute('INSERT INTO kv VALUES (?,?)',('account.pool-control:claude:pool-primary',json.dumps(control())))
        a.conn.commit()
    await runtime.run(cap)
    return store,runtime,comms,tmux,worker,meter


@pytest.mark.parametrize('verb',['tell','send'])
def test_cap_refusal_never_inputs_queues_or_silently_redelivers_the_same_id(tmp_path,verb):
    async def run():
        store,runtime,comms,tmux,worker,meter=await fixture(tmp_path)
        try:
            msg={'stream_id':worker['stream_id'],'message':'owned peer work','from_stream_id':PEER['stream_id'],
                 '_auth_context':PEER,'tell_id':'peer-one','request_id':'peer-one'}
            deliver=getattr(comms,verb)
            refused=await deliver(msg)
            assert refused['error_code']=='pool_at_cap' and refused['submission_confirmed'] is False
            assert refused['retryable'] is False and refused['queued_for_redelivery'] is False
            assert refused['pool_id']=='pool-primary' and tmux.pastes==[]
            assert await store.submit(lambda c:c.execute('SELECT COUNT(*) FROM v2_outbound_notices').fetchone()[0])==0
            await meter(20)
            replay=await deliver(msg)
            assert replay['error_code']=='pool_at_cap' and tmux.pastes==[]
            landed=await deliver(dict(msg,tell_id='peer-two',request_id='peer-two'))
            assert landed['submission_confirmed'] is True and len(tmux.pastes)==1
        finally: store.stop()
    asyncio.run(run())


def test_primary_operator_and_exact_seat_override_deliver_then_revoke_refuses(tmp_path):
    async def run():
        store,runtime,comms,tmux,worker,meter=await fixture(tmp_path)
        try:
            for auth in (AUTH,OPERATOR):
                result=await comms.tell({'stream_id':worker['stream_id'],'message':'authorized owned work',
                    'tell_id':'allow-'+str(len(tmux.pastes)),'_auth_context':auth})
                assert result['submission_confirmed'] is True
            request=dict(action='seat-override-set',stream=worker['stream_id'],expected_generation=worker['session_generation'],
                expected_revision=0,operation_id='allow-seat',enabled=True,reason='owned delivery drill',authority_ref='fixture-order',_auth_context=AUTH)
            await runtime.operation(request)
            message={'stream_id':worker['stream_id'],'message':'peer under exact exception','tell_id':'override-peer','_auth_context':PEER}
            assert (await comms.tell(message))['submission_confirmed'] is True
            await runtime.operation(dict(request,expected_revision=1,operation_id='revoke-seat',enabled=False))
            assert (await comms.tell(dict(message,tell_id='revoked-peer')))['error_code']=='pool_at_cap'
            assert len(tmux.pastes)==3
            status=await runtime.operation({'action':'status','_auth_context':AUTH})
            assert status['seat_overrides'][0]['active'] is False
        finally: store.stop()
    asyncio.run(run())


def test_operator_attachment_carries_provenance_to_receipt_and_physical_claim(tmp_path):
    async def run():
        store,runtime,comms,tmux,worker,meter=await fixture(tmp_path)
        try:
            msg={'stream_id':worker['stream_id'],'message':'inspect owned image','request_id':'operator-image','_auth_context':OPERATOR,
                 'attachments':[{'key':SHA,'mime':'image/png'}]}
            result=await comms.send(msg)
            assert result['submission_confirmed'] and len(tmux.pastes)==1
            checkpoint=await runtime.cap_monitor.state(worker['stream_id'],worker['session_generation'])
            assert checkpoint['operator_input']['reason']=='direct_operator_order'
            assert checkpoint['operator_input']['id']=='operator-image'
            meta=await store.submit(lambda c:json.loads(c.execute("SELECT meta_json FROM v2_send_receipts WHERE request_id='operator-image' ORDER BY rowid DESC LIMIT 1").fetchone()[0]))
            assert meta['pool_input']['reason']=='direct_operator_order'
        finally: store.stop()
    asyncio.run(run())


def test_outbox_policy_refusal_is_terminal_with_report_custody_retained(tmp_path):
    async def run():
        store,runtime,comms,tmux,worker,meter=await fixture(tmp_path)
        try:
            outbound=OutboundNoticeQueue(store,comms)
            notice=await outbound.enqueue(kind='report',dedupe_key='owned-report',tell_id='owned-report',
                recipient_stream_id=worker['stream_id'],body='owned durable report notice')
            await outbound.deliver_now(notice['notice_id'])
            row=await store.outbound_notice_for_dedupe('owned-report')
            assert row['terminal_at'] and not row['delivered_at'] and tmux.pastes==[]
            await meter(20);await outbound.drain_once(force=True)
            assert tmux.pastes==[]
        finally: store.stop()
    asyncio.run(run())
