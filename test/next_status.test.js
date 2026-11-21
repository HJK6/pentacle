'use strict';
const test = require('node:test'); const assert = require('node:assert/strict');
const { statusRollup, nextMode } = require('../renderer/next/status_card');
test('status rollup handles no plan, active step and completion', () => {
  assert.deepEqual(statusRollup(), { total:0, done:0, percent:0, label:'' });
  assert.deepEqual(statusRollup({plan:[{status:'done'},{status:'active'},{status:'pending'}]}), {total:3,done:1,percent:33,label:'Step 2 of 3'});
  assert.equal(statusRollup({plan:[{status:'done'},{status:'done'}]}).label, '2/2 done');
});
test('terminal and status replace each other and toggle back to the thread', () => {
  assert.equal(nextMode('thread','terminal'),'terminal'); assert.equal(nextMode('terminal','status'),'status');
  assert.equal(nextMode('status','terminal'),'terminal'); assert.equal(nextMode('terminal','terminal'),'thread');
});
