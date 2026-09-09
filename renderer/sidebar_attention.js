// Sidebar attention model — public navigation contract
//
// Brings the desktop sidebar to public behavior for (a) stable attention ordering
// and (b) the single primary action each row exposes. PURE: no DOM, no globals.
// app.js assembles a row descriptor per visible session (from state.sessions +
// the shared-core open-question selector + desktop unread-report tracking) and
// renders; this module owns only the ordering/tier/action decisions so they are
// unit-testable in isolation and match the locked comparator exactly.
//
// A row descriptor is:
//   { streamId, isWorking, lastEventAt, openQuestionCount, unreadReportCount }
// Extra fields (the session object itself) pass through untouched.

'use strict';

// Attention tiers — lower sorts higher in the list. Unread reports and
// status/spec issues DO NOT change the tier (public contract item 1); they only
// affect the per-row primary action and row badges.
const ATTENTION_TIER = Object.freeze({
  QUESTION: 0, // one or more open questions awaiting an answer
  WORKING: 1, // status === 'working'
  ORDINARY: 2, // live / idle / sending / offline
});

// Primary action precedence (public contract item 2), evaluated independently of the
// tier: open question, then unread report, then status/caret.
const PRIMARY_ACTION = Object.freeze({
  QUESTION: 'question',
  REPORT: 'report',
  STATUS: 'status',
});

function attentionTier(row) {
  if (row && row.openQuestionCount > 0) return ATTENTION_TIER.QUESTION;
  if (row && row.isWorking) return ATTENTION_TIER.WORKING;
  return ATTENTION_TIER.ORDINARY;
}

function primaryAction(row) {
  if (row && row.openQuestionCount > 0) return PRIMARY_ACTION.QUESTION;
  if (row && row.unreadReportCount > 0) return PRIMARY_ACTION.REPORT;
  return PRIMARY_ACTION.STATUS;
}

// parseEventTime returns epoch ms for a valid ISO string / numeric timestamp, or
// null for missing/invalid values. null is treated as "oldest" by the
// comparator so unparseable rows sort to the bottom of their tier deterministically.
function parseEventTime(value) {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value !== 'string' || value === '') return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

// compareAttentionRows implements the locked comparator: tier ascending, then
// valid last_event_at descending (missing/invalid = oldest), then stream_id
// ascending as the deterministic tiebreak.
function compareAttentionRows(a, b) {
  const tierA = attentionTier(a);
  const tierB = attentionTier(b);
  if (tierA !== tierB) return tierA - tierB;

  const timeA = parseEventTime(a && a.lastEventAt);
  const timeB = parseEventTime(b && b.lastEventAt);
  if (timeA !== timeB) {
    if (timeA === null) return 1; // a is oldest -> sorts after b
    if (timeB === null) return -1; // b is oldest -> sorts after a
    return timeB - timeA; // newer first
  }

  const streamA = String((a && a.streamId) || '');
  const streamB = String((b && b.streamId) || '');
  if (streamA < streamB) return -1;
  if (streamA > streamB) return 1;
  return 0;
}

// orderSidebarRows returns a NEW array, input order preserved as the final
// tiebreak for total-order safety (stream_id already disambiguates real rows).
// Each output row carries the original fields plus computed `attentionTier` and
// `primaryAction`. Input is never mutated.
function orderSidebarRows(rows) {
  const list = Array.isArray(rows) ? rows : [];
  const decorated = list.map((row, index) => ({ row, index }));
  decorated.sort((a, b) => {
    const cmp = compareAttentionRows(a.row, b.row);
    return cmp !== 0 ? cmp : a.index - b.index;
  });
  return decorated.map(({ row }) => ({
    ...row,
    attentionTier: attentionTier(row),
    primaryAction: primaryAction(row),
  }));
}

module.exports = {
  ATTENTION_TIER,
  PRIMARY_ACTION,
  attentionTier,
  primaryAction,
  parseEventTime,
  compareAttentionRows,
  orderSidebarRows,
};
