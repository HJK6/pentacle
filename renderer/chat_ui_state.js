function escapeHtml(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function normalizedEventText(text) {
  return String(text || '').replace(/\s+/g, ' ').trim();
}

const hostPresentation = require('./host_presentation');

function hostTitle(host, config = {}) {
  return host ? hostPresentation.hostLabel(config, host) : 'Agent';
}

function hostChrome(host, config = {}, roster) {
  return {
    header: 'var(--pc-header)',
    accent: hostPresentation.ACCENTS[hostPresentation.hostColor(config, host, roster)],
    surface: 'var(--pc-chip-bg)',
    border: 'var(--pc-line)',
    title: hostTitle(host, config),
  };
}

function extractWorkingTime(text) {
  const raw = String(text || '').trim().replace(/^[•⎿]\s+/, '');
  const match = raw.match(/^Working \(([^)]*)/);
  if (!match) return '';
  return match[1].split('•')[0].trim();
}

function isSummaryNoiseLine(line) {
  if (!line) return true;
  return (
    /^gpt-[\d.]+/i.test(line) ||
    /tab to queue message/i.test(line) ||
    /^new task\? \/clear to save/i.test(line) ||
    /bypass permissions on/i.test(line) ||
    /^tip: /i.test(line) ||
    /^messages to be submitted/i.test(line) ||
    /^press esc to interrupt/i.test(line) ||
    /^working \(\d+[smh]/i.test(line)
  );
}

function sanitizeSidebarDetail(text) {
  const value = String(text || '').trim();
  if (!value) return '';
  if (isSummaryNoiseLine(value)) return '';
  if (/^waiting for background terminal\b/i.test(value)) return '';
  if (/^(Read|Edit|Write|Bash|Grep|Glob|Agent|TodoWrite|Explored|Ran|Viewed Image|Updated|Monitor|Waited for background terminal)\b/i.test(value)) return '';
  if (/^Messages to be submitted after next tool call/i.test(value)) return '';
  return value;
}

function deriveComposerInputValue(localDraft, remoteDraft = '', localDraftTouched = false) {
  if (localDraftTouched) return String(localDraft || '');
  return String(localDraft || remoteDraft || '');
}

// Format an elapsed duration (ms) as a compact timer label: `Ns` under a
// minute, `Mm SSs` at/above a minute (e.g. 7000 -> "7s", 64000 -> "1m 04s").
function formatElapsed(ms) {
  const totalSec = Math.max(0, Math.floor(Number(ms) / 1000) || 0);
  if (totalSec < 60) return `${totalSec}s`;
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}m ${String(s).padStart(2, '0')}s`;
}

// Best-effort parse of a daemon working label ("Working (1m 23s)", "45s", ...)
// into seconds, so the client-side live timer can seed from the daemon's own
// elapsed instead of restarting at 0 when this client first observes working.
// Returns null when no duration token is present.
function parseWorkingSeconds(label) {
  const re = /(\d+)\s*([hms])/gi;
  let total = 0;
  let found = false;
  let match;
  while ((match = re.exec(String(label || '')))) {
    found = true;
    const n = Number(match[1]);
    const unit = match[2].toLowerCase();
    total += unit === 'h' ? n * 3600 : unit === 'm' ? n * 60 : n;
  }
  return found ? total : null;
}

// Status row: ONLY the working state shows a badge — a dot plus a live elapsed
// timer (the caller updates `.slot-chat-status-timer` each second). Idle and
// waiting render NO badge, so an idle session shows an empty status row instead
// of a circle. A genuine in-flight send still shows the pending badge.
function renderStatusBadges({ activity = 'idle', workingLabel = '', pending = false } = {}) {
  const showPending = pending && activity !== 'working' && activity !== 'unresponsive';
  const activityBadge = activity === 'working'
    ? `<span class="slot-chat-status-badge is-working" aria-label="Elapsed working time"><span class="activity-spinner slot-chat-status-dot"></span><span class="slot-chat-status-timer">${escapeHtml(workingLabel || '')}</span></span>`
    : activity === 'unresponsive'
      ? '<span class="slot-chat-status-badge is-unresponsive" role="status">Session unresponsive — tmux did not answer</span>'
    : '';
  const pendingBadge = showPending
    ? '<span class="slot-chat-status-badge is-pending" title="Pending send"><span class="slot-chat-status-dot"></span></span>'
    : '';
  return `
    <div class="slot-chat-status-left">
      ${activityBadge}
      ${pendingBadge}
    </div>`;
}

function renderDraftPreview({ remoteDraft = '', remotePending = false, activity = 'idle', timestampLabel = '' } = {}) {
  if (!remoteDraft) return '';
  const classes = [
    'slot-chat-draft-preview',
    remotePending ? 'is-pending' : '',
    activity === 'working' ? 'is-working' : '',
  ].filter(Boolean).join(' ');
  return `
    <div class="${classes}">
      <div class="slot-chat-draft-preview-head">
        <span class="slot-chat-draft-preview-label">${remotePending ? 'Queued for next tool call' : 'Draft in progress'}</span>
        <span class="slot-chat-draft-preview-time">${escapeHtml(timestampLabel)}</span>
      </div>
      <div class="slot-chat-draft-preview-body">${escapeHtml(remoteDraft)}</div>
    </div>`;
}

// ── Status card (public status-card contract) ────────────────
// Pure HTML for the per-session status card + daemon-attached indicators.
// Renders only the fields present: no placeholders for missing goal/plan/
// update; card age always derives from the daemon-stamped updated_at.
// Indicator slots (context_*, spec_issues) are populated by sibling lanes and
// render whenever present — even for sessions with no agent-written card.

function formatCardAge(updatedAtIso, nowMs) {
  const stamp = Date.parse(String(updatedAtIso || ''));
  if (!Number.isFinite(stamp)) return '';
  const ageS = Math.max(0, ((Number.isFinite(nowMs) ? nowMs : Date.now()) - stamp) / 1000);
  if (ageS < 60) return 'just now';
  if (ageS < 3600) return `${Math.floor(ageS / 60)}m ago`;
  if (ageS < 86400) return `${Math.floor(ageS / 3600)}h ago`;
  return `${Math.floor(ageS / 86400)}d ago`;
}

function statusCardStepRow(plan) {
  if (!Array.isArray(plan) || !plan.length) return '';
  const total = plan.length;
  const doneCount = plan.filter((step) => step && step.status === 'done').length;
  const active = plan.find((step) => step && step.status === 'active');
  const label = active
    ? `${Math.min(doneCount + 1, total)}/${total} · ${normalizedEventText(active.text)}`
    : (doneCount === total ? `${total}/${total} done` : `${doneCount}/${total}`);
  return `<div class="slot-status-card-row is-step" title="Current plan step">${escapeHtml(label)}</div>`;
}

function statusCardSpecIssues(streamSession) {
  return Array.isArray(streamSession && streamSession.spec_issues)
    ? streamSession.spec_issues.filter(Boolean)
    : [];
}

function statusCardIndicators(streamSession, card) {
  const badges = [];
  if (card && card.handoff_planned === true) {
    badges.push('<span class="slot-status-card-badge is-handoff" title="This session plans to hand off">handoff planned</span>');
  }
  const tokens = Number(streamSession && streamSession.context_tokens);
  if (Number.isFinite(tokens) && tokens > 0) {
    const windowTokens = Number(streamSession.model_context_window);
    const hasWindow = Number.isFinite(windowTokens) && windowTokens > 0;
    // Window % must be visible on the badge (not just the tooltip).
    const pctText = hasWindow ? ` · ${Math.round((tokens / windowTokens) * 100)}%` : '';
    const level = String(streamSession.context_level || '').toLowerCase();
    const levelClass = level ? ` is-ctx-${escapeHtml(level)}` : '';
    const title = hasWindow
      ? `${escapeHtml(String(tokens))} context tokens of ${escapeHtml(String(windowTokens))}`
      : `${escapeHtml(String(tokens))} context tokens`;
    badges.push(
      `<span class="slot-status-card-badge is-context${levelClass}" title="${title}">ctx ${escapeHtml(`${Math.round(tokens / 1000)}k`)}${escapeHtml(pctText)}</span>`,
    );
  }
  const specIssues = statusCardSpecIssues(streamSession);
  if (specIssues.length) {
    badges.push(
      `<span class="slot-status-card-badge is-spec-issue" title="This session has spec issues needing attention">&#9888; ${specIssues.length} spec issue${specIssues.length === 1 ? '' : 's'}</span>`,
    );
  }
  return badges.join('');
}

