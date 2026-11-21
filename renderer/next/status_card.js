'use strict';
// The rollup follows the current web's active-step convention.
function statusRollup(card = {}) {
  const plan = Array.isArray(card.plan) ? card.plan.filter(Boolean) : [];
  const total = plan.length; const done = plan.filter(step => step.status === 'done').length;
  const active = plan.findIndex(step => step.status === 'active');
  return { total, done, percent: total ? Math.round(done / total * 100) : 0,
    label: !total ? '' : done === total ? `${done}/${total} done` : `Step ${active < 0 ? Math.min(done + 1, total) : active + 1} of ${total}` };
}
function nextMode(current, requested) {
  if (!['thread', 'terminal', 'status'].includes(current) || !['terminal', 'status'].includes(requested)) throw new TypeError('invalid chat view');
  return current === requested ? 'thread' : requested;
}
module.exports = { statusRollup, nextMode };
