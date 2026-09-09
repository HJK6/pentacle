import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  applyPentacleEvent,
  initialPentacleStreamState,
  interpretPentacleEvent,
  selectSessionDetail,
  type PentacleEvent,
  type PentacleSessionSummary,
  type PentacleStreamState,
} from '../src/index.ts';

// Cross-consumer replay corpus (public-desktop-chat-core-convergence,
// scope 5). The same committed fixture bytes are replayed through the shared core here and,
// by thin wrappers, through each consumer adapter. Because desktop and mobile consume this
// exact core, identical fixture -> identical decisions is the parity contract.

const STREAM_ID = 'host_c:claude-host_c-4bdb7e6d';
const PEER_STREAM_ID = 'host_c:claude-host_c-1b7d6cb6';

function session(): PentacleSessionSummary {
  return {
    stream_id: STREAM_ID,
    host: 'host_c',
    provider: 'claude',
    session_name: 'claude-host_c-4bdb7e6d',
    last_event_at: '2025-01-14T21:11:00.000Z',
    last_text: '',
    last_kind: '',
    draft: '',
    pending: false,
    working: false,
    online: true,
  };
}

function loadCorpus(): PentacleEvent[] {
  const raw = readFileSync(new URL('./fixtures/convergence_replay_corpus_v1.jsonl', import.meta.url), 'utf8');
  return raw
    .split('\n')
    .filter((line) => line.trim().length > 0)
    .map((line) => JSON.parse(line) as PentacleEvent);
}

function detailFor(events: PentacleEvent[], visibleCount: number | 'all') {
  let state: PentacleStreamState = {
    ...initialPentacleStreamState,
    sessions: [session()],
    eventContentVersionByStream: { [STREAM_ID]: 1 },
  };
  for (const item of events) state = applyPentacleEvent(state, item);
  return selectSessionDetail(state, STREAM_ID, { visibleCount });
}

test('replay corpus: bounded transcript selection caps at the requested window', () => {
  const corpus = loadCorpus();
  const detail = detailFor(corpus, 16);
  assert.ok(detail);
  assert.ok(detail.transcriptItems.length <= 16, `expected <=16, got ${detail.transcriptItems.length}`);
  // Corpus intentionally carries more than 16 visible rows so the bound is exercised.
  const all = detailFor(corpus, 'all');
  assert.ok((all?.transcriptItems.length ?? 0) > 16);
});

test('replay corpus: exactly one answered-question representation, no raw ledger JSON', () => {
  const detail = detailFor(loadCorpus(), 'all');
  assert.ok(detail);
  const answered = detail.transcriptItems.filter((i) => i.eventCase === 'agent-question-answer');
  assert.equal(answered.length, 1);
  assert.equal(answered[0].displayRule, 'activity:question');
  assert.ok(answered[0].text.startsWith('Operator answered:'), answered[0].text);
  // No visible row leaks raw protocol/ledger JSON.
  for (const item of detail.transcriptItems) {
    assert.equal(item.text.includes('notification.answer'), false, item.text);
    assert.equal(item.text.includes('"type":"'), false, item.text);
  }
});

test('replay corpus: disclosure metadata is preserved on collapsible peer rows', () => {
  const detail = detailFor(loadCorpus(), 'all');
  const disclosed = (detail?.transcriptItems ?? []).filter((i) => i.disclosure);
  assert.ok(disclosed.length >= 1, 'expected at least one row with disclosure metadata');
  assert.ok(disclosed.some((i) => i.disclosure?.mode === 'collapsed-preview'));
});

test('replay corpus: ported provider-neutral structured-event decisions are stable', () => {
  const corpus = loadCorpus();
  const byPredicate = (fn: (e: PentacleEvent) => boolean) => corpus.find(fn)!;

  // The corpus TELL is a synthetic peer delivery (sender host_c:claude-…, body
  // "typed body"). Under public-agent-message-collapsed-row
  // a real typed peer tell/send is now VISIBLE as a compact agent card; only
  // daemon housekeeping and malformed protocol payloads stay hidden.
  const tell = byPredicate((e) => e.kind === 'TELL');
  const tellRow = interpretPentacleEvent(tell);
  assert.equal(tellRow.caseId, 'peer-agent-message');
  assert.equal(tellRow.displayRule, 'bubble:agent');
  assert.equal(tellRow.hidden, false);

  const fallback = byPredicate((e) => e.raw?.source === 'scrollback_fallback');
  assert.equal(interpretPentacleEvent(fallback).hidden, true);
  const revealed = interpretPentacleEvent(fallback, 'Agent', { revealScrollbackFallback: true });
  assert.equal(revealed.hidden, false);
  assert.equal(revealed.caseId, 'assistant-message');

  const codexRollout = byPredicate((e) => e.raw?.transport === 'codex-rollout');
  const rolloutRow = interpretPentacleEvent(codexRollout);
  assert.equal(rolloutRow.hidden, false);
  assert.equal(rolloutRow.caseId, 'assistant-message');
});

test('replay corpus: replay is deterministic (identical decisions across runs = consumer parity contract)', () => {
  const decisions = () =>
    (detailFor(loadCorpus(), 'all')?.transcriptItems ?? []).map((i) => ({
      eventCase: i.eventCase,
      displayRule: i.displayRule,
      text: i.text,
    }));
  assert.deepEqual(decisions(), decisions());
  void PEER_STREAM_ID;
});
