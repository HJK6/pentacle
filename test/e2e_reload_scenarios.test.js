'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const Module = require('node:module');
const { CdpSession } = require('./e2e/lib/cdp');
const { SCENARIOS } = require('./e2e/lib/web_scenarios');

const fixture = { streamId: 'local:reload-fixture', sessionName: 'reload-fixture' };
const stopped = new Error('boundary observed; unrelated scenario body not simulated');
const transient = 'Inspected target navigated or closed (-32000)';
const targets = ['public-chat-renderer-contracts', 'slot-survives-cc-reconnect', 'slot-column-split', 'question-free-text', 'web-work-lanes'];

// Load the unchanged question scenario with only its fixture RPC counterpart
// replaced. Every scenario function and every reload/readiness path is real.
// This is adoption coverage, not a replacement for the full browser gate.
function questionScenario() {
  const filename = require.resolve('./e2e/lib/question_free_text_scenario');
  const mod = new Module(filename, module);
  mod.filename = filename;
  mod.paths = Module._nodeModulePaths(path.dirname(filename));
  const ordinaryRequire = mod.require.bind(mod);
  mod.require = id => id === './closed_chat_scenario' ? {
    fixtureRequest: async (_runtime, streamId, message) => ({ ok: true, question: {
      notification_id: message.envelope?.question_id || message.question_id,
      state: 'answered', producer_stream_id: streamId,
    } }),
  } : ordinaryRequire(id);
  mod._compile(fs.readFileSync(filename, 'utf8'), filename);
  return mod.exports.questionFreeText;
}

