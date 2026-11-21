'use strict';

const { durableQuestionNotificationId, mergeDurableQuestionNotification } = require('../durable_question_model');

// The shared store carries snapshot notifications but does not index later
// notification frames. Keep the same merge and stale-open rules as app.js.
function createQuestionNotifications(onChange = () => {}, options = {}) {
  const records = new Map();
  const identities = new Map();
  const versions = new Map();
  let version = 0;
  const terminalNotifications = new Set();
  const terminalQuestions = new Set();
  const retiredQuestions = new Set();
  let inFlight = null;
  let refreshAgain = false;
  let readFailed = false;
  let missingIdentities = new Set();
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
        ? { ...record, question_identity_pending: true, question_identity_failed: readFailed || missingIdentities.has(id) }
        : record;
    });
  }
  function refreshIdentities() {
    if (typeof options.promptList !== 'function') return Promise.resolve();
    if (inFlight) { refreshAgain = true; return inFlight; }
    const wasFailed = readFailed || missingIdentities.size > 0;
    readFailed = false;
    missingIdentities = new Set();
    if (wasFailed) onChange();
    const startedAt = version;
    inFlight = Promise.resolve().then(() => options.promptList({ open: true })).then(reply => {
      if (!reply || reply.ok === false || !Array.isArray(reply.questions)) throw new Error('Question identity read failed');
      const existing = new Set([...records.values()].map(questionId));
      const listed = new Set();
      let incomplete = false;
      for (const item of reply.questions) {
        const id = item?.question_id;
        if (item?.state !== 'open') continue;
        if (!id) { incomplete = true; continue; }
        retiredQuestions.delete(id);
        listed.add(id);
        identities.set(id, item.spec_id || item.envelope?.spec_id || null);
        // prompt.list is the complete open set; notification.list is capped.
        // The durable envelope and notification ID already contain the fields
        // needed by the shared question form and resolve path.
        const envelope = item.envelope;
        const notificationId = item.notification_id;
        const producer = item.producer_stream_id || envelope?.producer_stream_id;
        if (!existing.has(id) && (!notificationId || !producer || !envelope)) incomplete = true;
        if (!existing.has(id) && notificationId && producer && envelope
            && !terminalNotifications.has(notificationId) && !terminalQuestions.has(id)) {
          add({ notification_id: notificationId, producer: 'agent_question.v1', state: 'open',
            title: envelope.title, body: envelope.body, answer_to_stream_id: producer,
            question: { ...envelope, question_id: id, producer_stream_id: producer,
              spec_id: item.spec_id || envelope.spec_id || null, state: 'open' } });
          existing.add(id);
        }
      }
      readFailed = incomplete;
      // A current open-list response can retire older open notifications, but
      // must not remove a notification received while that read was in flight.
      for (const [notificationId, record] of records) {
        const id = questionId(record);
        const open = (record.state == null || record.state === 'open')
          && (record.question?.state == null || record.question.state === 'open');
        if (id && open && !listed.has(id) && (versions.get(notificationId) || 0) <= startedAt) {
          records.delete(notificationId);
          identities.delete(id);
          retiredQuestions.add(id);
        }
      }
      missingIdentities = new Set([...records.entries()]
        .filter(([notificationId, record]) => (versions.get(notificationId) || 0) <= startedAt && needsIdentity(record))
        .map(([, record]) => questionId(record)));
      onChange();
    }).catch(() => { readFailed = true; onChange(); }).finally(() => {
      inFlight = null;
      if (refreshAgain) { refreshAgain = false; void refreshIdentities(); }
    });
    return inFlight;
  }
  function add(record) {
    const id = durableQuestionNotificationId(record);
    if (!id) return false;
    const previous = records.get(id);
    const state = record?.state || record?.question?.state;
    if ((!state || state === 'open') && retiredQuestions.has(questionId(record))) return false;
    if (state && state !== 'open') {
      terminalNotifications.add(id);
      const qid = questionId(record) || questionId(previous);
      if (qid) terminalQuestions.add(qid);
    }
    const merged = mergeDurableQuestionNotification(previous, record);
    if (!merged) return false;
    records.set(id, merged);
    versions.set(id, ++version);
    return true;
  }
  function ingest(items) {
    let changed = false;
    let missingIdentity = false;
    for (const item of items || []) {
      changed = add(item) || changed;
      if (needsIdentity(item) || retiredQuestions.has(questionId(item))) missingIdentity = true;
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
  return { frame, ingest, values, refreshIdentities, readFailed: () => readFailed };
}

module.exports = { createQuestionNotifications };
