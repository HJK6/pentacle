'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { resolveAssistantDirectTarget } = require('../renderer/assistant_direct_entry');

const source = { stream_id: 'home:assistant', session_kind: 'assistant_composite', display_name: 'Assistant' };
const target = { stream_id: 'host:live', session_generation: 'generation-1', host: 'host',
  session_name: 'live', display_name: 'Live seat', online: true, host_status: 'online', closed_at: null };
const binding = { sourceStreamId: source.stream_id, streamId: target.stream_id, generation: target.session_generation };
const resolve = (value, sessions = [source, target], entry = source.stream_id) =>
  resolveAssistantDirectTarget({ assistantDirectTarget: value }, sessions, entry);

test('exact configured composite entry resolves one open online ordinary generation', () => {
  const result = resolve(binding);
  assert.equal(result.enabled, true);
  assert.equal(result.error, undefined);
  assert.equal(result.target, target);
  assert.equal(result.source, source);
});

test('no setting or unrelated entry leaves ordinary route unchanged', () => {
  assert.equal(resolve(undefined).enabled, false);
  assert.equal(resolve(binding, [source, target], 'host:other').enabled, false);
});

test('bad or stale configured binding fails closed without a fallback target', () => {
  for (const [name, value, sessions] of [
    ['invalid config', { sourceStreamId: source.stream_id }, [source, target]],
    ['missing target', binding, [source]],
    ['changed generation', binding, [source, { ...target, session_generation: 'generation-2' }]],
    ['offline target', binding, [source, { ...target, online: false }]],
    ['closed target', binding, [source, { ...target, closed_at: '2026-09-26T00:00:00Z' }]],
    ['composite target', binding, [source, { ...target, session_kind: 'assistant_composite' }]],
    ['not-composite source', binding, [{ ...source, session_kind: undefined }, target]],
  ]) {
    const result = resolve(value, sessions);
    assert.equal(result.enabled, true, name);
    assert.match(result.error || '', /unavailable|invalid|changed/i, name);
    assert.equal(result.target, undefined, name);
  }
});
