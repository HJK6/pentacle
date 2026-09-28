// ══════════════════════════════════════════════════════════════
// ── Specs Dashboard ──────────────────────────────────────────
// ══════════════════════════════════════════════════════════════
// Read-only triage view over every spec under the shared-memory
// work/<status>/ tree. Talks to chat_streamd via the same WS that
// powers the Chats tab (round-tripped through main process IPC; see
// main/chat_stream_client.js specsList/Get/Drive/Capabilities and
// main.js specs:* handlers).
//
// Layout mirrors the Pi `specs` profile (control-dashboard-display
// dashboards/specs/profile.js): minimal full-width kanban that fits
// the viewport, no detail side-pane, no header chrome above the
// columns, no horizontal scroll. Clicking a card opens a modal with
// summary/spec/activity tabs plus the Drive / Spawn-additional action.

(function() {
'use strict';

const specsState = require('../specs_dashboard_state');
const { localIdentity } = require('../host_presentation');
const {
  DEFAULT_STATUSES,
  escapeHtml: esc,
  formatProgress,
  decideAction,
  gateHosts,
  liveLeaderSummary,
  sortForList,
  unresolvedReasonTooltip,
} = specsState;

let __specsShowAll = false;

// Shared dashboard definition layer (classic UMD global, loaded before this
// script in index.html). When present, the kanban + Specs/Epics toggle render
// from the ONE canonical triforce-dashboards `specs` board; this adapter keeps
// the desktop-only Drive/Spawn modal (the manifest `spec-drive` interactiveOnly
// action) wired through chat_streamd. Falls back to the legacy inline render if
// the shared lib is somehow absent.
function sharedSpecsBoard() {
  const TD = (typeof window !== 'undefined' && window.TriforceDashboards) || null;
  return TD && TD.boards && TD.boards.specs ? TD : null;
}

const POLL_INTERVAL_MS = 30000;
const IDLE_POLL_INTERVAL_MS = 60000;

// ── Status partition (mirror Pi profile.js exactly) ───────────
// Pi's partitionByStatus: name = row.status || row.lifecycle; route
// to known[name] if present, else to "other". Keeps the desktop and
// Pi consistent — if the daemon still emits the legacy `lifecycle`
// alias for any row, we fall back to it instead of dropping the row
// into "Other".
function _partitionRows(rows, statuses) {
  const known = {};
  for (const s of statuses) known[s.name] = [];
  const other = [];
  for (const row of rows || []) {
    if (!row || row.synthetic) continue;
    const name = row.status || row.lifecycle;
    if (row.status_unknown === true || !(name in known)) {
      other.push(row);
    } else {
      known[name].push(row);
    }
  }
  return { known, other };
}

// ── DOM construction ──────────────────────────────────────────

function mount(container) {
  container.innerHTML = '';
  const root = document.createElement('div');
  root.style.cssText = 'height:100%;box-sizing:border-box;';
  container.appendChild(root);
  // The shared triforce-dashboards `specs` board renders the full shell
  // (toggle + kanban) into root; this adapter owns data + the Drive modal.
  const kanban = root;

  const refs = {
    root,
    kanban,
    // Mutable state
    view: 'specs',
    epics: [],
    selectedSpecId: null,
    lastRows: [],
    statuses: DEFAULT_STATUSES.slice(),
    capabilities: null,
    config: null,
    pushListener: null,
    modalEl: null,
  };

  // Render an empty skeleton immediately so first paint isn't blank
  // while we wait for the first specs.list poll to return.
  renderKanban(refs);

  // Subscribe to specs.changed push events. unmount() removes the entry.
  const listeners = (window.__pentacleSpecsChangedListeners = window.__pentacleSpecsChangedListeners || []);
  refs.pushListener = (_payload) => {
    if (typeof window.refreshDashboardNow === 'function') window.refreshDashboardNow();
  };
  listeners.push(refs.pushListener);

  // Kick off the capabilities fetch in parallel with the first poll
  // so the modal can render hosts without an extra round trip.
  _fetchCapabilities(refs).catch(() => {});

  return refs;
}

function unmount(refs) {
  if (!refs) return;
  const listeners = window.__pentacleSpecsChangedListeners || [];
  const idx = listeners.indexOf(refs.pushListener);
  if (idx >= 0) listeners.splice(idx, 1);
  refs.pushListener = null;
  if (refs.modalEl && refs.modalEl.parentNode) {
    refs.modalEl.parentNode.removeChild(refs.modalEl);
  }
  refs.modalEl = null;
}

// ── Polling / data fetch ──────────────────────────────────────

async function pollFn(_refs) {
  try {
    const reply = await window.cc.specsList({});
    if (!reply || reply.ok === false) {
      return { error: (reply && reply.error) || 'Unknown specs.list error' };
    }
    return reply;
  } catch (e) {
    return { error: (e && e.message) || String(e) };
  }
}

async function _fetchCapabilities(refs) {
  try {
    const [reply, config] = await Promise.all([
      window.cc.specsCapabilities(),
      typeof window.cc.getConfig === 'function' ? window.cc.getConfig().catch(() => null) : null,
    ]);
    refs.config = config;
    if (reply && reply.ok !== false) {
      refs.capabilities = reply;
      if (Array.isArray(reply.statuses) && reply.statuses.length > 0) {
        refs.statuses = reply.statuses;
        renderKanban(refs);
      }
    }
  } catch (_) { /* tolerate; modal will rerequest */ }
}

// ── Top-level update ──────────────────────────────────────────

function update(refs, data) {
  if (!refs || !data) return;
  refs.lastRows = Array.isArray(data.specs) ? data.specs : [];
  if (Array.isArray(data.statuses) && data.statuses.length > 0) {
    refs.statuses = data.statuses;
  }
  if (Array.isArray(data.epics)) refs.epics = data.epics;
  renderKanban(refs);
}

// ── Kanban: one column per status, flex:1 split, no scroll ───

function renderKanban(refs) {
  const statuses = (refs.statuses && refs.statuses.length > 0) ? refs.statuses : DEFAULT_STATUSES;
  const TD = sharedSpecsBoard();
  if (TD) {
    TD.renderBoard(refs.kanban, TD.boards.specs, {
      view: refs.view || 'specs',
      specs: refs.lastRows,
      statuses,
      epics: refs.epics || [],
      showAll: __specsShowAll,
    }, { mode: 'interactive' });
    _attachToggle(refs, refs.kanban);
    _attachRowClicks(refs, refs.kanban);
    return;
  }
  // Legacy inline fallback (shared lib absent).
  const { known, other } = _partitionRows(refs.lastRows, statuses);
  const visibleStatuses = statuses.filter((s) => __specsShowAll || s.default_visible !== false);
  const cols = visibleStatuses.map((s) => _columnHtml(s, sortForList(known[s.name] || [])));
  if (other.length > 0) {
    cols.push(_columnHtml({ name: '__other__', display_label: 'Other', color: '#f5b78a' }, sortForList(other)));
  }
  refs.kanban.innerHTML = cols.join('');
  _attachRowClicks(refs, refs.kanban);
}

function _attachToggle(refs, container) {
  container.querySelectorAll('.specs-toggle button[data-view]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const v = btn.dataset.view === 'epics' ? 'epics' : 'specs';
      if (refs.view === v) return;
      refs.view = v;
      renderKanban(refs);
    });
  });
}

