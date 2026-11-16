const test = require('node:test');
const assert = require('node:assert/strict');

const {
  ATTENTION_TIER,
  PRIMARY_ACTION,
  attentionTier,
  primaryAction,
  parseEventTime,
  compareAttentionRows,
  orderSidebarRows,
} = require('../renderer/sidebar_attention');

function row(overrides = {}) {
  return {
    streamId: 'hosta:claude-a',
    isWorking: false,
    lastEventAt: '2026-07-29T00:00:00.000Z',
    openQuestionCount: 0,
    unreadReportCount: 0,
    ...overrides,
  };
}

// --- tier assignment (spec scope item 1) ---

test('tier 0 for any open question, above working', () => {
  assert.equal(attentionTier(row({ openQuestionCount: 1 })), ATTENTION_TIER.QUESTION);
  assert.equal(attentionTier(row({ openQuestionCount: 3, isWorking: true })), ATTENTION_TIER.QUESTION);
});

test('tier 1 for working with no open question', () => {
  assert.equal(attentionTier(row({ isWorking: true })), ATTENTION_TIER.WORKING);
});

test('tier 2 for ordinary rows regardless of unread reports or spec issues', () => {
  assert.equal(attentionTier(row()), ATTENTION_TIER.ORDINARY);
  assert.equal(attentionTier(row({ unreadReportCount: 5 })), ATTENTION_TIER.ORDINARY);
});

// --- primary action precedence (spec scope item 2): question > report > status ---

test('primary action prefers open question', () => {
  assert.equal(primaryAction(row({ openQuestionCount: 1, unreadReportCount: 2 })), PRIMARY_ACTION.QUESTION);
});

test('primary action falls to unread report when no question', () => {
  assert.equal(primaryAction(row({ openQuestionCount: 0, unreadReportCount: 1 })), PRIMARY_ACTION.REPORT);
});

test('primary action falls to status/caret when neither', () => {
  assert.equal(primaryAction(row()), PRIMARY_ACTION.STATUS);
  assert.equal(primaryAction(row({ isWorking: true })), PRIMARY_ACTION.STATUS);
});

// --- timestamp parsing (missing/invalid = oldest) ---

test('parseEventTime returns null for missing/invalid, ms for valid', () => {
  assert.equal(parseEventTime(null), null);
  assert.equal(parseEventTime(undefined), null);
  assert.equal(parseEventTime(''), null);
  assert.equal(parseEventTime('not-a-date'), null);
  assert.equal(parseEventTime(Number.NaN), null);
  assert.equal(parseEventTime('2026-07-29T00:00:00.000Z'), Date.parse('2026-07-29T00:00:00.000Z'));
  assert.equal(parseEventTime(1785308672000), 1785308672000);
});

// --- ordering (the locked comparator) ---

test('orders by tier first: question over working over ordinary', () => {
  const ordered = orderSidebarRows([
    row({ streamId: 'c', isWorking: false, lastEventAt: '2026-07-29T03:00:00Z' }),
    row({ streamId: 'w', isWorking: true, lastEventAt: '2026-07-29T01:00:00Z' }),
    row({ streamId: 'q', openQuestionCount: 1, lastEventAt: '2026-07-29T00:00:00Z' }),
  ]);
  assert.deepEqual(ordered.map((r) => r.streamId), ['q', 'w', 'c']);
  assert.deepEqual(ordered.map((r) => r.attentionTier), [0, 1, 2]);
});

test('a configured pinned row sorts ahead of every attention tier without changing its badges', () => {
  const ordered = orderSidebarRows([
    row({ streamId: 'question', openQuestionCount: 1 }),
    row({ streamId: 'assistant', isPinned: true, unreadReportCount: 2 }),
    row({ streamId: 'working', isWorking: true }),
  ]);
  assert.deepEqual(ordered.map((r) => r.streamId), ['assistant', 'question', 'working']);
  assert.equal(ordered[0].attentionTier, ATTENTION_TIER.ORDINARY);
  assert.equal(ordered[0].primaryAction, PRIMARY_ACTION.REPORT);
});

