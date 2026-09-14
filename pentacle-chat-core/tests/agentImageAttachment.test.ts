import assert from 'node:assert/strict';
import test from 'node:test';

import {
  interpretPentacleEvent,
  type ChatAttachment,
  type PentacleEvent,
} from '../src/index.ts';

const STREAM_ID = 'host_c:claude-host_c-1b7d6cb6';
const SHA = 'a'.repeat(64);

function imageAttachment(): ChatAttachment {
  return { key: SHA, mime: 'image/png', width: 800, height: 600, bytes: 1234 };
}

function assistImageEvent(overrides: Partial<PentacleEvent> = {}): PentacleEvent {
  return {
    daemon_seq: 7,
    host: 'host_c',
    provider: 'claude',
    session_id: STREAM_ID,
    session_name: 'claude-host_c-1b7d6cb6',
    stream_id: STREAM_ID,
    timestamp: '2026-09-13T22:00:00.000Z',
    kind: 'ASSIST',
    text: '',
    attachments: [imageAttachment()],
    raw: { source: 'agent_image', request_id: 'req-1' },
    ...overrides,
  };
}

test('agent-authored image with a caption renders as the agent-side image bubble', () => {
  const interp = interpretPentacleEvent(assistImageEvent({ text: 'here is the chart' }));
  assert.equal(interp.displayRule, 'bubble:assistant');
  assert.equal(interp.tone, 'assistant');
  assert.equal(interp.hidden, false);
  assert.equal(interp.event.attachments?.[0]?.key, SHA);
  assert.equal(interp.event.attachments?.[0]?.mime, 'image/png');
});

test('caption-less agent image is NOT suppressed as furniture/noise', () => {
  // The image-only send has empty text; without the ASSIST-attachment branch it
  // would fall into the terminal-furniture / transient-noise hidden paths.
  const interp = interpretPentacleEvent(assistImageEvent({ text: '' }));
  assert.equal(interp.hidden, false);
  assert.equal(interp.displayRule, 'bubble:assistant');
  assert.equal(interp.tone, 'assistant');
  assert.equal(interp.event.attachments?.length, 1);
});

test('a plain ASSIST event without attachments is unaffected by the new branch', () => {
  const interp = interpretPentacleEvent(assistImageEvent({ text: 'ordinary reply', attachments: undefined }));
  assert.equal(interp.tone, 'assistant');
  assert.equal(interp.event.attachments, undefined);
});
