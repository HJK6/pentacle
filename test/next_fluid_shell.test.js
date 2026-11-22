'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const { createRequire } = require('node:module');
const test = require('node:test');
const { JSDOM } = require('jsdom');
const entry = require.resolve('../renderer/next/entry');
const entryRequire = createRequire(entry);
const laneCore = require('../pentacle-chat-core/src/services/workLanes.ts');
const flush = () => new Promise(resolve => setImmediate(resolve));

function openShell(t, width = 1440, storedRail = null) {
  const dom = new JSDOM('<main id="app"></main>', { url: 'https://fixture.invalid/next/', runScripts: 'outside-only' });
  const { window } = dom;
  t.after(() => window.close());
  window.innerWidth = width;
  window.fetch = async () => { throw new Error('offline shell makes no network requests'); };
  if (storedRail) window.localStorage.setItem('pentacle.next.rail', storedRail);
  window.requestAnimationFrame = fn => setImmediate(fn);
  window.ResizeObserver = class { observe() {} disconnect() {} };
  if (!window.HTMLElement.prototype.setPointerCapture) {
    window.HTMLElement.prototype.setPointerCapture = function () {};
    window.HTMLElement.prototype.hasPointerCapture = function () { return false; };
  }
  const rows = [
    { stream_id: 'fixture:assistant', title: 'Fixture Assistant', assistant_display_name: 'Fixture Assistant', visibility: 'default', session_kind: 'assistant_composite' },
    ...['a','b','c'].map(id => ({ stream_id: 'fixture:'+id, title: id, visibility: 'default', session_kind: 'session', session_name: id, host: 'local', status_card: { goal: 'Goal '+id } })),
  ];
  const calls = [];
  const rendered = [];
  let onData;
  const ptyData = new Set();
  class Term {
    cols = 72;
    rows = 24;
    unicode = {};
    loadAddon() {}
    open(mount) { this.element = window.document.createElement('i'); mount.append(this.element); }
    attachCustomKeyEventHandler() {}
    onData(fn) { this.type = fn; }
    write(content) { this.element.textContent += content; }
    dispose() { this.element.remove(); }
  }
  class Addon { fit() {} onContextLoss() {} }
  window.PentacleChatCore = { ...laneCore, MAX_CHAT_ATTACHMENTS: 5 };
  window.PentacleChatView = {
    renderTranscriptTimelineHtml: id => `<p>Transcript for ${id}</p>`,
    hydrateFileAttachments: async () => {},
  };
  window.PentacleChatStore = {
    getState: () => ({ sessions: rows }), getLimits: () => [], subscribe() {},
    selectSessionDetail: id => { rendered.push(id); return id; }, applyFrame() {},
    sendTurn: (...args) => calls.push(['send', ...args]),
  };
  window.cc = {
    getChatStreamState: async () => ({ connected:true, sessions: rows }), promptList: async () => ({ok:true,questions:[]}),
    onChatStreamFrame() {}, onPtyData(fn) { ptyData.add(fn); },
    requestStreamEvents: async args => { calls.push(['history', args.streamId]); return { ok:true }; },
    createPty: async (...args) => { calls.push(['create', ...args]); return 'pty:'+args[0]; },
    killPty: async (...args) => { calls.push(['kill', ...args]); },
    exitCopyMode() {}, writePty: (...args)=>calls.push(['write', ...args]),
    chatSpawnCatalog: async () => ({ok:false,error:'test-only catalog'}),
  };
  onData = (slot, data) => { for (const fn of ptyData) fn(slot, data); };
  window.Function('require', fs.readFileSync(entry,'utf8'))(id => {
    if (id === '../web_cc') return { installWebCc() {} };
    if (id.startsWith('@xterm/')) return { Terminal: Term, FitAddon: Addon, Unicode11Addon: Addon, WebglAddon: Addon };
    return entryRequire(id);
  });
  const $ = css => window.document.querySelector(css);
  const choose = id => { $('button[aria-label=Chats]').click(); $(`.popover [data-stream-id="fixture:${id}"]`).click(); };
  return { $, window, calls, rendered, onData, choose, shown: () => [...window.document.querySelectorAll('.chat-card:not([hidden])')] };
}

