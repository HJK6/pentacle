'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { WebSocketServer } = require('ws');
const { installRenderer, mountRaceSlot, STREAM } = require('./helpers/renderer_chat');
const { isConfiguredAssistant } = require('../renderer/assistant_role');
const { isProtectedAssistantRename } = require('../main/assistant_role_guard');
const { registerTerminalIpc } = require('../main/terminal_adapter');
const clientSingleton = require('../main/chat_stream_client');
const { filterSidebarSessions, projectChatStreamSessionsToDesktop } = require('../renderer/sidebar_filter');

const composite = {
  host: 'hostc', session_name: 'bart', stream_id: 'hostc:bart',
  session_kind: 'assistant_composite', provider: 'composite',
  capabilities: { pane: false, terminal: false, assistant_composite_v1: true },
};

test('composite protection uses daemon metadata without a private role alias', () => {
  assert.equal(isConfiguredAssistant(composite, ''), true);
  assert.equal(isConfiguredAssistant({ title: 'Bart', provider: 'composite' }, ''), false);
  assert.equal(isProtectedAssistantRename({ chatStream: { hostMap: { local: 'hostc' } } },
    { sessions: [composite] }, 'local', 'bart'), true);
  assert.equal(isProtectedAssistantRename({}, { sessions: [composite] }, 'other', 'bart'), false);
});

test('inventory projection retains composite identity and never exposes backend rows', () => {
  const rows = projectChatStreamSessionsToDesktop([
    { ...composite, visibility: 'visible' },
    { ...composite, session_name: 'hidden', session_kind: 'assistant_backend', visibility: 'default' },
    { session_name: 'ordinary', visibility: 'default' },
  ], () => 'local');
  assert.deepEqual(filterSidebarSessions(rows).map(row => row.name), ['bart', 'ordinary']);
  assert.equal(rows[0].session_kind, 'assistant_composite');
  assert.equal(rows[0].capabilities.terminal, false);
});

test('terminal IPC rejects a composite before looking up a pane', async () => {
  const handlers = new Map();
  let lookups = 0;
  const stop = registerTerminalIpc({ handle: (k, fn) => handlers.set(k, fn), on() {} },
    { chatStream: { hostMap: { local: 'hostc' } } }, { snapshot: () => ({ sessions: [composite] }) },
    { execute: async () => { lookups++; throw new Error('unexpected pane lookup'); } });
  await assert.rejects(handlers.get('pty:create')({ sender: { id: 1, isDestroyed: () => false } }, 0, 'bart'), /chat.only|no terminal/i);
  assert.equal(lookups, 0);
  stop();
});

test('client advertises composite support and preserves explicit reply binding', async () => {
  const client = new clientSingleton.constructor();
  client._cfg = { features: { chatUi: true } };
  assert.equal(client._helloPayload({}, { kind: 'v1' }).capabilities?.assistant_composite_v1, true);
  client.noteInteraction = () => {};
  client.sendCommand = async (payload) => payload;
  const payload = await client.sendMessage({ host: 'hostc', sessionName: 'bart', text: 'Yes',
    replyToMessageId: 'message-1', replyToQuestionId: 'question-2' });
  assert.equal(payload.reply_to_message_id, 'message-1');
  assert.equal(payload.reply_to_question_id, 'question-2');
});

test('cached desktop snapshots preserve the negotiated capability for reloads', async (t) => {
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await new Promise(resolve => server.once('listening', resolve));
  const client = new clientSingleton.constructor();
  t.after(async () => {
    client.destroy();
    for (const socket of server.clients) socket.terminate();
    await new Promise(resolve => server.close(resolve));
  });
  server.on('connection', socket => {
    socket.send(JSON.stringify({ type: 'welcome' }));
    socket.on('message', data => {
      if (JSON.parse(data.toString()).type === 'hello') socket.send(JSON.stringify({
        type: 'snapshot', sessions: [composite], events: [], capabilities: { assistant_composite_v1: true },
      }));
    });
  });
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('fixture handshake timed out')), 3000);
    client.init({ features: { chatUi: true }, chatStream: {
      url: `ws://127.0.0.1:${server.address().port}`, token: 'test-only',
    } }, frame => { if (frame.type === 'snapshot') { clearTimeout(timer); resolve(); } });
  });
  assert.equal(client.snapshot().capabilities?.assistant_composite_v1, true);
});

