/* global Terminal, FitAddon */

// Load xterm.js and fit addon via require (Electron renderer with nodeIntegration off — use dynamic import)
const { Terminal } = require('@xterm/xterm');
const { FitAddon } = require('@xterm/addon-fit');
const { Unicode11Addon } = require('@xterm/addon-unicode11'); // REQUIRED: without this, ❯ and other unicode renders as __
const { WebglAddon } = require('@xterm/addon-webgl'); // GPU-accelerated rendering — fixes partial text paint on screen refresh
const CONFIG = require('../pentacle.config.js');

// API URL is mutable — on client machines it's rewritten to the SSH-tunneled
// port after getConfig() resolves. On the host it's the same local URL.
let API = CONFIG.apiServer.url;
let IS_CLIENT = false;
const THEME = CONFIG.terminal;

// Bootstrap async config from main so we can reroute the API URL before the
// first fetch fires. All top-level code runs under DOMContentLoaded, but
// network-touching code is kicked off from event handlers / timers — those
// fire after this promise resolves.
const CFG_READY = (async () => {
  try {
    const cfg = await window.cc.getConfig();
    if (cfg && cfg.apiUrl) API = cfg.apiUrl;
    IS_CLIENT = !!(cfg && cfg.isClient);
    // Clients default to creating remote sessions (the mac-mini). Users can
    // still toggle to local for WSL/macbook-local sessions.
    if (IS_CLIENT) newSessionLocation = 'remote';
    return cfg;
  } catch { return null; }
})();

// ── State ──────────────────────────────────────────────────────

const state = {
  sessions: [],
  trashed: [],
  bots: [],
  activeTab: 'sessions', // 'sessions' or 'bots'
  slots: [null, null, null, null], // { name, displayName } or { bot, displayName } or null
  terminals: [null, null, null, null], // { term, fitAddon } or null
  wheelThrottles: [0, 0, 0, 0], // last scroll timestamp per slot for throttling
  botSlots: [false, false, false, false], // true if slot has a bot detail panel (not a terminal)
  slotGen: [0, 0, 0, 0], // generation counter per slot — incremented on each attach to detect stale PTY exits
  showTrash: false,
  maximizedSlot: null, // null = grid view, 0-3 = maximized slot
  // Activity detection
  activityStates: {}, // sessionName:windowIndex -> 'working' | 'waiting' | 'idle'
  sessionSummaries: {}, // sessionName:windowIndex -> one-line summary string
  autoNames: {}, // sessionName -> auto-detected label (e.g. "pentacle refactor")
  sessionHosts: {}, // sessionName -> hostId (e.g. 'local', 'remote')
  // Dashboard state — all mutable dashboard state lives here
  currentView: 'chats',            // 'chats' | 'dashboards'
  selectedDashboard: null,          // dashboard id string
  dashboardPollToken: 0,            // generation counter
  dashboardPollTimer: null,         // setInterval id
  dashboardRefs: null,              // cached DOM refs from mount()
  dashboardLastData: null,          // last successful poll data
  dashboardLastUpdated: null,       // Date of last successful poll
  dashboardState: 'loading',        // 'loading' | 'loaded' | 'stale' | 'error'
  dashboardError: null,             // error message string
};

// ── API ────────────────────────────────────────────────────────

async function api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  try {
    const r = await fetch(API + path, opts);
    return await r.json();
  } catch (e) {
    console.error('API error:', e);
    return null;
  }
}

// ── Session Fetching ───────────────────────────────────────────

async function fetchSessions() {
  // The Python API only covers one host (remote in client mode, local in host
  // mode). On clients the other host (WSL local) is enumerated via tmux so
  // its sessions show up alongside the remote ones.
  const apiHostId = IS_CLIENT ? 'remote' : 'local';
  const [data, byHost] = await Promise.all([
    api('GET', '/api/sessions'),
    window.cc.listSessionsByHost ? window.cc.listSessionsByHost().catch(() => ({})) : Promise.resolve({}),
  ]);

  const apiActive = (data && data.active) || [];
  const seen = new Set();
  for (const s of apiActive) {
    s.hostId = apiHostId;
    state.sessionHosts[s.name] = apiHostId;
    seen.add(s.name);
  }

  const extras = [];
  for (const [hostId, sessions] of Object.entries(byHost || {})) {
    if (hostId === apiHostId) continue;
    for (const s of sessions) {
      if (seen.has(s.name)) continue; // API metadata wins on collision
      state.sessionHosts[s.name] = hostId;
      extras.push({
        name: s.name,
        display_name: s.name,
        title: '',
        preview: '',
        attached: !!s.attached,
        type: 'claude',
        hostId,
      });
    }
  }

  state.sessions = [...apiActive, ...extras];
  state.trashed = (data && data.trashed) || [];
  renderSidebar();
}

// ── Sidebar Rendering ──────────────────────────────────────────

function getSlotForSession(name) {
  for (let i = 0; i < 4; i++) {
    if (state.slots[i] && state.slots[i].name === name) return i;
  }
  return -1;
}

// ── Activity Detection & Summaries ────────────────────────────

function getActivityForSession(sessionName) {
  // Match sessionName to activityStates keys (format: "sessionName:0")
  for (const [key, val] of Object.entries(state.activityStates)) {
    if (key.startsWith(sessionName + ':')) return val;
  }
  return 'idle';
}

function getSourceForSession(sessionName) {
  const hostId = state.sessionHosts[sessionName];
  if (!hostId) return null;
  const names = CONFIG.hostNames || {};
  return names[hostId] || hostId;
}

function getSourceColorForSession(sessionName) {
  const hostId = state.sessionHosts[sessionName];
  const colors = CONFIG.hostColors || {};
  return colors[hostId] || 'green';
}

function extractSummary(paneContent) {
  if (!paneContent) return '';
  const lines = paneContent.split('\n').map(l => l.trim()).filter(l => l);
  // Look for Claude's status line patterns
  for (let i = lines.length - 1; i >= Math.max(0, lines.length - 30); i--) {
    const line = lines[i];
    // Match tool use lines like "Read file.js", "Edit main.py", "Bash npm test"
    if (/^(Read|Edit|Write|Bash|Grep|Glob|Agent|TodoWrite)\b/.test(line)) {
      return line.slice(0, 80);
    }
    // Match "Working on..." or task descriptions
    if (/^(Working|Building|Testing|Fixing|Adding|Updating|Creating|Running|Searching|Installing)\b/i.test(line)) {
      return line.slice(0, 80);
    }
  }
  // Fallback: last non-empty line that isn't a prompt
  for (let i = lines.length - 1; i >= Math.max(0, lines.length - 10); i--) {
    const line = lines[i];
    if (line && !line.startsWith('❯') && !line.includes('bypass') && !line.includes('auto-accept') && line.length > 3) {
      return line.slice(0, 80);
    }
  }
  return '';
}

function extractAutoName(paneContent) {
  if (!paneContent) return '';
  const lines = paneContent.split('\n');
  // Look for working directory hints (common in Claude Code output)
  for (let i = lines.length - 1; i >= Math.max(0, lines.length - 50); i--) {
    const line = lines[i].trim();
    // Match paths like "/Users/bartimaeus/pentacle" → extract "pentacle"
    const pathMatch = line.match(/\/Users\/\w+\/([a-zA-Z0-9_-]+)/);
    if (pathMatch) {
      return pathMatch[1];
    }
    // Match "cwd: /path/to/dir" patterns
    const cwdMatch = line.match(/(?:cwd|directory|repo|project)[:\s]+.*?([a-zA-Z0-9_-]+)\s*$/i);
    if (cwdMatch) {
      return cwdMatch[1];
    }
  }
  return '';
}

async function pollActivity() {
  try {
    const [actStates, panes] = await Promise.all([
      window.cc.detectActivity(),
      window.cc.captureAllPanes(),
    ]);
    // Main now returns keys as `hostId:sessionName:windowIdx`. Strip the host
    // prefix so the existing `sessionName:windowIdx`-keyed code keeps working.
    // (Collisions across hosts with identical session names are tolerated —
    // the user runs distinct workloads per host.)
    const stripHost = (obj) => {
      if (!obj) return obj;
      const out = {};
      for (const [k, v] of Object.entries(obj)) {
        const i = k.indexOf(':');
        if (i >= 0) {
          const hostId = k.slice(0, i);
          const rest = k.slice(i + 1);
          const sessionName = rest.split(':')[0];
          state.sessionHosts[sessionName] = hostId;
          out[rest] = v;
        } else {
          out[k] = v;
        }
      }
      return out;
    };
    if (actStates) state.activityStates = stripHost(actStates);
    if (panes) {
      const strippedPanes = stripHost(panes);
      for (const [key, content] of Object.entries(strippedPanes)) {
        state.sessionSummaries[key] = extractSummary(content);
        // Extract auto-name from pane content
        const sessionName = key.split(':')[0];
        if (!state.autoNames[sessionName]) {
          const name = extractAutoName(content);
          if (name) state.autoNames[sessionName] = name;
        }
      }
    }
    // Update activity strips on slot headers
    updateActivityStrips();
    // Re-render sidebar with activity data
    renderSidebar();
  } catch (e) {
    // Activity polling is best-effort
  }
}

function updateActivityStrips() {
  for (let i = 0; i < 4; i++) {
    const strip = document.getElementById(`activity-strip-${i}`);
    if (!strip) continue;
    strip.className = 'cell-activity-strip';
    if (state.slots[i] && !state.botSlots[i]) {
      const sessionName = state.slots[i].name;
      const activity = getActivityForSession(sessionName);
      if (activity === 'working' || activity === 'waiting') {
        strip.classList.add(activity);
      }
      // Update header label with auto-name if session still has its raw name
      const autoName = state.autoNames[sessionName];
      if (autoName && state.slots[i].displayName === sessionName) {
        state.slots[i].displayName = autoName;
        const label = document.querySelector(`#header-${i} .cell-label`);
        if (label) label.textContent = autoName;
      }
      // Refresh source tag on slot header (may appear after activity poll)
      if (CONFIG.features.sourceTags) {
        const header = document.getElementById(`header-${i}`);
        if (header && !header.querySelector('.cell-source-tag')) {
          const source = getSourceForSession(sessionName);
          if (source) {
            const tag = document.createElement('span');
            tag.className = `cell-source-tag color-${getSourceColorForSession(sessionName)}`;
            tag.textContent = source;
            const label = header.querySelector('.cell-label');
            if (label) label.after(tag);
          }
        }
      }
    }
  }
}

