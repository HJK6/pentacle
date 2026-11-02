const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { EventEmitter } = require('node:events');
const { branding } = require(process.env.BRANDING_MODULE || './lib/branding_scenario');

test('artifact write failure cannot mask assertion or prevent viewport cleanup', async () => {
  const originalWrite = fs.writeFileSync;
  const notes = [], calls = [];
  let evalCount = 0;
  const session = {
    ws: new EventEmitter(),
    async eval(expression) {
      if (expression.includes('innerWidth')) return {width:1600,height:1000};
      evalCount++;
      if (evalCount === 1) return {theme:'dark',style:false,refreshHidden:true};
      if (evalCount === 2) throw Error('primary assertion');
      return null;
    },
    async send(method) { calls.push(method); },
  };
  fs.writeFileSync = () => { throw Error('artifact write failed'); };
  try {
    await assert.rejects(branding({session,report:{dir:'/unused',note:value=>notes.push(value)},cdp:{},url:'http://fixture'}), /primary assertion/);
  } finally { fs.writeFileSync = originalWrite; }
  assert.ok(calls.includes('Emulation.clearDeviceMetricsOverride'));
  assert.ok(notes.some(note => note.includes('artifact write failed')));
});
