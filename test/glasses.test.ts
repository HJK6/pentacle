import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
let createRoot: typeof import('react-dom/client').createRoot;
import { GlassesController, QUICK_REPLIES } from '../renderer/src/glasses_controller';
import { GlassesApp } from '../renderer/src/glasses_app';
import { buildCc } from '../renderer/web_cc';
import { registerNotificationIpcHandlers } from '../main/notification_ipc_bridge';

const ASSISTANT = 'host-a:assistant';
const BACKEND = 'host-a:worker';
const CHAT = 'host-b:session';
function session(streamId: string, extra: any = {}) {
  const [host, session_name] = streamId.split(':');
  return { stream_id: streamId, host, session_name, display_name: 'Neutral assistant', provider: 'codex',
    session_generation: 'generation-1', visibility: 'default', status: 'open', online: true,
    working: false, last_event_at: '2026-01-01T12:00:00Z', ...extra };
}
function event(stream_id: string, text: string, seq: number, kind = 'ASSIST') {
  return { stream_id, host: stream_id.split(':')[0], session_name: stream_id.split(':')[1],
    session_id: stream_id, daemon_seq: seq, timestamp: `2026-01-01T12:00:${String(seq).padStart(2, '0')}Z`, kind, text, provider: 'codex' };
}
function question(id = 'question-1') {
  return { question_id: id, notification_id: `notice-${id}`, producer_stream_id: BACKEND, state: 'open',
    envelope: { title: 'Choose an action', body: 'Which path should we take?', response_mode: 'single_choice', allow_custom: true,
      options: [{ label: 'First path', value: 'first' }, { label: 'Second path', value: 'second' }] } };
}
function notice(q: any) {
  // Deliberately reverse action order: positional mapping would answer incorrectly.
  return { notification_id: q.notification_id, state: 'open', actions: [
    { action_id: 'action-second', kind: 'yes_no', choice: false, label: 'Second path', value: { question_id: q.question_id, answer: 'second' } },
    { action_id: 'action-first', kind: 'yes_no', choice: true, label: 'First path', value: { question_id: q.question_id, answer: 'first' } },
  ] };
}
async function harness(extra: any = {}) {
  const dom = new JSDOM('<!doctype html><html><body><div id="app"></div></body></html>', { url: 'https://example.test/glasses' });
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true });
  const scrolls: any[] = [];
  dom.window.HTMLElement.prototype.scrollBy = function (args: any) { scrolls.push(args); };
  let frame: (value: any) => void = () => {};
  let reconnect: () => void = () => {};
  let open = extra.questions || [];
  let notices = extra.notices || open.map(notice);
  const sends: any[] = [];
  const settles: any[] = [];
  const lists: any[] = [];
  const history: any[] = [];
  let ids = 0;
  const cc: any = {
    getAssistantBinding: async () => ({ ok: true, source: 'durable', stream_id: BACKEND, generation: 'generation-1' }),
    getChatStreamState: async () => ({ connected: true, sessions: [session(ASSISTANT, { session_kind: 'assistant_composite', visibility: 'visible' }), session(BACKEND, { session_kind: 'assistant_backend', visibility: 'hidden' }), session(CHAT, { display_name: 'Project session' })], events: [event(ASSISTANT, 'Startup message', 1)], drafts: {} }),
    onChatStreamFrame: (fn: any) => { frame = fn; return () => {}; },
    onReconnect: (fn: any) => { reconnect = fn; return () => {}; },
    requestStreamEvents: async (args: any) => { history.push(args); return { ok: true, events: args.streamId === CHAT ? [event(CHAT, 'Latest session line', 4)] : [] }; },
    promptList: async (args: any) => { lists.push(args); return { ok: true, questions: open }; },
    notificationList: async () => ({ ok: true, notifications: notices }),
    notificationResolve: async (...args: any[]) => { settles.push(args); if (!extra.unconfirmed) open = open.filter((q: any) => q.notification_id !== args[0]); return { ok: true }; },
    chatSendCorrelated: async (...args: any[]) => { sends.push(args); if (extra.sendFailure && sends.length === 1) return { ok: false, error: 'Connection lost' }; return { ok: true }; },
    ...extra.cc,
  };
  const controller = new GlassesController(cc, () => `id-${++ids}`);
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
  createRoot ||= require('react-dom/client').createRoot;
  const root = createRoot(dom.window.document.getElementById('app')!);
  await act(async () => { root.render(React.createElement(GlassesApp, { controller })); await controller.start(); });
  async function key(key: string) { await act(async () => { dom.window.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })); }); }
  async function click(selector: string) { await act(async () => { (dom.window.document.querySelector(selector) as HTMLElement).click(); }); }
  async function emit(value: any) { await act(async () => { frame(value); }); }
  async function close() { await act(async () => { controller.dispose(); root.unmount(); }); dom.window.close(); }
  return { dom, controller, sends, settles, lists, history, scrolls, key, click, emit, reconnect, close };
}

