import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex, utf8ToBytes } from '@noble/hashes/utils.js';
import {
  applyPentacleEvent,
  applySnapshotWithOptimisticReconciliation,
  findMatchingCompositeQueuedOptimisticIds,
  initialPentacleStreamState,
  interpretPentacleEvent,
  PENTACLE_MESSAGE_ENVELOPE_RENDER_POLICIES,
  sendOptimisticMessage,
  type PentacleEvent,
} from '../src/index.ts';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/message-envelopes.json', import.meta.url), 'utf8'));

function event(overrides: Partial<PentacleEvent> = {}): PentacleEvent {
  return {
    daemon_seq: 1,
    host: 'host',
    provider: 'codex',
    session_id: 'host:child',
    session_name: 'child',
    stream_id: 'host:child',
    timestamp: '2026-09-20T00:00:00Z',
    kind: 'USER',
    text: 'text',
    ...overrides,
  };
}

test('shared message-envelope fixture remains digest-bound', () => {
  assert.equal(fixture.schema_version, 1);
  assert.deepEqual(fixture.validation_baselines, {
    red: 'aea8673509ba97b95841cf1d8a10e57e10fa998c',
    final: '31afd4d32df37e01b2810975f7d830e75d6c3c38',
  });
  for (const item of fixture.cases) {
    assert.equal(bytesToHex(sha256(utf8ToBytes(item.wire_text))), item.wire_sha256, item.key);
    assert.equal(PENTACLE_MESSAGE_ENVELOPE_RENDER_POLICIES[item.kind as keyof typeof PENTACLE_MESSAGE_ENVELOPE_RENDER_POLICIES], item.render_policy, item.key);
  }
  const wrapped = fixture.auxiliary.wrapped_prompt_ask_ok;
  assert.equal(bytesToHex(sha256(utf8ToBytes(wrapped.wire_text))), wrapped.wire_sha256);
});

test('malformed and unknown envelope tags fail closed before raw projection', () => {
  const malformed = interpretPentacleEvent(event({
    text: 'operator prose',
    message_envelope: { kind: 'notice_marker', id: 'notice-fixture-1', schema_version: 2 } as any,
  }));
  assert.equal(malformed.hidden, true);
  assert.equal(malformed.displayRule, 'hidden:noise');

  const unknown = interpretPentacleEvent(event({
    text: 'operator prose',
    message_envelope: { kind: 'future_internal_kind', id: 'future-1', schema_version: 1 } as any,
  }));
  assert.equal(unknown.hidden, true);
  assert.equal(unknown.displayRule, 'hidden:noise');
});

test('valid registry tag is the policy authority over retained daemon raw metadata', () => {
  const item = fixture.cases.find((candidate: any) => candidate.key === 'notification-answer');
  const interpreted = interpretPentacleEvent(event({
    text: item.wire_text,
    message_envelope: { ...item.expected_tag, schema_version: 1 },
    raw: { daemon_notice: { malformed: true } },
  } as any));
  assert.equal(interpreted.caseId, 'agent-question-answer');
  assert.equal(interpreted.hidden, false);
});

test('tagged child-session close renders as the existing daemon activity row', () => {
  const item = fixture.cases.find((candidate: any) => candidate.key === 'child-session-closed');
  const interpreted = interpretPentacleEvent(event({
    text: item.wire_text,
    message_envelope: { ...item.expected_tag, schema_version: 1 },
  } as any));
  assert.equal(interpreted.caseId, 'daemon-notice');
  assert.equal(interpreted.displayRule, 'bubble:agent');
  assert.equal(interpreted.hidden, false);
  assert.equal(interpreted.label, 'Daemon');
  assert.equal(interpreted.text, item.expected_display);
});

test('tagged notification answer keeps its compact answer row', () => {
  const item = fixture.cases.find((candidate: any) => candidate.key === 'notification-answer');
  const interpreted = interpretPentacleEvent(event({
    text: item.wire_text,
    message_envelope: { ...item.expected_tag, schema_version: 1 },
  } as any));
  assert.equal(interpreted.caseId, 'agent-question-answer');
  assert.equal(interpreted.displayRule, 'activity:question');
  assert.equal(interpreted.text, item.expected_display);
  assert.equal(interpreted.notificationId, 'notification-fixture');
});

test('unregistered marker-prefixed USER text is fail-closed and never raw prose', () => {
  const raw = '[pentacle-notice:unknown-fixture]\noperator prose';
  const interpreted = interpretPentacleEvent(event({ text: raw }));
  assert.equal(interpreted.hidden, true);
  assert.equal(interpreted.displayRule, 'hidden:noise');
  assert.notEqual(interpreted.text, raw);
});

test('one bounded executor-result unwrap renders one Asked row and rejects malformed variants', () => {
  const wrapped = fixture.auxiliary.wrapped_prompt_ask_ok;
  const interpreted = interpretPentacleEvent(event({
    kind: 'TOOL_RESULT',
    text: wrapped.wire_text,
    raw: { source: 'structured', transport: 'codex-rollout' },
  }));
  assert.equal(interpreted.caseId, 'agent-question-ask');
  assert.equal(interpreted.displayRule, 'activity:question');
  assert.equal(interpreted.text, 'Asked: Fixture question (q-fixture)');
  assert.equal(interpreted.notificationId, 'notification-fixture');
  for (const text of wrapped.negative_texts) {
    const candidate = interpretPentacleEvent(event({
      kind: 'TOOL_RESULT',
      text,
      raw: { source: 'structured', transport: 'codex-rollout' },
    }));
    assert.notEqual(candidate.caseId, 'agent-question-ask', text);
  }
});

