const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(root, file), 'utf8');
// The main-process cc surface is split: native channels in main.js, the portable
// handlers in main/cc_handlers.js (shared with the headless web host). Guards
// below assert against both as one surface.
const readMain = () => read('main.js') + read('main/cc_handlers.js');

test('desktop spawn V2 crosses preload, main, and client with an explicit tuple', () => {
  const preload = read('preload.js');
  const main = readMain();
  const client = read('main/chat_stream_client.js');
  assert.match(preload, /chatSpawnV2: \(options\)/);
  assert.match(preload, /chatSpawnCatalog/);
  assert.match(main, /chat-stream:spawn-catalog/);
  assert.match(main, /spawnProfile === 'desktop_manual'/);
  assert.match(client, /payload\.schema = 'SpawnRequestV2'/);
  assert.match(client, /payload\.spawn_profile = spawnProfile/);
  assert.match(client, /payload\.catalog_version = catalogVersion/);
});

test('queued spawn state uses the uniform .ok contract without await IPC', () => {
  const preload = read('preload.js');
  const main = readMain();
  const client = read('main/chat_stream_client.js');
  assert.match(main, /response\?\.state === 'queued'/);
  assert.match(main, /streamId: response\.stream_id/);
  assert.doesNotMatch(preload, /chatAwaitSpawn/);
  assert.doesNotMatch(main, /chat-stream:await-spawn/);
  assert.doesNotMatch(main, /spawn\.queued|await_spawn\.pending/);
  assert.doesNotMatch(client, /spawn\.queued|await_spawn\.pending/);
});

test('main returns tuple readback and structured spawn errors', () => {
  const main = readMain();
  assert.match(main, /requested: session\?\.requested_launch_tuple/);
  assert.match(main, /resolved: session\?\.resolved_launch_tuple/);
  assert.match(main, /actualLaunch: session\?\.actual_launch_tuple/);
  assert.match(main, /remediation:/);
});