test('startup state and live chat-stream frames render latest assistant messages safely', async () => {
  const h = await harness();
  try {
    assert.equal(h.controller.assistantId, ASSISTANT, 'never chooses the bound backend as the chat target');
    await h.key('Enter');
    assert.match(h.dom.window.document.querySelector('.thread')!.textContent!, /Startup message/);
    await h.emit({ type: 'chat.event', event: event(ASSISTANT, '<script>new message</script>', 2) });
    await h.emit({ type: 'chat.event', event: event(ASSISTANT, 'User message', 3, 'USER') });
    const thread = h.dom.window.document.querySelector('.thread')!;
    assert.match(thread.textContent!, /new message/);
    assert.equal(thread.querySelector('script'), null);
    assert.equal(thread.querySelector('.slot-chat-row')!.classList.contains('is-user'), true, 'newest message is first');
    assert.match(thread.querySelector('.slot-chat-user-bubble')!.textContent!, /User message/);
    await h.key('ArrowDown');
    assert.equal(h.scrolls[0].top, 130);
  } finally { await h.close(); }
});

test('focus order, Enter/Space activation, drill and back follow the mock', async () => {
  const h = await harness();
  try {
    assert.equal(h.dom.window.document.activeElement!.textContent!.includes('Neutral assistant'), true);
    assert.equal(h.dom.window.document.querySelectorAll('.home .row').length, 2);
    await h.key('ArrowDown');
    assert.match(h.dom.window.document.activeElement!.textContent!, /Chats/);
    await h.key(' ');
    assert.equal(String(h.controller.top.type), 'chats');
    assert.match(h.dom.window.document.activeElement!.textContent!, /Project session/);
    await h.key('Enter');
    assert.equal(String(h.controller.top.type), 'chat');
    assert.match(h.dom.window.document.querySelector('.thread')!.textContent!, /Latest session line/);
    assert.ok(h.history.some(args => args.streamId === CHAT));
    await h.key('Escape'); assert.equal(String(h.controller.top.type), 'chats');
    await h.key('Backspace'); assert.equal(String(h.controller.top.type), 'home');
  } finally { await h.close(); }
});

