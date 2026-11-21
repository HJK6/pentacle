'use strict';

const { durableQuestionNotificationId, mergeDurableQuestionNotification } = require('../durable_question_model');

// The shared store carries snapshot notifications but does not index later
// notification frames. Keep the same merge and stale-open rules as app.js.
function createQuestionNotifications(onChange = () => {}, options = {}) {
  const records = new Map();
  const identities = new Map();
  let inFlight = null;
  let refreshAgain = false;
  let identityFailed = false;
  const questionId = record => record?.question?.question_id || record?.question_id || '';
  const hasIdentity = record => Object.prototype.hasOwnProperty.call(record?.question || {}, 'spec_id')
    && record.question.spec_id !== undefined;
  function needsIdentity(record) {
    const question = record?.question;
    return !!questionId(record) && (record?.state == null || record.state === 'open')
      && (question?.state == null || question.state === 'open')
      && !hasIdentity(record)
      && !identities.has(questionId(record));
  }
  function values() {
    return [...records.values()].map(record => {
      const id = questionId(record);
      if (hasIdentity(record)) return record;
      if (identities.has(id)) {
        const specId = identities.get(id);
        return { ...record, question: { ...record.question, spec_id: specId } };
      }
      return options.promptList && needsIdentity(record)
        ? { ...record, question_identity_pending: true, question_identity_failed: identityFailed }
        : record;
    });
  }
  function refreshIdentities() {
    if (typeof options.promptList !== 'function') return Promise.resolve();
    if (inFlight) { refreshAgain = true; return inFlight; }
    const wasFailed = identityFailed;
    identityFailed = false;
    if (wasFailed) onChange();
    inFlight = Promise.resolve().then(() => options.promptList({ open: true })).then(reply => {
      if (!reply || reply.ok === false || !Array.isArray(reply.questions)) throw new Error('Question identity read failed');
      for (const item of reply.questions) {
        const id = item?.question_id;
        if (!id || item.state !== 'open') continue;
        identities.set(id, item.spec_id || item.envelope?.spec_id || null);
      }
      identityFailed = [...records.values()].some(needsIdentity);
      onChange();
    }).catch(() => { identityFailed = true; onChange(); }).finally(() => {
      inFlight = null;
      if (refreshAgain) { refreshAgain = false; void refreshIdentities(); }
    });
    return inFlight;
  }
  function add(record) {
    const id = durableQuestionNotificationId(record);
    if (!id) return false;
    const previous = records.get(id);
    const merged = mergeDurableQuestionNotification(previous, record);
    if (!merged) return false;
    records.set(id, merged);
    return true;
  }
  function ingest(items) {
    let changed = false;
    let missingIdentity = false;
    for (const item of items || []) {
      changed = add(item) || changed;
      if (needsIdentity(item)) missingIdentity = true;
    }
    if (changed) onChange();
    if (missingIdentity) void refreshIdentities();
  }
  function frame(payload) {
    if (payload?.type === 'notification') {
      const item = payload.notification || (payload.producer || payload.question ? payload : null);
      if (item) ingest([item]);
    } else if (Array.isArray(payload?.notifications)) ingest(payload.notifications);
  }
  return { frame, ingest, values, refreshIdentities };
}

module.exports = { createQuestionNotifications };
