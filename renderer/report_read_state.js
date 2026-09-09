// Report read/unread tracking — spec_pentacle__desktop_chat_navigation_status_parity_2026_07_29
// scope item 5. Pure helpers over an asset bucket (state.chatStream.assets[streamId]).
//
// A report asset is UNREAD when it is a report-type asset, not dismissed, and
// either never opened or opened at an OLDER `updated_at` than its current one.
// Because `upsertAssetMetadata` bumps `updated_at` on republish/review-status
// change, comparing the recorded read marker against the live `updated_at`
// makes republished evidence re-flag itself unread with no explicit mark-unread
// call. Non-report assets (json_table/markdown/raw) never contribute unread state.
//
// `isDismissedKey(assetKey)` is injected because dismissal is keyed by the
// app-side `assetTabKey(streamId, assetKey)`; the pure module stays ignorant of
// that composition. Pass a no-op (or omit) when dismissal is irrelevant.

'use strict';

function isReportItem(item) {
  return !!item && item.content_type === 'report';
}

function readMarkerFor(bucket, key) {
  return bucket && bucket.readById ? bucket.readById[key] : undefined;
}

// isReportUnread — true iff the report at `key` currently needs attention.
function isReportUnread(bucket, key, isDismissedKey) {
  if (!bucket || !bucket.itemsById) return false;
  const item = bucket.itemsById[key];
  if (!isReportItem(item)) return false;
  if (typeof isDismissedKey === 'function' && isDismissedKey(key)) return false;
  return readMarkerFor(bucket, key) !== (item.updated_at || '');
}

// reportUnreadKeys — the report asset keys that are currently unread.
function reportUnreadKeys(bucket, isDismissedKey) {
  if (!bucket || !bucket.itemsById) return [];
  return Object.keys(bucket.itemsById).filter((key) => isReportUnread(bucket, key, isDismissedKey));
}

function reportUnreadCount(bucket, isDismissedKey) {
  return reportUnreadKeys(bucket, isDismissedKey).length;
}

// markReportRead — records the report at `key` as read at its current
// `updated_at`. No-op (returns false) for missing keys or non-report assets, so
// opening a non-report tab never mutates read state. Mutates bucket.readById.
function markReportRead(bucket, key) {
  if (!bucket || !bucket.itemsById) return false;
  const item = bucket.itemsById[key];
  if (!isReportItem(item)) return false;
  if (!bucket.readById) bucket.readById = {};
  bucket.readById[key] = item.updated_at || '';
  return true;
}

module.exports = {
  isReportItem,
  isReportUnread,
  reportUnreadKeys,
  reportUnreadCount,
  markReportRead,
};