// Spec-issue entries are listed as visible rows in the (expanded) card, not
// hidden in a tooltip — one row per obligation.
function statusCardSpecIssueRows(streamSession) {
  const specIssues = statusCardSpecIssues(streamSession);
  if (!specIssues.length) return '';
  return specIssues
    .map(
      (issue) =>
        `<div class="slot-status-card-row is-spec-issue-entry">&#9888; ${escapeHtml(normalizedEventText(issue.detail || issue.obligation_id || 'spec issue'))}</div>`,
    )
    .join('');
}

function renderStatusCard(streamSession = {}, { nowMs = Date.now() } = {}) {
  const session = streamSession || {};
  const card = session.status_card && typeof session.status_card === 'object' ? session.status_card : null;
  const indicators = statusCardIndicators(session, card);
  if (!card && !indicators) return '';
  const goalRow = card && card.goal
    ? `<div class="slot-status-card-row is-goal">${escapeHtml(normalizedEventText(card.goal))}</div>`
    : '';
  const stepRow = card ? statusCardStepRow(card.plan) : '';
  const updateRow = card && card.update
    ? `<div class="slot-status-card-row is-update">${escapeHtml(normalizedEventText(card.update))}</div>`
    : '';
  const specIssueRows = statusCardSpecIssueRows(session);
  const age = card ? formatCardAge(card.updated_at, nowMs) : '';
  const ageHtml = age ? `<span class="slot-status-card-age" title="Last status update">${escapeHtml(age)}</span>` : '';
  const metaRow = (ageHtml || indicators)
    ? `<div class="slot-status-card-row is-meta">${ageHtml}${indicators}</div>`
    : '';
  return `
    <div class="slot-status-card">
      ${goalRow}
      ${stepRow}
      ${updateRow}
      ${specIssueRows}
      ${metaRow}
    </div>`;
}