function renderSidebar() {
  const list = document.getElementById('session-list');
  const stats = document.getElementById('stats');

  const active = state.sessions;
  const working = active.filter(s => getActivityForSession(s.name) === 'working');
  const waiting = active.filter(s => getActivityForSession(s.name) === 'waiting');
  const idle = active.filter(s => getActivityForSession(s.name) === 'idle');
  stats.textContent = `${active.length} sessions | ${working.length} working | ${waiting.length} waiting`;

  function renderSessionItem(s) {
    const slotIdx = getSlotForSession(s.name);
    const isActive = slotIdx >= 0;
    const activity = getActivityForSession(s.name);
    const dotClass = activity === 'working' ? 'attached' : activity === 'waiting' ? 'chat' : (s.attached ? 'attached' : s.type);
    const hasTitle = s.title && s.title !== s.name;
    const autoName = state.autoNames[s.name];
    const displayName = s.display_name || (autoName ? autoName : s.name);
    // Get summary for this session
    const summaryKey = Object.keys(state.sessionSummaries).find(k => k.startsWith(s.name + ':'));
    const summary = summaryKey ? state.sessionSummaries[summaryKey] : '';

    const activityBadge = activity === 'working'
      ? `<span class="activity-indicator working" title="Working"><span class="activity-spinner"></span></span>`
      : activity === 'waiting'
        ? `<span class="activity-indicator waiting" title="Waiting"><svg width="10" height="10" viewBox="0 0 16 16" fill="currentColor"><path d="M8 3.5a.5.5 0 0 0-1 0V8a.5.5 0 0 0 .252.434l3.5 2a.5.5 0 0 0 .496-.868L8 7.71V3.5z"/><path d="M8 16A8 8 0 1 0 8 0a8 8 0 0 0 0 16zm7-8A7 7 0 1 1 1 8a7 7 0 0 1 14 0z"/></svg></span>`
        : '';
    const source = CONFIG.features.sourceTags ? getSourceForSession(s.name) : null;
    const sourceColor = source ? getSourceColorForSession(s.name) : 'green';
    const sourceTag = source ? `<span class="s-source-tag color-${sourceColor}">${esc(source)}</span>` : '';

    return `<div class="session-item ${isActive ? 'active' : ''}"
                 data-name="${esc(s.name)}"
                 data-display="${esc(displayName)}">
      <div class="s-top">
        <span class="s-dot ${dotClass}"></span>
        <span class="s-name">${esc(displayName)}</span>
        ${activityBadge}
        ${sourceTag}
        ${isActive ? `<span class="s-slot-badge">${slotIdx + 1}</span>` : ''}
        <button class="s-edit-btn" data-edit-name="${esc(s.name)}" data-edit-display="${esc(displayName)}" title="Rename"><svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M12.146.146a.5.5 0 0 1 .708 0l3 3a.5.5 0 0 1 0 .708l-10 10a.5.5 0 0 1-.168.11l-5 2a.5.5 0 0 1-.65-.65l2-5a.5.5 0 0 1 .11-.168l10-10zM11.207 2.5L13.5 4.793 14.793 3.5 12.5 1.207 11.207 2.5zm1.586 3L10.5 3.207 4 9.707V10h.5a.5.5 0 0 1 .5.5v.5h.5a.5.5 0 0 1 .5.5v.5h.293l6.5-6.5zm-9.761 5.175l-.106.106-1.528 3.821 3.821-1.528.106-.106A.5.5 0 0 1 5 12.5V12h-.5a.5.5 0 0 1-.5-.5V11h-.5a.5.5 0 0 1-.468-.325z"/></svg></button>
        <button class="s-trash-btn" data-trash-name="${esc(s.name)}" title="Move to trash"><svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M5.5 5.5A.5.5 0 0 1 6 6v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5zm2.5 0a.5.5 0 0 1 .5.5v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5zm3 .5a.5.5 0 0 0-1 0v6a.5.5 0 0 0 1 0V6z"/><path fill-rule="evenodd" d="M14.5 3a1 1 0 0 1-1 1H13v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V4h-.5a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1H6a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1h3.5a1 1 0 0 1 1 1v1zM4.118 4L4 4.059V13a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V4.059L11.882 4H4.118zM2.5 3V2h11v1h-11z"/></svg></button>
      </div>
      ${hasTitle ? `<div class="s-meta">${esc(s.name)}</div>` : ''}
      ${autoName && !hasTitle ? `<div class="s-meta">${esc(s.name)}</div>` : ''}
      ${summary ? `<div class="s-summary">${esc(summary)}</div>` : (s.preview ? `<div class="s-preview">${esc(s.preview)}</div>` : '')}
    </div>`;
  }

  let html = '';
  if (working.length > 0) {
    html += `<div class="sidebar-group-label working">Working (${working.length})</div>`;
    html += working.map(renderSessionItem).join('');
  }
  if (waiting.length > 0) {
    html += `<div class="sidebar-group-label waiting">Waiting (${waiting.length})</div>`;
    html += waiting.map(renderSessionItem).join('');
  }
  if (idle.length > 0) {
    html += `<div class="sidebar-group-label idle">Idle (${idle.length})</div>`;
    html += idle.map(renderSessionItem).join('');
  }
  // If no activity data yet, render flat list
  if (working.length === 0 && waiting.length === 0 && idle.length === 0 && active.length > 0) {
    html += active.map(renderSessionItem).join('');
  }
  list.innerHTML = html;

  // Click handlers
  const defaultHostId = IS_CLIENT ? 'remote' : 'local';
  list.querySelectorAll('.session-item').forEach(el => {
    el.addEventListener('click', () => {
      const name = el.dataset.name;
      const display = el.dataset.display;
      // Use the session's tracked host (set by fetchSessions / activity poll)
      // so sessions from non-API hosts (e.g. WSL local in client mode) attach
      // to the correct tmux server.
      const hostId = state.sessionHosts[name] || defaultHostId;
      assignToSlot(name, display, hostId);
    });
    el.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      const name = el.dataset.name;
      const hostId = state.sessionHosts[name] || defaultHostId;
      window.cc.showContextMenu(name, el.dataset.display, hostId);
    });
  });

  // Edit button handlers
  list.querySelectorAll('.s-edit-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      showRenameModal(btn.dataset.editName, btn.dataset.editDisplay);
    });
  });

  // Trash button handlers
  list.querySelectorAll('.s-trash-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      trashSession(btn.dataset.trashName);
    });
  });

  // Trash section
  renderTrash();
}

function renderTrash() {
  const section = document.getElementById('trash-section');
  const trashed = state.trashed;

  if (trashed.length === 0) {
    section.innerHTML = '';
    return;
  }

  let html = `<div class="trash-header" id="trash-toggle">
    <span>${state.showTrash ? '\u25BC' : '\u25B6'}</span>
    <span>Deleted</span>
    <span class="trash-count">${trashed.length}</span>
  </div>`;

  if (state.showTrash) {
    html += '<div class="trash-list">';
    html += trashed.map(s => `
      <div class="trash-item">
        <div class="s-name">${esc(s.display_name)}</div>
        <div class="s-actions">
          <button class="sb-btn" style="font-size:10px;padding:2px 6px"
                  onclick="restoreSession('${esc(s.agent_id)}')">Restore</button>
          <button class="sb-btn sb-btn-red" style="font-size:10px;padding:2px 6px"
                  onclick="killSession('${esc(s.name)}', '${esc(s.agent_id)}')">Delete</button>
        </div>
      </div>
    `).join('');
    html += '</div>';
  }

  section.innerHTML = html;

  document.getElementById('trash-toggle')?.addEventListener('click', () => {
    state.showTrash = !state.showTrash;
    renderTrash();
  });
}

