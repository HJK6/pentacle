'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const client = require('../main/chat_stream_client');
const { createWakeDelivery } = require('../renderer/wake_delivery');

test('active summary membership supplies lifecycle for strict wake without mutating the wire rows', async () => {
  const compact = [{ stream_id:'local:current', session_generation:'g1',
    pane_status:'pane_alive', visibility:'hidden', state:'ready' }];
  client.connected = true;
  client._sessions = compact;
  const normalized = client.snapshot().sessions;
  assert.equal(normalized[0].status, 'open');
  assert.equal(Object.hasOwn(compact[0], 'status'), false);
  const status = {mode:'on',wake:{enabled:true,generation:'w1',pending_count:0}};
  const calls = [];
  async function check(sessions, available) {
    const helper = createWakeDelivery({
      config:{features:{mic:true},mic:{wakeTargetStreamId:'old'},chatStream:{}},
      getState:async()=>({connected:true,sessions}),
      getBinding:async()=>({source:'durable',stream_id:'local:current',generation:'g1'}),
      api:()=>{calls.push('api');throw Error('must not claim')},
      sendTurn:()=>{calls.push('send');throw Error('must not send')}
    });
    await helper.tick(status);
    assert.equal(helper.message(status).includes('No wake target'), !available);
  }
  await check(normalized, true);
  // Unknown rows supplied directly to the strict consumer remain blocked.
  await check(compact, false);
  for (const extra of [{status:null}, {status:'closed'}, {status:undefined},
    {closed_at:'2026-09-28T00:00:00Z'}, {session_generation:'wrong'}, {pane_status:'pane_dead'}]) {
    client._sessions = [{...compact[0], ...extra}];
    const rows = client.snapshot().sessions;
    if (Object.hasOwn(extra,'status')) assert.equal(rows[0].status, extra.status);
    await check(rows, false);
  }
  client._sessions = compact.concat(compact);
  await check(client.snapshot().sessions, false);
  assert.deepEqual(calls, []);
  client.destroy();
});