function counterpart(name, { phase = 'post', message = transient, failAt = null, timeoutMs = 1000 } = {}) {
  const events = [];
  const readinessSeen = new Set();
  let page = null, pending = false, stage = 0, reloads = 0, arms = 0, failedArm = false;
  let columnRow = 0, fractions = [.5, .5];
  function freshPage() {
    return vm.createContext({ window: {
      focusStreamId: () => true, HOST: {}, PentacleChatStore: {}, PentacleChatView: {},
      cc: { getChatStreamState: async () => ({ connected: true, sessions: [{ stream_id: fixture.streamId }] }) },
      __laneFrameSink: () => {}, __ccSocket: {},
    }, document: { readyState: 'complete', getElementById: () => ({}), querySelectorAll: () => [{}, {}] } });
  }
  page = freshPage();
  function geometry() {
    return { grid: { x: 0, width: 1000 }, gap: 0, sidebar: { width: 200 },
      handles: fractions.map((f, i) => ({ x: f * 1000 - 5, y: i * 100, width: 10, height: 100 })),
      cells: fractions.flatMap((f, i) => [{ x: 0, y: i * 100, width: f * 1000, height: 100 }, { x: f * 1000, y: i * 100, width: (1-f) * 1000, height: 100 }]),
      saved: fractions[0], savedBottom: fractions[1], appearance: {} };
  }
  const injected = () => failAt === null || reloads === failAt;
  const session = {
    async send(method, args = {}) {
      events.push(['send', method, args]);
      if (method === 'Page.reload') {
        reloads++; pending = true; stage = 0;
      } else if (method === 'Input.dispatchMouseEvent') {
        if (args.type === 'mousePressed') columnRow = args.y >= 100 ? 1 : 0;
        if (args.type === 'mouseReleased') fractions[columnRow] = args.x / 1000;
      }
      return { identifier: 'owned-script' };
    },
    async eval(expression) {
      events.push(['eval', expression, reloads]);
      const marker = /window\.__(?:dashboardReloadMarker|questionOldDocument|splitOldDocument|staleDocument)\s*=\s*true/.test(expression);
      if (marker) {
        arms++;
        if (phase === 'arming' && !failedArm && (failAt === null || reloads + 1 === failAt)) {
          failedArm = true;
          throw new Error(message);
        }
        return vm.runInContext(expression, page);
      }
      if (pending) {
        if (phase === 'deadline' && injected()) return false;
        if (stage === 1 && injected() && phase === 'post') throw new Error(message);
        // Run the actual readiness expression against an old, otherwise-ready
        // document. A bare readiness poll returns true here; marker-aware code
        // has to wait through rollover before advancing to scenario readiness.
        if (stage < 2) {
          if (/__dashboardReloadMarker|__questionOldDocument|__splitOldDocument|__staleDocument/.test(expression)) {
            return vm.runInContext(expression, page);
          }
          if (/focusStreamId|PentacleChatStore &&|window\.cc && window\.HOST/.test(expression)) {
            events.push(['old-ready-accepted', expression, reloads]);
            return vm.runInContext(expression, page);
          }
          throw new Error('ready-looking old document escaped the reload boundary');
        }
        pending = false;
      }
      if (expression.includes('__dashboardReloadMarker !== true')) return vm.runInContext(expression, page);
      const readiness = /focusStreamId\(.*\) === true|grid-col-resizer\[aria-valuenow\]|PentacleChatStore &&|getState\(\).sessions.some|typeof window.__laneFrameSink|s.connected\s*===\s*true|s.sessions\s*\|\||!!window.__ccSocket|session-list|window.cc && window.HOST/;
      if (reloads && readiness.test(expression)) {
        const key = `${reloads}:${expression}`;
        if (!readinessSeen.has(key)) {
          readinessSeen.add(key);
          events.push(['readiness-pending', expression, reloads]);
          return false;
        }
        events.push(['readiness-ready', expression, reloads]);
      }
      if (name === 'question-free-text') {
        if (expression.includes('return {visible:')) return { visible: true, label: 'Answer', disabled: true };
        if (expression === 'window.__questionResolutions') return [{ result: { ok: true } }];
        return true;
      }
      if (name === 'slot-column-split') {
        if (expression.includes('const grid = document.querySelector')) {
          if (reloads) throw stopped;
          return geometry();
        }
        return true;
      }
      if (name === 'public-chat-renderer-contracts' && expression.includes('const store = window.PentacleChatStore')) throw stopped;
      // Supply just the initial work-lanes observation; the first report below
      // stops its body, and the real finally must still remove/reload/connect.
      if (name === 'web-work-lanes' && expression.includes('stats: document.getElementById')) return { stats: '1 sessions |', rows: ['fixture'], labels: [], lanes: 0 };
      return true;
    },
    // Use the real non-tolerant CDP wait, including its eval exception semantics.
    waitFor: CdpSession.prototype.waitFor,
    async click(selector) { events.push(['click', selector, reloads]); return true; },
    async type() { return true; },
  };
  const cdp = { async sleep(ms) {
    events.push(['sleep', ms, reloads]);
    if (pending) {
      stage++;
      if (stage === 2) page = freshPage();
    }
  } };
  const report = {
    ok(label, value) { events.push(['ok', label, value]); if (name === 'web-work-lanes') throw stopped; assert.equal(!!value, true, label); },
    note(label) { events.push(['note', label]); },
  };
  const ctx = { session, cdp, report, fixture, timeoutMs, runtime: { fixtureTokens: {} },
    tmux(args) { events.push(['tmux', args]); if (args[0] === 'new-session') throw stopped; return ''; } };
  return { ctx, events, get reloads() { return reloads; }, get arms() { return arms; } };
}