function _columnHtml(status, rows) {
  const accent = esc(status.color || '#8aa097');
  const label = esc(status.display_label || status.name);
  const cardsHtml = rows.length > 0
    ? rows.map(_cardHtml).join('')
    : `<div style="padding:10px;border:1px dashed #2a3b33;border-radius:8px;color:#5e6d65;font-size:11px;text-align:center;">No specs</div>`;
  // flex:1 1 0 with min-width:0 splits the row equally across however many
  // columns the renderer ends up showing — same approach as the Pi profile.
  return `<section data-status="${esc(status.name)}" style="flex:1 1 0;min-width:0;display:flex;flex-direction:column;border:1px solid #24342d;border-top:3px solid ${accent};border-radius:10px;background:#0f1713;overflow:hidden;">
    <header style="display:flex;align-items:center;justify-content:space-between;padding:8px 10px;border-bottom:1px solid #24342d;background:linear-gradient(180deg,rgba(255,255,255,0.02),transparent);">
      <div style="display:flex;align-items:center;gap:8px;min-width:0;">
        <span style="width:8px;height:8px;border-radius:50%;background:${accent};flex-shrink:0;"></span>
        <span style="font-size:11px;text-transform:uppercase;letter-spacing:0.6px;color:#e6eee9;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${label}</span>
      </div>
      <span style="font-size:11px;color:#8aa097;font-variant-numeric:tabular-nums;">${rows.length}</span>
    </header>
    <div data-role="column-body" style="display:flex;flex-direction:column;gap:6px;overflow-y:auto;overflow-x:hidden;padding:8px;flex:1;min-height:0;">${cardsHtml}</div>
  </section>`;
}

