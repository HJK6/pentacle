'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { installRenderer } = require('./helpers/renderer_chat');
const flush = () => new Promise(resolve => setImmediate(resolve));

async function boot(t) {
  const sessions = ['lead', 'other', 'child'].map(name => ({
    stream_id: `hostc:${name}`, host: 'hostc', session_name: name,
    name, title: name, provider: 'codex', spec_ids: ['shared'],
  }));
  const calls = [];
  const h = installRenderer({ initialSessions: sessions, ccOverrides: {
    assetGet: async args => { calls.push(args); return { body: 'report body' }; },
  } });
  t.after(() => h.dom.window.close());
  await flush(); await flush();
  const run = code => vm.runInContext(code, h.context);
  const read = code => JSON.parse(run(`JSON.stringify(${code})`));
  run(`state.chatStream.sessions = ${JSON.stringify(sessions)};
    state.slots[0] = {name: 'lead', hostId: 'local'};
    state.slots[1] = {name: 'other', hostId: 'local'};
    assetBucketForStream('hostc:lead').scopeStreamIds = ['hostc:lead', 'hostc:child'];
    assetBucketForStream('hostc:other').scopeStreamIds = ['hostc:other'];`);
  return { run, read, calls, h };
}

test('shared specs do not route broadcasts to unrelated chats; descendants retain source identity', async t => {
  const { run, read, calls } = await boot(t);
  run(`applyAssetUpdate({asset_id:'same', title:'Child', content_type:'report', spec_id:'shared',
    session_key:{host:'hostc',session_name:'child',stream_id:'hostc:child'}});`);
  assert.deepEqual(read('visibleAssetsForSlot(0).map(x => x.title)'), ['Child']);
  assert.deepEqual(read('visibleAssetsForSlot(1).map(x => x.title)'), []);
  const item = read('visibleAssetsForSlot(0)[0]');
  assert.equal(item.session_key.stream_id, 'hostc:child');
  await run(`ensureAssetBodyLoaded('hostc:child', 'same', ${JSON.stringify(item.asset_key)})`);
  assert.equal(calls[0].stream_id, 'hostc:child');
  assert.equal(calls[0].session_name, 'child');
});

test('duplicate ids across owners stay separate and removal affects only the true source', async t => {
  const { run, read } = await boot(t);
  for (const name of ['lead', 'child', 'other']) run(`applyAssetUpdate({asset_id:'same', title:'${name}', content_type:'report', spec_id:'shared',
    session_key:{host:'hostc',session_name:'${name}',stream_id:'hostc:${name}'}});`);
  assert.deepEqual(read('visibleAssetsForSlot(0).map(x => x.title).sort()'), ['child', 'lead']);
  assert.deepEqual(read('visibleAssetsForSlot(1).map(x => x.title)'), ['other']);
  run(`applyAssetRemoved({asset_id:'same',spec_id:'shared',session_key:{stream_id:'hostc:child'}})`);
  assert.deepEqual(read('visibleAssetsForSlot(0).map(x => x.title)'), ['lead']);
  assert.deepEqual(read('visibleAssetsForSlot(1).map(x => x.title)'), ['other']);
});
