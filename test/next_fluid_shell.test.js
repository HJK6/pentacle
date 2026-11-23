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

function openShell(t, width = 1440, storedRail = null, storageUnavailable = false, configure = () => {}) {
  const dom = new JSDOM('<main id="app"></main>', { url: 'https://fixture.invalid/next/', runScripts: 'outside-only', pretendToBeVisual: true });
  const { window } = dom;
  t.after(() => window.close());
  window.innerWidth = width;
  window.Map = Map; // CommonJS modules and the evaluated browser entry share one realm in production.
  window.fetch = async () => { throw new Error('offline shell makes no network requests'); };
  if (storedRail) window.localStorage.setItem('pentacle.next.rail', storedRail);
  if (storageUnavailable) Object.defineProperty(window, 'localStorage', {
    get() { throw new window.DOMException('Storage unavailable', 'SecurityError'); },
  });
  configure(window);
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
  const activity = {};
  const elapsed = {};
  const wireDrafts = {};
  let notify = () => {}, frame = () => {};
  let sendNowAllowed = false, sendStage = '';
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
    renderTranscriptTimelineHtml: (id, unused, options) =>
      `<p>Transcript for ${id}</p>` + (sendStage ? `<div class="slot-chat-send-status is-${sendStage}">${sendStage === 'sending' ? 'sending…' : 'Queued'}${sendStage === 'queued' && options?.allowQueuedSendNow ?
        `<button class="slot-chat-send-now" data-stream-id="${id}" data-optimistic-id="receipt">Send now</button>`: ''}</div>` : ''),
    hydrateFileAttachments: async () => {},
  };
  window.PentacleChatStore = {
    getState: () => ({ sessions: rows, drafts: wireDrafts }), getLimits: () => [], subscribe(fn) { notify = fn; },
    getTurnPhase: id => activity[id] ?? 'idle',
    getTurnState: id => ({ phase: activity[id] ?? 'idle' }),
    getWorkingElapsedMs: id => elapsed[id] ?? null,
    canSendQueuedNow: (id, oid) => id === 'fixture:a' && oid === 'receipt' && sendNowAllowed,
    sendQueuedNow: (...args) => calls.push(['queued-now', ...args]),
    selectSessionDetail: id => { rendered.push(id); return id; }, applyFrame() {},
    sendTurn: (...args) => calls.push(['send', ...args]),
  };
  window.cc = {
    getChatStreamState: async () => ({ connected:true, sessions: rows }), promptList: async () => ({ok:true,questions:[]}),
    onChatStreamFrame(fn) { frame = fn; }, onPtyData(fn) { ptyData.add(fn); },
    requestStreamEvents: async args => { calls.push(['history', args.streamId]); return { ok:true }; },
    createPty: async (...args) => { calls.push(['create', ...args]); return 'pty:'+args[0]; },
    killPty: async (...args) => { calls.push(['kill', ...args]); },
    exitCopyMode() {}, writePty: (...args)=>calls.push(['write', ...args]),
    chatSpawnCatalog: async () => ({ok:false,error:'test-only catalog'}),
    chatRename: async (...args) => { calls.push(['rename', ...args]); return {ok: false}; },
    chatClose: async (...args) => { calls.push(['close', ...args]); return {ok: true}; },
  };
  onData = (slot, data) => { for (const fn of ptyData) fn(slot, data); };
  window.Function('require', fs.readFileSync(entry,'utf8'))(id => {
    if (id === '../web_cc') return { installWebCc() {} };
    if (id.startsWith('@xterm/')) return { Terminal: Term, FitAddon: Addon, Unicode11Addon: Addon, WebglAddon: Addon };
    return entryRequire(id);
  });
  const $ = css => window.document.querySelector(css);
  const choose = id => { $("button[aria-label^=Chats]").click(); $(`.popover [data-stream-id="fixture:${id}"]`).click(); };
  return { $, window, calls, rendered, onData, choose, rows, activity, elapsed, wireDrafts,
    update: () => notify(),
    inventory: () => { frame({type:'session.inventory', sessions:rows}); notify(); },
    sendState: (stage, allowed) => { sendStage = stage; sendNowAllowed = allowed; notify(); },
    shown: () => [...window.document.querySelectorAll('.chat-card:not([hidden])')] };
}

function draft(shell, section, text) {
  const input = shell.$(`${section} [data-next-message]`);
  input.value = text;
  input.dispatchEvent(new shell.window.Event('input', { bubbles:true }));
  return input;
}