function _cardHtml(row) {
  const title = esc(row.title || row.spec_id || '(untitled)');
  const repo = row.repo
    ? `<span style="font-size:10px;padding:2px 7px;border-radius:999px;background:#202a25;color:#aab8b0;">${esc(row.repo)}</span>`
    : '';
  const statusPill = row.status
    ? `<span style="font-size:10px;padding:2px 7px;border-radius:999px;background:#1d2640;color:#a8b8ff;text-transform:uppercase;">${esc(row.status)}</span>`
    : '';
  const drift = row.frontmatter_drift
    ? `<span title="frontmatter status does not match folder" style="font-size:10px;padding:2px 7px;border-radius:999px;background:#3a2614;color:#f5b78a;text-transform:uppercase;">drift</span>`
    : '';
  const next = row.next_action
    ? `<div style="font-size:12px;color:#aab8b0;margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(row.next_action)}</div>`
    : '';
  const lead = liveLeaderSummary(row);
  const leadIndicator = lead.count > 0
    ? `<span title="Live leaders driving this spec" style="display:inline-flex;align-items:center;gap:4px;font-size:11px;color:#56d364;"><span style="width:8px;height:8px;border-radius:50%;background:#56d364;"></span>${lead.count}</span>`
    : '';
  return `<div class="spec-row" data-spec-id="${esc(row.spec_id)}" style="border:1px solid #24342d;background:#121a16;border-radius:8px;padding:8px 10px;cursor:pointer;">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;flex-wrap:wrap;">
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;min-width:0;">${statusPill}${repo}${drift}</div>
      ${leadIndicator}
    </div>
    <div style="margin-top:6px;font-size:13px;color:#f2fbf5;font-weight:600;line-height:1.25;">${title}</div>
    ${next}
  </div>`;
}

function _attachRowClicks(refs, container) {
  container.querySelectorAll('.spec-row').forEach((el) => {
    el.addEventListener('click', () => {
      const id = el.dataset.specId;
      if (!id) return;
      refs.selectedSpecId = id;
      const row = (refs.lastRows || []).find((r) => r.spec_id === id);
      if (row) _openSpecModal(refs, row);
    });
  });
}

// ── Spec modal: details + Drive action in one ────────────────

