'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { cleanupOwnedFixtures, isVerifiedAbsent } = require('./cleanup');

test('dispose failure still kills the owned server and removes both directories', async () => {
  const local = fs.mkdtempSync(path.join(os.tmpdir(), 'terminal-cleanup-local-'));
  const remote = fs.mkdtempSync(path.join(os.tmpdir(), 'terminal-cleanup-remote-'));
  let serverAlive = true;
  const result = await cleanupOwnedFixtures({
    serverClaimed: true,
    restoreClipboard: async () => {},
    disposePtys: () => { throw new Error('injected dispose failure'); },
    killServer: () => { serverAlive = false; },
    verifyServerAbsent: () => !serverAlive,
    removeRemote: () => fs.rmSync(remote, { recursive: true }),
    removeLocal: () => fs.rmSync(local, { recursive: true }),
  });
  assert.equal(serverAlive, false);
  assert.equal(fs.existsSync(local), false);
  assert.equal(fs.existsSync(remote), false);
  assert.deepEqual(result.errors.map(error => error.stage), ['disposePtys']);
});

test('remote removal failure still removes local files and reports both restore and remote errors', async () => {
  let localRemoved = false;
  const result = await cleanupOwnedFixtures({
    serverClaimed: false,
    restoreClipboard: () => { throw new Error('injected restore failure'); },
    disposePtys: () => {},
    removeRemote: () => { throw new Error('injected remote rm failure'); },
    removeLocal: () => { localRemoved = true; },
  });
  assert.equal(localRemoved, true);
  assert.deepEqual(result.errors.map(error => error.stage), ['restoreClipboard', 'removeRemote']);
});

test('uncertain server cleanup preserves both fixture roots and reports verification errors', async () => {
  let removed = 0;
  const result = await cleanupOwnedFixtures({
    serverClaimed: true,
    restoreClipboard: () => {}, disposePtys: () => {},
    killServer: () => { throw new Error('SSH disconnected'); },
    verifyServerAbsent: () => { throw new Error('SSH still disconnected'); },
    removeRemote: () => removed++, removeLocal: () => removed++,
  });
  assert.equal(removed, 0);
  assert.equal(result.fixtureDirectoriesPreserved, true);
  assert.deepEqual(result.errors.map(error => error.stage), ['verifyServerAbsent', 'killServer']);
});

test('a preclaimed create timeout still triggers cleanup; only verified server absence passes', async () => {
  let claimed = false, serverAlive = false, killed = false;
  try {
    claimed = true;
    await (async () => { serverAlive = true; throw new Error('create reply timed out'); })();
  } catch {}
  const result = await cleanupOwnedFixtures({
    serverClaimed: claimed,
    restoreClipboard: () => {}, disposePtys: () => {},
    killServer: () => { killed = true; serverAlive = false; },
    verifyServerAbsent: () => !serverAlive,
    removeLocal: () => {},
  });
  assert.equal(killed, true);
  assert.equal(serverAlive, false);
  assert.equal(result.serverCleaned, true);
  assert.equal(isVerifiedAbsent({ code: 1, stderr: 'no server running on /tmp/owned-socket\n' }), true);
  assert.equal(isVerifiedAbsent({ code: 255, stderr: 'ssh: connection failed' }), false);
  assert.equal(isVerifiedAbsent({ code: 1, stderr: 'permission denied' }), false);
  const absent = await cleanupOwnedFixtures({
    serverClaimed: true, restoreClipboard: () => {}, disposePtys: () => {},
    killServer: () => { throw new Error('server not created'); },
    verifyServerAbsent: () => true, removeLocal: () => {},
  });
  assert.equal(absent.errors.length, 0);
  assert.equal(absent.serverCleaned, true);
});
