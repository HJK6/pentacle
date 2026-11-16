"""Synthetic follow-on audit; not part of the scoped blob repair."""
import asyncio,json
from _shared import operator_auth
from test_managed_attachment_fetch_scope import scoped_peer
from test_managed_attachment_publish import publication_fixture,ASSISTANT

def test_rejected_send_must_not_claim_another_request_receipt(tmp_path):
 async def run():
  async with publication_fixture(tmp_path) as (blobs,store,server,msg):
   registry=operator_auth.OperatorCredentialRegistry(tmp_path/'registry.json');server.operator_credential_registry=registry
   peer,cid=scoped_peer(server,registry,ASSISTANT)
   admitted=await server.accept_assistant_operator_input(dict(to_stream_id=ASSISTANT,text='synthetic operator text',request_id='operator-receipt',optimistic_id='operator-input'),operator_principal='operator:synthetic')
   assert await store.get_send_receipt(ASSISTANT,'operator-receipt')
   denied=await server._dispatch(json.dumps(dict(type='send',to_stream_id=ASSISTANT,request_id='operator-receipt',optimistic_id='different-input',text='synthetic collision')),websocket=peer)
   assert denied[0].get('error_code'),denied
   result=await server._dispatch(json.dumps(dict(type='send.receipt.get',to_stream_id=ASSISTANT,request_id='operator-receipt')),websocket=peer)
   assert result[0].get('error_code')=='scope_denied',result
 asyncio.run(run())

async def send(server,peer,rid='scoped-request',identity='scoped-input',body='synthetic message'):
 return (await server._dispatch(json.dumps(dict(type='send',to_stream_id=ASSISTANT,request_id=rid,optimistic_id=identity,text=body)),websocket=peer))[0]


def test_same_owner_concurrent_retry_and_rotated_request_are_idempotent(tmp_path):
 async def run():
  async with publication_fixture(tmp_path) as (blobs,store,server,msg):
   registry=operator_auth.OperatorCredentialRegistry(tmp_path/'registry.json');server.operator_credential_registry=registry
   peer,cid=scoped_peer(server,registry,ASSISTANT)
   first,second=await asyncio.gather(send(server,peer),send(server,peer))
   assert first['type']==second['type']=='send.result'
   assert first['assistant_composite']['event_id']==second['assistant_composite']['event_id']
   assert await store.scoped_owner(kind='request',key='scoped-request')==cid
   rotated=await send(server,peer,rid='rotated-request')
   assert rotated['assistant_composite']['event_id']==first['assistant_composite']['event_id']
   assert await store.scoped_owner(kind='request',key='rotated-request')==cid
   assert await store.get_send_receipt(ASSISTANT,'rotated-request')
 asyncio.run(run())


def test_two_principals_racing_one_id_do_not_share_ownership(tmp_path):
 async def run():
  async with publication_fixture(tmp_path) as (blobs,store,server,msg):
   registry=operator_auth.OperatorCredentialRegistry(tmp_path/'registry.json');server.operator_credential_registry=registry
   one,cid1=scoped_peer(server,registry,ASSISTANT);two,cid2=scoped_peer(server,registry,ASSISTANT)
   a,b=await asyncio.gather(send(server,one),send(server,two,identity='second-input'))
   winner,loser=(one,two) if a['type']=='send.result' else (two,one)
   assert sum(x['type']=='send.result' for x in [a,b])==1
   owner=await store.scoped_owner(kind='request',key='scoped-request')
   assert owner==(cid1 if winner is one else cid2)
   denied=await server._dispatch(json.dumps(dict(type='send.receipt.get',to_stream_id=ASSISTANT,request_id='scoped-request')),websocket=loser)
   assert denied[0]['error_code']=='scope_denied'
   retry=await send(server,winner,identity='scoped-input' if winner is one else 'second-input')
   assert retry['type']=='send.result'
 asyncio.run(run())


def test_failed_admission_transaction_rolls_back_claim_and_all_rows(tmp_path):
 async def run():
  async with publication_fixture(tmp_path) as (blobs,store,server,msg):
   registry=operator_auth.OperatorCredentialRegistry(tmp_path/'registry.json');server.operator_credential_registry=registry
   peer,cid=scoped_peer(server,registry,ASSISTANT)
   await store.submit(lambda c:c.execute("CREATE TRIGGER synthetic_receipt_failure BEFORE INSERT ON v2_send_receipts BEGIN SELECT RAISE(ABORT,'synthetic receipt failure'); END"))
   denied=await send(server,peer)
   assert denied.get('error_code')
   assert await store.scoped_owner(kind='request',key='scoped-request') is None
   assert await store.get_send_receipt(ASSISTANT,'scoped-request') is None
   assert await store.get_assistant_composite_route(stream_id=ASSISTANT,input_identity='scoped-input') is None
   await store.submit(lambda c:c.execute('DROP TRIGGER synthetic_receipt_failure'))
   accepted=await send(server,peer)
   assert accepted['type']=='send.result'
   assert await store.scoped_owner(kind='request',key='scoped-request')==cid
 asyncio.run(run())


def test_foreign_replay_identity_cannot_claim_rotated_request(tmp_path):
 async def run():
  async with publication_fixture(tmp_path) as (blobs,store,server,msg):
   registry=operator_auth.OperatorCredentialRegistry(tmp_path/'registry.json');server.operator_credential_registry=registry
   one,cid=scoped_peer(server,registry,ASSISTANT);other,_=scoped_peer(server,registry,ASSISTANT)
   accepted=await send(server,one)
   denied=await send(server,other,rid='stolen-rotation')
   assert denied.get('error_code')=='assistant_request_owner_conflict'
   assert await store.scoped_owner(kind='request',key='stolen-rotation') is None
   assert await store.get_send_receipt(ASSISTANT,'stolen-rotation') is None
   retried=await send(server,one)
   assert retried['assistant_composite']['event_id']==accepted['assistant_composite']['event_id']
 asyncio.run(run())
