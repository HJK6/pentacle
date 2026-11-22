'use strict';

// The next view uses the same browser bridge and shared chat store as the
// existing web entry. The store remains the authority for chat state.
require('../web_cc').installWebCc();
const { filterSidebarSessions } = require('../sidebar_filter');
const { uniqueAssistantComposite } = require('../workspace_layout');
const { createSpawnCatalogLoader } = require('../spawn_catalog_loader');
const { createTerminalView, disposeTerminalView } = require('../terminal_view');
const { Terminal } = require('@xterm/xterm');
const { FitAddon } = require('@xterm/addon-fit');
const { Unicode11Addon } = require('@xterm/addon-unicode11');
const { WebglAddon } = require('@xterm/addon-webgl');
const { createTerminalPaste } = require('../terminal_paste');
const layout = require('./layout');
const { createDesignViewer } = require('./design_viewer');
const { initialDesignRoute, replaceDesignAddress } = require('./design_location');
const { mountComposer } = require('./composer');
const { mountNewChatForm } = require('./new_chat');
const { mountLimitsView } = require('./limits_view');
const hostPresentation = require('../host_presentation');
const { statusRollup, nextMode } = require('./status_card');
const { createLaneReads } = require('./lane_reads');
const { linkAssistantNames } = require('./lane_links');
const { createQuestions } = require('./questions');
const { createQuestionNotifications } = require('./question_notifications');
const { mountLanesCard, renderLaneIcon } = require('./lanes');
const { applyVersionedConnectionState } = require('../chat_stream_connection_state');
const store = window.PentacleChatStore;
const view = window.PentacleChatView;
const core = window.PentacleChatCore;
const cc = window.cc;
const config = window.__PENTACLE_CONFIG__ || {};
const catalogLoader = createSpawnCatalogLoader(() => cc.chatSpawnCatalog());
const root = document.getElementById('app');
const drafts = new Map();
let laneInventory = core.emptyWorkLanesInventory();
let laneInventoryCurrent = false;
let pendingLaneLink = new URLSearchParams(location.search).get('lane');
const laneReads = createLaneReads({ show: (laneId, options) => cc.workLanesShow(laneId, options) });
const laneConnection = { stateVersion: null, connected: false, error: null };
const laneListeners = new Set();
let lanesControl = null;
let questionsControl = null;
const questionNotifications = createQuestionNotifications(() => questionsControl?.update({ notifications: questionNotifications.values() }), { promptList: args => cc.promptList(args) });
function applyLaneFrame(frame) {
  if (!frame) return;
  if (frame.connected === false) {
    if (!applyVersionedConnectionState(laneConnection, frame, () => {})) return;
    laneInventoryCurrent = false;
    lanesControl?.render();
    return;
  }
  if (frame.type === 'work_lanes.inventory') {
    if (!laneConnection.connected || recoveryEpoch !== null) return;
  } else {
    // Connection snapshots carry state_version; daemon inventory pushes do not.
    if (frame.connected !== true || !applyVersionedConnectionState(laneConnection, frame, () => {})) return;
    if (!frame.work_lanes) return;
  }
  const next = core.applyWorkLanesFrame(laneInventory, frame);
  if (next === laneInventory) return;
  laneInventory = next;
  laneInventoryCurrent = laneConnection.connected;
  laneReads.updateInventory(next);
  for (const listener of laneListeners) listener(next);
  if (laneInventoryCurrent && pendingLaneLink && next.lanes.some(lane => lane.lane_id === pendingLaneLink)) {
    openLaneTarget(pendingLaneLink);
    pendingLaneLink = null;
  }
}
const state = { selected: null, admittedSession: null, mode: 'thread', layout: layout.createLayoutState(innerWidth), popover: null, hosts: {}, connected: false };
const icons = {
  lanes: '<svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 12m-2.2 0a2.2 2.2 0 1 0 4.4 0a2.2 2.2 0 1 0-4.4 0M5 6m-1.8 0a1.8 1.8 0 1 0 3.6 0a1.8 1.8 0 1 0-3.6 0M19 6m-1.8 0a1.8 1.8 0 1 0 3.6 0a1.8 1.8 0 1 0-3.6 0M12 20m-1.8 0a1.8 1.8 0 1 0 3.6 0a1.8 1.8 0 1 0-3.6 0M6.4 7.2l3.9 3.3M17.6 7.2l-3.9 3.3M12 14.2v4"/></svg>',
  questions: '<svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM9.5 9.6a2.5 2.5 0 1 1 3.7 2.2c-.8.5-1.2 1-1.2 1.8M12 16.8h.01"/></svg>',
  dashboards: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="4" y="4" width="7" height="7" rx="1.6"/><rect x="13" y="4" width="7" height="7" rx="1.6"/><rect x="4" y="13" width="7" height="7" rx="1.6"/><rect x="13" y="13" width="7" height="7" rx="1.6"/></svg>',
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
const designCard = node('section', 'card design-card', stage); designCard.hidden = true;
designCard.setAttribute('aria-label', 'Design');
const designViewer = createDesignViewer(designCard, {
  onNavigate: target => replaceDesignAddress(window, target, 'workspace'),
  onFullScreen: () => { state.layout = layout.toggleFullScreen(state.layout, 'dash', innerWidth); syncLayout(); },
  onClose: closeDesign,
});
let designOpen = false;
const lanesCard = node('section', 'card lanes-card', stage); lanesCard.hidden = true;
const brand = button('Assistant', '<span class="logo"></span>', 'rail-button brand on'); const group = node('div', 'rail-group', rail); const lanesButton = button('Lanes', icons.lanes, 'rail-button'); const dashboardsButton = button('Dashboards', icons.dashboards, 'rail-button'); const chatsButton = button('Chats', icons.chats, 'rail-button'); const questionsButton = button('Questions', icons.questions, 'rail-button'); group.append(lanesButton, dashboardsButton, chatsButton, questionsButton); rail.prepend(brand); node('div', 'rail-spacer', rail); const limitsButton = button('Limits and machines', icons.limits, 'rail-button'); const settingsButton = button('Settings', icons.settings, 'rail-button'); rail.append(limitsButton, settingsButton);
const popover = node('div', 'popover', root.querySelector('.next-app')); popover.hidden = true;
let assistantComposerControl = null; let assistantComposerStream = null; let cardComposerControl = null; let limitsControl = null;
let unmountQuestionList = null;
function mountAssistantComposer(id) { if (!id || id === assistantComposerStream) return; assistantComposerControl?.dispose(); assistantComposerStream = id; assistantComposerControl = mountComposer({ host: assistantComposer, transcript: assistantVoice, streamId: id, drafts, cc, chatStore: store }); }
function mountCardComposer(id) { cardComposerControl?.dispose(); cardComposerControl = id ? mountComposer({ host: cardComposer, transcript: cardVoice, streamId: id, drafts, cc, chatStore: store }) : null; }
function closePopover() { unmountQuestionList?.(); unmountQuestionList = null; limitsControl?.destroy(); limitsControl = null; state.popover = null; popover.hidden = true; chatsButton.classList.remove('open'); questionsButton.classList.remove('open'); limitsButton.classList.remove('open'); settingsButton.classList.remove('open'); }
function togglePopover(name) { if (state.popover === name) { closePopover(); return; } closePopover(); state.popover = name; popover.hidden = false; ({ chats: chatsButton, questions: questionsButton, limits: limitsButton, settings: settingsButton })[name].classList.add('open'); popover.classList.toggle('bottom', name === 'limits' || name === 'settings'); popover.classList.toggle('questions-popover', name === 'questions'); popover.classList.toggle('limits-popover', name === 'limits'); renderPopover(); }
function sessions() { return store.getState().sessions || []; }
function selectedSession() { return sessions().find(s => s.stream_id === state.selected) || (state.admittedSession?.stream_id === state.selected ? state.admittedSession : null); }
function assistantSession() { return uniqueAssistantComposite(sessions()); }
function requestHistory(id) { if (id) cc.requestStreamEvents({ streamId: id }).catch(() => {}); }
function laneNameTargets() {
  return laneInventory.lanes.flatMap(lane => {
    const detail = laneReads.getRead(lane.lane_id, 'goals').reply;
    const goals = detail?.projection?.goals || lane.goals || [];
    return [{ title: lane.title, laneId: lane.lane_id }, ...goals.map(goal => ({ title: goal.title, laneId: lane.lane_id, goalId: goal.goal_id }))];
  });
}
function openLaneTarget(laneId, goalId) {
  if (!lanesControl || !laneId) return;
  if (goalId) lanesControl.openGoal(laneId, goalId); else lanesControl.openLane(laneId);
  state.layout = layout.show(state.layout, 'lanes', true, innerWidth); syncLayout();
}
function paintTranscript(element, id) {
  element.dataset.streamId = id || '';
  if (!id) { element.textContent = 'Loading chat…'; return; }
  const detail = store.selectSessionDetail(id, { visibleCount: 120, includeDraft: false });
  const html = view.renderTranscriptTimelineHtml(detail);
  const targets = element === assistantInner ? laneNameTargets() : [];
  const linkVersion = JSON.stringify(targets);
  if (element.dataset.rendered !== html || element.dataset.linkVersion !== linkVersion) { const scroller = element.parentElement; const stick = !element.dataset.rendered || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80; element.innerHTML = html || '<div class="empty">No messages yet.</div>'; element.dataset.rendered = html; element.dataset.linkVersion = linkVersion; if (element === assistantInner) { linkAssistantNames(element, targets); for (const update of element.querySelectorAll('.slot-chat-lane-update[data-lane-id]')) { update.tabIndex = 0; update.setAttribute('role', 'button'); } } void view.hydrateFileAttachments(element); if (stick) scroller.scrollTop = scroller.scrollHeight; }
}
for (const transcript of [assistantInner, cardInner]) transcript.addEventListener('click', event => {
  const laneLink = event.target.closest('.next-lane-name-link');
  if (laneLink && transcript === assistantInner) { openLaneTarget(laneLink.dataset.laneId, laneLink.dataset.goalId); return; }
  const laneUpdate = event.target.closest('.slot-chat-lane-update[data-lane-id]');
  if (laneUpdate && transcript === assistantInner) { openLaneTarget(laneUpdate.dataset.laneId); return; }
  const copy = event.target.closest('.slot-chat-copy-btn');
  if (copy) void navigator.clipboard.writeText(copy.dataset.copyText || '');
  const retry = event.target.closest('.slot-chat-send-retry');
  if (retry) store.retryOptimisticSend(retry.dataset.optimisticId);
  const sendNow = event.target.closest('.slot-chat-send-now');
  if (sendNow) store.sendQueuedNow(sendNow.dataset.streamId, sendNow.dataset.optimisticId);
});
assistantInner.addEventListener('keydown', event => { const update = event.target.closest('.slot-chat-lane-update[data-lane-id]'); if (update && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); openLaneTarget(update.dataset.laneId); } });
function renderPopover() {
  if (state.popover === 'limits' && limitsControl) { limitsControl.update({ limits: store.getLimits(), hosts: state.hosts }); return; }
  popover.replaceChildren();
  if (state.popover === 'chats') {
    const newChat = button('New chat', 'New chat'); newChat.onclick = () => { closePopover(); openNewChat(); }; popover.append(newChat);
    const visible = filterSidebarSessions(sessions()).filter(s => s.session_kind !== 'assistant_composite').sort ( (a,b) => String(b.last_activity || b.last_event_at || '').localeCompare(String(a.last_activity || a.last_event_at || '')) );
    if (!visible.length) { const empty = node('div', 'empty', popover); empty.textContent = 'No chats yet.'; }
    const draftsByStream = store.getState().drafts || {};
    for (const session of visible) { const row = button(session.display_name || session.title || session.session_name, ''); row.dataset.streamId = session.stream_id; if (session.working || draftsByStream[session.stream_id]?.raw?.working) node('span', 'live-dot', row); row.append(document.createTextNode(session.display_name || session.title || session.session_name)); row.onclick = () => { closePopover(); openChat(session.stream_id); }; popover.append(row); }
  } else if (state.popover === 'questions') {
    unmountQuestionList = questionsControl?.mountList(popover) || null;
  } else if (state.popover === 'settings') {
    const heading = node('div', 'theme-heading', popover); heading.textContent = 'Theme'; const grid = node('div', 'themes', popover); const selected = document.documentElement.dataset.theme || 'dark';
    for (const theme of ['dark', 'light', 'cosmic']) { const row = button(theme, `<span class="theme-preview sw-${theme}" aria-hidden="true"><span class="theme-mini"><i></i><b></b></span></span><span>${theme[0].toUpperCase() + theme.slice(1)}</span>`, `theme-choice${selected === theme ? ' on' : ''}`); row.setAttribute('aria-pressed', String(selected === theme)); row.onclick = () => { document.documentElement.classList.add('swap'); document.documentElement.dataset.theme = theme; localStorage.setItem('pentacle.next.theme', theme); renderPopover(); requestAnimationFrame(() => document.documentElement.classList.remove('swap')); }; grid.append(row); }
  } else if (state.popover === 'limits') {
    const hostIds = Array.isArray(config.hostIds) && config.hostIds.length ? config.hostIds : ['local'];
    const hostOrder = hostIds.map(id => hostPresentation.streamHost(config, id));
    limitsControl = mountLimitsView(popover, { localHost: hostPresentation.streamHost(config, 'local'), hostOrder,
      hostLabel: host => { const id = hostIds.find(item => hostPresentation.streamHost(config, item).toLowerCase() === String(host).toLowerCase()) || host; return hostPresentation.hostLabel(config, id); } });
    limitsControl.update({ limits: store.getLimits(), hosts: state.hosts });
  }
}
function render() {
  const a = assistantSession(); paintTranscript(assistantInner, a?.stream_id); mountAssistantComposer(a?.stream_id);
  const assistantName = a?.assistant_display_name || a?.display_name || a?.title || 'Assistant'; brand.setAttribute('aria-label', assistantName); brand.title = assistantName;
  const s = selectedSession();
  if (state.selected && s) { title.textContent = s.display_name || s.title || s.session_name; statusButton.hidden = !s.status_card; paintTranscript(cardInner, s.stream_id); if (state.mode === 'status') renderStatus(s); }
  const meters = document.createElement('span'); meters.className = 'rail-meters'; meters.setAttribute('aria-hidden', 'true'); for (const limit of store.getLimits()) { const bar = document.createElement('i'); bar.style.height = `${Math.max(3, Math.round((limit.pct || 0) * 0.13) + 3)}px`; meters.append(bar); } limitsButton.replaceChildren(meters);
  if (state.popover && state.popover !== 'questions') renderPopover();
}
let terminal = null; let terminalGeneration = 0; let terminalQueue = Promise.resolve();
function disposeTerminal() { terminalGeneration++; const previous = terminal; terminal = null; disposeTerminalView(previous); terminalQueue = terminalQueue.then(() => cc.killPty(0)).catch(() => {}); terminalMount.replaceChildren(); }
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
function setMode(mode) { const s = selectedSession(); if (!s) return; const next = nextMode(state.mode, mode); if (next === 'terminal') void cardComposerControl?.cancelRecording(); if (state.mode === 'terminal' && next !== 'terminal') disposeTerminal(); state.mode = next; cardBody.hidden = state.mode !== 'thread'; cardComposer.hidden = state.mode === 'terminal'; terminalMount.hidden = state.mode !== 'terminal'; statusMount.hidden = state.mode !== 'status'; terminalButton.classList.toggle('active', state.mode === 'terminal'); statusButton.classList.toggle('active', state.mode === 'status'); if (state.mode === 'terminal') void openTerminal(s); if (state.mode === 'status') renderStatus(s); }
function syncLayout() {
  const visible = layout.fits(state.layout, innerWidth);
  assistant.hidden = !visible.assistant;
  card.hidden = !visible.cards.includes('chat');
  designCard.hidden = !visible.cards.includes('dash');
  stage.classList.toggle('has-design', !designCard.hidden);
  const full = designCard.querySelector('.design-full');
  const fullLabel = state.layout.solo === 'dash' ? 'Leave full screen' : 'Full screen';
  full.setAttribute('aria-label', fullLabel);
  full.title = fullLabel;
  lanesCard.hidden = !visible.cards.includes('lanes');
  lanesCard.classList.toggle('alone', !lanesCard.hidden && visible.cards.length === 1 && assistant.hidden);
  grip.hidden = assistant.hidden || !visible.cards.length || innerWidth < layout.PHONE_WIDTH;
  const cardsWidth = visible.cards.reduce((sum, key) => sum + layout.MIN_WIDTH[key], 0);
  const width = Math.max(layout.MIN_WIDTH.assistant, Math.min(state.layout.assistantWidth || 0, stage.clientWidth - cardsWidth));
  assistant.style.flex = state.layout.assistantWidth && !grip.hidden ? `0 0 ${width}px` : '';
  brand.classList.toggle('on', visible.assistant);
  chatsButton.classList.toggle('on', !card.hidden);
  dashboardsButton.classList.toggle('on', !designCard.hidden);
  dashboardsButton.setAttribute('aria-pressed', String(!designCard.hidden));
  lanesButton.classList.toggle('on', !lanesCard.hidden);
  if (designOpen && !state.layout.dash) {
    designOpen = false;
    designViewer.close();
    history.replaceState(history.state, '', '/next/');
  }
}

