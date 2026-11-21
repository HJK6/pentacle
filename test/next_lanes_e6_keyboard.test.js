'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('E6 keyboard uses native text-bearing Enter while keeping real Tab events', async () => {
  const source = fs.readFileSync(path.join(__dirname, 'e2e/lib/next_lanes_e6_scenario.js'), 'utf8');
  const start = source.indexOf('async function press(');
  const end = source.indexOf('\nfunction seedPendingGoal(', start);
  assert.ok(start >= 0 && end > start);
  const press = vm.runInNewContext(`(${source.slice(start, end).trim()})`);
  const frames = [];
  const ctx = { session: { async send(method, params) { frames.push(JSON.parse(JSON.stringify({method, ...params}))); } } };
  await press(ctx, 'Tab', 'Tab', 9);
  await press(ctx, 'Enter', 'Enter', 13);
  assert.deepEqual(frames.map(frame => [frame.type, frame.key]), [
    ['keyDown', 'Tab'], ['keyUp', 'Tab'], ['keyDown', 'Enter'], ['keyUp', 'Enter'],
  ]);
  assert.equal(frames[2].text, '\r');
  assert.equal(frames[2].unmodifiedText, '\r');
  assert.equal(frames[0].text, undefined);
});
