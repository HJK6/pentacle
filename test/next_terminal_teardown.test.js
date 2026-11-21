'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { disposeTerminalView } = require('../renderer/terminal_view');

test('terminal teardown contains renderer errors and still disconnects the observer', () => {
  const calls = [];
  const view = { observer: { disconnect() { calls.push('observer'); } }, term: { dispose() { calls.push('terminal'); throw new Error('renderer failed'); } } };
  assert.doesNotThrow(() => disposeTerminalView(view, { warn() { calls.push('warning'); } }));
  assert.deepEqual(calls, ['observer', 'terminal', 'warning']);
});

test('an observer error does not prevent renderer disposal', () => {
  let disposed = false;
  const view = { observer: { disconnect() { throw new Error('observer failed'); } }, term: { dispose() { disposed = true; } } };
  disposeTerminalView(view, { warn() {} });
  assert.equal(disposed, true);
});