async function _openSpecModal(refs, row) {
  if (refs.modalEl && refs.modalEl.parentNode) refs.modalEl.parentNode.removeChild(refs.modalEl);
  refs.modalEl = null;

  if (!refs.capabilities) await _fetchCapabilities(refs);
  const caps = refs.capabilities || { hosts: {} };
  const currentHost = refs.config ? localIdentity(refs.config) : null;
  const hostEntries = gateHosts(caps, row.machine, currentHost);
  const action = decideAction(row);

  const modal = document.createElement('div');
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.65);display:flex;align-items:center;justify-content:center;z-index:9999;padding:20px;';
  modal.dataset.role = 'specs-detail-modal';

  // State local to this modal instance.
  let selectedTab = 'summary';
  let fetchedDetail = null; // { spec_md, summary_md, error }
  let fetchingDetail = false;
  let provider = 'codex';
  let mode = action.kind === 'drive' ? 'drive_to_completion' : 'context_then_ask';

  const renderModalBody = () => {
    const isSynthetic = row.synthetic === true;
    const title = row.title || row.spec_id;
    const tracking = [
      row.machine && `machine: ${row.machine}`,
      row.owner && `owner: ${row.owner}`,
      row.updated_at && `updated: ${row.updated_at}`,
    ].filter(Boolean).join(' · ');
    const statusPill = isSynthetic
      ? `<span style="font-size:11px;padding:3px 8px;border-radius:999px;background:#3a2614;color:#f5b78a;text-transform:uppercase;">Unresolved</span>`
      : (row.status
        ? `<span style="font-size:11px;padding:3px 8px;border-radius:999px;background:#1d2640;color:#a8b8ff;text-transform:uppercase;">${esc(row.status)}</span>`
        : '');
    const driftPill = row.frontmatter_drift
      ? `<span title="folder name and frontmatter status disagree" style="font-size:11px;padding:3px 8px;border-radius:999px;background:#3a2614;color:#f5b78a;text-transform:uppercase;">drift</span>`
      : '';
    const tabsHtml = ['summary', 'spec', 'activity'].map((t) => {
      const active = selectedTab === t;
      return `<button type="button" data-tab="${t}" style="background:${active ? '#1c3a2d' : 'transparent'};color:${active ? '#8ee4bf' : '#aab8b0'};border:1px solid ${active ? '#2dd4bf' : '#24342d'};border-radius:6px 6px 0 0;padding:5px 12px;margin-right:4px;font-family:inherit;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;cursor:pointer;">${t}</button>`;
    }).join('');

    const bodyHtml = _renderModalTabBody(selectedTab, row, fetchedDetail, fetchingDetail);

    const actionHtml = _renderModalActionArea(row, action, hostEntries, mode, provider);

    modal.innerHTML = `
      <div role="dialog" aria-label="${esc(title)}" style="background:#121a16;border:1px solid #2dd4bf;border-radius:12px;width:min(720px,100%);max-height:calc(100vh - 40px);display:flex;flex-direction:column;font-family:var(--font-mono,monospace);color:#d7e4dc;">
        <div style="padding:16px 18px;border-bottom:1px solid #24342d;display:flex;flex-direction:column;gap:8px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px;">
            <div style="font-size:16px;font-weight:700;color:#f2fbf5;flex:1;min-width:0;">${esc(title)}</div>
            <button type="button" data-role="modal-close" style="background:transparent;color:#8aa097;border:none;font-size:20px;cursor:pointer;line-height:1;">×</button>
          </div>
          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
            ${statusPill}${driftPill}
            <button type="button" data-role="copy-spec-id" title="Copy spec_id" style="font-size:11px;background:transparent;color:#8aa097;border:1px solid #24342d;border-radius:6px;padding:3px 8px;cursor:pointer;font-family:inherit;">${esc(row.spec_id)}</button>
          </div>
          ${tracking ? `<div style="font-size:11px;color:#8aa097;">${esc(tracking)}</div>` : ''}
        </div>
        <div style="border-bottom:1px solid #24342d;padding:6px 14px 0 14px;display:flex;gap:0;">${tabsHtml}</div>
        <div data-role="modal-body" style="flex:1;overflow:auto;padding:14px 18px;min-height:120px;">${bodyHtml}</div>
        <div data-role="modal-action" style="border-top:1px solid #24342d;padding:14px 18px;display:flex;flex-direction:column;gap:10px;">${actionHtml}</div>
      </div>`;

    _wireModal(modal, {
      onClose: () => closeModal(),
      onTab: (t) => { selectedTab = t; if (t === 'spec' && !fetchedDetail && !fetchingDetail) fetchSpecBody(); renderModalBody(); },
      onProvider: (p) => { provider = p; renderModalBody(); },
      onModeToggle: action.kind === 'spawn_additional' ? (m) => { mode = m; renderModalBody(); } : null,
      onSubmit: () => submitDrive(),
      onLeaderClick: (sid) => {
        if (typeof window.focusStreamId === 'function') {
          window.focusStreamId(sid);
          closeModal();
        }
      },
      onCopy: (btn) => {
        try {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(row.spec_id);
            btn.textContent = '✓ copied';
            setTimeout(() => { btn.textContent = row.spec_id; }, 900);
          }
        } catch (_) {}
      },
      row, action, hostEntries, currentMode: mode, currentProvider: provider,
    });
  };

  const closeModal = () => {
    if (modal.parentNode) modal.parentNode.removeChild(modal);
    if (refs.modalEl === modal) refs.modalEl = null;
  };

  const fetchSpecBody = async () => {
    fetchingDetail = true;
    renderModalBody();
    try {
      const reply = await window.cc.specsGet(row.spec_id);
      if (!reply || reply.ok === false) {
        fetchedDetail = { error: (reply && reply.error) || 'specs.get failed' };
      } else {
        fetchedDetail = reply;
      }
    } catch (e) {
      fetchedDetail = { error: (e && e.message) || String(e) };
    } finally {
      fetchingDetail = false;
      renderModalBody();
    }
  };

  const submitDrive = async () => {
    if (action.kind === 'unresolved') return;
    const hostSelect = modal.querySelector('[data-role="modal-host"]');
    const host = hostSelect && hostSelect.value;
    const scopeNote = (modal.querySelector('[data-role="modal-scope-note"]').value || '').trim();
    const errorEl = modal.querySelector('[data-role="modal-error"]');
    errorEl.style.display = 'none';
    if (!host) { errorEl.textContent = 'Pick a host first.'; errorEl.style.display = ''; return; }
    const entry = hostEntries.find((h) => h.host === host);
    if (entry && !entry.enabled) {
      errorEl.textContent = `${host} is disabled: ${entry.tooltip || entry.reason || 'capability missing'}. Pick a supported host.`;
      errorEl.style.display = '';
      return;
    }
    const options = { host, provider, mode };
    if (scopeNote) options.scope_note = scopeNote;
    try {
      const reply = await window.cc.specsDrive(row.spec_id, options);
      if (!reply || reply.ok === false) {
        errorEl.textContent = reply && reply.error_code === 'host_capability_unsupported'
          ? `Host ${reply.host || host} rejected: ${reply.reason || reply.error || 'capability missing'}`
          : `Drive failed: ${(reply && (reply.error || reply.error_code)) || 'unknown'}`;
        errorEl.style.display = '';
        return;
      }
      const streamId = reply.stream_id;
      closeModal();
      if (streamId && typeof window.focusStreamId === 'function') {
        const tryFocus = (attempt) => {
          if (window.focusStreamId(streamId)) return;
          if (attempt > 8) return;
          setTimeout(() => tryFocus(attempt + 1), 250);
        };
        tryFocus(0);
      }
    } catch (e) {
      errorEl.textContent = `Drive failed: ${(e && e.message) || String(e)}`;
      errorEl.style.display = '';
    }
  };

  renderModalBody();
  document.body.appendChild(modal);
  refs.modalEl = modal;

  modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
}

