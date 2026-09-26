'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const esbuild = require('esbuild');

const { createCcHandlers, createCollector } = require('../main/cc_handlers');
const chatStreamClient = require('../main/chat_stream_client');

test('the bundled chat entry hands the captured generation to the browser bridge', async () => {
  const source = esbuild.buildSync({
    entryPoints: ['renderer/src/chat_core_entry.ts'],
    bundle: true, platform: 'browser', format: 'iife', target: 'chrome134', write: false,
  }).outputFiles[0].text;
  const calls = [];
  const context = {
    cc: { chatInterrupt(...args) {
      calls.push(Array.from(args));
      return Promise.resolve({ ok: true, interrupted: true, confirm: 'interrupt_unconfirmed' });
    } },
    console, process: { env: {} }, setTimeout, clearTimeout,
  };
  vm.runInNewContext(source, context, { filename: 'chat_core_entry.bundle.js' });
  const store = context.PentacleChatStore;
  store.applyFrame({ type: 'session.inventory', sessions: [{
    stream_id: 'peer:disposable-seat', host: 'peer', session_name: 'disposable-seat',
    provider: 'codex', session_generation: 'selected-generation',
  }] });
  store.getState().workingByStream['peer:disposable-seat'] = {
    phase: 'working', optimisticId: 'turn-1', sentAt: 1,
  };
  assert.equal(store.cancelTurn('peer:disposable-seat'), true);
  store.applyFrame({ type: 'session.inventory', sessions: [{
    stream_id: 'peer:disposable-seat', host: 'peer', session_name: 'disposable-seat',
    provider: 'codex', session_generation: 'replacement-generation',
  }] });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls, [['peer', 'disposable-seat', 'selected-generation']]);
  store.dispose();
});

function desktopInterruptArgs(host, sessionName, expectedSessionGeneration) {
  const calls = [];
  const ipcRenderer = {
    invoke(channel, ...args) { calls.push({ channel, args }); return Promise.resolve(); },
    send() {}, on() {}, removeAllListeners() {},
  };
  const context = {
    require(name) {
      if (name === 'electron') return { clipboard: { writeText() {}, readText: () => '' }, ipcRenderer };
      if (name === './config-loader') return { loadConfig: () => ({ config: {} }) };
      return require(name);
    },
    window: {}, process, console,
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'preload.js'), 'utf8'), context,
    { filename: 'preload.js' });
  context.window.cc.chatInterrupt(host, sessionName, expectedSessionGeneration);
  return { channel: calls[0]?.channel, args: Array.from(calls[0]?.args || []) };
}

test('desktop preload carries the selected generation and preserves a local legacy call', () => {
  assert.deepEqual(desktopInterruptArgs('peer', 'remote-seat', 'selected-generation'), {
    channel: 'chat-stream:interrupt', args: ['peer', 'remote-seat', 'selected-generation'],
  });
  assert.deepEqual(desktopInterruptArgs(undefined, 'local-seat', undefined), {
    channel: 'chat-stream:interrupt', args: ['local', 'local-seat', undefined],
  });
});

test('IPC and wire carry generation; a missing remote generation returns a typed code', async (t) => {
  const original = chatStreamClient.sendCommand;
  t.after(() => { chatStreamClient.sendCommand = original; });
  const payloads = [];
  chatStreamClient.sendCommand = async (payload, prefix) => {
    assert.equal(prefix, 'send.interrupt');
    payloads.push(payload);
    if (payload.host === 'peer' && !payload.expected_session_generation) {
      throw { type: 'send.interrupt.error', error_code: 'generation_required', error: 'generation_required' };
    }
    return { type: 'send.interrupt.ok', interrupted: true, confirm: 'interrupt_unconfirmed' };
  };
  const collector = createCollector();
  createCcHandlers({ CONFIG: { chatStream: {} }, chatStreamClient, harness: true }).register(collector);
  const interrupt = collector.table['chat-stream:interrupt'].handler;

  const delivered = await interrupt(null, 'peer', 'remote-seat', 'selected-generation');
  assert.equal(delivered.ok, true);
  assert.equal(payloads[0].expected_session_generation, 'selected-generation');

  const refused = await interrupt(null, 'peer', 'remote-seat');
  assert.equal(refused.ok, false);
  assert.equal(refused.code, 'generation_required');
  assert.equal(Object.hasOwn(payloads[1], 'expected_session_generation'), false);

  const local = await interrupt(null, 'local', 'local-seat');
  assert.equal(local.ok, true);
  assert.equal(Object.hasOwn(payloads[2], 'expected_session_generation'), false);
});