// ── Full slot-scoped status view ───────────────────────────────────────────
// public navigation status contract scope items
// 3 & 4. The inline compact card (renderStatusCard) stays as-is; this is the
// dedicated, complete view. Renders every field that is present — model/effort,
// goal, the COMPLETE plan (not just the active step), latest update, the full
// update history, context/handoff indicators, spec lifecycle, and spec issues —
// with stable DOM identifiers + ARIA and explicit return paths to the transcript
// and update log. Scroll preservation across live updates is the caller's job
// (the update-history region carries data-status-scroll for that).

function statusViewModelEffort(session) {
  const model = normalizedEventText(session && session.model);
  const effort = normalizedEventText(session && session.effort);
  if (!model && !effort) return '';
  const parts = [];
  if (model) parts.push(`<span class="slot-status-view-model">${escapeHtml(model)}</span>`);
  if (effort) parts.push(`<span class="slot-status-view-effort">effort: ${escapeHtml(effort)}</span>`);
  return `<div class="slot-status-view-row is-model-effort">${parts.join('')}</div>`;
}

function planProgress(plan) {
  const steps = Array.isArray(plan) ? plan.filter(Boolean) : [];
  const total = steps.length;
  const done = steps.filter((step) => step && step.status === 'done').length;
  const active = steps.findIndex((step) => step && step.status === 'active');
  return { total, done, activeIndex: active };
}

