'use strict';

// Work-lanes presentation for the desktop/web sidebar
// (spec_pentacle__first_class_work_lanes_2026_10 D8). The daemon projection
// owns lane identity, presented state, count, order and tap target; this module
// only renders it. It never reorders lanes and never derives the lane count
// from sessions. Chat-core helpers (tap target, ETA label) and the status-card
// renderer are injected so this file stays free of bundle imports.

const STATE_LABEL = { blocked: 'BLOCKED', active: 'ACTIVE', paused: 'PAUSED' };

function escapeHtml(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// `inventory` is null until the daemon has sent a lane frame (older daemons
// never do); only then does the header lead with a lane count.
function workLanesStatsText(inventory, { sessions, needsAnswer, working, search, sourceFiltered }) {
  const lanes = inventory && inventory.generated_at !== null ? `${inventory.counts.open} lanes | ` : '';
  return search
    ? `${lanes}${sessions} of ${sourceFiltered} sessions`
    : `${lanes}${sessions} sessions | ${needsAnswer} need answer | ${working} working`;
}

function leadCardSession(lane) {
  const card = lane.lead && lane.lead.status_card;
  if (!card || !(card.goal || card.update)) return null;
  return {
    status_card: {
      goal: card.goal || undefined,
      update: card.update || undefined,
      updated_at: card.updated_at || undefined,
    },
  };
}

function renderLaneRow(lane, { tapTarget, etaLabel, rowStatus, current = true, nowMs, renderLeadCard }) {
  const tap = tapTarget(lane);
  const status = rowStatus(lane, current);
  const attrs = [
    `data-lane-id="${escapeHtml(lane.lane_id)}"`,
    `data-lane-state="${escapeHtml(lane.state)}"`,
    `data-owner-kind="${escapeHtml(lane.owner_kind || '')}"`,
    `data-tap-action="${escapeHtml(tap.action)}"`,
  ];
  if (tap.stream_id) attrs.push(`data-stream-id="${escapeHtml(tap.stream_id)}"`);
  if (tap.generation) attrs.push(`data-generation="${escapeHtml(tap.generation)}"`);
  const eta = etaLabel(lane, nowMs);
  const etaHtml = current && eta.text
    ? `<span class="lane-eta${eta.stale ? ' is-stale' : ''}" title="${eta.stale ? 'No current lead is updating this ETA' : 'Lead ETA'}">${escapeHtml(eta.text)}</span>`
    : '';
  const owner = lane.owner_kind === 'operator'
    ? '<span class="lane-owner is-operator" title="Operator-started lane" aria-label="Operator-started lane">&#9670;</span>'
    : '<span class="lane-owner is-fd" title="FD-managed lane" aria-label="FD-managed lane">&#9671;</span>';
  const detail = status.detail
    ? `<div class="${lane.state === 'blocked' ? 'lane-blocker' : 'lane-summary'}">${escapeHtml(status.lifecycle)}: ${escapeHtml(status.detail)}</div>`
    : '';
  const chatNote = tap.action === 'unavailable'
    ? '<span class="lane-chat-note is-unavailable">Chat unavailable</span>'
    : tap.action === 'history' ? '<span class="lane-chat-note is-history">History</span>' : '';
  const cardSession = current && renderLeadCard ? leadCardSession(lane) : null;
  const cardHtml = cardSession ? renderLeadCard(cardSession, { nowMs }) : '';
  const step = current && lane.lead && lane.lead.status_card && lane.lead.status_card.active_step
    ? `<div class="lane-step">${escapeHtml(lane.lead.status_card.active_step)}</div>` : '';
  const badge = lane.parked ? 'PARKED' : STATE_LABEL[lane.state] || lane.state;
  const primary = status.primary && status.primary !== status.lifecycle
    ? `<div class="lane-activity">${escapeHtml(status.primary)}</div>` : '';
  const secondaryActivity = status.secondaryActivity
    ? `<div class="lane-summary">${escapeHtml(status.secondaryActivity)}</div>` : '';
  const presence = status.lead
    ? `<span class="lane-presence is-${status.lead.health}" title="${status.lead.label}" aria-hidden="true"></span>` : '';
  const leadNote = status.lead ? `<span class="lane-lead">${status.lead.label}</span>` : '';
  const question = status.questionRoute
    ? `<a class="lane-question-link" href="/next/?lane=${escapeHtml(encodeURIComponent(lane.lane_id))}">Open lane questions</a>` : '';
  return `<div class="lane-row-shell"><div class="lane-row" role="button" tabindex="0" ${attrs.join(' ')}>
    <div class="lane-head">
      ${presence}
      <span class="lane-title">${escapeHtml(lane.title || lane.lane_id)}</span>
      ${owner}
      <span class="lane-state is-${escapeHtml(lane.state)}">${current ? '' : 'Last known · '}${escapeHtml(badge)}</span>
    </div>
    ${primary}
    ${secondaryActivity}
    ${lane.summary ? `<div class="lane-summary">${escapeHtml(lane.summary)}</div>` : ''}
    ${detail}
    ${step}
    <div class="lane-meta">${leadNote}${etaHtml}${chatNote}</div>
    ${cardHtml}
  </div>${question}</div>`;
}

// The "Lanes (N)" sidebar section. N is the daemon's open count (counts.open),
// which can exceed lanes.length when the frame is truncated.
function renderWorkLanesPanelHtml(inventory, helpers) {
  if (!inventory || inventory.lanes.length === 0) return '';
  const rows = inventory.lanes.map((lane) => renderLaneRow(lane, helpers)).join('');
  const more = inventory.truncated || inventory.counts.open > inventory.lanes.length
    ? `<div class="lanes-truncated">Showing ${inventory.lanes.length} of ${inventory.counts.open} open lanes</div>`
    : '';
  return `<div class="sidebar-group-label lanes-label">Lanes (${inventory.counts.open})</div>${rows}${more}`;
}

module.exports = { workLanesStatsText, renderWorkLanesPanelHtml, escapeHtml };
