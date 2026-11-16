'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { desktopRuntimePath } = require('../../main/runtime_paths');

test('test runner child writes only to its assigned home', () => {
  const home = os.homedir();
  const marker = desktopRuntimePath();
  fs.mkdirSync(path.dirname(marker), { recursive: true });
  fs.writeFileSync(marker, 'fixture-runtime-marker\n');
  console.log(`MARKER_PROBE_JSON=${JSON.stringify({ home, HOME: process.env.HOME, USERPROFILE: process.env.USERPROFILE })}`);
  assert.equal(fs.readFileSync(marker, 'utf8'), 'fixture-runtime-marker\n');
  if (process.env.PENTACLE_MARKER_PROBE_FAIL === '1') {
    assert.fail('intentional fixture failure after marker write');
  }
});