function statusViewFullPlan(plan) {
  const steps = Array.isArray(plan) ? plan.filter(Boolean) : [];
  if (!steps.length) return '';
  const { total, done } = planProgress(steps);
  const header = `<div class="slot-status-view-plan-progress" role="status">${done}/${total} done</div>`;
  const rows = steps
    .map((step, index) => {
      const status = step.status === 'done' || step.status === 'active' ? step.status : 'pending';
      const marker = status === 'done' ? '✓' : status === 'active' ? '→' : '·';
      return `<li class="slot-status-view-step is-${status}" data-step-index="${index}"><span class="slot-status-view-step-marker" aria-hidden="true">${marker}</span><span class="slot-status-view-step-text">${escapeHtml(normalizedEventText(step.text))}</span></li>`;
    })
    .join('');
  return `<div class="slot-status-view-row is-plan"><div class="slot-status-view-label">Plan</div>${header}<ol class="slot-status-view-plan">${rows}</ol></div>`;
}

function statusViewUpdateHistory(card) {
  const updates = card && Array.isArray(card.updates) ? card.updates.filter(Boolean) : [];
  if (!updates.length) return '';
  // Newest first so the most recent update leads; the region is independently
  // scrollable and the caller restores scrollTop across live re-renders.
  const rows = updates
    .slice()
    .sort((a, b) => String(b.ts || '').localeCompare(String(a.ts || '')))
    .map(
      (entry) =>
        `<li class="slot-status-view-update"><span class="slot-status-view-update-ts">${escapeHtml(normalizedEventText(entry.ts))}</span><span class="slot-status-view-update-text">${escapeHtml(normalizedEventText(entry.text))}</span></li>`,
    )
    .join('');
  return `<div class="slot-status-view-row is-updates"><div class="slot-status-view-label">Update history</div><ul class="slot-status-view-history" data-status-scroll="updates" tabindex="0" aria-label="Update history">${rows}</ul></div>`;
}

function specLifecycleTone(spec) {
  if (!spec) return 'is-unknown';
  if (spec.ok === false) return 'is-attention';
  if (spec.ok === true) return 'is-ok';
  return 'is-unknown';
}

function statusViewSpecLifecycle(card) {
  const specs = card && Array.isArray(card.specs) ? card.specs.filter(Boolean) : [];
  if (!specs.length) return '';
  const rows = specs
    .map((spec) => {
      const tone = specLifecycleTone(spec);
      const status = normalizedEventText(spec.status) || (spec.ok === false ? 'attention' : spec.ok === true ? 'ok' : '');
      const statusHtml = status ? `<span class="slot-status-view-spec-status">${escapeHtml(status)}</span>` : '';
      const label = normalizedEventText(spec.label) || normalizedEventText(spec.id) || 'spec';
      const note = normalizedEventText(spec.note);
      const noteHtml = note ? `<span class="slot-status-view-spec-note">${escapeHtml(note)}</span>` : '';
      return `<li class="slot-status-view-spec ${tone}" data-spec-id="${escapeHtml(normalizedEventText(spec.id))}"><span class="slot-status-view-spec-label">${escapeHtml(label)}</span>${statusHtml}${noteHtml}</li>`;
    })
    .join('');
  return `<div class="slot-status-view-row is-spec-lifecycle"><div class="slot-status-view-label">Spec lifecycle</div><ul class="slot-status-view-specs">${rows}</ul></div>`;
}

