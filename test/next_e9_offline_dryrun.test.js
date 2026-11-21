'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const { createRequire } = require('node:module');
const test = require('node:test');
const { JSDOM } = require('jsdom');
const core = require('../pentacle-chat-core/src/services/workLanes.ts');
const { buildQuestionPlacement } = require('../renderer/next/questions');
const { CdpSession } = require('./e2e/lib/cdp');
const { L11_ID, REVISION_A, questionFixture } = require('./e2e/lib/next_lanes_fixtures');
const { PRODUCER, installLaneBridge, openQuestionList, waitForQuestion } = require('./e2e/lib/next_lanes_harness');
const {
  createE9Fixture, startQuestionHeadingObserver, verifyE9Placement, cleanupEvidence,
} = require('./e2e/lib/next_lanes_late_scenarios');

async function openOfflineE9(t) {
  const dom = new JSDOM('<main id="app"></main>', { url: 'https://fixture.invalid/next/', runScripts: 'outside-only' });
  t.after(() => {
    dom.window.__nextLanesQuestionHeadingGuard?.stop();
    dom.window.close();
  });
  const { window } = dom;
  const fixture = createE9Fixture();
  const sessions = [{ stream_id: PRODUCER, display_name: 'Gate chat one', visibility: 'default', session_kind: 'session' }];
  const notifications = fixture.questions.map(({ specId, title }, index) => {
    const question = { ...questionFixture(fixture.ids[index], specId, { title }), state: 'open' };
    return { notification_id: 'notice-' + question.question_id, producer: 'agent_question.v1',
      title, body: question.body, state: 'open', question };
  });
  // Prove that the fixture producer can supply a chat heading when a question has no spec.
  const control = { ...notifications[0], question: { ...notifications[0].question, spec_id: null } };
  const chat = buildQuestionPlacement({ sessions, notifications: [control] }).questions[0].placement;
  assert.equal(chat.kind, 'chat');
  assert.equal(chat.chatTitle, sessions[0].display_name);

  window.innerWidth = 1440;
  window.requestAnimationFrame = callback => setImmediate(() => callback(window.performance.now()));
  window.PentacleChatCore = core;
  window.PentacleChatStore = { getState: () => ({ sessions }), getLimits: () => [], applyFrame() {}, subscribe() {} };
  window.cc = {
    getChatStreamState: async () => ({ connected: true, sessions, notifications }),
    promptList: async () => ({ ok: true, questions: notifications.map(item => item.question) }),
    onChatStreamFrame(callback) { window.__nextLanesFrameSink = callback; },
    onPtyData() {},
  };
  // jsdom supplies DOM events but has no viewport geometry. Use the gate's actual click and wait operations.
  window.HTMLElement.prototype.scrollIntoView = () => {};
  const session = {
    eval: async expression => structuredClone(await window.eval(expression)),
    click: CdpSession.prototype.click,
    waitFor: CdpSession.prototype.waitFor,
  };
  const report = {
    ok(label, passed, detail) {
      if (!passed) assert.fail(label + '\n' + JSON.stringify(detail));
    },
  };
  const ctx = { session, report, timeoutMs: 2000 };
  await startQuestionHeadingObserver(session, fixture.ids);

  // Boot the entry with in-memory bridge inputs. Terminal and socket setup are outside this journey.
  const entry = require.resolve('../renderer/next/entry');
  const entryRequire = createRequire(entry);
  window.Function('require', fs.readFileSync(entry, 'utf8'))(id => {
    if (id === '../web_cc') return { installWebCc() {} };
    if (id.startsWith('@xterm/')) return {};
    return entryRequire(id);
  });
  await installLaneBridge(ctx, fixture.bridge);
  await openQuestionList(ctx);
  for (const [index, id] of fixture.ids.entries()) {
    await waitForQuestion(ctx, id);
    const row = window.document.querySelector(`.next-question-list-item[data-question-id="${id}"]`);
    assert.equal(row.querySelector('.next-question-heading-title')?.textContent, fixture.questions[index].title);
  }
  await window.__nextLanesSetInventory(fixture.firstFrame);
  return { window, ctx, fixture };
}

test('E9 placement journey executes the gate predicates with the real entry and click-away behavior', async t => {
  const { ctx, fixture } = await openOfflineE9(t);
  await verifyE9Placement(ctx, fixture);
});

test('E9 rejects a list that disappears during stale A and retains the missing row in failure evidence', async t => {
  const { window, ctx, fixture } = await openOfflineE9(t);
  const release = window.__nextLanesReleaseMatchingAndFlush;
  window.__nextLanesReleaseMatchingAndFlush = async match => {
    const flushed = release(match);
    if (match.laneId === L11_ID && match.revision === REVISION_A) {
      const button = window.document.querySelector('[aria-label^="Questions"]');
      button.click();
      await new Promise(resolve => window.requestAnimationFrame(resolve));
      button.click();
    }
    return flushed;
  };
  await assert.rejects(verifyE9Placement(ctx, fixture), /stays observable through the stale A release/);
  const evidence = await cleanupEvidence(ctx, []);
  assert.deepEqual(evidence.captureErrors, []);
  const moved = evidence.headingHistory.filter(item => item.questionId === fixture.ids[4] && item.surface === 'list');
  assert.equal(moved.at(-2).present, false);
  assert.equal(moved.at(-1).ownTitle, fixture.questions[4].title);
});