function esc(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ── Bots ───────────────────────────────────────────────────────

async function fetchBots() {
  const data = await api('GET', '/api/bots');
  if (data && Array.isArray(data)) {
    state.bots = data;
    renderBotList();
  }
}

function getSlotForBot(botId) {
  for (let i = 0; i < 4; i++) {
    if (state.botSlots[i] && state.slots[i] && state.slots[i].bot && state.slots[i].bot.bot_id === botId) return i;
  }
  return -1;
}

function formatTimeAgo(isoString) {
  if (!isoString) return '';
  const diff = Date.now() - new Date(isoString).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function formatUptime(startedAt) {
  if (!startedAt) return '';
  const diff = Date.now() - new Date(startedAt).getTime();
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  const remainMins = mins % 60;
  if (hours < 24) return `${hours}h ${remainMins}m`;
  const days = Math.floor(hours / 24);
  const remainHours = hours % 24;
  return `${days}d ${remainHours}h`;
}

function renderBotList() {
  const list = document.getElementById('bot-list');
  const stats = document.getElementById('bots-stats');
  if (!list || !stats) return;

  const bots = state.bots;
  const running = bots.filter(b => b.status === 'running').length;
  stats.textContent = `${bots.length} bots | ${running} running`;

  // Update tab label with count
  const botsTab = document.getElementById('tab-bots');
  if (botsTab) botsTab.textContent = `Bots${running > 0 ? ` (${running})` : ''}`;

  list.innerHTML = bots.map(b => {
    const slotIdx = getSlotForBot(b.bot_id);
    const isActive = slotIdx >= 0;
    const title = b.title || b.bot_name || b.bot_id;

    return `<div class="bot-item ${isActive ? 'active' : ''}" data-bot-id="${esc(b.bot_id)}">
      <div class="b-top">
        <span class="b-dot ${esc(b.status)}"></span>
        <span class="b-name">${esc(title)}</span>
        ${isActive ? `<span class="b-slot-badge">${slotIdx + 1}</span>` : ''}
        <span class="b-type">${esc(b.bot_type)}</span>
      </div>
      ${b.current_task ? `<div class="b-task">${esc(b.current_task)}</div>` : ''}
      <div class="b-meta">${formatTimeAgo(b.last_heartbeat)}</div>
    </div>`;
  }).join('');

  // Click handlers
  list.querySelectorAll('.bot-item').forEach(el => {
    el.addEventListener('click', () => {
      const botId = el.dataset.botId;
      const bot = state.bots.find(b => b.bot_id === botId);
      if (bot) openBotInSlot(bot);
    });
  });
}

function openBotInSlot(bot) {
  // Already in a slot? Focus it
  const existing = getSlotForBot(bot.bot_id);
  if (existing >= 0) {
    if (state.maximizedSlot !== null && state.maximizedSlot !== existing) {
      maximizeSlot(existing);
    }
    return;
  }

  if (state.maximizedSlot !== null) {
    const slot = state.maximizedSlot;
    attachBotToSlot(slot, bot);
    maximizeSlot(slot);
    return;
  }

  // Find first empty slot
  let slot = state.slots.findIndex(s => s === null);
  if (slot === -1) slot = 3;

  attachBotToSlot(slot, bot);
}

function attachBotToSlot(slot, bot) {
  // Kill existing terminal/bot in this slot
  detachSlot(slot);

  state.slotGen[slot]++;
  const title = bot.title || bot.bot_name || bot.bot_id;
  state.slots[slot] = { bot, displayName: title };
  state.botSlots[slot] = true;

  // Update header
  const header = document.getElementById(`header-${slot}`);
  const label = header.querySelector('.cell-label');
  label.textContent = title;
  label.classList.add('has-session');
  label.style.color = 'var(--cyan)';
  document.getElementById(`cell-${slot}`).classList.add('occupied');

  // Render bot detail into the terminal area
  const container = document.getElementById(`term-${slot}`);
  container.innerHTML = renderBotDetail(bot);

  // Wire up refresh for this bot panel
  const refreshBtn = container.querySelector('.bot-detail-refresh');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', async () => {
      await fetchBots();
      const updated = state.bots.find(b => b.bot_id === bot.bot_id);
      if (updated && state.botSlots[slot]) {
        state.slots[slot] = { bot: updated, displayName: updated.title || updated.bot_name || updated.bot_id };
        container.innerHTML = renderBotDetail(updated);
        // Re-wire refresh
        attachBotToSlot.__rewire?.(slot, updated);
      }
    });
  }

  renderBotList();
  renderSidebar();
}

function renderBotDetail(bot) {
  const title = bot.title || bot.bot_name || bot.bot_id;
  const metrics = bot.metrics || {};
  const scalarEntries = Object.entries(metrics).filter(([, v]) => !Array.isArray(v));
  const listEntries = Object.entries(metrics).filter(([, v]) => Array.isArray(v));

  let html = `<div class="bot-detail">`;

  // Status row
  html += `<div class="bot-detail-status">
    <span class="bot-status-badge ${esc(bot.status)}">${esc(bot.status)}</span>
    <span class="bot-type-badge">${esc(bot.bot_type)}</span>
    ${bot.started_at ? `<span class="bot-uptime">${formatUptime(bot.started_at)} uptime</span>` : ''}
  </div>`;

  // Current task
  if (bot.current_task) {
    html += `<div class="bot-detail-section">
      <div class="bot-detail-section-title">Current Task</div>
      <div class="bot-detail-field">${esc(bot.current_task)}</div>
    </div>`;
  }

  // Metrics
  if (scalarEntries.length > 0 || listEntries.length > 0) {
    html += `<div class="bot-detail-section">
      <div class="bot-detail-section-title">Metrics</div>`;
    for (const [key, val] of scalarEntries) {
      html += `<div class="bot-metric-card">
        <span class="bot-metric-key">${esc(key.replace(/_/g, ' '))}</span>
        <span class="bot-metric-val">${esc(String(val))}</span>
      </div>`;
    }
    for (const [key, items] of listEntries) {
      html += `<div style="margin-top:6px"><span class="bot-metric-key">${esc(key.replace(/_/g, ' '))}</span></div>`;
      for (const item of items) {
        const isDone = item.startsWith('[done]');
        const isActive = item.startsWith('>>');
        const isFailed = String(item).includes('FAILED');
        const text = item.replace(/^\[done\]\s*|\[pending\]\s*|^>>\s*/g, '');
        const iconClass = isDone ? 'done' : isActive ? 'active' : isFailed ? 'failed' : 'pending';
        const icon = isDone ? '\u2713' : isActive ? '\u25B6' : isFailed ? '\u2717' : '\u25CB';
        const textClass = isDone ? 'done' : isActive ? 'active' : isFailed ? 'failed' : '';
        html += `<div class="bot-task-item">
          <span class="bot-task-icon ${iconClass}">${icon}</span>
          <span class="bot-task-text ${textClass}">${esc(text)}</span>
        </div>`;
      }
    }
    html += `</div>`;
  }

  // Details
  html += `<div class="bot-detail-section">
    <div class="bot-detail-section-title">Details</div>
    <div class="bot-detail-grid">`;
  if (bot.started_at) {
    html += `<span class="bot-detail-label">Started</span><span class="bot-detail-value">${new Date(bot.started_at).toLocaleString()}</span>`;
  }
  if (bot.last_heartbeat) {
    html += `<span class="bot-detail-label">Heartbeat</span><span class="bot-detail-value">${new Date(bot.last_heartbeat).toLocaleString()}</span>`;
  }
  if (bot.host) {
    html += `<span class="bot-detail-label">Host</span><span class="bot-detail-value">${esc(bot.host)}</span>`;
  }
  if (bot.pid) {
    html += `<span class="bot-detail-label">PID</span><span class="bot-detail-value">${bot.pid}</span>`;
  }
  html += `</div></div>`;

  // Refresh button
  html += `<button class="bot-restart-btn bot-detail-refresh">Refresh</button>`;

  html += `</div>`;
  return html;
}

// ── Slot Assignment ────────────────────────────────────────────

function assignToSlot(sessionName, displayName, hostId) {
  // Already in a slot? Focus it (and maximize if in maximized mode)
  const existing = getSlotForSession(sessionName);
  if (existing >= 0) {
    if (state.maximizedSlot !== null && state.maximizedSlot !== existing) {
      // In maximized mode, switch the maximized view to this slot
      maximizeSlot(existing);
    }
    focusTerminal(existing);
    return;
  }

  if (state.maximizedSlot !== null) {
    // In maximized mode — replace the currently maximized slot
    const slot = state.maximizedSlot;
    attachSession(slot, sessionName, displayName, hostId);
    maximizeSlot(slot);
    return;
  }

  // Find first empty slot
  let slot = state.slots.findIndex(s => s === null);
  if (slot === -1) {
    // All full — replace the last slot (slot 3)
    slot = 3;
  }

  attachSession(slot, sessionName, displayName, hostId);
}