function renderStatusView(streamSession = {}, { nowMs = Date.now() } = {}) {
  const session = streamSession || {};
  const card = session.status_card && typeof session.status_card === 'object' ? session.status_card : null;
  const goalRow = card && card.goal
    ? `<div class="slot-status-view-row is-goal"><div class="slot-status-view-label">Goal</div><div class="slot-status-view-goal">${escapeHtml(normalizedEventText(card.goal))}</div></div>`
    : '';
  const latestUpdateRow = card && card.update
    ? `<div class="slot-status-view-row is-latest-update"><div class="slot-status-view-label">Latest update</div><div class="slot-status-view-update-latest">${escapeHtml(normalizedEventText(card.update))}</div></div>`
    : '';
  const indicators = statusCardIndicators(session, card);
  const indicatorsRow = indicators ? `<div class="slot-status-view-row is-indicators">${indicators}</div>` : '';
  const specIssueRows = statusCardSpecIssueRows(session);
  const specIssuesRow = specIssueRows
    ? `<div class="slot-status-view-row is-spec-issues"><div class="slot-status-view-label">Spec issues</div>${specIssueRows}</div>`
    : '';
  const age = card ? formatCardAge(card.updated_at, nowMs) : '';
  const ageHtml = age ? `<span class="slot-status-view-age" title="Last status update">${escapeHtml(age)}</span>` : '';
  // Return paths (item 4): back to transcript, and to the (in-view) update log.
  const returns = `
      <div class="slot-status-view-returns">
        <button type="button" class="slot-status-view-return" data-status-return="transcript" aria-label="Back to transcript">Transcript</button>
        <button type="button" class="slot-status-view-return" data-status-return="updates" aria-label="Jump to update log">Update log</button>
      </div>`;
  const body = [
    statusViewModelEffort(session),
    goalRow,
    statusViewFullPlan(card && card.plan),
    latestUpdateRow,
    statusViewUpdateHistory(card),
    indicatorsRow,
    statusViewSpecLifecycle(card),
    specIssuesRow,
  ].join('');
  return `
    <section class="slot-status-view" role="region" aria-label="Session status" data-status-view="1">
      <header class="slot-status-view-header">
        <span class="slot-status-view-title">Session status</span>
        ${ageHtml}
        ${returns}
      </header>
      <div class="slot-status-view-body">${body}</div>
    </section>`;
}

// ── Session Status card view (public status-card UI contract) ──
// The designed dedicated surface: a header glyph toggles this sectioned card
// into the chat body in place of the transcript. Same shipped data contract as
// the inline card (no daemon/schema change) — this is the richer presentation.

// Attention + content state for a session, driving the header glyph:
//  - hasContent: a card OR any daemon-attached indicator exists (toggle enabled)
//  - attention: a spec issue, advisory/handoff context level, or planned handoff
//    exists — the closed glyph must signal this so the at-a-glance cue is kept.
function statusCardAttentionState(streamSession = {}) {
  const s = streamSession || {};
  const card = s.status_card && typeof s.status_card === 'object' ? s.status_card : null;
  const specIssues = statusCardSpecIssues(s);
  const tokens = Number(s.context_tokens);
  const hasContext = Number.isFinite(tokens) && tokens > 0;
  const level = String(s.context_level || '').toLowerCase();
  const levelSignal = level === 'advisory' || level === 'handoff';
  // levelSignal is folded into hasIndicators so the "closed-glyph attention iff
  // advisory/handoff level" contract can never contradict a disabled glyph. The
  // daemon always sends context_tokens alongside a level, so this only matters
  // as a defensive invariant.
  const hasIndicators = specIssues.length > 0 || hasContext || levelSignal;
  const attention = specIssues.length > 0
    || levelSignal
    || (card ? card.handoff_planned === true : false);
  return { hasCard: !!card, hasIndicators, hasContent: !!card || hasIndicators, attention };
}

