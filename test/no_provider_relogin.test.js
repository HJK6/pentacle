'use strict';
// The Settings provider re-login controls were removed (operator request
// 2026-11-04); rebuild is tracked in spec_pentacle__provider_relogin_flow_validation_2026_09.
// These tests necessarily name the removed channel and files.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { buildCc } = require('../renderer/web_cc');
const { createCcHandlers, createCollector } = require('../main/cc_handlers');
const { createWsBridge } = require('../server/ws_bridge');

const root = path.join(__dirname, '..');

test('web_cc exposes no relogin members', () => {
  const transport = { call: async () => ({}), fire() {}, on() {}, onReconnect() {} };
  const cc = buildCc(transport, { clipboard: {}, config: {} });
  assert.deepEqual(Object.keys(cc).filter(key => /relogin/i.test(key)), []);
});

test('the real handler table answers provider-relogin:hosts with unknown_method on an admitted socket', async t => {
  const collector = createCollector();
  const stop = createCcHandlers({ CONFIG: { chatStream: { localHost: 'fixture' } }, chatStreamClient: {} }).register(collector);
  const bridge = createWsBridge({ table: collector.table });
  const replies = [];
  const socket = { send: raw => replies.push(JSON.parse(raw)) };
  // reloginAllowed is ignored once the option is removed; admitting it keeps this
  // assertion on the handler table rather than on the origin gate.
  bridge.addSocket(socket, { reloginAllowed: true, micStartAllowed: true });
  t.after(async () => { bridge.closeAll(); await stop(); });
  await bridge.handleMessage(socket, JSON.stringify({ id: 1, method: 'provider-relogin:hosts', args: [] }));
  assert.equal(replies.length, 1);
  assert.equal(replies[0].error && replies[0].error.code, 'unknown_method', JSON.stringify(replies[0]));
});

test('the re-login sources and bridge wiring are gone', () => {
  assert.equal(fs.existsSync(path.join(root, 'renderer/provider_relogin_ui.js')), false);
  assert.equal(fs.existsSync(path.join(root, 'main/provider_relogin.js')), false);
  for (const file of ['main/cc_handlers.js', 'preload.js', 'renderer/web_cc.js', 'server/ws_bridge.js']) {
    assert.doesNotMatch(fs.readFileSync(path.join(root, file), 'utf8'), /provider-relogin/, file);
  }
});