test('composite queued sends match a captured one-LF FIFO batch only', () => {
  const sends = [
    { optimistic_id: 'opt-1', stream_id: 'host:child', text: 'one', created_at: 1, queued_at: 1, status: 'queued' },
    { optimistic_id: 'opt-2', stream_id: 'host:child', text: 'two', created_at: 2, queued_at: 2, status: 'dispatched' },
    { optimistic_id: 'opt-3', stream_id: 'host:child', text: 'three', created_at: 3, queued_at: 3, status: 'acked' },
  ] as any;
  assert.deepEqual(findMatchingCompositeQueuedOptimisticIds(event({ text: 'one\ntwo\nthree' }), sends), ['opt-1', 'opt-2', 'opt-3']);
  assert.deepEqual(findMatchingCompositeQueuedOptimisticIds(event({ text: 'one\n\ntwo\n\nthree' }), sends), []);
  assert.deepEqual(findMatchingCompositeQueuedOptimisticIds(event({ text: 'two\none\nthree' }), sends), []);
  assert.deepEqual(findMatchingCompositeQueuedOptimisticIds(event({ text: 'one\ntwo' }), sends), []);
  assert.deepEqual(findMatchingCompositeQueuedOptimisticIds(event({ text: 'one\ntwo\nthree', optimistic_id: 'server-id' }), sends), []);
});

test('real reducer reconciles a one-LF FIFO batch into all original optimistic rows', () => {
  const streamId = 'host:child';
  const texts = ['one', 'two', 'three'];
  const ids = texts.map((_text, index) => `opt-${index + 1}`);
  let state = {
    ...initialPentacleStreamState,
    connected: true,
    sessions: [{
      stream_id: streamId,
      host: 'host',
      provider: 'codex',
      session_name: 'child',
      last_event_at: '2026-09-20T00:00:00Z',
      last_text: '',
      last_kind: '',
      draft: '',
      pending: false,
      working: false,
      online: true,
    }],
  };
  for (const [index, text] of texts.entries()) {
    state = sendOptimisticMessage(state, {
      streamId,
      text,
      optimisticId: ids[index],
      requestId: `req-${index + 1}`,
      createdAt: Date.parse(`2026-09-20T00:00:0${index}Z`),
      queuedAt: Date.parse(`2026-09-20T00:00:0${index}Z`),
      beginTurn: false,
    });
  }

  for (const negative of [
    'two\none\nthree',
    'one\ntwo',
    'one\n\ntwo\n\nthree',
  ]) {
    const candidate = applyPentacleEvent(state, event({ text: negative }));
    assert.deepEqual(Object.keys(candidate.optimisticSends ?? {}), ids, negative);
  }
  for (const negative of [
    event({ stream_id: 'other:child', text: 'one\ntwo\nthree' }),
    event({ optimistic_id: 'server-id', text: 'one\ntwo\nthree' }),
  ]) {
    const candidate = applyPentacleEvent(state, negative);
    assert.deepEqual(Object.keys(candidate.optimisticSends ?? {}), ids, negative.text);
  }

  const reconciled = applyPentacleEvent(state, event({
    daemon_seq: 9,
    text: 'one\ntwo\nthree',
    timestamp: '2026-09-20T00:00:03Z',
  }));
  assert.deepEqual(Object.keys(reconciled.optimisticSends ?? {}), []);
  assert.deepEqual(
    reconciled.events.filter((item) => item.client_origin && item.optimistic_id).map((item) => item.optimistic_id),
    ids,
  );
});

test('fresh snapshot reconciles a one-LF FIFO batch without mutating frozen events', () => {
  const streamId = 'host:child';
  const texts = ['one', 'two', 'three'];
  const ids = texts.map((_text, index) => `snapshot-opt-${index + 1}`);
  let state = {
    ...initialPentacleStreamState,
    connected: true,
    sessions: [{
      stream_id: streamId,
      host: 'host',
      provider: 'codex',
      session_name: 'child',
      last_event_at: '2026-09-20T00:00:00Z',
      last_text: '',
      last_kind: '',
      draft: '',
      pending: false,
      working: false,
      online: true,
    }],
  };
  for (const [index, text] of texts.entries()) {
    state = sendOptimisticMessage(state, {
      streamId,
      text,
      optimisticId: ids[index],
      requestId: `snapshot-req-${index + 1}`,
      createdAt: Date.parse(`2026-09-20T00:00:0${index}Z`),
      queuedAt: Date.parse(`2026-09-20T00:00:0${index}Z`),
      beginTurn: false,
    });
  }
  assert.equal(Object.isFrozen(state.events), true);

  for (const negative of [
    'two\none\nthree',
    'one\ntwo',
    'one\n\ntwo\n\nthree',
  ]) {
    const candidate = applySnapshotWithOptimisticReconciliation(state, {
      sessions: state.sessions,
      events: [event({ text: negative })],
    });
    assert.deepEqual(Object.keys(candidate.optimisticSends ?? {}), ids, negative);
  }

  const snapshot = applySnapshotWithOptimisticReconciliation(state, {
    sessions: state.sessions,
    events: [event({
      daemon_seq: 9,
      text: 'one\ntwo\nthree',
      timestamp: '2026-09-20T00:00:03Z',
    })],
  });

  assert.deepEqual(Object.keys(snapshot.optimisticSends ?? {}), []);
  assert.deepEqual(
    snapshot.events.filter((item) => item.client_origin && item.optimistic_id).map((item) => ({
      id: item.optimistic_id,
      text: item.text,
      pending: item.pending,
    })),
    ids.map((id, index) => ({ id, text: texts[index], pending: false })),
  );
});