function _renderModalTabBody(tab, row, fetchedDetail, fetching) {
  if (tab === 'summary') {
    if (row.synthetic) {
      return `<div title="${esc(`unresolved_reason: ${row.unresolved_reason || 'zero_matches'}`)}" style="color:#f5b78a;font-size:13px;">unresolved_reason: ${esc(row.unresolved_reason || 'zero_matches')} — ${esc(unresolvedReasonTooltip(row.unresolved_reason))}</div>`;
    }
    const sections = [];
    if (row.goal_excerpt) sections.push(`<div style="margin-bottom:10px;"><div style="font-size:11px;text-transform:uppercase;color:#7d9488;margin-bottom:4px;">Goal</div><div style="font-size:13px;color:#e6eee9;white-space:pre-wrap;">${esc(row.goal_excerpt)}</div></div>`);
    if (row.next_action) sections.push(`<div style="margin-bottom:10px;"><div style="font-size:11px;text-transform:uppercase;color:#7d9488;margin-bottom:4px;">Next action</div><div style="font-size:13px;color:#e6eee9;white-space:pre-wrap;">${esc(row.next_action)}</div></div>`);
    if (row.blockers) sections.push(`<div style="margin-bottom:10px;"><div style="font-size:11px;text-transform:uppercase;color:#7d9488;margin-bottom:4px;">Blockers</div><div style="font-size:13px;color:#f5b78a;white-space:pre-wrap;">${esc(row.blockers)}</div></div>`);
    const progress = formatProgress(row.progress);
    sections.push(`<div style="margin-bottom:10px;"><div style="font-size:11px;text-transform:uppercase;color:#7d9488;margin-bottom:4px;">Phase</div><div style="font-size:13px;color:${progress.phaseDrift ? '#f5b78a' : '#e6eee9'};">${esc(progress.phase)} — ${esc(progress.label)}${progress.phaseDrift ? ' (drift)' : ''}</div></div>`);
    return sections.length > 0 ? sections.join('') : '<div style="color:#7f9187;font-size:13px;">No summary fields parsed.</div>';
  }
  if (tab === 'spec') {
    if (fetching && !fetchedDetail) return `<div style="color:#7f9187;font-size:13px;">Loading spec…</div>`;
    if (fetchedDetail && fetchedDetail.error) return `<div style="color:#f47067;font-size:13px;">specs.get error: ${esc(fetchedDetail.error)}</div>`;
    if (fetchedDetail && fetchedDetail.spec_md != null) {
      return `<pre style="margin:0;font-family:var(--font-mono,monospace);font-size:12px;line-height:1.5;color:#d7e4dc;white-space:pre-wrap;">${esc(fetchedDetail.spec_md)}</pre>`;
    }
    return `<div style="color:#7f9187;font-size:13px;">Loading…</div>`;
  }
  if (tab === 'activity') {
    const leaders = Array.isArray(row.live_leaders) ? row.live_leaders : [];
    if (leaders.length === 0) return `<div style="color:#7f9187;font-size:13px;">No live leaders driving this spec right now.</div>`;
    return `<table style="width:100%;border-collapse:collapse;font-size:12px;color:#d7e4dc;">
      <thead><tr style="text-align:left;color:#7d9488;text-transform:uppercase;font-size:10px;letter-spacing:0.5px;">
        <th style="padding:6px 4px;">Stream</th><th style="padding:6px 4px;">Provider@Host</th><th style="padding:6px 4px;">Last event</th><th style="padding:6px 4px;">Handoff from</th>
      </tr></thead>
      <tbody>${leaders.map((l) => `<tr style="border-top:1px solid #1c2620;"><td style="padding:6px 4px;font-family:var(--font-mono,monospace);">${esc(l.stream_id || '')}</td><td style="padding:6px 4px;">${esc((l.provider || '?') + '@' + (l.host || '?'))}</td><td style="padding:6px 4px;">${esc(l.last_event_at || '')}</td><td style="padding:6px 4px;font-family:var(--font-mono,monospace);color:#8aa097;">${esc(l.handoff_from_stream_id || '')}</td></tr>`).join('')}</tbody>
    </table>`;
  }
  return '';
}

