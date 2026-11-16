'use strict';

// Pin the two real renderer paint paths. A send-landing bridge intentionally
// supplies both signals, but app.js still renders its legacy parsed-label clock
// whenever activity is working and the authoritative elapsed anchor is absent.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

test('working timer paints only under activity=working and retains its legacy fallback', () => {
  const app = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'app.js'), 'utf8');
  assert.match(app, /if \(activity === 'working'\) \{/);
  assert.match(app, /typeof daemonElapsed === 'number'/);
  assert.match(app, /else \{\s+if \(!state\.workingSince\[slot\]\)/s);
  assert.match(app, /timerLabel = chatUi\.formatElapsed\(Date\.now\(\) - state\.workingSince\[slot\]\)/);
});
