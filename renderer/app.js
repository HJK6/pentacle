/* global Terminal, FitAddon */

const { Terminal } = require('@xterm/xterm');
const { FitAddon } = require('@xterm/addon-fit');
const { Unicode11Addon } = require('@xterm/addon-unicode11'); // REQUIRED: without this, ❯ and other unicode renders as __
const { WebglAddon } = require('@xterm/addon-webgl'); // GPU-accelerated rendering — fixes partial text paint on screen refresh

const THEME = {
  background: '#0c1310',
  foreground: '#b5ccba',
  cursor: '#3fb950',
  cursorAccent: '#0c1310',
  selectionBackground: '#1e4d2b',
  black: '#0c1310',
  red: '#f47067',
  green: '#3fb950',
  yellow: '#d4a72c',
  blue: '#58a6ff',
  magenta: '#a78bfa',
  cyan: '#2dd4bf',
  white: '#b5ccba',
  brightBlack: '#4d6e56',
  brightRed: '#f47067',
  brightGreen: '#56d364',
  brightYellow: '#e0af68',
  brightBlue: '#79c0ff',
  brightMagenta: '#b8a0fa',
  brightCyan: '#56d4c4',
  brightWhite: '#d6e8da',
};

// ── State ──────────────────────────────────────────────────────

const state = {
  sessions: [],       // remote (from API) or all (server mode)
  localSessions: [],  // local WSL tmux sessions (client mode only)
  trashed: [],
  bots: [],
  activeTab: 'sessions',
  slots: [null, null, null, null],
  terminals: [null, null, null, null],
  wheelAbort: [null, null, null, null],
  botSlots: [false, false, false, false],
  showTrash: false,
  maximizedSlot: null,
  config: null,
};

// API URLs — set after config loads
let API = 'http://localhost:7777';
let MIC_API = 'http://127.0.0.1:7780';

// ── Config ────────────────────────────────────────────────────

async function loadConfig() {
  state.config = await window.cc.getConfig();
  if (state.config?.isClient) {
    // Point API at the SSH-tunneled Mac Mini API
    API = `http://localhost:${state.config.remoteApiPort}`;
  }
}

// ── API ───────────────────────────────────────────────────────

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

// ── Session Fetching ──────────────────────────────────────────

async function fetchSessions() {
  // Always fetch from API (server mode = local API, client mode = tunneled Mac API)
  const data = await api('GET', '/api/sessions');
  if (data) {
    state.sessions = data.active || [];
    state.trashed = data.trashed || [];
  }

  // In client mode, also fetch local WSL tmux sessions
  if (state.config?.isClient) {
    try {
      state.localSessions = await window.cc.listSessions();
    } catch {
      state.localSessions = [];
    }
  }

  renderSidebar();
}

// ── Sidebar Rendering ─────────────────────────────────────────

function getSlotForSession(name) {
  for (let i = 0; i < 4; i++) {
    if (state.slots[i] && state.slots[i].name === name) return i;
  }
  return -1;
}

