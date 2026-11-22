'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { test } = require('node:test');
const { serveFeaturedScore } = require('../server/melody');

async function request(file, method = 'GET') {
  const response = {
    writeHead(status, headers) { Object.assign(this, { status, headers }); return this; },
    end(body) { this.body = body; },
  };
  await serveFeaturedScore({ method }, response, file);
  return response;
}

test('featured composition preserves rests and returns only playable fields', async t => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'melody-score-'));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const file = path.join(dir, 'score.json');
  const score = {
    id: 'demo', name: 'Test phrase', defaultGapMs: 200,
    notes: [{ midi: 60, durationMs: 180, gapMs: 0 }, { midi: 65, durationMs: 150, gapMs: 400 }],
  };
  await fs.writeFile(file, JSON.stringify({ ...score, privateNotes: 'editorial comments' }));
  const loaded = await request(file);
  assert.equal(loaded.status, 200);
  assert.equal(loaded.headers['cache-control'], 'private, no-store');
  assert.deepEqual(JSON.parse(loaded.body), score);
  assert.equal((await request(null)).status, 404);
  assert.equal((await request(file, 'POST')).status, 405);
  score.notes[0].gapMs = -1;
  await fs.writeFile(file, JSON.stringify(score));
  assert.equal((await request(file)).status, 503);
});