async function attachSession(slot, sessionName, displayName, hostId) {
  hostId = hostId || 'local';
  // Kill existing terminal in this slot
  detachSlot(slot);

  state.slotGen[slot]++;
  const gen = state.slotGen[slot]; // capture generation to detect stale async resumes
  state.slots[slot] = { name: sessionName, displayName, hostId };

  // Update header
  const header = document.getElementById(`header-${slot}`);
  const label = header.querySelector('.cell-label');
  label.textContent = displayName;
  label.classList.add('has-session');
  document.getElementById(`cell-${slot}`).classList.add('occupied');

  // Source tag on slot header
  header.querySelector('.cell-source-tag')?.remove();
  if (CONFIG.features.sourceTags) {
    const source = getSourceForSession(sessionName);
    if (source) {
      const tag = document.createElement('span');
      tag.className = `cell-source-tag color-${getSourceColorForSession(sessionName)}`;
      tag.textContent = source;
      label.after(tag);
    }
  }

  // Create terminal
  const container = document.getElementById(`term-${slot}`);
  container.innerHTML = '';

  const term = new Terminal({
    theme: THEME,
    fontFamily: "'SFMono-Regular', 'SF Mono', '.SF NS Mono', 'Menlo', 'Monaco', monospace",
    fontSize: 13,
    cursorBlink: true,
    allowProposedApi: true,
    scrollback: 0,
  });

  const fitAddon = new FitAddon();
  const unicode11Addon = new Unicode11Addon();
  term.loadAddon(fitAddon);
  term.loadAddon(unicode11Addon);
  term.unicode.activeVersion = '11';
  term.open(container);

  // Enable WebGL renderer for GPU-accelerated full-frame draws.
  // The DOM renderer creates individual spans per cell — browser paint cycles
  // can leave cells partially rendered until a repaint is forced (e.g. by
  // selecting text). WebGL does full-frame GPU draws, eliminating this.
  try {
    const webglAddon = new WebglAddon();
    webglAddon.onContextLoss(() => {
      // Fall back to DOM renderer if GPU context is lost
      webglAddon.dispose();
    });
    term.loadAddon(webglAddon);
  } catch (e) {
    console.warn('[App] WebGL renderer unavailable, using DOM fallback:', e.message);
  }

  // Fit terminal BEFORE spawning PTY so tmux renders at the correct size
  // from the very first frame. Without this, the PTY spawns at 80x24,
  // tmux redraws at the wrong size (garbled flash), then fit fires and
  // tmux redraws again correctly — causing the "unicode flash" on old chats.
  await new Promise(resolve => requestAnimationFrame(resolve));
  // Bail if another attachSession took over this slot while we awaited
  if (state.slotGen[slot] !== gen) { try { term.dispose(); } catch {} return; }
  fitAddon.fit();

  // Keyboard enhancements for terminal input
  term.attachCustomKeyEventHandler((e) => {
    if (e.type !== 'keydown') return true;

    // Copy: Cmd+C on mac (intercept always — mac users don't rely on Ctrl
    // here). On Win/Linux, Ctrl+Shift+C always copies; plain Ctrl+C copies
    // only when there's a selection, otherwise falls through to SIGINT
    // (standard Windows Terminal / VSCode behavior).
    const isMac = navigator.platform.toLowerCase().includes('mac');
    if (e.metaKey && e.key === 'c') {
      const sel = term.getSelection();
      if (sel) { navigator.clipboard.writeText(sel); return false; }
    }
    if (!isMac && e.ctrlKey && !e.metaKey && (e.key === 'c' || e.key === 'C')) {
      const sel = term.getSelection();
      if (e.shiftKey) {
        if (sel) navigator.clipboard.writeText(sel);
        return false;  // Ctrl+Shift+C always swallowed
      }
      if (sel) { navigator.clipboard.writeText(sel); return false; }
      // no selection + no shift → fall through so Ctrl+C becomes SIGINT
    }

    // Paste: Cmd+V on mac. Ctrl+V / Ctrl+Shift+V on Win/Linux. preventDefault
    // is REQUIRED — without it Chromium dispatches a native `paste` into
    // xterm's hidden textarea, causing a double paste.
    const pasteCombo = (e.metaKey && e.key === 'v') ||
                       (!isMac && e.ctrlKey && !e.metaKey && (e.key === 'v' || e.key === 'V'));
    if (pasteCombo) {
      e.preventDefault();
      e.stopPropagation();
      navigator.clipboard.readText().then(text => {
        if (text) {
          window.cc.exitCopyMode(slot);
          window.cc.writePty(slot, text);
        }
      });
      return false;
    }

    // Ctrl+Enter → insert newline in Claude Code (send CSI u via tmux send-keys -H)
    if (e.ctrlKey && e.key === 'Enter') {
      window.cc.exitCopyMode(slot);
      window.cc.tmuxSend(slot, '-H', '1B', '5B', '31', '33', '3B', '35', '75');
      return false;
    }

    // Backspace with selection → delete selected chars worth of input
    if (e.key === 'Backspace' && !e.metaKey && !e.ctrlKey) {
      const sel = term.getSelection();
      if (sel && sel.length > 0) {
        term.clearSelection();
        // Send one backspace per selected character to erase from shell input
        window.cc.writePty(slot, '\x7f'.repeat(sel.length));
        return false;
      }
    }

    // Fn+Up / Fn+Down (PageUp/PageDown) → tmux scrollback
    if (e.key === 'PageUp') {
      if (state.slots[slot]?.paneId) window.cc.scrollTmux(slot, 'up', 15);
      return false;
    }
    if (e.key === 'PageDown') {
      if (state.slots[slot]?.paneId) window.cc.scrollTmux(slot, 'down', 15);
      return false;
    }

    return true;
  });

  // Wire input — exit tmux copy-mode before sending keystrokes
  term.onData(data => {
    window.cc.exitCopyMode(slot);
    window.cc.writePty(slot, data);
  });

  state.terminals[slot] = { term, fitAddon };

  // Spawn PTY at the fitted terminal size (not the default 80x24).
  // createPty returns the immutable tmux pane ID (e.g. %5) which survives
  // session renames. All tmux commands (scroll, copy-mode) target this ID.
  const paneId = await window.cc.createPty(slot, sessionName, hostId, term.cols, term.rows);
  // Bail if another attachSession took over this slot while we awaited
  if (state.slotGen[slot] !== gen) return;
  if (!paneId) {
    console.warn(`[attach] createPty returned null for session=${sessionName}, detaching`);
    detachSlot(slot);
    return;
  }
  state.slots[slot].paneId = paneId;

  // Observe resize — only send resizePty when cols/rows ACTUALLY change.
  // Without this guard, attaching a session to one slot triggers ResizeObservers
  // on ALL slots (DOM layout recalc), causing unnecessary tmux redraws that
  // flash garbled content on the other terminals.
  let resizeTimer = null;
  const ro = new ResizeObserver(() => {
    if (!state.terminals[slot]) return;
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!state.terminals[slot]) return;
      const { term: t, fitAddon: fa } = state.terminals[slot];
      const oldCols = t.cols;
      const oldRows = t.rows;
      fa.fit();
      if (t.cols !== oldCols || t.rows !== oldRows) {
        window.cc.resizePty(slot, t.cols, t.rows);
      }
    }, 50);
  });
  ro.observe(container);
  state.terminals[slot]._ro = ro;

  renderSidebar();
}

function detachSlot(slot) {
  const wasBot = state.botSlots[slot];
  const sessionName = state.slots[slot] && state.slots[slot].name;

  // Bump generation FIRST — this invalidates any pending onPtyExit setTimeout
  // callbacks for the OLD session. Without this, killing the old PTY fires an
  // async pty:exit event that captures the NEW session's generation (because
  // attachSession increments slotGen after detachSlot returns), making the
  // guard in onPtyExit think the NEW session exited and detaching it.
  state.slotGen[slot]++;

  // Reset UI FIRST — this must always happen regardless of cleanup errors
  const header = document.getElementById(`header-${slot}`);
  const label = header.querySelector('.cell-label');
  label.textContent = `Slot ${slot + 1}`;
  label.classList.remove('has-session');
  label.style.color = '';
  header.querySelector('.cell-source-tag')?.remove();
  document.getElementById(`cell-${slot}`).classList.remove('occupied');

  // Clear state before async/throwing operations
  const hadSlot = !!state.slots[slot];
  state.slots[slot] = null;
  state.botSlots[slot] = false;
  if (sessionName) delete state.autoNames[sessionName];

  // Dispose terminal (may throw if WebGL context is lost, etc.)
  try {
    if (state.terminals[slot]) {
      if (state.terminals[slot]._ro) {
        state.terminals[slot]._ro.disconnect();
      }
      state.terminals[slot].term.dispose();
    }
  } catch (e) {
    console.warn('[App] terminal dispose error:', e);
  }
  state.terminals[slot] = null;

  // Kill PTY (async IPC, fire-and-forget)
  if (hadSlot && !wasBot) {
    window.cc.killPty(slot);
  }

  // Clear terminal container
  const container = document.getElementById(`term-${slot}`);
  container.innerHTML = '<div class="cell-empty">Click a session or bot to attach</div>';

  // Reset input bar
  inputBarState[slot] = false;
  const inputBar = document.getElementById(`input-bar-${slot}`);
  if (inputBar) inputBar.style.display = 'none';
  const kbBtn = document.querySelector(`.cell-kb-toggle[data-slot="${slot}"]`);
  if (kbBtn) kbBtn.classList.remove('active');
  const textarea = document.getElementById(`input-textarea-${slot}`);
  if (textarea) { textarea.value = ''; textarea.style.height = ''; }

  // If this was the maximized slot, go back to grid view
  if (state.maximizedSlot === slot) {
    minimizeAll();
  }

  renderSidebar();
  if (wasBot) renderBotList();
}

function focusTerminal(slot) {
  if (state.terminals[slot]) {
    state.terminals[slot].term.focus();
  }
}

// ── Maximize / Minimize ───────────────────────────────────────

function maximizeSlot(slot) {
  const grid = document.querySelector('.grid');

  if (state.maximizedSlot === slot) {
    // Already maximized — minimize back to grid
    minimizeAll();
    return;
  }

  state.maximizedSlot = slot;
  grid.classList.add('maximized');

  for (let i = 0; i < 4; i++) {
    const cell = document.getElementById(`cell-${i}`);
    cell.classList.toggle('maximized-cell', i === slot);
  }

  // Refit the maximized terminal after layout settles
  requestAnimationFrame(() => {
    if (state.terminals[slot]) {
      const { term: t, fitAddon: fa } = state.terminals[slot];
      const oldCols = t.cols;
      const oldRows = t.rows;
      fa.fit();
      if (t.cols !== oldCols || t.rows !== oldRows) {
        window.cc.resizePty(slot, t.cols, t.rows);
      }
      t.focus();
    }
  });

  renderSidebar();
}

function minimizeAll() {
  state.maximizedSlot = null;
  const grid = document.querySelector('.grid');
  grid.classList.remove('maximized');

  for (let i = 0; i < 4; i++) {
    document.getElementById(`cell-${i}`).classList.remove('maximized-cell');
  }

  // Refit all terminals — only resize PTY when size actually changes
  requestAnimationFrame(() => {
    for (let i = 0; i < 4; i++) {
      if (state.terminals[i]) {
        const { term: t, fitAddon: fa } = state.terminals[i];
        const oldCols = t.cols;
        const oldRows = t.rows;
        fa.fit();
        if (t.cols !== oldCols || t.rows !== oldRows) {
          window.cc.resizePty(i, t.cols, t.rows);
        }
      }
    }
  });

  renderSidebar();
}

// ── PTY Events ─────────────────────────────────────────────────

window.cc.onPtyData((slot, data) => {
  if (state.terminals[slot]) {
    state.terminals[slot].term.write(data);
  }
});

window.cc.onPtyExit(async (slot, exitCode) => {
  // Capture the slot generation so we can detect if the slot was reused
  const gen = state.slotGen[slot];

  // If slot was already manually detached (user clicked X), skip entirely
  if (!state.slots[slot] && !state.terminals[slot]) return;

  const sessionName = state.slots[slot]?.name;
  console.warn(`[pty:exit] slot=${slot} session=${sessionName} exitCode=${exitCode}`);

  // If the tmux session still exists, auto-reconnect instead of detaching.
  // This handles cases where the PTY process dies (crash, signal) but the
  // tmux session and its Claude Code process are still alive.
  const hostId = state.slots[slot]?.hostId || 'local';
  if (sessionName) {
    try {
      const sessionAlive = await window.cc.checkSession(sessionName, hostId);
      if (state.slotGen[slot] !== gen) return; // slot was reused during await
      if (sessionAlive) {
        console.warn(`[pty:exit] auto-reconnecting slot=${slot} session=${sessionName}`);
        if (state.terminals[slot]) {
          state.terminals[slot].term.writeln('\r\n\x1b[90m--- Reconnecting... ---\x1b[0m');
        }
        const displayName = state.slots[slot]?.displayName || sessionName;
        // Small delay to avoid rapid reconnect loops if PTY keeps dying
        setTimeout(() => {
          if (state.slotGen[slot] === gen) {
            attachSession(slot, sessionName, displayName, hostId);
          }
        }, 500);
        return;
      }
    } catch (e) {
      console.warn(`[pty:exit] checkSession failed:`, e);
    }
    if (state.slotGen[slot] !== gen) return;
  }

  // Session is truly gone — show message and detach
  if (state.terminals[slot]) {
    state.terminals[slot].term.writeln('\r\n\x1b[90m--- Session ended ---\x1b[0m');
  }
  setTimeout(() => {
    if (state.slotGen[slot] === gen) {
      detachSlot(slot);
    }
  }, 1000);
});

