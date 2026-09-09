const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(root, 'renderer', 'app.js'), 'utf8');
const preloadSource = fs.readFileSync(path.join(root, 'preload.js'), 'utf8');

test('pop-out context is synchronous and guarded before terminal creation', () => {
  assert.match(preloadSource, /function chatPopoutContextFromArgv/);
  assert.match(preloadSource, /chatPopoutContext: \(\) => chatPopoutContext/);
  const guard = appSource.indexOf('if (IS_CHAT_POPOUT) {');
  const terminal = appSource.indexOf('const term = new Terminal({');
  assert.ok(guard >= 0 && terminal > guard, 'pop-out return must precede terminal construction');
  assert.match(appSource, /window\.PentacleChatStore\?\.applyFrame\?\.\(\{ type: 'snapshot'/);
  assert.match(appSource, /attachSession\(0, CHAT_POPOUT_CONTEXT\.session_name/);
});
