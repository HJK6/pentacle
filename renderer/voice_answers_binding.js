'use strict';

// P6 mobile segment/registry port, including its one-decimal segment precision
// (Packet6 erratum 1). No visit accumulation.
const MIN_SEGMENT_MS = 1500;
const MAX_BOUND_ITEMS = 20;
const VOICE_ANSWERS_VERSION = 1;
const seconds = ms => Math.round(ms / 100) / 10;
class SegmentTracker {
  constructor(trackedKeys) { this.tracked = new Set(trackedKeys); this.visits = []; this.open = null; }
  enter(key, atMs) {
    if (this.open && this.open.key === key) return;
    this.finish(atMs);
    if (key !== null && this.tracked.has(key)) {
      this.open = { key, startMs: atMs, endMs: null }; this.visits.push(this.open);
    }
  }
  finish(atMs) { if (this.open) { this.open.endMs = atMs; this.open = null; } }
  covered(nowMs) {
    const best = new Map();
    for (const visit of this.visits) {
      const endMs = visit.endMs ?? nowMs;
      if (endMs - visit.startMs < MIN_SEGMENT_MS) continue;
      const prior = best.get(visit.key);
      if (!prior || endMs - visit.startMs > prior.endMs - prior.startMs) best.set(visit.key, { startMs: visit.startMs, endMs });
    }
    const ordered = [...best.entries()].sort((a, b) => a[1].startMs - b[1].startMs).slice(0, MAX_BOUND_ITEMS);
    return new Map(ordered.map(([key, span]) => [key, { start_s: seconds(span.startMs), end_s: seconds(span.endMs) }]));
  }
  msUntilCovered(nowMs) {
    if (!this.open) return null;
    const remaining = MIN_SEGMENT_MS - (nowMs - this.open.startMs);
    return remaining > 0 ? remaining : null;
  }
}
const notificationId = page => page.notification?.notification_id || page.notification?.id || '';
function isVoiceEligible(page) {
  return page?.source === 'durable' && !!page.notification?.question?.question_id
    && !!notificationId(page) && !!page.notification?.question?.producer_stream_id;
}
function createSegments(now = Date.now) {
  let tracker = new SegmentTracker([]); let t0 = 0; let n = 0; let finishedAt = null;
  return {
    start(pages, currentKey) {
      t0 = now(); finishedAt = null;
      n = pages.filter(page => page.source === 'durable').length;
      tracker = new SegmentTracker(pages.filter(isVoiceEligible).map(page => page.key));
      tracker.enter(currentKey, 0);
    },
    enter(key) { if (finishedAt === null) tracker.enter(key, now() - t0); },
    finish() { if (finishedAt === null) { finishedAt = now() - t0; tracker.finish(finishedAt); } },
    covered() { return tracker.covered(finishedAt ?? now() - t0); },
    tracked(key) { return tracker.tracked.has(key); },
    msUntilCovered() { return finishedAt === null ? tracker.msUntilCovered(now() - t0) : null; },
    get n() { return n; },
  };
}
function selectedSet(segments, pages, surfaceStreamId) {
  const byKey = new Map(pages.map(page => [page.key, page]));
  const items = [];
  for (const [key, segment] of segments.covered()) {
    const page = byKey.get(key);
    if (!isVoiceEligible(page)) continue;
    // A durable web card is one globally unique notification. Its identifier
    // is also the opaque wire key, avoiding growth from the internal pager key.
    // Malformed over-limit IDs remain host validation failures, never truncated.
    items.push({ key: notificationId(page), question_id: page.notification.question.question_id,
      notification_id: notificationId(page), producer_stream_id: page.notification.question.producer_stream_id,
      surface_stream_id: surfaceStreamId, prompt: page.model?.prompt ?? '', segment });
  }
  return items;
}

const bindings = new Map();
const copyItem = item => ({ ...item, segment: { ...item.segment } });
function registerVoiceAnswersBinding(recordingId, items) {
  bindings.delete(recordingId);
  if (!items.length) return;
  bindings.set(recordingId, items.map(copyItem));
  while (bindings.size > 32) bindings.delete(bindings.keys().next().value);
}
function releaseVoiceAnswersBinding(recordingId) { bindings.delete(recordingId); }
function voiceAnswersItemCount(recordingId) { return bindings.get(recordingId)?.length ?? 0; }
function buildVoiceAnswersMeta(recordingId, { blobSha, durationS }) {
  const items = bindings.get(recordingId);
  return items ? { version: VOICE_ANSWERS_VERSION, recording_id: recordingId, blob_sha: blobSha,
    duration_s: durationS, items: items.map(copyItem) } : null;
}
module.exports = { MIN_SEGMENT_MS, MAX_BOUND_ITEMS, VOICE_ANSWERS_VERSION, SegmentTracker,
  createSegments, selectedSet, isVoiceEligible, registerVoiceAnswersBinding,
  releaseVoiceAnswersBinding, voiceAnswersItemCount, buildVoiceAnswersMeta };