function openDesign(target = null) {
  closePopover();
  state.layout = layout.show(state.layout, 'dash', 'design', innerWidth);
  syncLayout();
  designOpen = true;
  void designViewer.open(target);
}

function closeDesign() {
  state.layout = layout.shut(state.layout, 'dash', innerWidth);
  syncLayout();
}

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
    const response = await catalogLoader.load({ refresh: true }); if (generation !== newChatGeneration) return;
    if (!response?.ok) throw new Error(response?.error || 'Unable to load models');
    const hostIds = Array.isArray(config.hostIds) && config.hostIds.length ? config.hostIds : ['local'];
    newChatControl = mountNewChatForm(newChatMount, { catalog: response.catalog, machineOptions: hostIds.map(id => ({ id, label: hostPresentation.hostLabel(config, id) })), localHostId: config.chatStream?.localHost || config.localHostId || 'local', accountLabels: config.chatStream?.accountLabels || {}, spawn: request => cc.chatSpawnV2(request), store,
      onStarted: ({ streamId, session }) => openChat(streamId, session) });
  } catch (error) { if (generation === newChatGeneration) newChatMount.textContent = error.message || 'Unable to load models'; }
}
function closeCard() { leaveNewChat(); disposeTerminal(); mountCardComposer(null); state.selected = null; state.mode = 'thread'; state.layout = layout.shut(state.layout, 'chat', innerWidth); syncLayout(); render(); }
brand.onclick = () => { closePopover(); const wasSolo = !!state.layout.solo; state.layout = layout.toAssistant(state.layout, innerWidth); if (!wasSolo && !state.layout.chat) { leaveNewChat(); disposeTerminal(); mountCardComposer(null); state.selected = null; } syncLayout(); render(); };
lanesButton.onclick = () => { closePopover(); state.layout = layout.fits(state.layout, innerWidth).cards.includes('lanes') ? layout.shut(state.layout, 'lanes', innerWidth) : layout.show(state.layout, 'lanes', true, innerWidth); syncLayout(); };
dashboardsButton.onclick = () => {
  closePopover();
  if (designCard.hidden) openDesign();
  else closeDesign();
};
chatsButton.onclick = () => togglePopover('chats'); questionsButton.onclick = () => togglePopover('questions'); limitsButton.onclick = () => togglePopover('limits'); settingsButton.onclick = () => togglePopover('settings'); closeButton.onclick = closeCard;
terminalButton.onclick = () => setMode('terminal'); statusButton.onclick = () => setMode('status');
fullButton.onclick = () => { state.layout = layout.toggleFullScreen(state.layout, 'chat', innerWidth); syncLayout(); };
document.addEventListener('keydown', event => {
  if (state.popover && event.key === 'Escape') { closePopover(); return; }
  if (!designCard.hidden && (designCard.contains(event.target) || event.target === document.body) && designViewer.handleKey(event)) {
    event.preventDefault();
    return;
  }
  if (event.key === 'Escape' && state.layout.solo) {
    state.layout = layout.toggleFullScreen(state.layout, state.layout.solo, innerWidth);
    syncLayout();
  }
});
document.addEventListener('click', event => { const path = event.composedPath?.() || []; if (state.popover && !path.includes(popover) && !path.includes(rail) && !popover.contains(event.target) && !rail.contains(event.target)) closePopover(); });
window.addEventListener('resize', () => syncLayout());
assistant.addEventListener('focusin', () => { state.layout = layout.markUsed(state.layout, 'assistant'); });
card.addEventListener('focusin', () => { state.layout = layout.markUsed(state.layout, 'chat'); });
assistant.addEventListener('pointerdown', () => { state.layout = layout.markUsed(state.layout, 'assistant'); });
card.addEventListener('pointerdown', () => { state.layout = layout.markUsed(state.layout, 'chat'); });
lanesCard.addEventListener('pointerdown', () => { state.layout = layout.markUsed(state.layout, 'lanes'); });
lanesCard.addEventListener('focusin', () => { state.layout = layout.markUsed(state.layout, 'lanes'); });
designCard.addEventListener('pointerdown', () => { state.layout = layout.markUsed(state.layout, 'dash'); });
designCard.addEventListener('focusin', () => { state.layout = layout.markUsed(state.layout, 'dash'); });
const theme = localStorage.getItem('pentacle.next.theme'); if (['dark','light','cosmic'].includes(theme)) document.documentElement.dataset.theme = theme;
let assistantHistory = null;
questionsControl = createQuestions({ document, cc, laneReads, renderLaneIcon, getInventory: () => laneInventory, getNotifications: () => questionNotifications.values(), getSessions: sessions, retryQuestionIdentity: () => questionNotifications.refreshIdentities(), questionReadFailed: () => questionNotifications.readFailed(),
  onOpenSpec: specId => { const placement = questionsControl.getSnapshot().list.find(item => item.placement?.specId === specId)?.placement; closePopover(); if (placement?.laneId) { openLaneTarget(placement.laneId, placement.goalId); lanesControl?.openSpec(placement.laneId, placement.goalId, specId); } },
  onOpenChat: streamId => { closePopover(); openChat(streamId); } });