function statusCardContextBadge(streamSession) {
  const tokens = Number(streamSession && streamSession.context_tokens);
  if (!Number.isFinite(tokens) || tokens <= 0) return '';
  const windowTokens = Number(streamSession.model_context_window);
  const hasWindow = Number.isFinite(windowTokens) && windowTokens > 0;
  const pctText = hasWindow ? ` · ${Math.round((tokens / windowTokens) * 100)}%` : '';
  const level = String(streamSession.context_level || '').toLowerCase();
  const levelClass = level ? ` is-ctx-${escapeHtml(level)}` : '';
  const title = hasWindow
    ? `${escapeHtml(String(tokens))} context tokens of ${escapeHtml(String(windowTokens))}`
    : `${escapeHtml(String(tokens))} context tokens`;
  return `<span class="session-status-context${levelClass}" title="${title}">${escapeHtml(`${Math.round(tokens / 1000)}k`)}${escapeHtml(pctText)}</span>`;
}

function statusCardStepGlyph(status) {
  if (status === 'done') return '<span class="session-status-step-glyph is-done" aria-hidden="true">&#10003;</span>';
  if (status === 'active') return '<span class="session-status-step-glyph is-active" aria-hidden="true"></span>';
  return '<span class="session-status-step-glyph is-pending" aria-hidden="true"></span>';
}

function renderSessionStatusCardView(streamSession = {}, { nowMs = Date.now() } = {}) {
  const s = streamSession || {};
  const card = s.status_card && typeof s.status_card === 'object' ? s.status_card : null;
  const { hasContent } = statusCardAttentionState(s);
  if (!hasContent) return '';

  const age = card ? formatCardAge(card.updated_at, nowMs) : '';
  const ageHtml = age ? `<span class="session-status-age" title="Last status update">updated ${escapeHtml(age)}</span>` : '';
  const ctxBadge = statusCardContextBadge(s);
  const handoffChip = card && card.handoff_planned === true
    ? '<span class="session-status-handoff" title="This session plans to hand off">handoff planned</span>'
    : '';

  const goalHtml = card && card.goal
    ? `<div class="session-status-section is-goal"><div class="session-status-label">GOAL</div><div class="session-status-goal-text">${escapeHtml(normalizedEventText(card.goal))}</div></div>`
    : '';

  let planHtml = '';
  const plan = card && Array.isArray(card.plan) ? card.plan.filter(Boolean) : [];
  if (plan.length) {
    const total = plan.length;
    const done = plan.filter((st) => st && st.status === 'done').length;
    const activeIdx = plan.findIndex((st) => st && st.status === 'active');
    // Current step is the ACTIVE step (design contract), not done+1 — they can
    // differ for non-canonical plans (e.g. [pending, active]).
    const rollup = done === total
      ? `${total}/${total} done`
      : `Step ${activeIdx >= 0 ? activeIdx + 1 : Math.min(done + 1, total)} of ${total}`;
    const pct = Math.round((done / total) * 100);
    const steps = plan.map((st) => {
      const status = st.status === 'done' ? 'done' : (st.status === 'active' ? 'active' : 'pending');
      return `<div class="session-status-step is-${status}">${statusCardStepGlyph(status)}<span class="session-status-step-text">${escapeHtml(normalizedEventText(st.text || ''))}</span></div>`;
    }).join('');
    planHtml = `<div class="session-status-section is-plan">
        <div class="session-status-plan-head"><div class="session-status-label">PLAN</div><span class="session-status-rollup">${escapeHtml(rollup)}</span></div>
        <div class="session-status-progress" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><div class="session-status-progress-fill" style="width:${pct}%"></div></div>
        <div class="session-status-steps">${steps}</div>
      </div>`;
  }

  const updateHtml = card && card.update
    ? `<div class="session-status-section is-update"><div class="session-status-label">LATEST UPDATE <span class="session-status-pulse" aria-hidden="true"></span></div><div class="session-status-update-panel">${escapeHtml(normalizedEventText(card.update))}</div></div>`
    : '';

  const specIssues = statusCardSpecIssues(s);
  let specHtml = '';
  if (specIssues.length) {
    const cards = specIssues.map((issue) => {
      const id = issue.obligation_id ? `<span class="session-status-issue-id">${escapeHtml(String(issue.obligation_id))}</span>` : '';
      const iage = issue.set_at ? `<span class="session-status-issue-age">${escapeHtml(formatCardAge(issue.set_at, nowMs))}</span>` : '';
      const detail = escapeHtml(normalizedEventText(issue.detail || issue.obligation_id || 'spec issue'));
      return `<div class="session-status-issue-card"><div class="session-status-issue-head">${id}${iage}</div><div class="session-status-issue-detail">${detail}</div></div>`;
    }).join('');
    specHtml = `<div class="session-status-section is-spec-issues"><div class="session-status-label is-warn">&#9888; Spec issues &middot; ${specIssues.length}</div>${cards}</div>`;
  }

  return `<div class="session-status-card">
      <div class="session-status-header">
        <div class="session-status-title"><span class="session-status-glyph" aria-hidden="true"></span><span class="session-status-title-label">SESSION STATUS</span></div>
        <div class="session-status-header-meta">${ageHtml}${ctxBadge}${handoffChip}<button type="button" class="session-status-close" title="Close session status" aria-label="Close session status">&times;</button></div>
      </div>
      ${goalHtml}${planHtml}${updateHtml}${specHtml}
    </div>`;
}

