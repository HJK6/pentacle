const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const read = (...parts) => fs.readFileSync(path.join(root, ...parts), 'utf8');

test('desktop roster has one frame-fed source and no list-sessions path', () => {
  const renderer = read('renderer', 'app.js');
  const main = read('main.js');
  const preload = read('preload.js');
  const client = read('main', 'chat_stream_client.js');

  assert.match(renderer, /Object\.defineProperty\(state, ['"]sessions['"]/);
  assert.equal((renderer.match(/state\.chatStream\.sessions\s*=/g) || []).length, 2);
  assert.doesNotMatch(renderer, /state\.sessions\s*=/);
  assert.doesNotMatch(renderer, /state\.sessionHosts/);
  assert.doesNotMatch(renderer, /fetchSessions|listChatSessions/);
  assert.doesNotMatch(main, /chat-stream:list-sessions|list_sessions/);
  assert.doesNotMatch(preload, /listChatSessions|listSessions/);
  assert.doesNotMatch(client, /listSessions|list_sessions/);
});