test('compose input/change commits and retry retains the same optimisticId', async () => {
  const h = await harness({ sendFailure: true });
  try {
    await h.key('Enter'); await h.key('Enter');
    assert.equal(h.controller.voice!.phase, 'rec');
    const textarea = h.dom.window.document.querySelector('textarea') as HTMLTextAreaElement;
    assert.equal(h.dom.window.document.activeElement, textarea, 'field is focusable for system composer activation');
    await act(async () => { textarea.value = 'A dictated update'; textarea.dispatchEvent(new h.dom.window.Event('input', { bubbles: true })); textarea.dispatchEvent(new h.dom.window.Event('change', { bubbles: true })); });
    assert.equal(h.controller.voice!.text, 'A dictated update');
    assert.equal(h.sends.length, 0, 'committing text cannot send accidentally');
    await h.click('.send-button');
    assert.equal(h.controller.voice!.phase, 'rec');
    assert.match(h.dom.window.document.querySelector('.voice .error')!.textContent!, /Connection lost/);
    await h.click('.send-button');
    assert.equal(h.controller.voice!.phase, 'sent');
    assert.equal(h.sends.length, 2);
    assert.deepEqual(h.sends[0].slice(0, 3), ['host-a', 'assistant', 'A dictated update']);
    assert.ok(h.sends[0][4]);
    assert.equal(h.sends[0][4], h.sends[1][4]);
    assert.equal(h.sends[0][3], h.sends[1][3]);
    assert.notEqual(h.sends[0][3], h.sends[0][4]);
  } finally { await h.close(); }
});

test('quick replies use the same correlated send and block duplicate activation while sending', async () => {
  let resolveSend: (value: any) => void = () => {};
  const calls: any[] = [];
  const h = await harness({ cc: { chatSendCorrelated: (...args: any[]) => { calls.push(args); return new Promise(resolve => { resolveSend = resolve; }); } } });
  try {
    await h.key('Enter'); await h.key('Enter');
    await h.key('ArrowDown'); // disabled empty send button is skipped
    assert.match(h.dom.window.document.activeElement!.textContent!, /brief update/);
    await h.key('Enter');
    assert.equal(h.controller.voice!.phase, 'sending');
    await h.key('Enter'); assert.equal(calls.length, 1);
    await act(async () => { resolveSend({ ok: true }); });
    assert.equal(h.controller.voice!.phase, 'sent');
    assert.deepEqual(calls[0].slice(0, 3), ['host-a', 'assistant', QUICK_REPLIES[0]]);
    assert.ok(calls[0][4]);
  } finally { await h.close(); }
});

test('questions map exact action IDs independent of ordering, page, read back and return', async () => {
  const h = await harness({ questions: [question(), question('question-2')] });
  try {
    assert.ok(h.lists.some(args => args.producer_stream_id === ASSISTANT && args.open === true));
    await h.key('Enter'); await h.key('ArrowRight');
    assert.equal(h.controller.top.type, 'questions');
    assert.match(h.dom.window.document.querySelector('.question-text')!.textContent!, /Which path/);
    assert.equal(h.dom.window.document.querySelectorAll('.pager span').length, 2);
    await h.key('ArrowRight'); assert.equal(h.controller.page, 1);
    await h.key('ArrowLeft'); assert.equal(h.controller.page, 0);
    await h.key('ArrowDown'); await h.key('Enter');
    assert.equal(h.settles[0][0], 'notice-question-1');
    assert.equal(h.settles[0][2].action_id, 'action-second');
    assert.deepEqual(h.settles[0][2].selections, ['second']);
    assert.equal(h.controller.answered.has('question-1'), true);
    assert.ok(h.dom.window.document.querySelector('.pager .answered'));
    await h.key('Enter'); assert.equal(h.controller.top.type, 'chat', 'last answer returns to chat');
  } finally { await h.close(); }
});

test('missing or mismatched notice actions fail closed; custom answer cannot bypass them', async () => {
  for (const notices of [[], [{ notification_id: 'notice-question-1', actions: [{ action_id: 'wrong', value: { answer: 'unrelated' } }] }]]) {
    const h = await harness({ questions: [question()], notices });
    try {
      await h.key('Enter'); await h.key('ArrowRight');
      assert.match(h.dom.window.document.querySelector('.read-only')!.textContent!, /Answer from phone\/desktop/);
      await h.key('Enter');
      assert.equal(h.settles.length, 0);
      assert.equal(h.dom.window.document.querySelectorAll('.amber button:not(:disabled)').length, 0);
    } finally { await h.close(); }
  }
});

