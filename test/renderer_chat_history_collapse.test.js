'use strict';

// L6 (spec_pentacle__web_chat_transcript_collapse_2026_09): a chat slot whose
// history load leaves no rows must not present the synthesized answered-questions
// group as the whole conversation. It shows a status line and re-requests history
// on a bounded schedule until the daemon's rows arrive.

const test = require('node:test');
const assert = require('node:assert/strict');
const { installRenderer, mountRaceSlot, STREAM } = require('./helpers/renderer_chat');

const flush = () => new Promise((resolve) => setImmediate(resolve));
const ANSWERS_ONLY = '<details class="slot-chat-v3-answers"><summary>You answered 2 questions</summary></details>';

function fixture({ rowsAfterCall = Infinity } = {}) {
  const calls = [];
  const timers = [];
  let rows = [];
  const installed = installRenderer({
    requestStreamEvents: async (args) => {
      calls.push(args);
      if (calls.length >= rowsAfterCall) rows = [{ id: 'row-1', displayRule: 'bubble:assistant', text: 'ASSISTANT-TEXT' }];
      return { ok: true, count: 0 };
    },
    selectSessionDetail: (streamId) => ({ streamId, title: 'Race Fixture', providerLabel: 'claude', transcriptItems: rows, remainingCount: 0 }),
    renderTranscriptTimelineHtml: (detail) => (detail.transcriptItems.length
      ? `<article class="slot-chat-row"><div class="slot-chat-assistant-card">ASSISTANT-TEXT</div></article>${ANSWERS_ONLY}`
      : ANSWERS_ONLY),
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
  });
  return { ...installed, calls, timers };
}

async function mount(context) {
  await flush(); await flush();
  mountRaceSlot(context);
  await flush(); await flush();
}

function runDueTimers(timers, context) {
  const due = timers.splice(0);
  for (const timer of due) timer.fn();
  return due.map((timer) => timer.ms);
}

function listText(dom) {
  return dom.window.document.querySelector('.slot-chat-list')?.textContent || '';
}

test('zero-row history with answered questions shows a status line, never the answers group alone', async () => {
  const { context, dom, calls } = fixture();
  await mount(context);
  assert.equal(calls.length >= 1, true, 'initial history request');
  vmRender(context);
  const state = dom.window.document.querySelector('.slot-chat-history-state');
  assert.ok(state, 'status line present above the answered-questions group');
  assert.match(state.textContent, /Loading messages/);
  assert.match(listText(dom), /You answered 2 questions/);
});

test('zero-row history re-requests on a bounded backoff and renders text once rows arrive', async () => {
  const { context, dom, calls, timers } = fixture({ rowsAfterCall: 3 });
  await mount(context);
  const delays = [];
  for (let i = 0; i < 6 && !listText(dom).includes('ASSISTANT-TEXT'); i += 1) {
    delays.push(...runDueTimers(timers, context));
    await flush(); await flush();
    vmRender(context);
  }
  assert.equal(calls.length, 3);
  assert.deepEqual(delays.slice(0, 2), [1000, 2000]);
  assert.match(listText(dom), /ASSISTANT-TEXT/);
  assert.equal(dom.window.document.querySelector('.slot-chat-history-state'), null, 'status line cleared once rows render');
});

test('exhausted retries leave an honest error line with a manual Retry that resets the budget', async () => {
  const { context, dom, calls, timers } = fixture();
  await mount(context);
  const delays = [];
  for (let i = 0; i < 10; i += 1) {
    delays.push(...runDueTimers(timers, context));
    await flush(); await flush();
    vmRender(context);
  }
  assert.deepEqual(delays, [1000, 2000, 4000, 8000, 16000]);
  assert.equal(calls.length, 6, 'initial request plus five retries, then stop');
  const state = dom.window.document.querySelector('.slot-chat-history-state');
  assert.match(state.textContent, /Messages could not be loaded/);
  state.querySelector('.slot-chat-history-retry').click();
  await flush(); await flush();
  assert.equal(calls.length, 7, 'manual retry requests again');
  vmRender(context);
  assert.deepEqual(runDueTimers(timers, context), [1000], 'manual retry restarts the schedule');
});

function vmRender(context) {
  require('node:vm').runInContext('renderSlotChat(0)', context);
}

test('retry stops when the slot leaves chat mode', async () => {
  const { context, calls, timers } = fixture();
  await mount(context);
  require('node:vm').runInContext("state.slotViewModes[0] = 'terminal'", context);
  runDueTimers(timers, context);
  await flush();
  assert.equal(calls.length, 1);
  assert.equal(STREAM.length > 0, true);
});