function compactIdentifier(value) {
  return normalizedEventText(value).toLowerCase();
}

function desktopSessionMatchesStreamSession(streamSession = {}, desktopSession = {}, streamHost = '') {
  const hostMatches = !streamHost || streamSession.host === streamHost;
  const desktopIds = [
    desktopSession.streamId,
    desktopSession.name,
    desktopSession.displayName,
    desktopSession.display_name,
    desktopSession.title,
  ].map(compactIdentifier).filter(Boolean);
  const streamIds = [
    streamSession.stream_id,
    streamSession.session_name,
    streamSession.display_name,
    streamSession.title,
  ].map(compactIdentifier).filter(Boolean);
  const idMatches = desktopIds.some((candidate) => streamIds.includes(candidate));
  return { hostMatches, idMatches };
}

// Strong match ONLY: host AND id both match. Used as the authoritative
// intended stream for a slot (Bug1 chat_ui_hardening_batch3 new-chat-flash
// guard) — it never falls back to an id-only cross-host/blank-name match, so a
// transient id-only collision with a PREVIOUS stream cannot masquerade as the
// bound stream. Returns null when this session's own stream is not-yet-present.
function findStrongStreamSessionForDesktopSession(streamState = {}, desktopSession = {}, streamHost = '') {
  const sessions = streamState.sessions || [];
  return (
    sessions.find((item) => {
      const match = desktopSessionMatchesStreamSession(item, desktopSession, streamHost);
      return match.hostMatches && match.idMatches;
    }) || null
  );
}

function findStreamSessionForDesktopSession(streamState = {}, desktopSession = {}, streamHost = '') {
  const sessions = streamState.sessions || [];
  return (
    findStrongStreamSessionForDesktopSession(streamState, desktopSession, streamHost) ||
    sessions.find((item) => desktopSessionMatchesStreamSession(item, desktopSession, streamHost).idMatches) ||
    null
  );
}

module.exports = {
  deriveComposerInputValue,
  escapeHtml,
  extractWorkingTime,
  formatElapsed,
  parseWorkingSeconds,
  findStreamSessionForDesktopSession,
  findStrongStreamSessionForDesktopSession,
  hostChrome,
  hostTitle,
  isSummaryNoiseLine,
  formatCardAge,
  renderDraftPreview,
  renderStatusBadges,
  renderStatusCard,
  renderStatusView,
  planProgress,
  specLifecycleTone,
  renderSessionStatusCardView,
  statusCardAttentionState,
  sanitizeSidebarDetail,
};