function renderSessionItem(s, isRemote) {
  const slotIdx = getSlotForSession(s.name);
  const isActive = slotIdx >= 0;
  const dotClass = s.attached ? 'attached' : s.type;
  const hasTitle = s.title && s.title !== s.name;
  const sourceTag = state.config?.isClient
    ? (isRemote ? '<span class="s-source-badge remote">MAC</span>' : '<span class="s-source-badge local">LOCAL</span>')
    : '';

  return `<div class="session-item ${isActive ? 'active' : ''}"
               data-name="${esc(s.name)}"
               data-display="${esc(s.display_name)}"
               data-remote="${isRemote}">
    <div class="s-top">
      <span class="s-dot ${dotClass}"></span>
      <span class="s-name">${esc(s.display_name)}</span>
      ${sourceTag}
      ${isActive ? `<span class="s-slot-badge">${slotIdx + 1}</span>` : ''}
      <button class="s-edit-btn" data-edit-name="${esc(s.name)}" data-edit-display="${esc(s.display_name)}" data-remote="${isRemote}" title="Rename"><svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M12.146.146a.5.5 0 0 1 .708 0l3 3a.5.5 0 0 1 0 .708l-10 10a.5.5 0 0 1-.168.11l-5 2a.5.5 0 0 1-.65-.65l2-5a.5.5 0 0 1 .11-.168l10-10zM11.207 2.5L13.5 4.793 14.793 3.5 12.5 1.207 11.207 2.5zm1.586 3L10.5 3.207 4 9.707V10h.5a.5.5 0 0 1 .5.5v.5h.5a.5.5 0 0 1 .5.5v.5h.293l6.5-6.5zm-9.761 5.175l-.106.106-1.528 3.821 3.821-1.528.106-.106A.5.5 0 0 1 5 12.5V12h-.5a.5.5 0 0 1-.5-.5V11h-.5a.5.5 0 0 1-.468-.325z"/></svg></button>
      <button class="s-trash-btn" data-trash-name="${esc(s.name)}" data-remote="${isRemote}" title="Move to trash"><svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M5.5 5.5A.5.5 0 0 1 6 6v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5zm2.5 0a.5.5 0 0 1 .5.5v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5zm3 .5a.5.5 0 0 0-1 0v6a.5.5 0 0 0 1 0V6z"/><path fill-rule="evenodd" d="M14.5 3a1 1 0 0 1-1 1H13v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V4h-.5a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1H6a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1h3.5a1 1 0 0 1 1 1v1zM4.118 4L4 4.059V13a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V4.059L11.882 4H4.118zM2.5 3V2h11v1h-11z"/></svg></button>
    </div>
    ${hasTitle ? `<div class="s-meta">${esc(s.name)}</div>` : ''}
    ${s.preview ? `<div class="s-preview">${esc(s.preview)}</div>` : ''}
  </div>`;
}

function renderSidebar() {
  const list = document.getElementById('session-list');
  const stats = document.getElementById('stats');

  if (state.config?.isClient) {
    // Client mode: local + remote sections
    const local = state.localSessions;
    const remote = state.sessions;
    stats.textContent = `${local.length} local | ${remote.length} remote`;

    let html = '';

    if (local.length > 0) {
      html += '<div class="session-group-header">LOCAL</div>';
      html += local.map(s => renderSessionItem(s, false)).join('');
    }

    if (remote.length > 0) {
      html += '<div class="session-group-header remote">MAC MINI</div>';
      html += remote.map(s => renderSessionItem(s, true)).join('');
    }

    if (local.length === 0 && remote.length === 0) {
      html = '<div class="session-empty">No sessions found</div>';
    }

    list.innerHTML = html;
  } else {
    // Server mode: original behavior
    const active = state.sessions;
    stats.textContent = `${active.length} sessions | ${active.filter(s => s.attached).length} attached`;
    list.innerHTML = active.map(s => renderSessionItem(s, false)).join('');
  }

  // Click handlers
  list.querySelectorAll('.session-item').forEach(el => {
    el.addEventListener('click', () => {
      assignToSlot(el.dataset.name, el.dataset.display, el.dataset.remote === 'true');
    });
    el.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      window.cc.showContextMenu(el.dataset.name, el.dataset.display);
    });
  });

  list.querySelectorAll('.s-edit-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      showRenameModal(btn.dataset.editName, btn.dataset.editDisplay, btn.dataset.remote === 'true');
    });
  });

  list.querySelectorAll('.s-trash-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      trashSession(btn.dataset.trashName, btn.dataset.remote === 'true');
    });
  });

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

// ── Bots ──────────────────────────────────────────────────────

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

  list.querySelectorAll('.bot-item').forEach(el => {
    el.addEventListener('click', () => {
      const bot = state.bots.find(b => b.bot_id === el.dataset.botId);
      if (bot) openBotInSlot(bot);
    });
  });
}

function openBotInSlot(bot) {
  const existing = getSlotForBot(bot.bot_id);
  if (existing >= 0) {
    if (state.maximizedSlot !== null && state.maximizedSlot !== existing) maximizeSlot(existing);
    return;
  }

  if (state.maximizedSlot !== null) {
    attachBotToSlot(state.maximizedSlot, bot);
    maximizeSlot(state.maximizedSlot);
    return;
  }

  let slot = state.slots.findIndex(s => s === null);
  if (slot === -1) slot = 3;
  attachBotToSlot(slot, bot);
}