test('fluid shell uses two actual card controllers and preserves draft and scroll, but starts in thread on replacement', async t => {
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
  assert.equal(s.$('[data-pane=chat] .status').hidden,true, 'the returning stream starts in thread mode');
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


test('returning to a chat that last used terminal starts in thread in a single card', async t => {
  const s = openShell(t, 440);
  await flush();
  s.choose('a');
  s.$('[data-pane=chat] [aria-label=Terminal]').click();
  for (let i=0;i<5;i++) await flush();
  assert.deepEqual(s.calls.filter(row => row[0]==='create').map(row => row.slice(1,3)), [[0,'a']]);
  s.choose('b');
  s.choose('a');
  for (let i=0;i<5;i++) await flush();
  assert.equal(s.$('[data-pane=chat] .terminal').hidden,true);
  assert.equal(s.$('[data-pane=chat] .thread').hidden,false);
  assert.deepEqual(s.calls.filter(row => row[0]==='create').map(row => row.slice(1,3)), [[0,'a']],
    'selecting A again did not open a second PTY');
});

test('narrow Keep puts Assistant on stage without losing the session in Chats', async t => {
  const s = openShell(t, 640);
  await flush();
  s.choose('a');
  s.$('.rail .brand').click();
  assert.equal(s.$('.assistant-overlay').hidden, false);
  s.$('[aria-label="Keep Fixture Assistant on screen"]').click();
  assert.equal(s.$('[data-pane=assistant]').hidden, false);
  assert.equal(s.shown().length, 0);
  s.choose('a');
  assert.equal(s.shown()[0].querySelector('.thread-inner').dataset.streamId,'fixture:a');
});

test('storage denial falls back to a left rail and still starts the workspace', async t => {
  const s = openShell(t, 1440, null, true);
  await flush();
  assert.equal(s.$('html').dataset.rail, 'left');
  assert.equal(s.$('[data-pane=assistant]').hidden, false);
  s.choose('a');
  assert.equal(s.shown()[0].querySelector('.thread-inner').dataset.streamId, 'fixture:a');
});

test('U1: the Next working time uses authoritative elapsed, then the carried start, a label or zero', async t => {
  const s = openShell(t);
  await flush(); s.choose('a');
  const a = s.rows[1]; a.working = true;
  const timer = () => s.$('[data-pane=chat] .next-working-time')?.textContent ?? '';
  s.elapsed[a.stream_id] = 3725000;
  s.update();
  assert.equal(timer(), '1h 02m', 'a real working chat renders the long time');
  for (const [ms, want] of [[7000,'7s'], [64000,'1m 04s']]) {
    s.elapsed[a.stream_id] = ms; s.update(); assert.equal(timer(), want);
  }
  delete s.elapsed[a.stream_id];
  a.working_label = '7s'; s.update(); assert.equal(timer(), '1m 04s');
  a.working = false; s.update(); assert.equal(timer(), '');
  a.working = true; s.update(); assert.equal(timer(), '7s');
  a.session_generation = 'replacement'; a.working_label = ''; s.update(); assert.equal(timer(), '0s');
  assert.equal(s.$('[data-pane=chat] .next-working').hidden, false);
});

test('U2: session, draft, and phase each work independently but the composite trusts its own state', async t => {
  const s = openShell(t);
  await flush(); s.choose('a');
  const [assistant, a] = s.rows;
  const active = key => s.$(`[data-pane=${key}] .next-working`)?.hidden === false;
  a.working = true; s.update(); assert.equal(active('chat'), true, 'inventory work shows a real status row');
  a.working = false; s.update(); assert.equal(active('chat'), false);
  s.wireDrafts[a.stream_id] = {raw: {working:true}}; s.update(); assert.equal(active('chat'), true);
  delete s.wireDrafts[a.stream_id]; s.activity[a.stream_id] = 'pending'; s.update(); assert.equal(active('chat'), true);
  assert.equal(s.$('[data-pane=chat] .next-working-time').textContent, '', 'pending alone leaves the duration blank');
  s.activity[a.stream_id] = 'working'; s.update();
  assert.equal(s.$('[data-pane=chat] .next-working-time').textContent, '0s', 'confirmed phase starts the clock');
  s.activity[a.stream_id] = 'idle'; s.update(); assert.equal(active('chat'), false);
  s.activity[assistant.stream_id] = 'working';
  s.wireDrafts[assistant.stream_id] = {raw:{working:true,working_label:'20s'}};
  s.update(); assert.equal(active('assistant'), false);
  assistant.working = true;
  s.elapsed[assistant.stream_id] = 9000;
  s.update(); assert.equal(active('assistant'), true);
  assert.equal(s.$('[data-pane=assistant] .next-working-time').textContent, '9s');
  delete s.elapsed[assistant.stream_id];
  s.update(); assert.equal(s.$('[data-pane=assistant] .next-working-time').textContent, '', 'no composite fallback');
});

test('U3: the rail counts newly unread chats offstage and does not mark a hidden or solo-covered card seen', async t => {
  const s = openShell(t);
  const [assistant, a, b, c] = s.rows;
  c.last_kind = 'ASSIST'; c.last_text = 'First boot';
  const badge = () => s.$('button[aria-label^=Chats]').getAttribute('aria-label');
  await flush(); assert.equal(badge(), 'Chats');
  s.choose('a'); s.choose('b');
  s.$('[data-pane=chat] [aria-label="Full screen"]').click();
  b.last_kind = 'ASSIST'; b.last_text = 'Behind solo'; s.update();
  assert.equal(badge(), 'Chats, 1 unread', 'a hidden stage card does not consume unread');
  a.last_kind = 'ASSIST'; a.last_text = 'Visible'; s.update(); assert.equal(badge(), 'Chats, 1 unread');
  Object.defineProperty(s.window.document, 'hidden', { configurable:true, value:true });
  s.$('[data-pane=chat] [aria-label="Leave full screen"]').click();
  assert.equal(badge(), 'Chats, 1 unread', 'hidden document does not consume unread');
  Object.defineProperty(s.window.document, 'hidden', { configurable:true, value:false });
  s.window.document.dispatchEvent(new s.window.Event('visibilitychange'));
  assert.equal(badge(), 'Chats');
  c.last_text = 'An unseen answer'; s.update(); assert.equal(badge(), 'Chats, 1 unread');
  c.last_kind = 'USER'; s.update(); assert.equal(badge(), 'Chats');
  assistant.last_kind = 'ASSIST'; assistant.last_text = 'Assistant out of view'; s.update();
  assert.equal(s.$('.brand').classList.contains('assistant-unread'), true);
  s.$('.brand').click();
  assert.equal(s.$('.brand').classList.contains('assistant-unread'), false);
});

test('U4: effective model and effort appear on a chat card but not in the new form', async t => {
  const s = openShell(t);
  const [, a, b] = s.rows;
  Object.assign(a,{model:'claude-sonnet-5',effort:'low',effective_model:'claude-opus-5-5',effective_effort:'high'});
  Object.assign(b,{model:'gpt-6-sol',effort:'default'});
  await flush(); s.choose('a');
  assert.equal(s.$('[data-pane=chat] .next-chat-model')?.textContent ?? '', 'Opus 5.5 · high');
  a.effective_effort = 'default'; s.update();
  assert.equal(s.$('[data-pane=chat] .next-chat-model').textContent, 'Opus 5.5');
  s.choose('b'); assert.equal(s.$('[data-pane=chat2] .next-chat-model').textContent, '6 Sol');
  b.model = null; s.update(); assert.equal(s.$('[data-pane=chat2] .next-chat-model').hidden, true);
  s.$('[aria-label=Chats]').click(); s.$('.popover [aria-label="New chat"]').click();
  assert.equal(s.$('.chat-card:has(.new-chat-host:not([hidden])) .next-chat-model').hidden, true);
});

test('U5: send decoration rechecks the capability even when transcript HTML itself did not change', async t => {
  const s = openShell(t);
  await flush(); s.choose('a');
  s.sendState('sending', false);
  const status = () => s.$('[data-pane=chat] .slot-chat-send-status');
  assert.equal(status().textContent, 'Sending', 'Next status words differ from the undecorated shared output');
  assert.equal(status().querySelectorAll('.stream i').length, 4);
  s.sendState('queued', false);
  assert.equal(status().querySelector('.slot-chat-send-now'), null);
  s.sendState('queued', true);
  const button = status().querySelector('.slot-chat-send-now');
  assert.equal(button.hidden, false);
  button.click(); assert.deepEqual(s.calls.find(r=>r[0]==='queued-now'), ['queued-now','fixture:a','receipt']);
  s.sendState('queued', false);
  assert.equal(button.isConnected, false);
  button.click(); assert.equal(s.calls.filter(r=>r[0]==='queued-now').length, 1, 'stale UI cannot dispatch');
});

test('title switching trades two sessions with draft and scroll, resetting both seats to thread', async t => {
  const s = openShell(t);
  await flush(); s.choose('a'); s.choose('b');
  draft(s,'[data-pane=chat]','draft a'); draft(s,'[data-pane=chat2]','draft b');
  s.$('[data-pane=chat2] .thread').scrollTop = 71;
  s.$('[data-pane=chat2] [aria-label=Status]').click();
  s.$('[data-pane=chat] .next-chat-title')?.click();
  const other = s.$('.popover:not([hidden]) [data-stream-id="fixture:b"]');
  assert.ok(other, 'the title opens a list for its own seat'); other.click();
  assert.equal(s.$('[data-pane=chat] .thread-inner').dataset.streamId, 'fixture:b');
  assert.equal(s.$('[data-pane=chat2] .thread-inner').dataset.streamId, 'fixture:a');
  assert.equal(s.$('[data-pane=chat] [data-next-message]').value,'draft b');
  assert.equal(s.$('[data-pane=chat2] [data-next-message]').value,'draft a');
  assert.equal(s.$('[data-pane=chat] .status').hidden,true);
  assert.equal(s.$('[data-pane=chat2] .status').hidden,true);
  assert.equal(s.$('[data-pane=chat] .thread').scrollTop,71);
});

test('rename suppresses Escape/blur saves, rejects empty names and rolls back a refusal; confirm closes the right seat', async t => {
  const s = openShell(t);
  await flush(); s.choose('a'); s.choose('b');
  s.$('[aria-label=Chats]').click();
  const row = id => s.$(`.next-chat-row:has([data-stream-id="fixture:${id}"])`);
  const startRename = () => {
    row('a').querySelector('.next-chat-rename').click();
    return s.$('.next-chat-edit');
  };
  const key = (el, key) => el.dispatchEvent(new s.window.KeyboardEvent('keydown',{key,bubbles:true}));
  assert.ok(row('a'), 'shared chat row is mounted');
  let input = startRename();
  assert.equal(input.selectionEnd, input.value.length);
  input.value='cancelled'; key(input,'Escape'); input.blur();
  input=startRename(); input.value='    '; input.blur();
  assert.equal(s.calls.filter(c=>c[0]==='rename').length,0);
  input=startRename(); input.value='optimistic'; key(input,'Enter');
  assert.equal(s.$('[data-pane=chat] .next-chat-title').textContent,'optimistic');
  await flush();
  assert.equal(s.$('[data-pane=chat] .next-chat-title').textContent,'a');
  assert.deepEqual(s.calls.filter(c=>c[0]==='rename'),[['rename','local','a','optimistic']]);
  row('a').querySelector('.next-chat-delete').click();
  s.$('.next-chat-cancel').click();
  assert.equal(s.calls.filter(c=>c[0]==='close').length,0);
  row('a').querySelector('.next-chat-delete').click();
  s.$('.next-chat-confirm').click(); await flush();
  assert.deepEqual(JSON.parse(JSON.stringify(s.calls.filter(c=>c[0]==='close'))),[['close','local','a',{operatorConfirm:true,force:true}]]);
  assert.equal(row('a'),null);
  assert.equal(s.$('[data-pane=chat]').hidden,true);
  assert.equal(s.$('[data-pane=chat2]').hidden,false);
});

test('U1 Settings applies an exact shared width, keeps focus during renders and restores invalid input', async t => {
  const s = openShell(t, 1440, null, false, window => window.localStorage.setItem('pentacle.next.read', '640'));
  await flush();
  const root = s.$('html');
  assert.equal(root.style.getPropertyValue('--read'), '640px', 'a stored width reaches the live workspace');
  s.$('[aria-label=Settings]').click();
  const field = s.$('#next-text-width');
  const slider = s.$('.next-width-slider');
  assert.equal(field.value, '640');
  assert.equal(field.maxLength, 4);
  field.focus();
  assert.ok(root.classList.contains('next-read-guides'));
  const input = (el, text) => { el.value = text; el.dispatchEvent(new s.window.Event('input', {bubbles:true})); };
  input(field, '905');
  assert.equal(root.style.getPropertyValue('--read'), '905px');
  assert.equal(s.window.localStorage.getItem('pentacle.next.read'), '905');
  s.update();
  assert.equal(s.window.document.activeElement, field);
  input(field, '12a'); assert.equal(root.style.getPropertyValue('--read'), '905px');
  field.blur(); assert.equal(field.value, '905');
  assert.ok(!root.classList.contains('next-read-guides'));
  slider.focus(); input(slider, '1200');
  assert.equal(field.value, '1200');
  assert.equal(root.style.getPropertyValue('--read'), '1200px');
  input(field, '2400');
  assert.equal(slider.value, '1200');
  assert.equal(slider.getAttribute('aria-valuetext'), '1200 pixels');
  assert.equal(field.value, '2400');
  s.$('[aria-label=Settings]').click();
  assert.ok(!root.classList.contains('next-read-guides'));
});

test('U2 Settings lists named physical microphones, preserves a sole selected mic and falls back on device loss', async t => {
  const d = (deviceId,label) => ({kind:'audioinput',deviceId,label});
  const alias = d('default','Default');
  const one = d('a','Input A'), two = d('b','Input B'), three = d('c','Input C');
  let devices = [alias, one, two];
  let target, selectionFailure;
  const requests = [];
  const s = openShell(t, 1440, null, false, window => {
    Object.defineProperty(window,'isSecureContext',{value:true});
    target = new window.EventTarget();
    target.enumerateDevices = async () => devices;
    target.getUserMedia = async opts => {
      requests.push(JSON.parse(JSON.stringify(opts)));
      throw new window.DOMException('Fixture, no capture', selectionFailure || 'NotAllowedError');
    };
    Object.defineProperty(window.navigator,'mediaDevices',{value:target});
    window.localStorage.setItem('pentacle.next.mic','b');
  });
  const store = s.window.localStorage;
  await flush(); s.choose('a'); s.$('[aria-label=Settings]').click();
  const current = () => s.$('.next-mic-choice')?.firstElementChild.textContent ?? '';
  assert.equal(current(), 'Input B', 'a saved microphone restores as a concrete choice');
  s.$('.next-mic-choice').click();
  assert.deepEqual([...s.window.document.querySelectorAll('.next-mic-options button')].map(el=>el.textContent),
    ['System default','Input A','Input B']);
  s.$('[aria-label=Settings]').click();
  const record = async pane => {
    s.$(`[data-pane=${pane}] [data-next-mic]`).click();
    await flush(); await flush();
  };
  await record('chat'); await record('assistant');
  assert.deepEqual(requests, [{audio:{deviceId:{exact:'b'}}},{audio:{deviceId:{exact:'b'}}}]);
  assert.equal(store.getItem('pentacle.next.mic'),'b', 'permission refusal is not unplugging');
  const updateDevices = async rows => { devices=rows; target.dispatchEvent(new s.window.Event('devicechange')); await flush(); };
  await updateDevices([alias, d('b','')]);
  s.$('[aria-label=Settings]').click();
  assert.equal(s.$('.next-mic-setting').hidden,true);
  assert.equal(store.getItem('pentacle.next.mic'),'b');
  s.$('[aria-label=Settings]').click();
  await record('chat');
  assert.deepEqual(requests.at(-1),{audio:{deviceId:{exact:'b'}}});
  selectionFailure='NotFoundError';
  await record('chat');
  assert.equal(store.getItem('pentacle.next.mic'),null);
  await updateDevices([alias,one,three]);
  s.$('[aria-label=Settings]').click();
  assert.equal(current(),'System default');
  s.$('[aria-label=Settings]').click();
  await record('assistant');
  assert.deepEqual(requests.at(-1),{audio:true});
});


test('only rail and title chat lists opt into the narrow scrollbar', async t => {
  const s=openShell(t);
  await flush();
  const popover=s.$('.popover');
  s.$('.chats-button').click();
  assert.ok(popover.classList.contains('next-chat-list-popover'));
  s.$('.popover [data-stream-id="fixture:a"]').click();
  s.$('[data-pane=chat] .next-chat-title').click();
  assert.ok(popover.classList.contains('next-chat-list-popover'));
  s.$('[aria-label=Settings]').click();
  assert.equal(popover.classList.contains('next-chat-list-popover'),false);
  const limits=s.$('[aria-label="Limits and machines"]');
  limits.click();
  try { assert.equal(popover.classList.contains('next-chat-list-popover'),false); }
  finally { limits.click(); }
});