test('within a tier, newer last_event_at sorts first', () => {
  const ordered = orderSidebarRows([
    row({ streamId: 'a', lastEventAt: '2026-07-29T01:00:00Z' }),
    row({ streamId: 'b', lastEventAt: '2026-07-29T05:00:00Z' }),
    row({ streamId: 'c', lastEventAt: '2026-07-29T03:00:00Z' }),
  ]);
  assert.deepEqual(ordered.map((r) => r.streamId), ['b', 'c', 'a']);
});

test('missing/invalid timestamps sort to the bottom of their tier', () => {
  const ordered = orderSidebarRows([
    row({ streamId: 'valid', lastEventAt: '2026-07-29T02:00:00Z' }),
    row({ streamId: 'missing', lastEventAt: null }),
    row({ streamId: 'invalid', lastEventAt: 'garbage' }),
  ]);
  // 'valid' first; the two null-time rows follow, tie-broken by stream_id asc.
  assert.deepEqual(ordered.map((r) => r.streamId), ['valid', 'invalid', 'missing']);
});

test('equal timestamps tie-break by stream_id ascending', () => {
  const ts = '2026-07-29T02:00:00Z';
  const ordered = orderSidebarRows([
    row({ streamId: 'hosta:z', lastEventAt: ts }),
    row({ streamId: 'hosta:a', lastEventAt: ts }),
    row({ streamId: 'hosta:m', lastEventAt: ts }),
  ]);
  assert.deepEqual(ordered.map((r) => r.streamId), ['hosta:a', 'hosta:m', 'hosta:z']);
});

test('unread reports and spec issues do not reorder rows across tiers', () => {
  // A working row with no reports must still sort above an ordinary row that has
  // unread reports — attention badges never promote a tier.
  const ordered = orderSidebarRows([
    row({ streamId: 'ordinary-with-reports', isWorking: false, unreadReportCount: 9, lastEventAt: '2026-07-29T09:00:00Z' }),
    row({ streamId: 'working-no-reports', isWorking: true, unreadReportCount: 0, lastEventAt: '2026-07-29T01:00:00Z' }),
  ]);
  assert.deepEqual(ordered.map((r) => r.streamId), ['working-no-reports', 'ordinary-with-reports']);
});

test('ordering is deterministic regardless of input order (stable total order)', () => {
  const rows = [
    row({ streamId: 'q1', openQuestionCount: 2, lastEventAt: '2026-07-29T00:10:00Z' }),
    row({ streamId: 'q2', openQuestionCount: 1, lastEventAt: '2026-07-29T00:20:00Z' }),
    row({ streamId: 'w1', isWorking: true, lastEventAt: '2026-07-29T00:30:00Z' }),
    row({ streamId: 'o1', lastEventAt: 'bad' }),
    row({ streamId: 'o2', lastEventAt: '2026-07-29T00:40:00Z' }),
  ];
  const expected = orderSidebarRows(rows).map((r) => r.streamId);
  const shuffled = [rows[3], rows[0], rows[4], rows[2], rows[1]];
  assert.deepEqual(orderSidebarRows(shuffled).map((r) => r.streamId), expected);
  assert.deepEqual(expected, ['q2', 'q1', 'w1', 'o2', 'o1']);
});

test('orderSidebarRows does not mutate input and decorates output', () => {
  const input = [row({ streamId: 'x', openQuestionCount: 1 })];
  const snapshot = JSON.parse(JSON.stringify(input));
  const out = orderSidebarRows(input);
  assert.deepEqual(input, snapshot); // input untouched
  assert.equal(out[0].attentionTier, ATTENTION_TIER.QUESTION);
  assert.equal(out[0].primaryAction, PRIMARY_ACTION.QUESTION);
  assert.equal(out[0].streamId, 'x'); // original fields pass through
});

test('compareAttentionRows is a usable comparator (returns sign, not just truthy)', () => {
  const a = row({ streamId: 'a', openQuestionCount: 1 });
  const b = row({ streamId: 'b', isWorking: true });
  assert.ok(compareAttentionRows(a, b) < 0);
  assert.ok(compareAttentionRows(b, a) > 0);
  assert.equal(compareAttentionRows(a, a), 0);
});

test('handles empty and non-array input safely', () => {
  assert.deepEqual(orderSidebarRows([]), []);
  assert.deepEqual(orderSidebarRows(null), []);
  assert.deepEqual(orderSidebarRows(undefined), []);
});