function attachBotToSlot(slot, bot) {
  detachSlot(slot);
  const title = bot.title || bot.bot_name || bot.bot_id;
  state.slots[slot] = { bot, displayName: title };
  state.botSlots[slot] = true;

  const header = document.getElementById(`header-${slot}`);
  const label = header.querySelector('.cell-label');
  label.textContent = title;
  label.classList.add('has-session');
  label.style.color = 'var(--cyan)';
  document.getElementById(`cell-${slot}`).classList.add('occupied');

  const container = document.getElementById(`term-${slot}`);
  container.innerHTML = renderBotDetail(bot);

  const refreshBtn = container.querySelector('.bot-detail-refresh');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', async () => {
      await fetchBots();
      const updated = state.bots.find(b => b.bot_id === bot.bot_id);
      if (updated && state.botSlots[slot]) {
        state.slots[slot] = { bot: updated, displayName: updated.title || updated.bot_name || updated.bot_id };
        container.innerHTML = renderBotDetail(updated);
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
  html += `<div class="bot-detail-status">
    <span class="bot-status-badge ${esc(bot.status)}">${esc(bot.status)}</span>
    <span class="bot-type-badge">${esc(bot.bot_type)}</span>
    ${bot.started_at ? `<span class="bot-uptime">${formatUptime(bot.started_at)} uptime</span>` : ''}
  </div>`;

  if (bot.current_task) {
    html += `<div class="bot-detail-section">
      <div class="bot-detail-section-title">Current Task</div>
      <div class="bot-detail-field">${esc(bot.current_task)}</div>
    </div>`;
  }

  if (scalarEntries.length > 0 || listEntries.length > 0) {
    html += `<div class="bot-detail-section"><div class="bot-detail-section-title">Metrics</div>`;
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

  html += `<div class="bot-detail-section"><div class="bot-detail-section-title">Details</div><div class="bot-detail-grid">`;
  if (bot.started_at) html += `<span class="bot-detail-label">Started</span><span class="bot-detail-value">${new Date(bot.started_at).toLocaleString()}</span>`;
  if (bot.last_heartbeat) html += `<span class="bot-detail-label">Heartbeat</span><span class="bot-detail-value">${new Date(bot.last_heartbeat).toLocaleString()}</span>`;
  if (bot.host) html += `<span class="bot-detail-label">Host</span><span class="bot-detail-value">${esc(bot.host)}</span>`;
  if (bot.pid) html += `<span class="bot-detail-label">PID</span><span class="bot-detail-value">${bot.pid}</span>`;
  html += `</div></div>`;
  html += `<button class="bot-restart-btn bot-detail-refresh">Refresh</button>`;
  html += `</div>`;
  return html;
}

// ── Slot Assignment ───────────────────────────────────────────

function assignToSlot(sessionName, displayName, remote = false) {
  const existing = getSlotForSession(sessionName);
  if (existing >= 0) {
    if (state.maximizedSlot !== null && state.maximizedSlot !== existing) maximizeSlot(existing);
    focusTerminal(existing);
    return;
  }

  if (state.maximizedSlot !== null) {
    attachSession(state.maximizedSlot, sessionName, displayName, remote);
    maximizeSlot(state.maximizedSlot);
    return;
  }

  let slot = state.slots.findIndex(s => s === null);
  if (slot === -1) slot = 3;
  attachSession(slot, sessionName, displayName, remote);
}

async function attachSession(slot, sessionName, displayName, remote = false) {
  detachSlot(slot);
  state.slots[slot] = { name: sessionName, displayName, remote };

  const header = document.getElementById(`header-${slot}`);
  const label = header.querySelector('.cell-label');
  label.textContent = displayName;
  label.classList.add('has-session');
  if (remote && state.config?.isClient) label.style.color = 'var(--cyan)';
  document.getElementById(`cell-${slot}`).classList.add('occupied');

  const container = document.getElementById(`term-${slot}`);
  container.innerHTML = '';

  const term = new Terminal({
    theme: THEME,
    fontFamily: "'SFMono-Regular', 'SF Mono', '.SF NS Mono', 'Cascadia Code', 'Menlo', 'Monaco', monospace",
    fontSize: 13,
    cursorBlink: true,
    allowProposedApi: true,
    scrollback: 0,
  });

  const fitAddon = new FitAddon();
  const unicode11Addon = new Unicode11Addon();
  term.loadAddon(fitAddon);
  term.loadAddon(unicode11Addon);
  term.open(container);
  term.unicode.activeVersion = '11';

  try {
    const webglAddon = new WebglAddon();
    webglAddon.onContextLoss(() => { webglAddon.dispose(); });
    term.loadAddon(webglAddon);
  } catch (e) {
    console.warn('[App] WebGL renderer unavailable, using DOM fallback:', e.message);
  }

  const viewport = container.querySelector('.xterm-viewport');
  const nukeViewportScroll = () => {
    if (viewport) {
      viewport.style.overflowY = 'hidden';
      viewport.style.touchAction = 'none';
    }
  };
  nukeViewportScroll();

  let viewportObserver;
  if (viewport) {
    viewportObserver = new MutationObserver(nukeViewportScroll);
    viewportObserver.observe(viewport, { attributes: true, attributeFilter: ['style'] });
  }

  requestAnimationFrame(() => {
    fitAddon.fit();
    window.cc.resizePty(slot, term.cols, term.rows);
  });

  if (state.wheelAbort[slot]) state.wheelAbort[slot].abort();
  const wheelAC = new AbortController();
  state.wheelAbort[slot] = wheelAC;

  let lastScrollTime = 0;
  const handleWheel = (e) => {
    e.preventDefault();
    e.stopImmediatePropagation();
    const now = Date.now();
    if (now - lastScrollTime < 50) return;
    lastScrollTime = now;
    const lines = Math.max(1, Math.round(Math.abs(e.deltaY) / 25));
    window.cc.scrollTmux(slot, e.deltaY < 0 ? 'up' : 'down', lines);
  };
  container.addEventListener('wheel', handleWheel, { passive: false, capture: true, signal: wheelAC.signal });
  if (viewport) viewport.addEventListener('wheel', handleWheel, { passive: false, signal: wheelAC.signal });

  term.attachCustomKeyEventHandler((e) => {
    if (e.type !== 'keydown') return true;
    if ((e.ctrlKey || e.metaKey) && e.key === 'c') {
      const sel = term.getSelection();
      if (sel) { navigator.clipboard.writeText(sel); return false; }
    }
    if (e.ctrlKey && e.key === 'Enter') {
      window.cc.exitCopyMode(slot);
      window.cc.tmuxSend(slot, '-H', '1B', '5B', '31', '33', '3B', '35', '75');
      return false;
    }
    if (e.key === 'Backspace' && !e.metaKey && !e.ctrlKey) {
      const sel = term.getSelection();
      if (sel && sel.length > 0) {
        term.clearSelection();
        window.cc.writePty(slot, '\x7f'.repeat(sel.length));
        return false;
      }
    }
    return true;
  });

  term.onData(data => {
    window.cc.exitCopyMode(slot);
    window.cc.writePty(slot, data);
  });

  state.terminals[slot] = { term, fitAddon, viewportObserver };
  await window.cc.createPty(slot, sessionName, remote);

  const ro = new ResizeObserver(() => {
    if (state.terminals[slot]) {
      state.terminals[slot].fitAddon.fit();
      window.cc.resizePty(slot, state.terminals[slot].term.cols, state.terminals[slot].term.rows);
    }
  });
  ro.observe(container);
  state.terminals[slot]._ro = ro;

  renderSidebar();
}

function detachSlot(slot) {
  const wasBot = state.botSlots[slot];

  if (state.wheelAbort[slot]) { state.wheelAbort[slot].abort(); state.wheelAbort[slot] = null; }
  if (state.terminals[slot]) {
    if (state.terminals[slot].viewportObserver) state.terminals[slot].viewportObserver.disconnect();
    if (state.terminals[slot]._ro) state.terminals[slot]._ro.disconnect();
    state.terminals[slot].term.dispose();
    state.terminals[slot] = null;
  }

  if (state.slots[slot]) {
    if (!wasBot) window.cc.killPty(slot);
    state.slots[slot] = null;
  }
  state.botSlots[slot] = false;

  const container = document.getElementById(`term-${slot}`);
  container.innerHTML = '<div class="cell-empty">Click a session to attach</div>';
  const label = document.getElementById(`header-${slot}`).querySelector('.cell-label');
  label.textContent = `Slot ${slot + 1}`;
  label.classList.remove('has-session');
  label.style.color = '';
  document.getElementById(`cell-${slot}`).classList.remove('occupied');

  if (state.maximizedSlot === slot) minimizeAll();
  renderSidebar();
  if (wasBot) renderBotList();
}

function focusTerminal(slot) {
  if (state.terminals[slot]) state.terminals[slot].term.focus();
}

// ── Maximize / Minimize ──────────────────────────────────────

function maximizeSlot(slot) {
  const grid = document.querySelector('.grid');
  if (state.maximizedSlot === slot) { minimizeAll(); return; }

  state.maximizedSlot = slot;
  grid.classList.add('maximized');
  for (let i = 0; i < 4; i++) document.getElementById(`cell-${i}`).classList.toggle('maximized-cell', i === slot);

  requestAnimationFrame(() => {
    if (state.terminals[slot]) {
      state.terminals[slot].fitAddon.fit();
      window.cc.resizePty(slot, state.terminals[slot].term.cols, state.terminals[slot].term.rows);
      state.terminals[slot].term.focus();
    }
  });
  renderSidebar();
}

function minimizeAll() {
  state.maximizedSlot = null;
  document.querySelector('.grid').classList.remove('maximized');
  for (let i = 0; i < 4; i++) document.getElementById(`cell-${i}`).classList.remove('maximized-cell');
  requestAnimationFrame(() => {
    for (let i = 0; i < 4; i++) {
      if (state.terminals[i]) {
        state.terminals[i].fitAddon.fit();
        window.cc.resizePty(i, state.terminals[i].term.cols, state.terminals[i].term.rows);
      }
    }
  });
  renderSidebar();
}

// ── PTY Events ────────────────────────────────────────────────

window.cc.onPtyData((slot, data) => {
  if (state.terminals[slot]) state.terminals[slot].term.write(data);
});

window.cc.onPtyExit((slot, exitCode) => {
  if (state.terminals[slot]) state.terminals[slot].term.writeln('\r\n\x1b[90m--- Session ended ---\x1b[0m');
  setTimeout(() => detachSlot(slot), 1000);
});

// ── IPC from Main Process ─────────────────────────────────────

window.cc.onAssignSlot((slot, sessionName, remote) => {
  const allSessions = [...state.sessions, ...state.localSessions];
  const session = allSessions.find(s => s.name === sessionName);
  const displayName = session ? session.display_name : sessionName;
  attachSession(slot, sessionName, displayName, remote || false);
});

window.cc.onAction((action, sessionName, extra) => {
  if (action === 'rename') showRenameModal(sessionName, extra);
  else if (action === 'trash') trashSession(sessionName);
});

// ── Actions ───────────────────────────────────────────────────

async function trashSession(name, remote = false) {
  if (remote || !state.config?.isClient) {
    // Remote or server mode: use API
    await api('POST', '/api/trash', { session: name });
  } else {
    // Local session (client mode): kill via IPC
    await window.cc.killSession(name, false);
  }
  for (let i = 0; i < 4; i++) {
    if (state.slots[i] && state.slots[i].name === name) detachSlot(i);
  }
  fetchSessions();
}

window.restoreSession = async function(agentId) {
  if (agentId) { await api('POST', '/api/restore', { agent_id: agentId }); fetchSessions(); }
};

window.killSession = async function(name, agentId) {
  await api('POST', '/api/kill', { session: name, agent_id: agentId }); fetchSessions();
};

async function cleanupDead() {
  const r = await api('POST', '/api/cleanup');
  if (r && r.killed && r.killed.length) {
    for (const killed of r.killed) {
      for (let i = 0; i < 4; i++) {
        if (state.slots[i] && state.slots[i].name === killed) detachSlot(i);
      }
    }
  }
  fetchSessions();
}

let newSessionLocation = 'local';

function showNewSessionModal() {
  // Show location toggle only on client machines
  const locDiv = document.getElementById('new-session-location');
  if (state.config?.isClient) {
    locDiv.style.display = '';
  } else {
    locDiv.style.display = 'none';
  }
  document.getElementById('new-session-overlay').style.display = 'flex';
}

function hideNewSessionModal() {
  document.getElementById('new-session-overlay').style.display = 'none';
}

function sendProgrammaticInput(slot, text) {
  if (!state.terminals[slot]) return;
  window.cc.exitCopyMode(slot);
  window.cc.writePty(slot, text);
  setTimeout(() => window.cc.writePty(slot, '\r'), 100);
}

async function newSession(agent) {
  hideNewSessionModal();
  const location = state.config?.isClient ? newSessionLocation : 'local';
  const remote = location === 'remote';

  let slot = state.slots.findIndex(s => s === null);
  if (slot === -1) slot = 3;

  const result = await window.cc.newSession(agent, location);
  if (!result) return;

  const { sessionName } = result;
  await attachSession(slot, sessionName, sessionName, remote);

  const agentConfig = state.config?.agents?.[agent];
  if (agentConfig?.startupMessage) {
    const delay = Number(agentConfig.startupDelayMs) || 1500;
    setTimeout(() => {
      if (state.slots[slot]?.name !== sessionName) return;
      sendProgrammaticInput(slot, agentConfig.startupMessage);
    }, delay);
  }

  setTimeout(fetchSessions, 2000);
}

// ── Rename Modal ──────────────────────────────────────────────

let renameTarget = null;
let renameRemote = false;

function showRenameModal(sessionName, currentTitle, remote = false) {
  renameTarget = sessionName;
  renameRemote = remote;
  document.getElementById('modal-title').textContent = `Rename: ${sessionName}`;
  document.getElementById('modal-input').value = currentTitle || sessionName;
  document.getElementById('modal-overlay').style.display = 'flex';
  document.getElementById('modal-input').focus();
  document.getElementById('modal-input').select();
}

function hideRenameModal() {
  document.getElementById('modal-overlay').style.display = 'none';
  renameTarget = null;
  renameRemote = false;
}

document.getElementById('modal-cancel').addEventListener('click', hideRenameModal);
document.getElementById('modal-overlay').addEventListener('click', (e) => {
  if (e.target === document.getElementById('modal-overlay')) hideRenameModal();
});

document.getElementById('modal-confirm').addEventListener('click', async () => {
  const newName = document.getElementById('modal-input').value.trim();
  if (newName && renameTarget) {
    if (renameRemote || !state.config?.isClient) {
      await api('POST', '/api/rename', { session: renameTarget, new_name: newName });
    } else {
      await window.cc.renameSession(renameTarget, newName, false);
    }
    hideRenameModal();
    fetchSessions();
  }
});

document.getElementById('modal-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') document.getElementById('modal-confirm').click();
  if (e.key === 'Escape') hideRenameModal();
});

