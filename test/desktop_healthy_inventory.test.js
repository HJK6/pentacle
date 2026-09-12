const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');

test('degraded and cold-start inventory has no non-daemon sidebar source', () => {
  const renderer = fs.readFileSync(path.join(root, 'renderer', 'app.js'), 'utf8');
  const main = fs.readFileSync(path.join(root, 'main.js'), 'utf8')
    + fs.readFileSync(path.join(root, 'main', 'cc_handlers.js'), 'utf8');
  const preload = fs.readFileSync(path.join(root, 'preload.js'), 'utf8');

  assert.doesNotMatch(renderer, /setInterval\(fetchSessions,\s*5000\)/);
  assert.doesNotMatch(renderer, /setInterval\(pollActivity,\s*3000\)/);
  assert.doesNotMatch(renderer, /detectActivity\(\)/);
  assert.doesNotMatch(renderer, /captureAllPanes\(\)/);
  assert.equal(/Object\.defineProperty\(state, ['"]sessions['"]/.test(renderer), true);
  assert.equal(/state\.sessions\s*=/.test(renderer), false);
  assert.equal(/async function fetchSessions\(\)/.test(renderer), false);
  assert.doesNotMatch(renderer, /Raw tmux sessions/);
  assert.equal((main.match(/\.handle\('tmux:/g) || []).length, 2);
  assert.equal((preload.match(/ipcRenderer\.invoke\('tmux:/g) || []).length, 2);
  assert.doesNotMatch(main, /\.handle\('pty:detect-activity'/);
  assert.doesNotMatch(main, /\.handle\('pty:capture-all-panes'/);
  assert.doesNotMatch(preload, /detectActivity|captureAllPanes/);
});