async function renderer(t) {
  const h = installRenderer();
  h.dom.window.cc.killPty = () => {};
  t.after(() => h.dom.window.close());
  await new Promise(setImmediate);
  mountRaceSlot(h.context);
  vm.runInContext(`Object.assign(state.chatStream.sessions[0], ${JSON.stringify(composite)}, {
    stream_id: ${JSON.stringify(STREAM)}, session_name: 'claude-hostc-race', name: 'claude-hostc-race'
  });`, h.context);
  return h;
}

test('composite slot stays chat-only across mode changes, disconnection and attach', async (t) => {
  const h = await renderer(t);
  vm.runInContext("ensureSlotModeToggle(0); updateSlotViewMode(0, 'terminal')", h.context);
  assert.equal(vm.runInContext('state.slotViewModes[0]', h.context), 'chat');
  assert.equal(h.dom.window.document.querySelector('#header-0 [data-mode="terminal"]'), null);
  vm.runInContext("state.chatStream.connected = false; updateSlotViewMode(0, 'status')", h.context);
  assert.equal(vm.runInContext('state.slotViewModes[0]', h.context), 'chat');
  // The real attach path must return before creating any xterm/PTY, not merely hide it.
  await vm.runInContext("attachSession(0, 'claude-hostc-race', 'Bart', 'local')", h.context);
  assert.equal(vm.runInContext('state.slotViewModes[0]', h.context), 'chat');
  assert.equal(vm.runInContext('!!state.terminals[0]', h.context), false);
});

test('an open question does not consume an unrelated Bart composer message', async (t) => {
  const h = await renderer(t);
  vm.runInContext("state.slotChatRefs[0].inputEl.value = 'Also, plan the new website';", h.context);
  await vm.runInContext('sendChatComposer(0)', h.context);
  assert.equal(h.dismissCalls.length, 0);
  assert.equal(h.sendCalls.length, 1);
  assert.equal(h.sendCalls[0].text, 'Also, plan the new website');
});

test('a composite popout bootstrap cannot allocate a terminal', async (t) => {
  const h = installRenderer({ initialSessions: [composite], popoutContext: {
    stream_id: composite.stream_id, host: composite.host, desktop_host: 'local',
    session_name: composite.session_name, title: 'Bart',
  } });
  t.after(() => h.dom.window.close());
  await new Promise(setImmediate);
  assert.equal(vm.runInContext('state.slotViewModes[0]', h.context), 'chat');
  assert.equal(vm.runInContext('state.maximizedSlot', h.context), 0);
  assert.equal(vm.runInContext('!!state.terminals[0]', h.context), false);
});