function _renderModalActionArea(row, action, hostEntries, mode, provider) {
  if (action.kind === 'unresolved') {
    const reason = action.unresolvedReason || 'zero_matches';
    return `<div style="font-size:11px;color:#f5b78a;">unresolved_reason: ${esc(reason)} — ${esc(unresolvedReasonTooltip(action.unresolvedReason))}</div>`;
  }

  const leaderChipsHtml = (action.leaderChips || []).length > 0
    ? `<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:#7d9488;">Live leaders:</div>
        ${action.leaderChips.map((c) => `<button type="button" data-stream-id="${esc(c.streamId)}" title="Switch to chat ${esc(c.streamId)}" style="background:#173126;color:#8ee4bf;border:1px solid #2dd4bf;border-radius:999px;padding:3px 10px;font-size:11px;cursor:pointer;font-family:inherit;">${esc(c.label)}</button>`).join('')}
      </div>`
    : '';

  const hostOptions = hostEntries.map((h) => `<option value="${esc(h.host)}"${h.defaultSelected ? ' selected' : ''}${h.enabled ? '' : ' disabled'} title="${esc(h.tooltip || '')}">${esc(h.host)}${h.enabled ? '' : ' (disabled: ' + esc(h.reason || 'unsupported') + ')'}</option>`).join('');

  const codexActive = provider === 'codex';
  const claudeActive = provider === 'claude';
  const submitLabel = mode === 'drive_to_completion' ? 'Drive this spec' : 'Spawn additional leader';

  return `${leaderChipsHtml}
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
      <label style="display:flex;flex-direction:column;gap:4px;font-size:11px;color:#7d9488;text-transform:uppercase;letter-spacing:0.5px;">
        Host
        <select data-role="modal-host" style="background:#0d1310;color:#e6eee9;border:1px solid #2a3b33;padding:6px 8px;border-radius:6px;font-family:inherit;font-size:13px;text-transform:none;letter-spacing:0;">${hostOptions}</select>
      </label>
      <div style="display:flex;flex-direction:column;gap:4px;font-size:11px;color:#7d9488;text-transform:uppercase;letter-spacing:0.5px;">
        Provider
        <div style="display:flex;gap:6px;">
          <button type="button" data-provider="codex" style="background:${codexActive ? '#173126' : 'transparent'};color:${codexActive ? '#8ee4bf' : '#aab8b0'};border:1px solid ${codexActive ? '#2dd4bf' : '#24342d'};border-radius:6px;padding:5px 12px;font-family:inherit;font-size:12px;cursor:pointer;">codex</button>
          <button type="button" data-provider="claude" style="background:${claudeActive ? '#173126' : 'transparent'};color:${claudeActive ? '#8ee4bf' : '#aab8b0'};border:1px solid ${claudeActive ? '#2dd4bf' : '#24342d'};border-radius:6px;padding:5px 12px;font-family:inherit;font-size:12px;cursor:pointer;">claude</button>
        </div>
      </div>
    </div>
    <label style="display:flex;flex-direction:column;gap:4px;font-size:11px;color:#7d9488;text-transform:uppercase;letter-spacing:0.5px;">
      Scope note (optional)
      <textarea data-role="modal-scope-note" rows="2" placeholder="e.g. only stage 3" style="background:#0d1310;color:#e6eee9;border:1px solid #2a3b33;padding:6px 8px;border-radius:6px;font-family:inherit;font-size:13px;text-transform:none;letter-spacing:0;resize:vertical;"></textarea>
    </label>
    <div data-role="modal-error" style="display:none;color:#f47067;font-size:12px;"></div>
    <div style="display:flex;justify-content:flex-end;gap:8px;">
      <button type="button" data-role="modal-cancel" style="background:transparent;color:#aab8b0;border:1px solid #24342d;border-radius:6px;padding:6px 14px;font-family:inherit;font-size:13px;cursor:pointer;">Cancel</button>
      <button type="button" data-role="modal-submit" style="background:#2dd4bf;color:#0d1310;border:none;border-radius:6px;padding:6px 14px;font-family:inherit;font-size:13px;font-weight:600;cursor:pointer;">${esc(submitLabel)}</button>
    </div>`;
}

