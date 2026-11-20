'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const path = require('node:path');
const esbuild = require('esbuild');
const { installRenderer, STREAM } = require('./helpers/renderer_chat');

const session = {
  stream_id: STREAM, host: 'hostc', session_name: 'claude-hostc-race',
  session_kind: 'assistant_composite', provider: 'composite', visibility: 'default',
  capabilities: { pane: false, terminal: false, assistant_composite_v1: true },
};
const note = (seq, type = 'relay') => ({
  stream_id: STREAM, host: session.host, session_name: session.session_name,
  kind: 'ASSIST_TEXT', provider: 'composite', daemon_seq: seq,
  message_id: `publication:note-${seq}`, timestamp: `2026-01-01T00:00:0${seq}Z`,
  text: `${type} note ${seq} https://example.invalid/note #test`, note_type: type,
  ref: 'message:synthetic-trigger', raw: { assistant_composite: true },
});
const bundle = esbuild.buildSync({ entryPoints: [path.join(__dirname, '../renderer/src/chat_core_entry.ts')],
  bundle: true, format: 'iife', target: 'chrome134', write: false, logLevel: 'silent' }).outputFiles[0].text;

test('actual composite slot fetches on cold reload and reconnect and merges overlapping notes once', async t => {
  for (const reload of [false, true]) {
    const requests = [];
    let frameHandler;
    const h = installRenderer({ initialSessions: [session], questionOverride: null,
      requestStreamEvents: args => new Promise(resolve => requests.push({ args, resolve })),
      ccOverrides: { onChatStreamFrame: handler => { frameHandler = handler; } },
    });
    t.after(() => h.dom.window.close());
    await new Promise(setImmediate);
    h.context.structuredClone = structuredClone;
    h.context.crypto = h.dom.window.crypto;
    vm.runInContext(bundle, h.context);
    for (const name of ['PentacleChatStore', 'PentacleChatView', 'PentacleChatCore']) h.dom.window[name] = h.context[name];
    const store = h.dom.window.PentacleChatStore;
    vm.runInContext(`
      ensureSlotChatSurface(0);
      state.slots[0] = { name: 'claude-hostc-race', displayName: 'Assistant', hostId: 'local' };
      state.slotViewModes[0] = 'chat';
      state.chatStream.sessions = [${JSON.stringify(session)}];
    `, h.context);
    frameHandler({ type: 'snapshot', connected: true, sessions: [session], events: [] });
    vm.runInContext('renderSlotChat(0)', h.context);
    assert.equal(requests.length, 1, 'cold summary snapshot must fetch composite history');
    assert.equal(requests[0].args.streamId, STREAM);
    const first = note(1);
    const second = note(2, 'problem');
    // A new page has no transcript memory. History still supplies the note.
    frameHandler({ type: 'stream_events', stream_id: STREAM, events: reload ? [first, second] : [first] });
    requests[0].resolve({ ok: true, received: reload ? 2 : 1, nextBeforeDaemonSeq: 1 });
    await new Promise(setImmediate);
    assert.equal(store.selectSessionDetail(STREAM).transcriptItems.filter(e => JSON.stringify(e).includes(first.text)).length, 1);
    // Host reconnect can occur without a visible disconnected state in the web tab.
    frameHandler({ type: '__reconnect', generation: 1, next_generation: 2 });
    assert.equal(requests.length, 2, 'actual reconnect handler must invalidate the completed history load');
    assert.equal(requests[1].args.streamId, STREAM);
    frameHandler({ type: 'chat.event', event: second });
    frameHandler({ type: 'chat.event', event: second }); // retry replay uses the same daemon identity
    frameHandler({ type: 'stream_events', stream_id: STREAM, events: [first, second] });
    requests[1].resolve({ ok: true, received: 2, nextBeforeDaemonSeq: 1 });
    await new Promise(setImmediate);
    const detail = store.selectSessionDetail(STREAM);
    for (const event of [first, second]) {
      assert.equal(detail.transcriptItems.filter(e => JSON.stringify(e).includes(event.text)).length, 1);
      assert.ok(detail.transcriptItems.some(item => JSON.stringify(item).includes(event.text)));
    }
    vm.runInContext('renderSlotChat(0)', h.context);
    const text = h.dom.window.document.body.textContent;
    assert.ok(text.includes(first.text));
    assert.ok(text.includes(second.text));
  }
});
