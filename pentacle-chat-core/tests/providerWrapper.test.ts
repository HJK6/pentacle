import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { optimisticMatchesServerUser } from '../src/services/optimisticMatch.ts';
import type { PentacleEvent } from '../src/types/pentacle.ts';
import { normalizeProviderUserText, normalizeSubmissionText, providerDisplayText } from '../src/services/providerWrapper.ts';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/provider-wrapper.json', import.meta.url), 'utf8'));
const timestamp = fixture.record.timestamp;
const stream = 'hosta:wrapper-target';
const send = { stream_id: stream, text: fixture.display_text, created_at: Date.parse(timestamp), optimistic_id: 'opt-wrapper' };
const event: PentacleEvent = {
  daemon_seq: 1, host: 'hosta', provider: 'claude', session_id: fixture.record.sessionId,
  session_name: 'wrapper-target', stream_id: stream, timestamp, kind: 'USER',
  text: fixture.record.message.content,
  raw: { source: 'structured', transport: 'claude-jsonl' },
};

test('captured authenticated Claude wrapper reconciles optimistic echo', () => {
  assert.equal(optimisticMatchesServerUser(send, event, 60_000), true);
});

test('shared fixture has identical display, wrapper and whitespace semantics', () => {
  assert.equal(fixture.schema_version, 1);
  assert.deepEqual(normalizeProviderUserText(event.text, 'claude', true), {
    text: fixture.display_text, provider_wrapper: fixture.wrapper,
  });
  for (const sample of fixture.normalization_cases) {
    assert.equal(normalizeSubmissionText(sample.text), sample.normalized);
  }
  for (const text of fixture.negative_texts) {
    assert.deepEqual(normalizeProviderUserText(text, 'claude', true), { text });
  }
  assert.deepEqual(normalizeProviderUserText(event.text, 'claude'), { text: event.text });
  assert.deepEqual(normalizeProviderUserText(event.text, 'codex', true), { text: event.text });
  assert.deepEqual(normalizeProviderUserText(event.text + '\n', 'claude', true), { text: event.text + '\n' });
});

test('tagged display text retains a literal nested envelope', () => {
  const literal = event.text;
  const outer = `\n\n<pasted_content id="0008">\n${literal}\n</pasted_content id="0008">\n`;
  const projected = normalizeProviderUserText(outer, 'claude', true);
  assert.equal(projected.provider_wrapper?.id, '0008');
  assert.equal(projected.text, literal);
  const tagged = { ...event, ...projected };
  assert.equal(providerDisplayText(tagged), literal);
  assert.equal(optimisticMatchesServerUser({ ...send, text: literal }, tagged, 60_000), true);
  assert.equal(optimisticMatchesServerUser(send, tagged, 60_000), false);
});

test('submission fallback has daemon whitespace and ANSI normalization', () => {
  assert.equal(optimisticMatchesServerUser({ ...send, text: 'hello world' }, { ...event, text: '\u001b[31mhello\u001b[0m\r\n world\u00a0  ' }, 60_000), true);
});

test('similar or unauthenticated wrapper text is not an optimistic echo', () => {
  for (const text of fixture.negative_texts) {
    assert.equal(optimisticMatchesServerUser(send, { ...event, text }, 60_000), false);
  }
  assert.equal(optimisticMatchesServerUser(send, { ...event, raw: undefined }, 60_000), false);
  assert.equal(optimisticMatchesServerUser(send, { ...event, provider: 'codex' }, 60_000), false);
});

test('identity and time guards still bound normalized text fallback', () => {
  assert.equal(optimisticMatchesServerUser(send, { ...event, stream_id: 'hostb:other' }, 60_000), false);
  assert.equal(optimisticMatchesServerUser(send, { ...event, kind: 'TELL' }, 60_000), false);
  assert.equal(optimisticMatchesServerUser(send, { ...event, client_origin: true }, 60_000), false);
  assert.equal(optimisticMatchesServerUser(send, { ...event, optimistic_id: 'other' }, 60_000), false);
  assert.equal(optimisticMatchesServerUser(send, { ...event, timestamp: 'invalid' }, 60_000), false);
  assert.equal(optimisticMatchesServerUser({ ...send, created_at: send.created_at - 60_001 }, event, 60_000), false);
});