async function exercise(name, options) {
  const fake = counterpart(name, options);
  const scenario = name === 'question-free-text' ? questionScenario() : SCENARIOS.find(([id]) => id === name)[1];
  let error;
  try { await scenario(fake.ctx); } catch (caught) { error = caught; }
  return { fake, error };
}
function assertSuccess(name, { fake, error }) {
  assert.equal(error, name === 'question-free-text' ? undefined : stopped, `${name}: unexpected boundary failure: ${error?.message}`);
  assert.equal(fake.reloads, name === 'question-free-text' ? 4 : name === 'slot-column-split' ? 1 : 2);
  assert.equal(fake.events.filter(([type]) => type === 'old-ready-accepted').length, 0, 'never proceed using the ready old document');
  assert.equal(fake.events.filter(([type, expr]) => type === 'eval' && expr === 'window.__dashboardReloadMarker = true').length >= fake.reloads, true, 'all invocations arm the shared helper');
  assert.ok(fake.events.some(([type]) => type === 'readiness-pending'), 'scenario readiness is independently pending after the fresh document');
  const expressions = fake.events.filter(([type]) => type === 'eval').map(([,expr]) => expr).join('\n');
  if (name === 'question-free-text') {
    assert.match(expressions, /focusStreamId\("local:reload-fixture"\) === true/);
    assert.equal(fake.events.filter(([type,label]) => type === 'ok' && label.includes('answered card stays retired after reload')).length, 4);
  } else if (name === 'slot-column-split') {
    const afterReload = fake.events.slice(fake.events.findIndex(([type, method]) => type === 'send' && method === 'Page.reload'));
    assert.ok(afterReload.some(([type,expr]) => type === 'eval' && expr.includes(".grid-col-resizer[aria-valuenow]")));
    assert.ok(afterReload.some(([type,expr]) => type === 'eval' && expr.includes('requestAnimationFrame')));
  } else if (name === 'public-chat-renderer-contracts') {
    assert.equal((expressions.match(/PentacleChatStore && window.PentacleChatView && window.cc/g) || []).length, 4);
    assert.match(expressions, /getState\(\).sessions.some/);
  } else if (name === 'web-work-lanes') {
    assert.match(expressions, /typeof window.__laneFrameSink === "function"/);
    assert.match(expressions, /s.sessions \|\| \[\]/);
    assert.equal((expressions.match(/s.connected === true/g) || []).length, 4);
    assert.ok(fake.events.some(([type, method]) => type === 'send' && method === 'Page.removeScriptToEvaluateOnNewDocument'));
  } else {
    assert.match(expressions, /!!window.__ccSocket/);
    assert.match(expressions, /s.sessions\|\|\[\]/);
    assert.equal((expressions.match(/s.connected===true/g) || []).length, 4);
    assert.ok(fake.events.some(([type, label, value]) => type === 'ok' && label.includes('cleanup restored') && value));
  }
}

for (const name of targets) {
  for (const phase of ['arming', 'post']) test(`${name}: real reload paths reject the old document and survive ${phase} context rollover`, async () => {
    assertSuccess(name, await exercise(name, { phase }));
  });
  for (const message of ['Target closed', 'unrelated protocol failure']) test(`${name}: genuine ${message} remains fatal`, async () => {
    const { error } = await exercise(name, { message, phase: 'post', failAt: 1 });
    assert.match(error?.message || '', new RegExp(message));
  });
  test(`${name}: fresh-document deadline remains fatal`, async () => {
    const { error, fake } = await exercise(name, { phase: 'deadline', timeoutMs: 0, failAt: 1 });
    if (name === 'slot-survives-cc-reconnect') {
      assert.match(error?.message || '', /cleanup restored a clean page/);
      assert.ok(fake.events.some(([type, label]) => type === 'note' && /fresh dashboard document/.test(label)));
    } else assert.match(error?.message || '', /fresh dashboard document/);
  });
}
for (const name of ['public-chat-renderer-contracts', 'web-work-lanes', 'slot-survives-cc-reconnect']) {
  test(`${name}: cleanup failure cannot turn into a pass`, async () => {
    const { error, fake } = await exercise(name, { message: 'Target closed', phase: 'post', failAt: 2 });
    assert.match(error?.message || '', name === 'slot-survives-cc-reconnect' ? /cleanup restored a clean page/ : /Target closed/);
    assert.equal(fake.reloads, 2);
  });
}
test('all target scenarios retain their unique registration and original order', () => {
  const names = SCENARIOS.map(([name]) => name);
  assert.deepEqual(names.filter(name => targets.includes(name)), targets);
  for (const name of targets) assert.equal(names.filter(id => id === name).length, 1);
  assert.equal(names.indexOf('web-voice-answers'), names.indexOf('question-free-text') + 1);
  assert.equal(names.indexOf('web-dashboards-revamp'), names.indexOf('web-voice-answers') + 1);
});