// Exercise the shipped renderer bundle -> preload -> shared IPC -> socket payload.
async function wireRenderer(t, compositeMode = true) {
  const fs = require('node:fs');
  const path = require('node:path');
  const esbuild = require('esbuild');
  const { createCcHandlers, createCollector } = require('../main/cc_handlers');
  const h = await renderer(t);
  const session = { ...composite, stream_id: STREAM, session_name: 'claude-hostc-race' };
  if (!compositeMode) { session.session_kind = 'agent'; session.provider = 'codex'; session.capabilities = {}; }
  vm.runInContext(`state.chatStream.sessions = [${JSON.stringify(session)}]`, h.context);
  const client = new clientSingleton.constructor();
  client._sessions = [session];
  client.connected = true;
  client.noteInteraction = () => {};
  const wires = [];
  client._ws = { readyState: 1, send(data) {
    const frame = JSON.parse(data); wires.push(frame);
    queueMicrotask(() => {
      const pending = client._pending.get(frame.request_id);
      client._pending.delete(frame.request_id);
      pending.resolve({ delivery: 'not_landed', action_committed: false, reason: 'fixture transport failure' });
    });
  } };
  const collector = createCollector();
  createCcHandlers({ CONFIG: { chatStream: {} }, chatStreamClient: client, harness: true }).register(collector);
  const preload = { window: {}, process, console, require(name) {
    if (name === 'electron') return { clipboard: {}, ipcRenderer: {
      invoke(channel, ...args) { return collector.table[channel].handler({}, ...args); },
      on() {}, send() {}, removeAllListeners() {},
    } };
    if (name === './config-loader') return { loadConfig: () => ({ config: {} }) };
    return require(name);
  } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../preload.js'), 'utf8'), preload);
  h.dom.window.cc.chatSendCorrelated = preload.window.cc.chatSendCorrelated;
  h.context.cc = h.dom.window.cc;
  h.context.crypto = h.dom.window.crypto;
  const bundle = esbuild.buildSync({ entryPoints: [path.join(__dirname, '../renderer/src/chat_core_entry.ts')],
    bundle: true, format: 'iife', target: 'chrome134', write: false, logLevel: 'silent' }).outputFiles[0].text;
  vm.runInContext(bundle, h.context);
  for (const name of ['PentacleChatStore', 'PentacleChatView', 'PentacleChatCore']) h.dom.window[name] = h.context[name];
  const store = h.dom.window.PentacleChatStore;
  store.applyFrame({ type: 'snapshot', connected: true, sessions: [session], events: [{
    ...session, session_id: session.session_name, kind: 'ASSIST_TEXT', daemon_seq: 1,
    timestamp: new Date().toISOString(), message_id: 'message-question',
    reply_to_question_id: 'question-bound', text: 'Choose the next step',
  }] });
  vm.runInContext('renderSlotChat(0)', h.context);
  return { ...h, store, wires };
}

test('desktop composite composer sends exact input and identity through real IPC; ordinary sends retain their wire', async t => {
  for (const compositeMode of [true, false]) {
    const h = await wireRenderer(t, compositeMode);
    const literal = '  Plan this\n\nwithout changing spacing  ';
    vm.runInContext(`state.slotChatRefs[0].inputEl.value = ${JSON.stringify(literal)}`, h.context);
    await vm.runInContext('sendChatComposer(0)', h.context);
    await new Promise(setImmediate);
    assert.equal(h.wires.length, 1);
    const wire = h.wires[0];
    assert.equal(wire.type, 'send');
    assert.ok(wire.request_id);
    if (compositeMode) {
      assert.equal(wire.stream_id, STREAM);
      assert.equal(wire.message, literal);
      assert.ok(wire.msg_id);
      assert.equal(wire.host, undefined);
      assert.equal(wire.session_name, undefined);
      assert.equal(wire.text, undefined);
      assert.equal(wire.optimistic_id, undefined);
    } else {
      assert.equal(wire.host, 'hostc');
      assert.equal(wire.session_name, 'claude-hostc-race');
      assert.equal(wire.text, literal.trim());
      assert.ok(wire.optimistic_id);
      assert.equal(wire.stream_id, undefined);
    }
  }
});

test('clicking a question-bound desktop reply preserves both IDs through composer, IPC and retry', async t => {
  const h = await wireRenderer(t);
  const button = h.dom.window.document.querySelector('.slot-chat-reply-btn');
  assert.ok(button);
  button.click();
  vm.runInContext("state.slotChatRefs[0].inputEl.value = 'Yes'", h.context);
  await vm.runInContext('sendChatComposer(0)', h.context);
  await new Promise(setImmediate);
  assert.equal(h.wires.length, 1);
  assert.equal(h.wires[0].reply_to_message_id, 'message-question');
  assert.equal(h.wires[0].reply_to_question_id, 'question-bound');
  assert.equal(h.store.retryOptimisticSend(h.wires[0].msg_id), true);
  await new Promise(setImmediate);
  assert.equal(h.wires.length, 2);
  assert.equal(h.wires[1].msg_id, h.wires[0].msg_id);
  assert.notEqual(h.wires[1].request_id, h.wires[0].request_id);
  assert.equal(h.wires[1].reply_to_message_id, 'message-question');
  assert.equal(h.wires[1].reply_to_question_id, 'question-bound');
});

test('composite attachment-only retries keep the blob envelope and logical message ID', async t => {
  const h = await wireRenderer(t);
  const attachments = [{ key: 'blob-fixture', mime: 'image/png', name: 'diagram.png', size: 12 }];
  const id = h.store.sendTurn(STREAM, '', attachments);
  await new Promise(setImmediate);
  assert.equal(h.wires[0].message, '');
  assert.deepEqual(h.wires[0].attachments, attachments);
  assert.equal(h.store.retryOptimisticSend(id), true);
  await new Promise(setImmediate);
  assert.equal(h.wires[1].msg_id, id);
  assert.deepEqual(h.wires[1].attachments, attachments);
  assert.notEqual(h.wires[1].request_id, h.wires[0].request_id);
});