let laneRenderPending = false;
questionsControl.subscribe(snapshot => { questionsButton.dataset.count = String(snapshot.openCount); questionsButton.setAttribute('aria-label', snapshot.openCount ? `Questions (${snapshot.openCount})` : 'Questions'); if (lanesControl && !laneRenderPending) { laneRenderPending = true; queueMicrotask(() => { laneRenderPending = false; lanesControl?.render(); }); } });
lanesControl = mountLanesCard(lanesCard, { getState: () => ({ inventory: laneInventory, current: laneInventoryCurrent }), reads: laneReads, questions: questionsControl,
  navigation: { onClose: () => { state.layout = layout.shut(state.layout, 'lanes', innerWidth); syncLayout(); } } });
store.subscribe(() => { const id = assistantSession()?.stream_id; if (id && id !== assistantHistory) { assistantHistory = id; requestHistory(id); } questionsControl.update({ sessions: sessions() }); render(); });
laneListeners.add(() => { questionsControl.update({ inventory: laneInventory }); lanesControl?.render(); render(); });
laneReads.subscribe(() => render());
let recoveryEpoch = null; let recoveryPending = false; let readySignals = 0;
cc.onChatStreamFrame(frame => { store.applyFrame(frame); applyLaneFrame(frame); questionNotifications.frame(frame); if (frame?.type === 'hosts.stats' && frame.hosts) state.hosts = frame.hosts; if (state.popover === 'limits') renderPopover(); if (frame?.connected === true || frame?.type === '__reconnect') { readySignals++; if (recoveryEpoch !== null) void loadSnapshot().catch(() => {}); } });
async function loadSnapshot() {
  if (recoveryPending) return; recoveryPending = true;
  const signal = readySignals;
  try {
    const connection = cc.webConnectionState?.(); const snapshot = await cc.getChatStreamState();
    const current = cc.webConnectionState?.(); if (connection && (!current?.open || current.epoch !== connection.epoch)) return;
    state.connected = !!snapshot?.connected; state.hosts = snapshot?.hosts_stats || {}; store.applyFrame({ type: 'snapshot', ...(snapshot || {}) }); applyLaneFrame({ type: 'snapshot', ...(snapshot || {}) }); questionNotifications.frame(snapshot);
    void questionNotifications.refreshIdentities();
    if (typeof cc.notificationList === 'function') void cc.notificationList({ states: ['open'] }).then(reply => { if (Array.isArray(reply?.notifications)) questionNotifications.ingest(reply.notifications); }).catch(() => {});
    if (snapshot?.connected && recoveryEpoch !== null && current?.epoch === recoveryEpoch) {
      const unresolved = Object.values(store.getState().optimisticSends || {}).filter(send => ['queued','dispatched','indeterminate'].includes(send.status));
      for (const id of new Set(unresolved.map(send => send.stream_id))) { const result = await cc.requestStreamEvents({ streamId:id, limit:1 }); if (!result?.ok) return; }
      if (cc.webConnectionState?.()?.epoch === recoveryEpoch) { store.webHostReady?.(recoveryEpoch); recoveryEpoch = null; }
      if (assistantSession()?.stream_id) requestHistory(assistantSession().stream_id); if (state.selected) requestHistory(state.selected);
    }
    render();
  } finally { recoveryPending = false; if (recoveryEpoch !== null && readySignals !== signal && cc.webConnectionState?.()?.open) void loadSnapshot().catch(() => {}); }
}
cc.onDisconnect?.(({ epoch }) => {
  recoveryEpoch = null;
  laneInventoryCurrent = false;
  laneConnection.connected = false;
  laneConnection.stateVersion = null;
  lanesControl?.render();
  store.webHostDisconnected?.(epoch);
});
cc.onReconnect?.(({ epoch }) => { recoveryEpoch = epoch; void loadSnapshot().catch(() => {}); });
syncLayout();
const designRoute = initialDesignRoute(window, 'workspace');
if (designRoute) openDesign(designRoute.target);
void loadSnapshot().catch(() => render());
