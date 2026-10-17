'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { installRenderer } = require('./helpers/renderer_chat');
const flush = () => new Promise(resolve => setImmediate(resolve));

async function boot(t) {
  const sessions = ['lead', 'other', 'child'].map(name => ({
    stream_id: `local:${name}`, host: 'local', session_name: name,
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
  run(`CONFIG.chatStream.hostMap.local = 'local';
    CONFIG.hostNames.local = 'local';
    state.chatStream.sessions = ${JSON.stringify(sessions)};
    state.slots[0] = {name: 'lead', hostId: 'local'};
    state.slots[1] = {name: 'other', hostId: 'local'};
    assetBucketForStream('local:lead').scopeStreamIds = ['local:lead', 'local:child'];
    assetBucketForStream('local:other').scopeStreamIds = ['local:other'];`);
  return { run, read, calls, h };
}

test('shared specs do not route broadcasts to unrelated chats; descendants retain source identity', async t => {
  const { run, read, calls } = await boot(t);
  run(`applyAssetUpdate({asset_id:'same', title:'Child', content_type:'report', spec_id:'shared',
    session_key:{host:'local',session_name:'child',stream_id:'local:child'}});`);
  assert.deepEqual(read('visibleAssetsForSlot(0).map(x => x.title)'), ['Child']);
  assert.deepEqual(read('visibleAssetsForSlot(1).map(x => x.title)'), []);
  const item = read('visibleAssetsForSlot(0)[0]');
  assert.equal(item.session_key.stream_id, 'local:child');
  await run(`ensureAssetBodyLoaded('local:child', 'same', ${JSON.stringify(item.asset_key)})`);
  assert.equal(calls[0].stream_id, 'local:child');
  assert.equal(calls[0].session_name, 'child');
});

test('duplicate ids across owners stay separate and removal affects only the true source', async t => {
  const { run, read } = await boot(t);
  for (const name of ['lead', 'child', 'other']) run(`applyAssetUpdate({asset_id:'same', title:'${name}', content_type:'report', spec_id:'shared',
    session_key:{host:'local',session_name:'${name}',stream_id:'local:${name}'}});`);
  assert.deepEqual(read('visibleAssetsForSlot(0).map(x => x.title).sort()'), ['child', 'lead']);
  assert.deepEqual(read('visibleAssetsForSlot(1).map(x => x.title)'), ['other']);
  run(`applyAssetRemoved({asset_id:'same',spec_id:'shared',session_key:{stream_id:'local:child'}})`);
  assert.deepEqual(read('visibleAssetsForSlot(0).map(x => x.title)'), ['lead']);
  assert.deepEqual(read('visibleAssetsForSlot(1).map(x => x.title)'), ['other']);
});

test('binding changes refresh scope, reject late lists, and reconcile an empty snapshot', async t => {
  const { run, read, h } = await boot(t);
  const pending = [];
  h.dom.window.cc.assetList = args => new Promise(resolve => pending.push({ args, resolve }));
  const oldRequest = run('fetchSlotAssetSnapshot(0)');
  const old = pending.at(-1);
  run(`applyChatStreamPayload({type:'assistant.binding.changed',stream_id:'local:child',generation:'g2'})`);
  assert.ok(pending.length > 1);
  // There are two attached slots; locate the refreshed lead request.
  const fresh = pending.findLast(x => x.args.stream_id === 'local:lead');
  fresh.resolve({scope_stream_ids:['local:lead','local:child'],assets:[
    {asset_id:'same',title:'fresh',content_type:'report',stream_id:'local:child'}]});
  await flush();
  old.resolve({scope_stream_ids:['local:lead','local:old'],assets:[
    {asset_id:'same',title:'stale',content_type:'report',stream_id:'local:old'}]});
  await oldRequest;
  assert.deepEqual(read('visibleAssetsForSlot(0).map(x=>x.title)'), ['fresh']);
  const empty = run('fetchSlotAssetSnapshot(0)');
  pending.at(-1).resolve({scope_stream_ids:['local:lead','local:child'],assets:[]});
  await empty;
  assert.deepEqual(read('visibleAssetsForSlot(0)'), []);
  assert.equal(run("unreadReportCountForStreamImpl('local:lead')"), 0);
});

test('shared viewer epochs and live mutations win over in-flight snapshots', async t => {
  const { run, read, h } = await boot(t);
  const pending = [];
  h.dom.window.cc.assetList = args => new Promise(resolve => pending.push({ args, resolve }));
  run("state.slots[1] = {name:'lead',hostId:'local'}");
  const first = run('fetchSlotAssetSnapshot(0)');
  const old = pending.at(-1);
  const second = run('fetchSlotAssetSnapshot(1)');
  const fresh = pending.at(-1);
  fresh.resolve({scope_stream_ids:['local:lead','local:child'],assets:[]});
  await second;
  old.resolve({scope_stream_ids:['local:lead','local:old'],assets:[]});
  await first;
  assert.deepEqual(read("state.chatStream.assets['local:lead'].scopeStreamIds"), ['local:lead','local:child']);
  const update = {asset_id:'live',title:'live',content_type:'report',stream_id:'local:child',
    session_key:{host:'local',session_name:'child',stream_id:'local:child'}};
  const beforePush = run('fetchSlotAssetSnapshot(0)');
  run(`applyAssetUpdate(${JSON.stringify(update)})`);
  pending.at(-1).resolve({scope_stream_ids:['local:lead','local:child'],assets:[]});
  await beforePush;
  assert.deepEqual(read('visibleAssetsForSlot(0).map(x=>x.title)'), ['live']);
  const beforeRemove = run('fetchSlotAssetSnapshot(1)');
  run(`applyAssetRemoved({asset_id:'live',session_key:{stream_id:'local:child'}})`);
  pending.at(-1).resolve({scope_stream_ids:['local:lead','local:child'],assets:[update]});
  await beforeRemove;
  assert.deepEqual(read('visibleAssetsForSlot(0)'), []);
  assert.equal(run("unreadReportCountForStreamImpl('local:lead')"), 0);
});
