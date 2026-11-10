'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { installedJourney } = require('./lib/installed_journey_scenario');

async function readHistory(mode) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'installed-reader-'));
  const text = 'sassy sessions  preserve\tspaces\nand letter s';
  const fixtures = [0, 1, 2].map(i => ({ stream_id: 'fixture:reader-' + i, host: 'fixture', session_name: 'reader-' + i }));
  const before = [{ daemon_seq:1, kind:'USER', text, attachments:[] },
    { daemon_seq:2, kind:'ASSIST_TEXT', text:'fixture reply', attachments:[] }];
  let selected;
  const expressions = [];
  const session = {
    async send(method, args) {
      if (method === 'Page.navigate') selected = JSON.parse(new URL(args.url).searchParams.get('pentacle-chat-popout')).stream_id;
      return { identifier:'owned-hook' };
    },
    async waitFor() {},
    async eval(expression) {
      if (expression.includes('injected:window.__PENTACLE_CONFIG__')) return { injected:'accepted', served:'accepted' };
      if (expression.startsWith('window.__dashboardReloadMarker')) return true;
      if (expression.includes('window.cc.requestStreamEvents(')) return { ok:true, received:2, exhausted:true };
      if (expression.includes('getState().events')) { assert.ok(selected); return before; }
      if (expression.includes('document.querySelectorAll')) {
        expressions.push(expression);
        const nodes = mode === 'missing' ? [] : Array(mode === 'duplicate' ? 2 : 1).fill({ textContent:text });
        return vm.runInNewContext(expression, { document:{ querySelectorAll:() => nodes } });
      }
      if (expression.includes('optimisticSends')) return [];
      throw new Error('unexpected scenario evaluation: ' + expression);
    },
  };
  const descriptor = { phase:'history', url:'https://fixture.invalid', fixtures,
    expected_build_id:'accepted', before:{ 0:before, 1:before }, inputs:{ 0:[text], 1:[text] } };
  try {
    const run = installedJourney({ session, cdp:{ sleep:async () => {} }, report:{ dir, ok() {} } }, descriptor);
    if (mode === 'once') {
      await run;
      assert.equal(expressions.length, 2);
      for (const expression of expressions) assert.ok(expression.includes('replace(/\\s+/g'), 'serialized browser reader retains whitespace escape');
    } else {
      await assert.rejects(run, error => error.message.includes('rendered logical input appears once')
        && error.actual === (mode === 'duplicate' ? 2 : 0));
    }
  } finally {
    fs.rmSync(dir, { recursive:true, force:true });
  }
}

test('actual serialized history reader preserves letter s and normalizes whitespace', () => readHistory('once'));
test('actual serialized history reader refuses duplicate logical input', () => readHistory('duplicate'));
test('actual serialized history reader refuses missing logical input', () => readHistory('missing'));
