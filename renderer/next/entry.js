'use strict';

// The next view uses the same browser bridge and shared chat store as the
// existing web entry. The store remains the authority for chat state.
require('../web_cc').installWebCc();
const { filterSidebarSessions } = require('../sidebar_filter');
const { uniqueAssistantComposite } = require('../workspace_layout');
const { createSpawnCatalogLoader } = require('../spawn_catalog_loader');
const { createTerminalView } = require('../terminal_view');
const { Terminal } = require('@xterm/xterm');
const { FitAddon } = require('@xterm/addon-fit');
const { Unicode11Addon } = require('@xterm/addon-unicode11');
const { WebglAddon } = require('@xterm/addon-webgl');
const { createTerminalPaste } = require('../terminal_paste');
const layout = require('./layout');
const { mountComposer } = require('./composer');
const { mountNewChatForm } = require('./new_chat');
const hostPresentation = require('../host_presentation');
const { statusRollup, nextMode } = require('./status_card');
const store = window.PentacleChatStore;
const view = window.PentacleChatView;
const cc = window.cc;
const config = window.__PENTACLE_CONFIG__ || {};
const catalogLoader = createSpawnCatalogLoader(() => cc.chatSpawnCatalog());
const root = document.getElementById('app');
const drafts = new Map();
// Lanes are not mounted in this slice; the shared layout module retains its
// automatic-lanes rule for the slice that supplies the lane card.
const state = { selected: null, admittedSession: null, mode: 'thread', layout: { ...layout.createLayoutState(innerWidth), lanes: false, autoLanes: false, order: [] }, popover: null, hosts: {}, connected: false };
const icons = {
  chats: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 5h12a3 3 0 0 1 3 3v6a3 3 0 0 1-3 3H9l-4 3v-3a3 3 0 0 1-2-3V8a3 3 0 0 1 1-3Z"/><path d="M19 9h1a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2v2l-3-2"/></svg>',
  limits: '<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="11" width="4" height="8" rx="2"/><rect x="10" y="6" width="4" height="13" rx="2"/><rect x="17" y="14" width="4" height="5" rx="2"/></svg>',
  settings: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 7h16M4 17h16M9 4v6m6 4v6"/></svg>',
  close: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M6 6l12 12M18 6 6 18"/></svg>',
  full: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 9V4h5m6 0h5v5M20 15v5h-5M9 20H4v-5"/></svg>',
  terminal: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3.5" y="4.5" width="17" height="15" rx="1"/><path d="m7 9 3 3-3 3m5 0h4"/></svg>',
  status: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="m3 6 1.5 1.5L7 5m3 1h10M3 12l1.5 1.5L7 11m3 1h10M4.5 18h.1M10 18h10"/></svg>'
};
function button(label, content, cls = '') {
  const el = document.createElement('button'); el.type = 'button'; el.className = cls; el.setAttribute('aria-label', label); el.title = label; el.innerHTML = content; return el;
}
function node(tag, cls, parent) { const el = document.createElement(tag); el.className = cls; parent?.appendChild(el); return el; }
root.innerHTML = '<div class="next-app"><div class="stage"></div><nav class="rail" aria-label="Navigation"></nav></div>';
const stage = root.querySelector('.stage'); const rail = root.querySelector('.rail');
const assistant = node('section', 'pane', stage); const assistantThread = node('div', 'thread', assistant); const assistantInner = node('div', 'thread-inner', assistantThread); const assistantVoice = node('div', 'voice-host', assistantThread); const assistantComposer = node('div', 'composer-host', assistant);
const grip = node('div', 'grip', stage); grip.setAttribute('aria-hidden', 'true'); grip.hidden = true;
const card = node('section', 'card', stage); card.hidden = true;
const head = node('header', 'card-head', card); const title = node('div', 'card-title', head);
const statusButton = button('Status', icons.status, 'icon-button'); const terminalButton = button('Terminal', icons.terminal, 'icon-button'); const fullButton = button('Full screen', icons.full, 'icon-button fullscreen'); const closeButton = button('Close', icons.close, 'icon-button'); head.append(statusButton, terminalButton, fullButton, closeButton);
const cardBody = node('div', 'thread', card); const cardInner = node('div', 'thread-inner', cardBody); const cardVoice = node('div', 'voice-host', cardBody); const cardComposer = node('div', 'composer-host', card);
const terminalMount = node('div', 'terminal'); card.insertBefore(terminalMount, cardComposer); terminalMount.hidden = true;
const statusMount = node('div', 'status'); card.insertBefore(statusMount, cardComposer); statusMount.hidden = true;
const newChatMount = node('div', 'new-chat-host', card); newChatMount.hidden = true;
const brand = button('Assistant', '<span class="logo"></span>', 'rail-button brand on'); const group = node('div', 'rail-group', rail); const chatsButton = button('Chats', icons.chats, 'rail-button'); group.append(chatsButton); rail.prepend(brand); node('div', 'rail-spacer', rail); const limitsButton = button('Limits and machines', icons.limits, 'rail-button'); const settingsButton = button('Settings', icons.settings, 'rail-button'); rail.append(limitsButton, settingsButton);
const popover = node('div', 'popover', root.querySelector('.next-app')); popover.hidden = true;
let assistantComposerControl = null; let assistantComposerStream = null; let cardComposerControl = null;
function mountAssistantComposer(id) { if (!id || id === assistantComposerStream) return; assistantComposerControl?.dispose(); assistantComposerStream = id; assistantComposerControl = mountComposer({ host: assistantComposer, transcript: assistantVoice, streamId: id, drafts, cc, chatStore: store }); }
function mountCardComposer(id) { cardComposerControl?.dispose(); cardComposerControl = id ? mountComposer({ host: cardComposer, transcript: cardVoice, streamId: id, drafts, cc, chatStore: store }) : null; }
function closePopover() { state.popover = null; popover.hidden = true; chatsButton.classList.remove('open'); limitsButton.classList.remove('open'); settingsButton.classList.remove('open'); }
function togglePopover(name) { if (state.popover === name) { closePopover(); return; } state.popover = name; popover.hidden = false; popover.classList.toggle('bottom', name !== 'chats'); renderPopover(); }
function sessions() { return store.getState().sessions || []; }
function selectedSession() { return sessions().find(s => s.stream_id === state.selected) || (state.admittedSession?.stream_id === state.selected ? state.admittedSession : null); }
function assistantSession() { return uniqueAssistantComposite(sessions()); }
function requestHistory(id) { if (id) cc.requestStreamEvents({ streamId: id }).catch(() => {}); }
function paintTranscript(element, id) {
  element.dataset.streamId = id || '';
  if (!id) { element.textContent = 'Loading chat…'; return; }
  const detail = store.selectSessionDetail(id, { visibleCount: 120, includeDraft: false });
  const html = view.renderTranscriptTimelineHtml(detail);
  if (element.dataset.rendered !== html) { const scroller = element.parentElement; const stick = !element.dataset.rendered || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80; element.innerHTML = html || '<div class="empty">No messages yet.</div>'; element.dataset.rendered = html; void view.hydrateFileAttachments(element); if (stick) scroller.scrollTop = scroller.scrollHeight; }
}
for (const transcript of [assistantInner, cardInner]) transcript.addEventListener('click', event => {
  const copy = event.target.closest('.slot-chat-copy-btn');
  if (copy) void navigator.clipboard.writeText(copy.dataset.copyText || '');
  const retry = event.target.closest('.slot-chat-send-retry');
  if (retry) store.retryOptimisticSend(retry.dataset.optimisticId);
  const sendNow = event.target.closest('.slot-chat-send-now');
  if (sendNow) store.sendQueuedNow(sendNow.dataset.streamId, sendNow.dataset.optimisticId);
});
function renderPopover() {
  popover.replaceChildren();
  if (state.popover === 'chats') {
    const newChat = button('New chat', 'New chat'); newChat.onclick = () => { closePopover(); openNewChat(); }; popover.append(newChat);
    const visible = filterSidebarSessions(sessions()).filter(s => s.session_kind !== 'assistant_composite').sort ( (a,b) => String(b.last_activity || b.last_event_at || '').localeCompare(String(a.last_activity || a.last_event_at || '')) );
    if (!visible.length) { const empty = node('div', 'empty', popover); empty.textContent = 'No chats yet.'; }
    const draftsByStream = store.getState().drafts || {};
    for (const session of visible) { const row = button(session.display_name || session.title || session.session_name, ''); row.dataset.streamId = session.stream_id; if (session.working || draftsByStream[session.stream_id]?.raw?.working) node('span', 'live-dot', row); row.append(document.createTextNode(session.display_name || session.title || session.session_name)); row.onclick = () => { closePopover(); openChat(session.stream_id); }; popover.append(row); }
  } else if (state.popover === 'settings') {
    for (const theme of ['dark', 'light', 'cosmic']) { const row = button(theme, `<span class="theme-swatch sw-${theme}" aria-hidden="true"></span>${theme[0].toUpperCase() + theme.slice(1)}`); row.onclick = () => { document.documentElement.classList.add('swap'); document.documentElement.dataset.theme = theme; localStorage.setItem('pentacle.next.theme', theme); requestAnimationFrame(() => document.documentElement.classList.remove('swap')); }; popover.append(row); }
  } else if (state.popover === 'limits') {
    for (const limit of store.getLimits()) { const label = node('div', 'popover-title', popover); label.textContent = `${limit.label} ${limit.pct == null ? '—' : `${Math.round(limit.pct)}%`}`; const bar = node('div', 'meter', popover); const fill = node('span', '', bar); fill.style.width = `${Math.max(0, Math.min(100, limit.pct || 0))}%`; }
    for (const [host, stats] of Object.entries(state.hosts)) { const row = node('div', 'popover-title', popover); const pct = (used, total) => total > 0 ? `${Math.round(used / total * 100)}%` : '—'; row.textContent = `${host} · CPU ${stats.cpu_usage_pct == null ? '—' : `${Math.round(stats.cpu_usage_pct)}%`} · RAM ${pct(stats.memory_used_bytes,stats.memory_total_bytes)} · Disk ${pct(stats.disk_used_bytes,stats.disk_total_bytes)}`; }
  }
}
function render() {
  const a = assistantSession(); paintTranscript(assistantInner, a?.stream_id); mountAssistantComposer(a?.stream_id);
  const assistantName = a?.assistant_display_name || a?.display_name || a?.title || 'Assistant'; brand.setAttribute('aria-label', assistantName); brand.title = assistantName;
  const s = selectedSession();
  if (state.selected && s) { title.textContent = s.display_name || s.title || s.session_name; statusButton.hidden = !s.status_card; paintTranscript(cardInner, s.stream_id); if (state.mode === 'status') renderStatus(s); }
  const meters = document.createElement('span'); meters.className = 'rail-meters'; meters.setAttribute('aria-hidden', 'true'); for (const limit of store.getLimits()) { const bar = document.createElement('i'); bar.style.height = `${Math.max(3, Math.round((limit.pct || 0) * 0.13) + 3)}px`; meters.append(bar); } limitsButton.replaceChildren(meters);
  if (state.popover) renderPopover();
}
let terminal = null; let terminalGeneration = 0; let terminalQueue = Promise.resolve();
function disposeTerminal() { terminalGeneration++; if (terminal) { terminal.observer?.disconnect(); terminal.term.dispose(); terminal = null; } terminalQueue = terminalQueue.then(() => cc.killPty(0)).catch(() => {}); terminalMount.replaceChildren(); }
cc.onPtyData((slot, data) => { if (slot === 0 && terminal) terminal.term.write(data); });
async function openTerminal(session) {
  disposeTerminal(); const generation = terminalGeneration;
  await terminalQueue;
  if (generation !== terminalGeneration) return;
  const style = getComputedStyle(document.documentElement);
  const pair = createTerminalView(terminalMount, { background: style.getPropertyValue('--term-bg').trim(), foreground: style.getPropertyValue('--term-text').trim(), cursor: style.getPropertyValue('--accent').trim() }, { Terminal, FitAddon, Unicode11Addon, WebglAddon });
  terminal = pair; await new Promise(resolve => requestAnimationFrame(resolve));
  if (generation !== terminalGeneration) return;
  pair.fitAddon.fit();
  const paste = createTerminalPaste({ term: pair.term, readClipboard: () => cc.readClipboard(), pastePty: text => cc.pastePty(0, text), isCurrent: () => generation === terminalGeneration && !!terminal?.paneId });
  pair.term.element.addEventListener('paste', paste.nativePaste, { capture: true });
  pair.term.attachCustomKeyEventHandler(event => paste.key(event, /mac/i.test(navigator.platform)));
  pair.term.onData(data => { if (!paste.captureData(data)) { cc.exitCopyMode(0); cc.writePty(0, data); } });
  let paneId;
  const hostId = (config.hostIds || ['local']).find(id => hostPresentation.streamHost(config, id).toLowerCase() === String(session.host || '').toLowerCase()) || session.hostId || session.host;
  terminalQueue = cc.createPty(0, session.session_name || session.name, hostId, pair.term.cols, pair.term.rows).then(id => { paneId = id; });
  await terminalQueue;
  if (generation !== terminalGeneration) return;
  pair.paneId = paneId;
  pair.observer = new ResizeObserver(() => { if (generation !== terminalGeneration) return; const cols = pair.term.cols, rows = pair.term.rows; pair.fitAddon.fit(); if (cols !== pair.term.cols || rows !== pair.term.rows) cc.resizePty(0, pair.term.cols, pair.term.rows); });
  pair.observer.observe(terminalMount);
}
function renderStatus(session) {
  const c = session.status_card; statusMount.replaceChildren(); if (!c) return;
  const plan = Array.isArray(c.plan) ? c.plan.filter(Boolean) : []; const rollup = statusRollup(c);
  const top = node('div', 'status-top', statusMount); const chip = node('span', 'status-chip', top); chip.textContent = rollup.label; chip.hidden = !plan.length;
  if (c.updated_at && Number.isFinite(Date.parse(c.updated_at))) { const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(c.updated_at)) / 1000)); const age = node('span', 'status-age', top); age.textContent = `updated ${seconds < 60 ? 'just now' : seconds < 3600 ? `${Math.floor(seconds / 60)}m ago` : `${Math.floor(seconds / 3600)}h ago`}`; }
  const add = (heading, text) => { if (!text) return; const h = node('h2', '', statusMount); h.textContent = heading; const p = node('p', '', statusMount); p.textContent = text; };
  add('Goal', c.goal); if (plan.length) { const h = node('h2', '', statusMount); h.textContent = 'Plan'; const bar = node('div', 'meter', statusMount); const fill = node('span', '', bar); fill.style.width = `${rollup.percent}%`; const list = node('ol', '', statusMount); for (const step of plan) { const item = node('li', step.status || 'pending', list); const glyph = node('span', 'status-glyph', item); glyph.textContent = step.status === 'done' ? '✓' : step.status === 'active' ? '◉' : '○'; item.append(document.createTextNode(step.text)); } } add('Latest update', c.update);
}
function setMode(mode) { const s = selectedSession(); if (!s) return; const next = nextMode(state.mode, mode); if (next === 'terminal') void cardComposerControl?.cancelRecording(); if (state.mode === 'terminal' && next !== 'terminal') disposeTerminal(); state.mode = next; cardBody.hidden = state.mode !== 'thread'; terminalMount.hidden = state.mode !== 'terminal'; statusMount.hidden = state.mode !== 'status'; terminalButton.classList.toggle('active', state.mode === 'terminal'); statusButton.classList.toggle('active', state.mode === 'status'); if (state.mode === 'terminal') void openTerminal(s); if (state.mode === 'status') renderStatus(s); }
function syncLayout() { const visible = layout.fits(state.layout, innerWidth); assistant.hidden = !visible.assistant; card.hidden = !visible.cards.includes('chat'); grip.hidden = assistant.hidden || card.hidden || innerWidth < 600; const width = Math.max(280, Math.min(state.layout.assistantWidth || 0, stage.clientWidth - 312)); assistant.style.flex = state.layout.assistantWidth && !grip.hidden ? `0 0 ${width}px` : ''; brand.classList.toggle('on', visible.assistant); chatsButton.classList.toggle('on', !card.hidden); }
grip.addEventListener('pointerdown', event => { event.preventDefault(); grip.setPointerCapture(event.pointerId); document.body.classList.add('dragging'); });
grip.addEventListener('pointermove', event => { if (!grip.hasPointerCapture(event.pointerId)) return; state.layout = layout.setAssistantWidth(state.layout, event.clientX - stage.getBoundingClientRect().left, stage.clientWidth, innerWidth); syncLayout(); });
grip.addEventListener('pointerup', event => { if (grip.hasPointerCapture(event.pointerId)) grip.releasePointerCapture(event.pointerId); document.body.classList.remove('dragging'); });
let newChatControl = null; let newChatGeneration = 0;
function leaveNewChat() { newChatGeneration++; newChatControl?.destroy(); newChatControl = null; newChatMount.hidden = true; head.hidden = false; cardComposer.hidden = false; }
function openChat(id, admittedSession = null) { leaveNewChat(); disposeTerminal(); state.selected = id; state.admittedSession = admittedSession; state.mode = 'thread'; terminalButton.hidden = false; cardBody.hidden = false; terminalMount.hidden = statusMount.hidden = true; state.layout = layout.show(state.layout, 'chat', id, innerWidth); mountCardComposer(id); syncLayout(); requestHistory(id); render(); }
async function openNewChat() {
  leaveNewChat(); disposeTerminal(); mountCardComposer(null); state.selected = null; state.mode = 'new'; state.layout = layout.show(state.layout, 'chat', '__new__', innerWidth); syncLayout();
  const generation = newChatGeneration; title.textContent = ''; statusButton.hidden = terminalButton.hidden = true; cardBody.hidden = terminalMount.hidden = statusMount.hidden = cardComposer.hidden = true; newChatMount.hidden = false; newChatMount.textContent = 'Loading…';
  try {
    const response = await catalogLoader.load(); if (generation !== newChatGeneration) return;
    if (!response?.ok) throw new Error(response?.error || 'Unable to load models');
    const hostIds = config.hostIds || Object.keys(config.hosts || {});
    newChatControl = mountNewChatForm(newChatMount, { catalog: response.catalog, machineOptions: hostIds.map(id => ({ id, label: config.hostNames?.[id] || id })), localHostId: config.chatStream?.localHost || config.localHostId || 'local', accountLabels: config.chatStream?.accountLabels || {}, spawn: request => cc.chatSpawnV2(request), store,
      onStarted: ({ streamId, session }) => openChat(streamId, session) });
  } catch (error) { if (generation === newChatGeneration) newChatMount.textContent = error.message || 'Unable to load models'; }
}
function closeCard() { leaveNewChat(); disposeTerminal(); mountCardComposer(null); state.selected = null; state.mode = 'thread'; state.layout = layout.shut(state.layout, 'chat', innerWidth); syncLayout(); render(); }
brand.onclick = () => { closePopover(); const wasSolo = !!state.layout.solo; state.layout = layout.toAssistant(state.layout, innerWidth); if (!wasSolo && !state.layout.chat) { leaveNewChat(); disposeTerminal(); mountCardComposer(null); state.selected = null; } syncLayout(); render(); };
chatsButton.onclick = () => togglePopover('chats'); limitsButton.onclick = () => togglePopover('limits'); settingsButton.onclick = () => togglePopover('settings'); closeButton.onclick = closeCard;
terminalButton.onclick = () => setMode('terminal'); statusButton.onclick = () => setMode('status');
fullButton.onclick = () => { state.layout = layout.toggleFullScreen(state.layout, 'chat', innerWidth); syncLayout(); };
document.addEventListener('keydown', event => { if (event.key !== 'Escape') return; if (state.popover) closePopover(); else if (state.layout.solo) { state.layout = layout.toggleFullScreen(state.layout, 'chat', innerWidth); syncLayout(); } });
document.addEventListener('click', event => { if (state.popover && !popover.contains(event.target) && !rail.contains(event.target)) closePopover(); });
window.addEventListener('resize', () => syncLayout());
assistant.addEventListener('focusin', () => { state.layout = layout.markUsed(state.layout, 'assistant'); });
card.addEventListener('focusin', () => { state.layout = layout.markUsed(state.layout, 'chat'); });
assistant.addEventListener('pointerdown', () => { state.layout = layout.markUsed(state.layout, 'assistant'); });
card.addEventListener('pointerdown', () => { state.layout = layout.markUsed(state.layout, 'chat'); });
const theme = localStorage.getItem('pentacle.next.theme'); if (['dark','light','cosmic'].includes(theme)) document.documentElement.dataset.theme = theme;
let assistantHistory = null;
store.subscribe(() => { const id = assistantSession()?.stream_id; if (id && id !== assistantHistory) { assistantHistory = id; requestHistory(id); } render(); });
let recoveryEpoch = null; let recoveryPending = false; let readySignals = 0;
cc.onChatStreamFrame(frame => { store.applyFrame(frame); if (frame?.type === 'hosts.stats' && frame.hosts) state.hosts = frame.hosts; if (state.popover === 'limits') renderPopover(); if (frame?.connected === true || frame?.type === '__reconnect') { readySignals++; if (recoveryEpoch !== null) void loadSnapshot().catch(() => {}); } });
async function loadSnapshot() {
  if (recoveryPending) return; recoveryPending = true;
  const signal = readySignals;
  try {
    const connection = cc.webConnectionState?.(); const snapshot = await cc.getChatStreamState();
    const current = cc.webConnectionState?.(); if (connection && (!current?.open || current.epoch !== connection.epoch)) return;
    state.connected = !!snapshot?.connected; state.hosts = snapshot?.hosts_stats || {}; store.applyFrame({ type: 'snapshot', ...(snapshot || {}) });
    if (snapshot?.connected && recoveryEpoch !== null && current?.epoch === recoveryEpoch) {
      const unresolved = Object.values(store.getState().optimisticSends || {}).filter(send => ['queued','dispatched','indeterminate'].includes(send.status));
      for (const id of new Set(unresolved.map(send => send.stream_id))) { const result = await cc.requestStreamEvents({ streamId:id, limit:1 }); if (!result?.ok) return; }
      if (cc.webConnectionState?.()?.epoch === recoveryEpoch) { store.webHostReady?.(recoveryEpoch); recoveryEpoch = null; }
      if (assistantSession()?.stream_id) requestHistory(assistantSession().stream_id); if (state.selected) requestHistory(state.selected);
    }
    render();
  } finally { recoveryPending = false; if (recoveryEpoch !== null && readySignals !== signal && cc.webConnectionState?.()?.open) void loadSnapshot().catch(() => {}); }
}
cc.onDisconnect?.(({ epoch }) => { recoveryEpoch = null; store.webHostDisconnected?.(epoch); });
cc.onReconnect?.(({ epoch }) => { recoveryEpoch = epoch; void loadSnapshot().catch(() => {}); });
void loadSnapshot().catch(() => render());
