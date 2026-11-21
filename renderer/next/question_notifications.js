'use strict';

const { durableQuestionNotificationId, mergeDurableQuestionNotification } = require('../durable_question_model');

// The shared store carries snapshot notifications but does not index later
// notification frames. Keep the same merge and stale-open rules as app.js.
function createQuestionNotifications(onChange = () => {}) {
  const records = new Map();
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
    for (const item of items || []) changed = add(item) || changed;
    if (changed) onChange();
  }
  function frame(payload) {
    if (payload?.type === 'notification') {
      const item = payload.notification || (payload.producer || payload.question ? payload : null);
      if (item) ingest([item]);
    } else if (Array.isArray(payload?.notifications)) ingest(payload.notifications);
  }
  return { frame, ingest, values: () => [...records.values()] };
}

module.exports = { createQuestionNotifications };