// ── Sidebar Tab Switching ─────────────────────────────────────

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

// ── Toolbar Buttons ───────────────────────────────────────────

document.getElementById('btn-new').addEventListener('click', showNewSessionModal);
document.getElementById('new-session-claude').addEventListener('click', () => newSession('claude'));
document.getElementById('new-session-codex').addEventListener('click', () => newSession('codex'));
document.getElementById('new-session-cancel').addEventListener('click', hideNewSessionModal);
document.getElementById('new-session-overlay').addEventListener('click', (e) => {
  if (e.target.id === 'new-session-overlay') hideNewSessionModal();
});
// Location toggle buttons
document.querySelectorAll('.new-loc-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    newSessionLocation = btn.dataset.loc;
    document.querySelectorAll('.new-loc-btn').forEach(b => b.classList.toggle('active', b === btn));
  });
});
document.getElementById('btn-cleanup').addEventListener('click', cleanupDead);
document.getElementById('btn-refresh').addEventListener('click', fetchSessions);

// ── Slot Header Buttons ──────────────────────────────────────

document.querySelectorAll('.cell-close').forEach(btn => {
  btn.addEventListener('click', (e) => { e.stopPropagation(); detachSlot(parseInt(btn.dataset.slot)); });
});

document.querySelectorAll('.cell-edit').forEach(btn => {
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const slot = parseInt(btn.dataset.slot);
    if (state.slots[slot] && !state.botSlots[slot]) {
      showRenameModal(state.slots[slot].name, state.slots[slot].displayName, state.slots[slot].remote);
    }
  });
});