test('resolve acknowledgement without removal from open set never paints answered', async () => {
  const h = await harness({ questions: [question()], unconfirmed: true });
  try {
    await h.key('Enter'); await h.key('ArrowRight'); await h.key('Enter');
    assert.equal(h.settles.length, 1);
    assert.equal(h.controller.answered.size, 0);
    assert.match(h.dom.window.document.querySelector('.error')!.textContent!, /not confirmed/);
    assert.equal(h.controller.top.type, 'questions');
  } finally { await h.close(); }
});

test('custom question text settles the question instead of sending a new chat message', async () => {
  const h = await harness({ questions: [question()] });
  try {
    await h.key('Enter'); await h.key('ArrowRight'); await h.key('ArrowDown'); await h.key('ArrowDown'); await h.key('Enter');
    await act(async () => { h.controller.commit('A different path'); });
    await h.click('.send-button');
    assert.equal(h.sends.length, 0);
    assert.equal(h.settles[0][2].custom_text, 'A different path');
    assert.equal(h.controller.answered.has('question-1'), true);
  } finally { await h.close(); }
});

test('unavailable, ambiguous and stale binding cannot open/send into an assistant', async () => {
  const variants = [[], [session(ASSISTANT, { session_kind: 'assistant_composite' }), session('host-a:another', { session_kind: 'assistant_composite' })],
    [session(ASSISTANT, { session_kind: 'assistant_composite' }), session(BACKEND, { session_generation: 'generation-2' })]];
  for (const sessions of variants) {
    const h = await harness({ cc: { getChatStreamState: async () => ({ connected: true, sessions, events: [], drafts: {} }) } });
    try {
      assert.equal(h.controller.assistantId, '');
      assert.match(h.dom.window.document.querySelector('.error')!.textContent!, /unavailable|Ambiguous/);
      await h.key('Enter'); assert.equal(String(h.controller.top.type), 'home');
      assert.equal(h.sends.length, 0);
    } finally { await h.close(); }
  }
});

test('live inventory generation change hides the old assistant transcript', async () => {
  const h = await harness();
  try {
    await h.key('Enter');
    await h.emit({ type: 'session.inventory', sessions: [session(ASSISTANT, { session_kind: 'assistant_composite' }), session(BACKEND, { session_generation: 'generation-2' })] });
    assert.equal(h.controller.assistantId, '');
    assert.doesNotMatch(h.dom.window.document.querySelector('.thread')!.textContent!, /Startup message/);
    assert.match(h.dom.window.document.querySelector('.thread')!.textContent!, /binding unavailable/);
    assert.equal((h.dom.window.document.querySelector('.compose-button') as HTMLButtonElement).disabled, true);
  } finally { await h.close(); }
});

test('working, blocked and offline indicators follow live session data', async () => {
  const h = await harness();
  try {
    await h.key('ArrowDown'); await h.key('Enter');
    await h.emit({ type: 'session.inventory', sessions: [session(CHAT, { working: true })] });
    assert.ok(h.dom.window.document.querySelector('.state-dot.working'));
    await h.emit({ type: 'session.inventory', sessions: [session(CHAT, { question: { prompt: 'Waiting' } })] });
    assert.ok(h.dom.window.document.querySelector('.state-dot.blocked'));
    await h.emit({ type: 'session.inventory', sessions: [session(CHAT, { online: false, host_status: 'offline' })] });
    assert.ok(h.dom.window.document.querySelector('.state-dot.offline'));
  } finally { await h.close(); }
});

test('binding invalidation while composing prevents submission', async () => {
  const h = await harness();
  try {
    await h.key('Enter'); await h.key('Enter');
    await act(async () => { h.controller.commit('Keep this draft'); });
    await h.emit({ type: 'session.inventory', sessions: [session(ASSISTANT, { session_kind: 'assistant_composite' }), session(BACKEND, { session_generation: 'generation-2' })] });
    await h.click('.send-button');
    assert.equal(h.sends.length, 0);
    assert.match(h.dom.window.document.querySelector('.voice .error')!.textContent!, /binding changed/);
    await h.key('Escape'); assert.equal(h.controller.voice, null);
  } finally { await h.close(); }
});

