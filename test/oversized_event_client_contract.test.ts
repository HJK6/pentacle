import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { interpretPentacleEvent } from 'pentacle-chat-core';
import type { PentacleEvent } from 'pentacle-chat-core';

// The daemon regression asserts its actual encoder output equals this fixture.
const preview = JSON.parse(fs.readFileSync(path.join(
  process.cwd(), 'pentacle-chat-core/tests/fixtures/oversized-event-preview.json',
), 'utf8')) as PentacleEvent;

test('existing shared renderer displays the daemon oversized peer preview marker', () => {
  const rendered = interpretPentacleEvent(preview);
  assert.equal(rendered.hidden, false);
  assert.equal(rendered.displayRule, 'bubble:agent');
  assert.equal(rendered.text, preview.text);
  assert.match(rendered.text, /\[Event truncated for backfill/);
});

test('oversized peer preview preserves daemon housekeeping visibility', () => {
  const rendered = interpretPentacleEvent({
    ...preview, raw: { ...preview.raw, sender: 'daemon:status' },
  });
  assert.equal(rendered.hidden, true);
});