// ── IPC from Main Process ──────────────────────────────────────

window.cc.onAssignSlot((slot, sessionName, hostId) => {
  const session = state.sessions.find(s => s.name === sessionName);
  const displayName = session ? session.display_name : sessionName;
  attachSession(slot, sessionName, displayName, hostId || 'local');
});

window.cc.onAction((action, sessionName, extra) => {
  if (action === 'rename') {
    showRenameModal(sessionName, extra);
  } else if (action === 'trash') {
    trashSession(sessionName);
  }
});

// ── Actions ────────────────────────────────────────────────────

async function trashSession(name) {
  await api('POST', '/api/trash', { session: name });
  // Detach if in a slot
  for (let i = 0; i < 4; i++) {
    if (state.slots[i] && state.slots[i].name === name) {
      detachSlot(i);
    }
  }
  fetchSessions();
}

// Expose for inline onclick in trash items
window.restoreSession = async function(agentId) {
  if (agentId) {
    await api('POST', '/api/restore', { agent_id: agentId });
    fetchSessions();
  }
};

window.killSession = async function(name, agentId) {
  await api('POST', '/api/kill', { session: name, agent_id: agentId });
  fetchSessions();
};

async function cleanupDead() {
  const r = await api('POST', '/api/cleanup');
  if (r && r.killed && r.killed.length) {
    // Detach any killed sessions from slots
    for (const killed of r.killed) {
      for (let i = 0; i < 4; i++) {
        if (state.slots[i] && state.slots[i].name === killed) detachSlot(i);
      }
    }
  }
  fetchSessions();
}

// Default depends on mode: clients prefer 'remote' (the mac-mini); hosts use 'local'.
// CFG_READY flips this to 'remote' once we know IS_CLIENT.
let newSessionLocation = 'local';

function showNewSessionModal() {
  // Only show the local/remote toggle when Pentacle is running as a client
  // (CONFIG has a `remote` block). Host-mode machines hide it entirely.
  const toggle = document.getElementById('new-session-location');
  if (toggle) toggle.style.display = IS_CLIENT ? 'flex' : 'none';
  if (IS_CLIENT) {
    document.querySelectorAll('#new-session-location [data-loc]').forEach(b => {
      b.classList.toggle('active', b.dataset.loc === newSessionLocation);
    });
  }
  document.getElementById('new-session-overlay').style.display = 'flex';
}

function hideNewSessionModal() {
  document.getElementById('new-session-overlay').style.display = 'none';
}

function sendProgrammaticInput(slot, text) {
  if (!text || !state.slots[slot] || state.botSlots[slot] || !state.terminals[slot]) return;

  window.cc.exitCopyMode(slot);

  const lines = text.split('\n');
  for (let i = 0; i < lines.length; i++) {
    if (lines[i]) window.cc.writePty(slot, lines[i]);
    if (i < lines.length - 1) {
      window.cc.tmuxSend(slot, '-H', '1B', '5B', '31', '33', '3B', '35', '75');
    }
  }
  // Delay the submit \r so it lands as a separate keystroke event, not part of the
  // text burst. Codex's TUI batches bytes received in a short window into one chunk
  // and treats \r inside the chunk as a literal paste-newline (insert) instead of
  // Enter (submit) — without this gap, "hello\r" appears in the input but never
  // submits. Claude submits on \r regardless of timing, so the delay is harmless
  // there. 100ms is well above the empirical threshold (~20ms) with margin for slow
  // systems; barely perceptible to users.
  setTimeout(() => window.cc.writePty(slot, '\r'), 100);
}

async function newSession(agent) {
  const location = newSessionLocation || 'local';
  hideNewSessionModal();

  // Find first empty slot, or default to slot 3 (swap out)
  let slot = state.slots.findIndex(s => s === null);
  if (slot === -1) slot = 3;

  // Create a new tmux session with a shell, then launch the agent inside it.
  // The shell survives even if the agent exits immediately (auth, startup error).
  // newSession now returns { sessionName, hostId } or null.
  const result = await window.cc.newSession(agent, location);
  if (!result) return;
  const sessionName = result.sessionName || result; // tolerate legacy string return
  const hostId = result.hostId || (location === 'remote' ? 'remote' : 'local');
  state.sessionHosts[sessionName] = hostId;

  // Attach it to the slot
  await attachSession(slot, sessionName, sessionName, hostId);

  const agentConfig = CONFIG.agents[agent] || CONFIG.agents.claude;
  if (agentConfig.startupMessage) {
    const startupDelayMs = Number(agentConfig.startupDelayMs) || 1500;
    setTimeout(() => {
      if (state.slots[slot]?.name !== sessionName) return;
      sendProgrammaticInput(slot, agentConfig.startupMessage);
    }, startupDelayMs);
  }

  // Refresh sidebar after a short delay to pick up the new session
  setTimeout(fetchSessions, 2000);
}

// ── Rename Modal ───────────────────────────────────────────────

let renameTarget = null;

function showRenameModal(sessionName, currentTitle) {
  renameTarget = sessionName;
  document.getElementById('modal-title').textContent = `Rename: ${sessionName}`;
  document.getElementById('modal-input').value = currentTitle || sessionName;
  document.getElementById('modal-overlay').style.display = 'flex';
  document.getElementById('modal-input').focus();
  document.getElementById('modal-input').select();
}

function hideRenameModal() {
  document.getElementById('modal-overlay').style.display = 'none';
  renameTarget = null;
}

document.getElementById('modal-cancel').addEventListener('click', hideRenameModal);
document.getElementById('modal-overlay').addEventListener('click', (e) => {
  if (e.target === document.getElementById('modal-overlay')) hideRenameModal();
});

document.getElementById('modal-confirm').addEventListener('click', async () => {
  const newName = document.getElementById('modal-input').value.trim();
  if (newName && renameTarget) {
    await api('POST', '/api/rename', { session: renameTarget, new_name: newName });
    hideRenameModal();
    fetchSessions();
  }
});

document.getElementById('modal-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') document.getElementById('modal-confirm').click();
  if (e.key === 'Escape') hideRenameModal();
});

// ── Sidebar Tab Switching ──────────────────────────────────────

document.querySelectorAll('.sidebar-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    const target = tab.dataset.tab;
    state.activeTab = target;

    document.querySelectorAll('.sidebar-tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');

    document.getElementById('panel-sessions').style.display = target === 'sessions' ? 'flex' : 'none';
    document.getElementById('panel-bots').style.display = target === 'bots' ? 'flex' : 'none';

    if (target === 'bots') fetchBots();
  });
});

// ── View Switcher (Chats / Dashboards) ───────────────────────