test('free-text question with an open notice and no option actions remains answerable', async () => {
  const q = question(); q.envelope.response_mode = 'free_text'; q.envelope.options = [];
  const h = await harness({ questions: [q], notices: [{ notification_id: q.notification_id, state: 'open', actions: [] }] });
  try {
    await h.key('Enter'); await h.key('ArrowRight'); await h.key('Enter');
    assert.ok(h.controller.voice);
    await act(async () => { h.controller.commit('A custom response'); });
    await h.click('.send-button');
    assert.equal(h.settles[0][1], 'resolved');
    assert.equal(h.settles[0][2].text, 'A custom response');
    assert.equal(h.controller.answered.has(q.question_id), true);
  } finally { await h.close(); }
});

test('long question text scrolls by arrows before moving focus to the next option', async () => {
  const h = await harness({ questions: [question()] });
  try {
    await h.key('Enter'); await h.key('ArrowRight');
    const text = h.dom.window.document.querySelector('.question-text') as HTMLElement;
    Object.defineProperties(text, { scrollHeight: { value: 500 }, clientHeight: { value: 185 } });
    await h.key('ArrowDown');
    assert.equal(h.scrolls.at(-1).top, 100);
    assert.equal(h.controller.focus, 0);
  } finally { await h.close(); }
});

test('600 square viewport, contained overflow, local fonts and bundled React contract', () => {
  const root = process.cwd();
  const html = fs.readFileSync(path.join(root, 'renderer/glasses.html'), 'utf8');
  const css = fs.readFileSync(path.join(root, 'renderer/glasses.css'), 'utf8');
  const build = fs.readFileSync(path.join(root, 'scripts/build-web.js'), 'utf8');
  assert.match(html, /width=600,height=600/);
  const dom = new JSDOM(`<style>${css}</style><div class="glasses"></div>`);
  try {
    const style = dom.window.getComputedStyle(dom.window.document.querySelector('.glasses')!);
    assert.equal(style.width, '600px'); assert.equal(style.height, '600px'); assert.equal(style.overflow, 'hidden');
    assert.equal(dom.window.getComputedStyle(dom.window.document.body).overflow, 'hidden');
    assert.ok(parseFloat(dom.window.getComputedStyle(dom.window.document.body).fontSize) >= 16);
    for (const family of ['Rajdhani', 'JetBrains Mono', 'Cinzel']) assert.ok(css.includes(family));
    for (const match of html.matchAll(/href="([^"]+\.ttf)"/g)) assert.ok(fs.existsSync(path.join(root, 'renderer', match[1])));
    assert.doesNotMatch(html + css, /unpkg|babel|https?:\/\//i);
    assert.match(build, /glasses_entry\.tsx/); assert.match(build, /glasses\.bundle\.js/);
  } finally { dom.window.close(); }
});

test('existing browser bridge preserves exact action and selections to the daemon client', async () => {
  const calls: any[] = [];
  const table: any = {};
  registerNotificationIpcHandlers({ handle: (channel: string, handler: any) => { table[channel] = handler; } },
    { notificationResolve: async (args: any) => { calls.push(args); return {}; } });
  const cc = buildCc({ call: async (channel: string, ...args: any[]) => table[channel]({}, ...args), fire: () => {}, on: () => () => {} }, { clipboard: {}, chatPopoutContext: null, config: {} });
  await cc.notificationResolve('neutral-notice', 'yes_no', { action_id: 'exact-action', selections: ['exact-value'], submit: true });
  assert.equal(calls[0].notification_id, 'neutral-notice');
  assert.equal(calls[0].action_id, 'exact-action');
  assert.deepEqual(calls[0].selections, ['exact-value']);
});
