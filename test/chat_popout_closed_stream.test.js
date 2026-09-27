'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const vm = require('node:vm');
const esbuild = require('esbuild');
const { installRenderer } = require('./helpers/renderer_chat');

const streamId = 'hostc:closed-history';
const session = { stream_id: streamId, host: 'hostc', session_name: 'closed-history', provider: 'codex' };
const events = Array.from({ length: 71 }, (_, index) => ({
  ...session, kind: 'ASSIST_TEXT', daemon_seq: index + 1,
  message_id: `assistant-${index}`, timestamp: new Date(1700000000000 + index * 1000).toISOString(),
  text: `Fetched assistant row ${index}`,
}));
events.push({ ...session, kind: 'USER', daemon_seq: 72, message_id: 'user-1',
  timestamp: new Date(1700000072000).toISOString(), text: 'Fetched operator row' });
const bundle = esbuild.buildSync({ entryPoints: [path.join(__dirname, '../renderer/src/chat_core_entry.ts')],
  bundle: true, format: 'iife', target: 'chrome134', write: false, logLevel: 'silent' }).outputFiles[0].text;

async function fixture(t, roster = []) {
  const h = installRenderer({ initialSessions: roster, questionOverride: null, popoutContext: {
    stream_id: streamId, host: session.host, desktop_host: 'local',
    session_name: session.session_name, title: 'Historical popout',
  } });
  t.after(() => h.dom.window.close());
  await new Promise(setImmediate);
  vm.runInContext(bundle, h.context);
  for (const key of ['PentacleChatStore', 'PentacleChatView', 'PentacleChatCore']) h.dom.window[key] = h.context[key];
  h.dom.window.PentacleChatStore.applyFrame({ type: 'snapshot', connected: true, sessions: roster, events: [] });
  h.dom.window.PentacleChatStore.applyFrame({ type: 'stream_events', stream_id: streamId, events });
  vm.runInContext(`
    ensureSlotChatSurface(0);
    state.slotViewModes[0] = 'chat';
    state.chatStream.connected = true;
    state.chatStream.sessions = ${JSON.stringify(roster)};
    renderSlotChat(0);
  `, h.context);
  assert.equal(vm.runInContext('state.slots[0].popoutStreamId', h.context), streamId);
  assert.equal(vm.runInContext('!!state.terminals[0]', h.context), false);
  return h;
}

function assertTranscript(h) {
  const detail = h.dom.window.PentacleChatStore.selectSessionDetail(streamId, { visibleCount: 'all', includeDraft: false });
  assert.equal(detail.transcriptItems.filter(item => item.displayRule === 'bubble:assistant').length, 71);
  const list = h.dom.window.document.querySelector('.slot-chat-list');
  assert.ok(list, 'chat surface exists');
  assert.equal(list.dataset.streamId, streamId, 'popout binds its explicit identity');
  assert.equal(list.querySelectorAll('.slot-chat-assistant-card').length, 71, 'all fetched ASSIST_TEXT rows paint');
  assert.equal(list.querySelectorAll('.slot-chat-row.is-user').length, 1);
  assert.match(list.textContent, /Fetched operator row/);
  assert.equal(list.querySelector('.slot-chat-empty'), null);
}

test('closed popout paints fetched history without a live roster entry', async t => {
  const h = await fixture(t);
  assertTranscript(h);
  const identity = vm.runInContext('chatSessionStateForSession(state.slots[0])', h.context);
  assert.equal(identity.stream_id, streamId);
  assert.equal(identity.status, undefined, 'missing roster never fabricates open status');
  assert.equal(identity.working, undefined, 'missing roster never fabricates live activity');
});

test('open popout retains roster metadata and paints the same fetched rows', async t => {
  const h = await fixture(t, [{ ...session, status: 'open', working: true, pending_peer_messages: 2 }]);
  assertTranscript(h);
  assert.equal(vm.runInContext('chatSessionStateForSession(state.slots[0]).working', h.context), true);
  assert.equal(vm.runInContext('pendingPeerMessagesForSession(state.slots[0])', h.context), 2);
});

test('a closed explicit binding does not pick a live session with the same name', async t => {
  const h = await fixture(t, [{ ...session, stream_id: 'hostc:replacement', status: 'open' }]);
  assertTranscript(h);
  assert.equal(vm.runInContext('chatSessionStateForSession(state.slots[0]).stream_id', h.context), streamId);
});

test('explicit history identity does not bypass a stale assistant-direct binding', async t => {
  const h = await fixture(t);
  vm.runInContext(`state.slots[0].assistantDirect = {
    sourceId: 'hostc:missing-assistant', targetId: ${JSON.stringify(streamId)}, generation: 'stale'
  }`, h.context);
  assert.equal(vm.runInContext('chatSessionStateForSession(state.slots[0])', h.context), null);
});