function switchView(view) {
  if (state.currentView === view) return;

  // Cleanup current dashboard if leaving dashboards view
  if (state.currentView === 'dashboards') {
    stopDashboardPolling();
    unmountCurrentDashboard();
  }

  state.currentView = view;

  // Toggle DOM visibility
  document.querySelector('.grid').style.display = view === 'chats' ? '' : 'none';
  document.getElementById('dashboard-content').style.display = view === 'dashboards' ? '' : 'none';
  document.querySelector('.sidebar-tabs').style.display = view === 'chats' ? (CONFIG.features.botsTab ? '' : 'none') : 'none';
  document.getElementById('panel-dashboards').style.display = view === 'dashboards' ? 'flex' : 'none';

  // Update view switcher buttons
  document.querySelectorAll('.view-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.view === view));

  if (view === 'chats') {
    // Restore Sessions/Bots panels
    document.getElementById('panel-sessions').style.display = state.activeTab === 'sessions' ? 'flex' : 'none';
    document.getElementById('panel-bots').style.display = state.activeTab === 'bots' ? 'flex' : 'none';
    // Refit all terminals
    requestAnimationFrame(() => {
      for (let i = 0; i < 4; i++) {
        if (state.terminals[i]) {
          const { term: t, fitAddon: fa } = state.terminals[i];
          const oldCols = t.cols;
          const oldRows = t.rows;
          fa.fit();
          if (t.cols !== oldCols || t.rows !== oldRows) {
            window.cc.resizePty(i, t.cols, t.rows);
          }
        }
      }
    });
  } else {
    // Hide chat panels
    document.getElementById('panel-sessions').style.display = 'none';
    document.getElementById('panel-bots').style.display = 'none';
    renderDashboardList();
    if (!state.selectedDashboard && window.DASHBOARDS.length > 0) {
      selectDashboard(window.DASHBOARDS[0].id);
    } else if (state.selectedDashboard) {
      mountAndPoll(state.selectedDashboard);
    }
  }
}

function selectDashboard(id) {
  if (state.selectedDashboard === id) return;
  stopDashboardPolling();
  unmountCurrentDashboard();
  state.selectedDashboard = id;
  state.dashboardLastData = null;
  state.dashboardState = 'loading';
  renderDashboardList(); // update active highlight
  mountAndPoll(id);
}

function mountAndPoll(id) {
  const db = window.DASHBOARDS.find(d => d.id === id);
  if (!db) return;
  // Reset dashboard state for fresh mount
  state.dashboardState = 'loading';
  state.dashboardError = null;
  state.dashboardLastData = null;
  state.dashboardLastUpdated = null;
  const container = document.getElementById('dashboard-content');
  container.innerHTML = ''; // clear previous
  state.dashboardRefs = db.mount(container);
  updateDashboardStatusBadge();
  startDashboardPolling();
}

function unmountCurrentDashboard() {
  if (state.dashboardRefs && state.selectedDashboard) {
    const db = window.DASHBOARDS.find(d => d.id === state.selectedDashboard);
    if (db && db.unmount) db.unmount(state.dashboardRefs);
  }
  state.dashboardRefs = null;
}

function renderDashboardList() {
  const list = document.getElementById('dashboard-list');
  if (!list) return;
  list.innerHTML = window.DASHBOARDS.map(d => {
    const isActive = d.id === state.selectedDashboard;
    return `<div class="dashboard-item ${isActive ? 'active' : ''}" data-dashboard-id="${d.id}">
      <div class="dashboard-item-top">
        <span class="dashboard-dot" style="background:${d.color}"></span>
        <span class="dashboard-name">${d.name}</span>
      </div>
      <div class="dashboard-desc">${d.description}</div>
    </div>`;
  }).join('');

  list.querySelectorAll('.dashboard-item').forEach(el => {
    el.addEventListener('click', () => selectDashboard(el.dataset.dashboardId));
  });
}

// ── Dashboard Polling ─────────────────────────────────────────

function startDashboardPolling() {
  stopDashboardPolling();
  const db = window.DASHBOARDS.find(d => d.id === state.selectedDashboard);
  if (!db || !state.dashboardRefs) return;

  state.dashboardPollToken++;
  const token = state.dashboardPollToken;
  let inFlight = false; // closure-scoped per generation
  let currentInterval = db.pollInterval; // may flip to idlePollInterval when idle

  // Re-arm the setInterval with a new period if it changed (e.g. active→idle).
  // Dashboards that opt in provide `idleFn(data) -> bool` and `idlePollInterval`
  // in their registration — when idleFn returns true, we poll at the slower
  // rate until idleFn returns false again.
  function _retuneInterval(nextInterval) {
    if (nextInterval === currentInterval) return;
    currentInterval = nextInterval;
    if (state.dashboardPollTimer) clearInterval(state.dashboardPollTimer);
    state.dashboardPollTimer = setInterval(poll, currentInterval);
  }

  async function poll() {
    if (inFlight) return;
    if (state.dashboardPollToken !== token) return;
    inFlight = true;
    try {
      // Pass refs so dashboards that need state (e.g. 0DTE selected trader)
      // can read it without going through localStorage on every tick.
      const data = await db.pollFn(state.dashboardRefs);
      if (state.dashboardPollToken !== token) return; // stale generation
      if (data && !data.error) {
        state.dashboardLastData = data;
        state.dashboardLastUpdated = new Date();
        state.dashboardState = 'loaded';
        state.dashboardError = null;
        db.update(state.dashboardRefs, data);
        updateDashboardStatusBadge();

        // Idle-slowdown: if the dashboard declared itself idle for this data,
        // bump the interval down to `idlePollInterval`. If it's no longer
        // idle, snap back to the normal interval.
        if (typeof db.idleFn === 'function' && db.idlePollInterval) {
          const isIdle = !!db.idleFn(data);
          _retuneInterval(isIdle ? db.idlePollInterval : db.pollInterval);
        }
      } else {
        state.dashboardError = data?.error || 'Unknown error';
        state.dashboardState = state.dashboardLastData ? 'stale' : 'error';
        updateDashboardStatusBadge();
      }
    } catch (e) {
      if (state.dashboardPollToken !== token) return;
      state.dashboardError = e.message;
      state.dashboardState = state.dashboardLastData ? 'stale' : 'error';
      updateDashboardStatusBadge();
    } finally {
      inFlight = false;
    }
  }

  poll();
  state.dashboardPollTimer = setInterval(poll, currentInterval);
}

function stopDashboardPolling() {
  if (state.dashboardPollTimer) {
    clearInterval(state.dashboardPollTimer);
    state.dashboardPollTimer = null;
  }
  state.dashboardPollToken++; // invalidates any in-flight poll for the old generation
}

function updateDashboardStatusBadge() {
  if (!state.dashboardRefs) return;
  const { statusBadge, lastUpdated, retryBtn } = state.dashboardRefs;
  if (!statusBadge) return;

  if (state.dashboardState === 'loading') {
    statusBadge.textContent = 'Loading...';
    statusBadge.className = 'pipeline-status loading';
    if (retryBtn) retryBtn.style.display = 'none';
  } else if (state.dashboardState === 'loaded') {
    statusBadge.textContent = 'Live';
    statusBadge.className = 'pipeline-status live';
    if (lastUpdated) lastUpdated.textContent = 'Updated just now';
    if (retryBtn) retryBtn.style.display = 'none';
  } else if (state.dashboardState === 'stale') {
    const ago = state.dashboardLastUpdated
      ? Math.round((Date.now() - state.dashboardLastUpdated) / 1000)
      : '?';
    statusBadge.textContent = 'Stale';
    statusBadge.className = 'pipeline-status stale';
    if (lastUpdated) lastUpdated.textContent = `Last updated ${ago}s ago`;
    if (retryBtn) retryBtn.style.display = 'none';
  } else {
    statusBadge.textContent = 'Error';
    statusBadge.className = 'pipeline-status error';
    if (lastUpdated) lastUpdated.textContent = state.dashboardError || 'Connection failed';
    if (retryBtn) retryBtn.style.display = '';
  }
}

// Exposed for retry button in dashboard DOM
window.retryDashboardPoll = function() { startDashboardPolling(); };

document.querySelectorAll('.view-btn').forEach(btn => {
  btn.addEventListener('click', () => switchView(btn.dataset.view));
});

// ── Toolbar Buttons ────────────────────────────────────────────

document.getElementById('btn-new').addEventListener('click', showNewSessionModal);
document.getElementById('new-session-claude').addEventListener('click', () => newSession('claude'));
document.getElementById('new-session-codex').addEventListener('click', () => newSession('codex'));
document.getElementById('new-session-cancel').addEventListener('click', hideNewSessionModal);
// Local/remote toggle — only rendered when IS_CLIENT
document.querySelectorAll('#new-session-location [data-loc]').forEach(btn => {
  btn.addEventListener('click', () => {
    newSessionLocation = btn.dataset.loc;
    document.querySelectorAll('#new-session-location [data-loc]').forEach(b => {
      b.classList.toggle('active', b === btn);
    });
  });
});
document.getElementById('new-session-overlay').addEventListener('click', (e) => {
  if (e.target.id === 'new-session-overlay') hideNewSessionModal();
});
document.getElementById('btn-cleanup').addEventListener('click', cleanupDead);
document.getElementById('btn-refresh').addEventListener('click', fetchSessions);

// ── Slot Header Buttons ───────────────────────────────────────

document.querySelectorAll('.cell-close').forEach(btn => {
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const slot = parseInt(btn.dataset.slot);
    detachSlot(slot);
  });
});

document.querySelectorAll('.cell-edit').forEach(btn => {
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const slot = parseInt(btn.dataset.slot);
    if (state.slots[slot] && !state.botSlots[slot]) {
      showRenameModal(state.slots[slot].name, state.slots[slot].displayName);
    }
  });
});

document.querySelectorAll('.cell-trash').forEach(btn => {
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const slot = parseInt(btn.dataset.slot);
    if (state.slots[slot] && !state.botSlots[slot]) {
      trashSession(state.slots[slot].name);
    }
  });
});

document.querySelectorAll('.cell-maximize').forEach(btn => {
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const slot = parseInt(btn.dataset.slot);
    maximizeSlot(slot);
  });
});

// Also allow double-click on cell header to toggle maximize
document.querySelectorAll('.cell-header').forEach(header => {
  header.addEventListener('dblclick', (e) => {
    const slot = parseInt(header.id.replace('header-', ''));
    if (state.slots[slot]) {
      maximizeSlot(slot);
    }
  });
});

// ── Input Bar ─────────────────────────────────────────────────

const inputBarState = [false, false, false, false]; // visible per slot

function toggleInputBar(slot) {
  inputBarState[slot] = !inputBarState[slot];
  const bar = document.getElementById(`input-bar-${slot}`);
  const btn = document.querySelector(`.cell-kb-toggle[data-slot="${slot}"]`);
  if (bar) {
    bar.style.display = inputBarState[slot] ? 'flex' : 'none';
    if (inputBarState[slot]) {
      const textarea = document.getElementById(`input-textarea-${slot}`);
      if (textarea) textarea.focus();
    }
  }
  if (btn) btn.classList.toggle('active', inputBarState[slot]);

  // Refit terminal since available space changed
  requestAnimationFrame(() => {
    if (state.terminals[slot]) {
      const { term: t, fitAddon: fa } = state.terminals[slot];
      const oldCols = t.cols;
      const oldRows = t.rows;
      fa.fit();
      if (t.cols !== oldCols || t.rows !== oldRows) {
        window.cc.resizePty(slot, t.cols, t.rows);
      }
    }
  });
}

function sendInputBar(slot) {
  const textarea = document.getElementById(`input-textarea-${slot}`);
  if (!textarea || !state.slots[slot] || state.botSlots[slot]) return;
  const text = textarea.value;
  if (!text) return;
  sendProgrammaticInput(slot, text);

  textarea.value = '';
  textarea.style.height = '';
}

// Wire up keyboard toggle buttons
document.querySelectorAll('.cell-kb-toggle').forEach(btn => {
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const slot = parseInt(btn.dataset.slot);
    toggleInputBar(slot);
  });
});

// Wire up send buttons
document.querySelectorAll('.cell-input-send').forEach(btn => {
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const slot = parseInt(btn.dataset.slot);
    sendInputBar(slot);
  });
});

// Wire up textareas — Enter to send, Shift+Enter for newline, auto-grow
for (let i = 0; i < 4; i++) {
  const textarea = document.getElementById(`input-textarea-${i}`);
  if (!textarea) continue;
  const slot = i;

  textarea.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendInputBar(slot);
    }
    if (e.key === 'Escape') {
      toggleInputBar(slot);
    }
  });

  textarea.addEventListener('input', () => {
    // Auto-grow, show scrollbar only when at max height
    textarea.style.height = '';
    const h = Math.min(textarea.scrollHeight, 120);
    textarea.style.height = h + 'px';
    textarea.style.overflowY = textarea.scrollHeight > 120 ? 'auto' : 'hidden';
  });
}

// ── Usage Footer ──────────────────────────────────────────────

async function fetchUsage() {
  const data = await api('GET', '/api/usage');
  if (data && data.status !== 'no_data' && data.status !== 'error') {
    renderUsage(data);
  }
}