function draft(shell, section, text) {
  const input = shell.$(`${section} [data-next-message]`);
  input.value = text;
  input.dispatchEvent(new shell.window.Event('input', { bubbles:true }));
  return input;
}

test('fluid shell uses two actual card controllers and preserves draft, status mode and scroll across replacement', async t => {
  const s = openShell(t);
  await flush();
  s.choose('a'); s.choose('b');
  assert.equal(s.shown().length, 2, 'production entry actually mounts two cards');
  assert.match(s.$('[data-pane=chat] .thread-inner').textContent, /fixture:a/);
  assert.match(s.$('[data-pane=chat2] .thread-inner').textContent, /fixture:b/);
  draft(s, '[data-pane=chat]', 'A only').dispatchEvent(new s.window.KeyboardEvent('keydown', {key:'Enter',bubbles:true,cancelable:true}));
  draft(s, '[data-pane=chat2]', 'B draft').focus();
  await flush();
  assert.ok(s.calls.some(([name,id,value])=>name==='send'&&id==='fixture:a'&&value==='A only'));
  s.$('[data-pane=chat] .thread').scrollTop=37;
  s.$('[data-pane=chat] [aria-label=Status]').click();
  assert.equal(s.$('[data-pane=chat] .status').hidden,false);
  s.choose('c');
  assert.equal(s.$('[data-pane=chat] .thread-inner').dataset.streamId,'fixture:c');
  assert.equal(s.$('[data-pane=chat2] [data-next-message]').value,'B draft');
  s.$('[data-pane=chat2] [data-next-message]').focus();
  s.choose('a');
  assert.equal(s.$('[data-pane=chat] .status').hidden,false, 'the returning stream keeps status mode');
  assert.match(s.$('[data-pane=chat] .status').textContent,/Goal a/);
  s.$('[data-pane=chat] [aria-label=Status]').click();
  assert.equal(s.$('[data-pane=chat] .thread').scrollTop,37);
  assert.equal(s.$('[data-pane=chat2] [data-next-message]').value,'B draft');
  for (let i=0;i<2;i++) { s.$('button[aria-label=Chats]').click(); s.$('.popover [aria-label="New chat"]').click(); }
  assert.equal(s.window.document.querySelectorAll('.chat-card:not([hidden]) .new-chat-host:not([hidden])').length,1);
});

test('fluid terminal controllers own two independent bridge slots and preserve the surviving terminal', async t => {
  const s = openShell(t);
  await flush();
  s.choose('a'); s.choose('b');
  assert.equal(s.shown().length,2);
  s.$('[data-pane=chat] [aria-label=Terminal]').click();
  s.$('[data-pane=chat2] [aria-label=Terminal]').click();
  for (let i=0;i<5;i++) await flush();
  assert.deepEqual(s.calls.filter(c=>c[0]==='create').map(c=>c.slice(1,3)), [[0,'a'],[1,'b']]);
  s.onData(0,'alpha'); s.onData(1,'beta');
  assert.equal(s.$('[data-pane=chat] .terminal').textContent,'alpha');
  assert.equal(s.$('[data-pane=chat2] .terminal').textContent,'beta');
  s.$('[data-pane=chat] [aria-label=Close]').click();
  await flush();
  assert.deepEqual(s.calls.filter(c=>c[0]==='kill'),[['kill',0]]);
  s.onData(1,' stays');
  assert.equal(s.$('[data-pane=chat2] .terminal').textContent,'beta stays');
});

test('fluid shell sidebar reads storage and mark preserves the hidden chat beneath its overlay', async t => {
  const s = openShell(t, 1440, 'top');
  await flush();
  assert.equal(s.window.document.documentElement.dataset.rail,'top');
  s.$('[aria-label=Settings]').click(); s.$('[aria-label=Left]').click();
  assert.equal(s.window.localStorage.getItem('pentacle.next.rail'),'left');
  s.choose('a'); s.choose('b');
  const mark = s.$('.rail .brand');
  mark.click();
  assert.equal(mark.getAttribute('aria-pressed'),'true');
  assert.equal(s.$('.assistant-overlay').hidden,false);
  assert.equal(s.shown().length,2);
  s.$('[aria-label="Keep Fixture Assistant on screen"]').click();
  assert.equal(s.shown().length,1);
  assert.equal(s.shown()[0].querySelector('.thread-inner').dataset.streamId,'fixture:b');
});
