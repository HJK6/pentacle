const test = require('node:test');
const assert = require('node:assert/strict');

const {
  isReportItem,
  isReportUnread,
  reportUnreadKeys,
  reportUnreadCount,
  markReportRead,
} = require('../renderer/report_read_state');

function bucket(items = {}, readById = {}) {
  return { itemsById: { ...items }, readById: { ...readById }, dismissedById: {} };
}
const report = (updated_at) => ({ content_type: 'report', updated_at });
const table = (updated_at) => ({ content_type: 'json_table', updated_at });

test('isReportItem only true for report content_type', () => {
  assert.equal(isReportItem(report('t1')), true);
  assert.equal(isReportItem(table('t1')), false);
  assert.equal(isReportItem(null), false);
});

test('a never-opened report is unread', () => {
  const b = bucket({ r1: report('t1') });
  assert.equal(isReportUnread(b, 'r1'), true);
  assert.deepEqual(reportUnreadKeys(b), ['r1']);
  assert.equal(reportUnreadCount(b), 1);
});

test('non-report assets never count as unread', () => {
  const b = bucket({ t1: table('t1'), r1: report('t1') });
  assert.equal(isReportUnread(b, 't1'), false);
  assert.equal(reportUnreadCount(b), 1); // only r1
});

test('marking read clears unread at the current updated_at', () => {
  const b = bucket({ r1: report('t1') });
  assert.equal(markReportRead(b, 'r1'), true);
  assert.equal(b.readById.r1, 't1');
  assert.equal(isReportUnread(b, 'r1'), false);
  assert.equal(reportUnreadCount(b), 0);
});

test('republish (new updated_at) re-flags a read report as unread', () => {
  const b = bucket({ r1: report('t1') });
  markReportRead(b, 'r1');
  assert.equal(isReportUnread(b, 'r1'), false);
  // Republish bumps updated_at; the stale read marker must not satisfy it.
  b.itemsById.r1 = report('t2');
  assert.equal(isReportUnread(b, 'r1'), true);
  assert.equal(reportUnreadCount(b), 1);
  // Re-opening at the new version clears it again.
  markReportRead(b, 'r1');
  assert.equal(isReportUnread(b, 'r1'), false);
});

test('markReportRead is a no-op for non-report and missing keys', () => {
  const b = bucket({ t1: table('t1') });
  assert.equal(markReportRead(b, 't1'), false);
  assert.equal(markReportRead(b, 'nope'), false);
  assert.deepEqual(b.readById, {});
});

test('dismissed reports do not count as unread', () => {
  const b = bucket({ r1: report('t1'), r2: report('t1') });
  const dismissed = new Set(['r1']);
  const isDismissed = (k) => dismissed.has(k);
  assert.equal(isReportUnread(b, 'r1', isDismissed), false);
  assert.equal(isReportUnread(b, 'r2', isDismissed), true);
  assert.deepEqual(reportUnreadKeys(b, isDismissed), ['r2']);
  assert.equal(reportUnreadCount(b, isDismissed), 1);
});

test('missing updated_at is treated as empty-string version consistently', () => {
  const b = bucket({ r1: { content_type: 'report' } }); // no updated_at
  assert.equal(isReportUnread(b, 'r1'), true); // never read
  markReportRead(b, 'r1');
  assert.equal(b.readById.r1, '');
  assert.equal(isReportUnread(b, 'r1'), false);
});

test('empty/missing bucket is safe', () => {
  assert.equal(reportUnreadCount(null), 0);
  assert.equal(reportUnreadCount({}), 0);
  assert.deepEqual(reportUnreadKeys(undefined), []);
  assert.equal(isReportUnread(null, 'r1'), false);
  assert.equal(markReportRead(null, 'r1'), false);
});