function _wireModal(modal, hooks) {
  modal.querySelectorAll('[data-role="modal-close"], [data-role="modal-cancel"]').forEach((b) => b.addEventListener('click', hooks.onClose));
  modal.querySelectorAll('button[data-tab]').forEach((b) => b.addEventListener('click', () => hooks.onTab(b.dataset.tab)));
  modal.querySelectorAll('button[data-provider]').forEach((b) => b.addEventListener('click', () => hooks.onProvider(b.dataset.provider)));
  modal.querySelectorAll('button[data-stream-id]').forEach((b) => b.addEventListener('click', () => hooks.onLeaderClick(b.dataset.streamId)));
  const submit = modal.querySelector('[data-role="modal-submit"]');
  if (submit) submit.addEventListener('click', hooks.onSubmit);
  const copy = modal.querySelector('[data-role="copy-spec-id"]');
  if (copy) copy.addEventListener('click', () => hooks.onCopy(copy));
}

// ── Register ──────────────────────────────────────────────────

window.DASHBOARDS.push({
  id: 'specs',
  name: 'Specs',
  description: 'Read-only triage across work/<status>/; drive or spawn leaders.',
  color: '#f5b78a',
  mount, update, unmount, pollFn,
  pollInterval: POLL_INTERVAL_MS,
  idlePollInterval: IDLE_POLL_INTERVAL_MS,
  idleFn: (data) => !data || !Array.isArray(data.specs) || data.specs.length === 0,
});

window.setSpecsShowAll = function setSpecsShowAll(show) {
  __specsShowAll = !!show;
  if (typeof window.refreshDashboardNow === 'function') window.refreshDashboardNow();
};

})();