function usageBarClass(pct) {
  if (pct >= 80) return 'high';
  if (pct >= 50) return 'mid';
  return 'low';
}

function renderUsage(u) {
  const footer = document.getElementById('usage-footer');
  const sessionPct = u.session_pct || 0;
  const weekPct = u.week_all_pct || 0;

  footer.innerHTML = `
    <div class="usage-label">
      <span>Session</span>
      <span>${sessionPct}%</span>
    </div>
    <div class="usage-bar">
      <div class="usage-bar-fill ${usageBarClass(sessionPct)}" style="width:${sessionPct}%"></div>
    </div>
    <div class="usage-resets">Resets ${esc(u.session_resets || '')}</div>

    <div class="usage-label" style="margin-top:8px">
      <span>Weekly (All Models)</span>
      <span>${weekPct}%</span>
    </div>
    <div class="usage-bar">
      <div class="usage-bar-fill ${usageBarClass(weekPct)}" style="width:${weekPct}%"></div>
    </div>
    <div class="usage-resets">Resets ${esc(u.week_all_resets || '')}</div>
  `;
}

// ── Codex Usage Footer ───────────────────────────────────────

async function fetchCodexUsage() {
  const footer = document.getElementById('codex-usage-footer');
  const data = await api('GET', '/api/codex-usage');
  if (data && data.status !== 'no_data' && data.status !== 'error') {
    footer.style.display = '';
    renderCodexUsage(data);
  } else {
    footer.style.display = 'none';
  }
}

function renderCodexUsage(u) {
  const footer = document.getElementById('codex-usage-footer');
  const sessionPct = u.session_pct != null ? u.session_pct : 0;
  const weekPct = u.weekly_pct != null ? u.weekly_pct : 0;

  footer.innerHTML = `
    <div class="usage-label">
      <span>Codex 5h</span>
      <span>${sessionPct}%</span>
    </div>
    <div class="usage-bar">
      <div class="usage-bar-fill ${usageBarClass(sessionPct)}" style="width:${sessionPct}%"></div>
    </div>
    <div class="usage-resets">Resets ${esc(u.session_resets || '')}</div>

    <div class="usage-label" style="margin-top:8px">
      <span>Codex Weekly</span>
      <span>${weekPct}%</span>
    </div>
    <div class="usage-bar">
      <div class="usage-bar-fill ${usageBarClass(weekPct)}" style="width:${weekPct}%"></div>
    </div>
    <div class="usage-resets">Resets ${esc(u.weekly_resets || '')}</div>
  `;
}

// ── Mic API ──────────────────────────────────────────────────

const MIC_API = CONFIG.micServerUrl || 'http://127.0.0.1:7780';

async function micApi(method, path) {
  try {
    const r = await fetch(MIC_API + path, { method });
    return await r.json();
  } catch (e) {
    return null;
  }
}

// ── Voice Record per Slot (uses mic server copy ability) ──────

const voiceState = { activeSlot: null, pollTimer: null, prevCopied: '' };

async function toggleVoiceRecord(slot) {
  if (!state.terminals[slot]) return;

  const btn = document.querySelector(`.cell-voice[data-slot="${slot}"]`);

  // If already recording this slot, stop capture.
  // IMPORTANT: clear activeSlot + stop poller BEFORE awaiting /copy/stop so
  // any in-flight poll tick bails out (it checks voiceState.activeSlot after
  // its await resumes). Otherwise the manual-stop response and the poller
  // both see the new on_last_copied and paste it twice.
  if (voiceState.activeSlot === slot) {
    stopVoicePoll(slot);
    const r = await micApi('POST', '/copy/stop');
    if (r && r.copied && r.copied.trim() && r.copied !== voiceState.prevCopied) {
      voiceState.prevCopied = r.copied;
      window.cc.writePty(slot, r.copied.trim());
    }
    return;
  }

  // If recording another slot, stop that first
  if (voiceState.activeSlot !== null) {
    await micApi('POST', '/copy/stop');
    stopVoicePoll(voiceState.activeSlot);
  }

  // Mic must be in "on" mode — auto-enable if off
  let status = await micApi('GET', '/status');
  if (!status) {
    console.error('Mic server not reachable');
    return;
  }
  if (status.mode === 'off' || status.mode === 'offline') {
    if (status.mode === 'offline') {
      await window.cc.startMicServer();
      await new Promise(r => setTimeout(r, 1000));
    }
    await micApi('POST', '/mode/on');
    // Wait for model loading
    for (let i = 0; i < 10; i++) {
      await new Promise(r => setTimeout(r, 500));
      status = await micApi('GET', '/status');
      if (status && status.mode === 'on') break;
    }
    if (!status || status.mode !== 'on') {
      console.error('Failed to auto-enable mic');
      return;
    }
    // Update sidebar mic UI
    setTimeout(fetchMicStatus, 100);
  }

  // Remember current on_last_copied so we don't paste stale text
  voiceState.prevCopied = status.on_last_copied || '';

  // Start copy ability via mic server
  const r = await micApi('POST', '/copy/start');
  if (!r || !r.ok) {
    console.error('Failed to start copy:', r);
    return;
  }

  voiceState.activeSlot = slot;
  if (btn) btn.classList.add('recording');

  // Poll mic status — when capture ends (user said "over" / "end copy"),
  // paste result. If the user clicked the slot button instead, the manual-stop
  // path above already handled it; this poller bails on the activeSlot check
  // so we don't double-paste.
  voiceState.pollTimer = setInterval(async () => {
    const s = await micApi('GET', '/status');
    if (voiceState.activeSlot !== slot) return; // manual stop or another slot took over
    if (!s) return;
    if (s.on_listener_state !== 'CAPTURING') {
      const copied = s.on_last_copied;
      if (copied && copied.trim() && copied !== voiceState.prevCopied && state.terminals[slot]) {
        voiceState.prevCopied = copied;
        window.cc.writePty(slot, copied.trim());
      }
      stopVoicePoll(slot);
    }
  }, 500);
}

function stopVoicePoll(slot) {
  if (voiceState.pollTimer) {
    clearInterval(voiceState.pollTimer);
    voiceState.pollTimer = null;
  }
  const btn = document.querySelector(`.cell-voice[data-slot="${slot}"]`);
  if (btn) btn.classList.remove('recording');
  voiceState.activeSlot = null;
}

// Wire up voice buttons
for (const btn of document.querySelectorAll('.cell-voice')) {
  btn.addEventListener('click', (e) => {
    const slot = parseInt(btn.dataset.slot, 10);
    toggleVoiceRecord(slot);
  });
}

// ── Mic Control ───────────────────────────────────────────────

const micState = { mode: 'off', lastTranscriptIdx: 0, meetingWindowOpen: false };

function updateMicUI(data) {
  const dot = document.getElementById('mic-status-dot');
  const info = document.getElementById('mic-info');
  const btn = document.getElementById('mic-btn-toggle');
  const copyBtn = document.getElementById('mic-btn-copy');
  const meetingBtn = document.getElementById('mic-btn-meeting');
  const preview = document.getElementById('mic-transcript-preview');

  dot.className = 'mic-status-dot';

  if (!data) {
    info.textContent = 'Mic server offline';
    btn.textContent = 'Start';
    btn.className = 'mic-btn mic-toggle mic-start';
    copyBtn.disabled = true;
    meetingBtn.disabled = true;
    copyBtn.className = 'mic-btn mic-btn-copy';
    meetingBtn.className = 'mic-btn mic-btn-meeting';
    micState.mode = 'offline';
    return;
  }

  micState.mode = data.mode;
  const mode = data.mode;
  const isCopy = mode === 'clipboard';
  const isMeeting = mode === 'meeting' || data.meeting_active;
  const isOn = mode === 'on';

  // Toggle button
  btn.textContent = isOn ? 'On' : 'Off';
  btn.className = 'mic-btn mic-toggle' + (isOn ? ' selected-on' : '');

  // Copy button
  copyBtn.textContent = isCopy ? 'Stop Copy' : 'Copy';
  copyBtn.className = 'mic-btn mic-btn-copy' + (isCopy ? ' active' : '');
  copyBtn.disabled = isMeeting;

  // Meeting button
  meetingBtn.textContent = isMeeting ? 'Stop Meeting' : 'Meeting';
  meetingBtn.className = 'mic-btn mic-btn-meeting' + (isMeeting ? ' active' : '');
  meetingBtn.disabled = isCopy;

  if (isCopy) {
    dot.classList.add('active-clipboard');
    info.innerHTML = '<span style="color:var(--green)">Clipboard capture active</span>';
    preview.innerHTML = '';
  } else if (isMeeting) {
    dot.classList.add('active-meeting');
    const mins = Math.floor(data.duration / 60);
    const secs = Math.floor(data.duration % 60);
    info.innerHTML = `<span style="color:var(--red)">Recording ${String(mins).padStart(2,'0')}:${String(secs).padStart(2,'0')}</span> — ${data.transcript_count} lines`;
    // Auto-open meeting window
    if (!micState.meetingWindowOpen) {
      micState.meetingWindowOpen = true;
      window.cc.openMeeting();
    }
  } else if (!isOn) {
    info.textContent = 'Mic off';
    preview.innerHTML = '';
  } else {
    // Always-on mode
    const listenerState = data.on_listener_state || 'LISTENING';

    if (listenerState === 'AWAKE') {
      dot.classList.add('active-awake');
      info.innerHTML = '<span style="color:#00ff66">Listening for command...</span>';
      if (!data.on_last_copied) preview.innerHTML = '';
    } else if (listenerState === 'CAPTURING') {
      dot.classList.add('active-capturing');
      info.innerHTML = '<span style="color:#00ff66">Copying...</span>';
      const texts = data.on_captured_texts || [];
      if (texts.length > 0) {
        preview.innerHTML = texts.map(t =>
          `<div class="mic-transcript-line">${esc(t)}</div>`
        ).join('');
        preview.scrollTop = preview.scrollHeight;
      }
    } else if (listenerState === 'MEETING') {
      dot.classList.add('active-meeting');
      const mins = Math.floor(data.duration / 60);
      const secs = Math.floor(data.duration % 60);
      info.innerHTML = `<span style="color:var(--red)">Recording ${String(mins).padStart(2,'0')}:${String(secs).padStart(2,'0')}</span> — ${data.transcript_count} lines`;
      if (!micState.meetingWindowOpen) {
        micState.meetingWindowOpen = true;
        window.cc.openMeeting();
      }
    } else if (listenerState === 'CALIBRATING') {
      dot.classList.add('active-awake');
      info.innerHTML = '<span style="color:var(--yellow)">Calibrating...</span>';
    } else {
      dot.classList.add('active-on');
      if (data.on_last_copied) {
        info.innerHTML = '<span style="color:var(--green)">[Copied]</span>';
        preview.innerHTML = '';
      } else {
        info.textContent = `Say "${CONFIG.wakeWord}" to wake`;
        preview.innerHTML = '';
      }
    }
  }

  // Close meeting window tracking if we left meeting
  const inMeeting = isMeeting || (isOn && (data.on_listener_state === 'MEETING'));
  if (!inMeeting && micState.meetingWindowOpen) {
    micState.meetingWindowOpen = false;
  }
}