document.querySelectorAll('.cell-trash').forEach(btn => {
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const slot = parseInt(btn.dataset.slot);
    if (state.slots[slot] && !state.botSlots[slot]) trashSession(state.slots[slot].name, state.slots[slot].remote);
  });
});

document.querySelectorAll('.cell-maximize').forEach(btn => {
  btn.addEventListener('click', (e) => { e.stopPropagation(); maximizeSlot(parseInt(btn.dataset.slot)); });
});

document.querySelectorAll('.cell-header').forEach(header => {
  header.addEventListener('dblclick', () => {
    const slot = parseInt(header.id.replace('header-', ''));
    if (state.slots[slot]) maximizeSlot(slot);
  });
});

// ── Usage Footer ─────────────────────────────────────────────

async function fetchUsage() {
  const data = await api('GET', '/api/usage');
  if (data && data.status !== 'no_data' && data.status !== 'error') renderUsage(data);
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
    <div class="usage-label"><span>Session</span><span>${sessionPct}%</span></div>
    <div class="usage-bar"><div class="usage-bar-fill ${usageBarClass(sessionPct)}" style="width:${sessionPct}%"></div></div>
    <div class="usage-resets">Resets ${esc(u.session_resets || '')}</div>
    <div class="usage-label" style="margin-top:8px"><span>Weekly (All Models)</span><span>${weekPct}%</span></div>
    <div class="usage-bar"><div class="usage-bar-fill ${usageBarClass(weekPct)}" style="width:${weekPct}%"></div></div>
    <div class="usage-resets">Resets ${esc(u.week_all_resets || '')}</div>
  `;
}

// ── Codex Usage ──────────────────────────────────────────────

async function fetchCodexUsage() {
  const data = await api('GET', '/api/codex-usage');
  if (data && data.status !== 'no_data' && data.status !== 'error') {
    renderCodexUsage(data);
  }
}

function renderCodexUsage(u) {
  const footer = document.getElementById('codex-usage-footer');
  if (!footer) return;

  const sessionPct = u.session_pct || 0;
  const weeklyPct = u.weekly_pct || 0;

  footer.style.display = '';
  footer.innerHTML = `
    <div class="usage-label"><span>Codex 5h</span><span>${sessionPct}%</span></div>
    <div class="usage-bar"><div class="usage-bar-fill ${usageBarClass(sessionPct)}" style="width:${sessionPct}%"></div></div>
    <div class="usage-resets">Resets ${esc(u.session_resets || '')}</div>
    <div class="usage-label" style="margin-top:8px"><span>Codex Weekly</span><span>${weeklyPct}%</span></div>
    <div class="usage-bar"><div class="usage-bar-fill ${usageBarClass(weeklyPct)}" style="width:${weeklyPct}%"></div></div>
    <div class="usage-resets">Resets ${esc(u.weekly_resets || '')}</div>
  `;
}

// ── Mic Control ──────────────────────────────────────────────

const micState = { mode: 'off', lastTranscriptIdx: 0, meetingWindowOpen: false };

async function micApi(method, path) {
  // In client mode, mic server isn't local — skip
  if (state.config?.isClient) return null;
  try {
    const r = await fetch(MIC_API + path, { method });
    return await r.json();
  } catch { return null; }
}

function updateMicUI(data) {
  const dot = document.getElementById('mic-status-dot');
  const info = document.getElementById('mic-info');
  const btn = document.getElementById('mic-btn-toggle');
  const preview = document.getElementById('mic-transcript-preview');

  dot.className = 'mic-status-dot';

  if (!data) {
    info.textContent = 'Mic server offline';
    btn.textContent = 'Off';
    btn.className = 'mic-btn mic-toggle';
    return;
  }

  micState.mode = data.mode;
  const isOn = data.mode === 'on';
  btn.textContent = isOn ? 'On' : 'Off';
  btn.className = 'mic-btn mic-toggle' + (isOn ? ' selected-on' : '');

  if (!isOn) { info.textContent = 'Mic off'; preview.innerHTML = ''; return; }

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
      preview.innerHTML = texts.map(t => `<div class="mic-transcript-line">${esc(t)}</div>`).join('');
      preview.scrollTop = preview.scrollHeight;
    }
  } else if (listenerState === 'MEETING') {
    dot.classList.add('active-meeting');
    const mins = Math.floor(data.duration / 60);
    const secs = Math.floor(data.duration % 60);
    info.innerHTML = `<span style="color:var(--red)">Recording ${String(mins).padStart(2,'0')}:${String(secs).padStart(2,'0')}</span> — ${data.transcript_count} lines`;
    preview.innerHTML = '';
    if (!micState.meetingWindowOpen) { micState.meetingWindowOpen = true; window.cc.openMeeting(); }
  } else if (listenerState === 'CALIBRATING') {
    dot.classList.add('active-awake');
    info.innerHTML = '<span style="color:var(--yellow)">Calibrating...</span>';
  } else {
    if (listenerState !== 'AWAKE') dot.classList.add('active-on');
    if (data.on_last_copied) {
      info.innerHTML = '<span style="color:var(--green)">Copied to clipboard</span>';
      preview.innerHTML = `<div class="mic-transcript-line" style="color:var(--green)">${esc(data.on_last_copied)}</div>`;
    } else {
      info.textContent = 'Say "Hey Bart" to wake';
      preview.innerHTML = '';
    }
  }

  if (listenerState !== 'MEETING' && micState.meetingWindowOpen) micState.meetingWindowOpen = false;
}

async function fetchMicStatus() {
  const data = await micApi('GET', '/status');
  updateMicUI(data);

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
      let partialEl = preview.querySelector('.mic-partial');
      if (tData.partial) {
        if (!partialEl) { partialEl = document.createElement('div'); partialEl.className = 'mic-partial'; preview.appendChild(partialEl); }
        partialEl.textContent = '\u25B8 ' + tData.partial;
        preview.scrollTop = preview.scrollHeight;
      } else if (partialEl) partialEl.remove();
    }
  }
}

document.getElementById('mic-btn-toggle').addEventListener('click', async () => {
  const newMode = micState.mode === 'on' ? 'off' : 'on';
  await micApi('POST', `/mode/${newMode}`);
  micState.lastTranscriptIdx = 0;
  document.getElementById('mic-transcript-preview').innerHTML = '';
  setTimeout(fetchMicStatus, newMode === 'on' ? 1500 : 500);
});

// ── Init ──────────────────────────────────────────────────────

for (let i = 0; i < 4; i++) {
  document.getElementById(`term-${i}`).innerHTML = '<div class="cell-empty">Click a session to attach</div>';
}

loadConfig().then(() => {
  fetchSessions();
  fetchUsage();
  fetchCodexUsage();
  fetchBots();

  setInterval(fetchSessions, 5000);
  setInterval(fetchUsage, 30000);
  setInterval(fetchCodexUsage, 30000);
  setInterval(fetchBots, 10000);

  if (!state.config?.isClient) {
    // Mic only works on the server (Mac Mini)
    fetchMicStatus();
    setInterval(fetchMicStatus, 1000);
  } else {
    // Hide mic panel on client machines
    const micPanel = document.getElementById('mic-panel');
    if (micPanel) micPanel.style.display = 'none';
  }
});