async function fetchMicStatus() {
  const data = await micApi('GET', '/status');
  updateMicUI(data);

  // If in meeting mode, fetch new transcript lines
  if (data && data.mode === 'meeting') {
    const tData = await micApi('GET', `/transcript/since/${micState.lastTranscriptIdx}`);
    if (tData) {
      const preview = document.getElementById('mic-transcript-preview');
      if (tData.lines.length > 0) {
        for (const line of tData.lines) {
          const div = document.createElement('div');
          div.className = 'mic-transcript-line';
          div.textContent = line;
          preview.appendChild(div);
        }
        micState.lastTranscriptIdx = tData.total;
        preview.scrollTop = preview.scrollHeight;
      }
      // Update partial
      let partialEl = preview.querySelector('.mic-partial');
      if (tData.partial) {
        if (!partialEl) {
          partialEl = document.createElement('div');
          partialEl.className = 'mic-partial';
          preview.appendChild(partialEl);
        }
        partialEl.textContent = '\u25B8 ' + tData.partial;
        preview.scrollTop = preview.scrollHeight;
      } else if (partialEl) {
        partialEl.remove();
      }
    }
  }
}

document.getElementById('mic-btn-toggle').addEventListener('click', async () => {
  if (micState.mode === 'offline') {
    // Server is down — start it
    const btn = document.getElementById('mic-btn-toggle');
    const info = document.getElementById('mic-info');
    btn.textContent = 'Starting...';
    btn.className = 'mic-btn mic-toggle mic-start';
    info.textContent = 'Starting mic server...';
    const ok = await window.cc.startMicServer();
    if (ok) {
      setTimeout(fetchMicStatus, 500);
    } else {
      info.textContent = 'Failed to start mic server';
      btn.textContent = 'Start';
    }
    return;
  }
  const newMode = micState.mode === 'on' ? 'off' : 'on';
  await micApi('POST', `/mode/${newMode}`);
  micState.lastTranscriptIdx = 0;
  document.getElementById('mic-transcript-preview').innerHTML = '';
  setTimeout(fetchMicStatus, newMode === 'on' ? 1500 : 500);
});

document.getElementById('mic-btn-copy').addEventListener('click', async () => {
  if (micState.mode === 'offline') return;
  const newMode = micState.mode === 'clipboard' ? 'off' : 'clipboard';
  await micApi('POST', `/mode/${newMode}`);
  setTimeout(fetchMicStatus, 500);
});

document.getElementById('mic-btn-meeting').addEventListener('click', async () => {
  if (micState.mode === 'offline') return;
  const isMeeting = micState.mode === 'meeting';
  const newMode = isMeeting ? 'off' : 'meeting';
  await micApi('POST', `/mode/${newMode}`);
  micState.lastTranscriptIdx = 0;
  document.getElementById('mic-transcript-preview').innerHTML = '';
  setTimeout(fetchMicStatus, isMeeting ? 500 : 2000);
});

// ── Init ───────────────────────────────────────────────────────

// Apply config — theme, app name, feature flags
(function applyConfig() {
  // Set titlebar text and document title
  const titleEl = document.getElementById('titlebar-text');
  if (titleEl) titleEl.textContent = CONFIG.appName.toUpperCase();
  document.title = CONFIG.appName;

  // Apply CSS custom properties from config theme
  const themeVarMap = { bg: '--bg', bg2: '--bg2', bg3: '--bg3', fg: '--fg', fgDim: '--fg-dim',
    blue: '--blue', green: '--green', red: '--red', yellow: '--yellow',
    purple: '--purple', cyan: '--cyan', border: '--border' };

  function applyThemeVars(theme) {
    if (!theme) return;
    for (const [key, cssVar] of Object.entries(themeVarMap)) {
      if (theme[key]) document.documentElement.style.setProperty(cssVar, theme[key]);
    }
  }

  // Apply initial theme
  const isDark = document.documentElement.dataset.theme !== 'light';
  applyThemeVars(isDark ? CONFIG.dark : CONFIG.light);

  // Wire up theme toggle to also apply config colors
  const themeToggle = document.getElementById('theme-toggle');
  if (themeToggle) {
    themeToggle.addEventListener('click', () => {
      const html = document.documentElement;
      const nowDark = html.dataset.theme !== 'light';
      html.dataset.theme = nowDark ? 'light' : 'dark';
      themeToggle.innerHTML = nowDark ? '&#9788;' : '&#9790;';
      applyThemeVars(nowDark ? CONFIG.light : CONFIG.dark);
    });
  }

  // Hide mic panel + voice record buttons if disabled
  if (!CONFIG.features.mic) {
    const micPanel = document.getElementById('mic-panel');
    if (micPanel) micPanel.style.display = 'none';
    document.querySelectorAll('.cell-voice').forEach(btn => btn.style.display = 'none');
  }

  // Hide usage footers if disabled
  if (!CONFIG.features.usage) {
    const usageFooter = document.getElementById('usage-footer');
    if (usageFooter) usageFooter.style.display = 'none';
    const codexUsageFooter = document.getElementById('codex-usage-footer');
    if (codexUsageFooter) codexUsageFooter.style.display = 'none';
  }

  // Hide input bar toggle buttons if disabled
  if (!CONFIG.features.inputBar) {
    document.querySelectorAll('.cell-kb-toggle').forEach(btn => btn.style.display = 'none');
  }

  // Hide bots tab if disabled — show flat session list without tabs
  if (!CONFIG.features.botsTab) {
    const tabBar = document.querySelector('.sidebar-tabs');
    if (tabBar) tabBar.style.display = 'none';
  }

  // Hide dashboards view switcher if disabled — just show chats, no label
  if (!CONFIG.features.dashboards) {
    const viewSwitcher = document.querySelector('.view-switcher');
    if (viewSwitcher) viewSwitcher.style.display = 'none';
  }
})();

// Set empty state for all cells.
for (let i = 0; i < 4; i++) {
  const container = document.getElementById(`term-${i}`);
  container.innerHTML = '<div class="cell-empty">Click a session or bot to attach</div>';
}

// Single document-level capture-phase wheel handler for ALL slots.
// Previous approach: per-container handlers. These failed silently when xterm's
// WebGL canvas or viewport absorbed wheel events before the container's capture
// handler could fire (Chromium compositor-level scroll interception).
// Document-level capture fires FIRST, before any element-level handler.
document.addEventListener('wheel', (e) => {
  // Walk up from event target to find which .cell-terminal slot we're in
  let el = e.target;
  let container = null;
  while (el && el !== document) {
    if (el.classList && el.classList.contains('cell-terminal')) {
      container = el;
      break;
    }
    el = el.parentElement;
  }
  if (!container) return; // not over a terminal slot

  const slotMatch = container.id.match(/^term-(\d)$/);
  if (!slotMatch) return;
  const slot = parseInt(slotMatch[1], 10);

  // Only scroll if slot has an active terminal session (not a bot panel, not empty)
  if (!state.slots[slot] || state.botSlots[slot] || !state.terminals[slot]) {
    const now = Date.now();
    if (now - (state._scrollDebugTs?.[slot] || 0) > 1000) {
      if (!state._scrollDebugTs) state._scrollDebugTs = [0,0,0,0];
      state._scrollDebugTs[slot] = now;
      console.warn(`[scroll:blocked] slot=${slot} slots=${!!state.slots[slot]} botSlots=${state.botSlots[slot]} terminals=${!!state.terminals[slot]}`);
    }
    return;
  }
  e.preventDefault();
  e.stopImmediatePropagation();
  const now = Date.now();
  if (now - state.wheelThrottles[slot] < 50) return;
  state.wheelThrottles[slot] = now;
  if (!state.slots[slot].paneId) return; // PTY not yet created or createPty failed
  const lines = Math.max(1, Math.round(Math.abs(e.deltaY) / 25));
  window.cc.scrollTmux(slot, e.deltaY < 0 ? 'up' : 'down', lines);
}, { passive: false, capture: true });

// Wait for main-process config (isClient, tunneled apiUrl) before kicking off
// any network activity — the first fetchSessions() otherwise targets the
// pre-bootstrap hardcoded URL and fails on clients.
CFG_READY.then((cfg) => {
  fetchSessions();
  pollActivity();
  setInterval(fetchSessions, 5000);
  setInterval(pollActivity, 3000);

  if (CONFIG.features.usage) {
    fetchUsage();
    setInterval(fetchUsage, 30000);
    fetchCodexUsage();
    setInterval(fetchCodexUsage, 30000);
  }
  // Mic panel — enabled on any platform when features.mic is true.
  // The mic server is cross-platform (MicServer.app on macOS, Python direct on Windows/Linux).
  if (CONFIG.features.mic) {
    fetchMicStatus();
    setInterval(fetchMicStatus, 1000);
  } else {
    const panel = document.getElementById('mic-panel');
    if (panel) panel.style.display = 'none';
  }
  if (CONFIG.features.botsTab) {
    fetchBots();
    setInterval(fetchBots, 10000);
  }
});
