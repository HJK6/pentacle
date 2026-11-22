'use strict';

// The next view uses the same browser bridge and shared chat store as the
// existing web entry. The store remains the authority for chat state.
require('../web_cc').installWebCc();
const { filterSidebarSessions } = require('../sidebar_filter');
const { uniqueAssistantComposite } = require('../workspace_layout');
const { createSpawnCatalogLoader } = require('../spawn_catalog_loader');
const { Terminal } = require('@xterm/xterm');
const { FitAddon } = require('@xterm/addon-fit');
const { Unicode11Addon } = require('@xterm/addon-unicode11');
const { WebglAddon } = require('@xterm/addon-webgl');
const layout = require('./layout');
const { createMelodyViewer } = require('./melody_viewer');
const { createDesignViewer } = require('./design_viewer');
const { initialDesignRoute, replaceDesignAddress } = require('./design_location');
const { hydrateDesignPreviews, markDesignLinks } = require('./design_links');
const { hydrateImageAttachments } = require('./image_attachments');
const { parseDesignRoute } = require('../../lib/design_routes.cjs');
const { mountComposer } = require('./composer');
const { mountChatCard, NEW_CHAT } = require('./chat_card');
const { mountStageLayout } = require('./stage_layout');
const { mountLimitsView } = require('./limits_view');
const hostPresentation = require('../host_presentation');
const { createLaneReads } = require('./lane_reads');
const { linkAssistantNames } = require('./lane_links');
const { createQuestions } = require('./questions');
const { createQuestionNotifications } = require('./question_notifications');
const { mountLanesCard, renderLaneIcon } = require('./lanes');
const { applyVersionedConnectionState } = require('../chat_stream_connection_state');
const { mountChatList } = require('./chat_list');
const { isProtectedAssistant } = require('../chat_presentation');
const { configuredAssistantRole } = require('../assistant_role');
const { isWorking, workingClock, createSeenMarks, mountWorkingRow, decorateSendLines } = require('./chat_status');
const { createReadingWidth } = require('./reading_width');
const { createMicrophonePreference } = require('./microphone');
const { mountSettingsFields } = require('./settings_fields');
const { trackScroll } = require('./scroll_activity');
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
let storage;
try { storage = window.localStorage; } catch {}
const readSetting = key => { try { return storage?.getItem(key); } catch { return null; } };
const saveSetting = (key, value) => { try { storage?.setItem(key, value); } catch {} };
const reading = createReadingWidth(document.documentElement, storage);
let settingsFields = null;
const microphone = createMicrophonePreference(window, storage, () => settingsFields?.paintMicrophone());
trackScroll(document);
const seen = createSeenMarks(storage);
const clock = workingClock();
const pendingNames = new Map();
const pendingDeletes = new Set();
const hiddenClosed = new Set();
const role = configuredAssistantRole(config.features);
const isProtected = session => isProtectedAssistant([session], role);
let chatList = null;
let switchSeat = null;
let inventorySeen = false;
const railKey = 'pentacle.next.rail';
const state = { layout: layout.createLayoutState(innerWidth, readSetting(railKey)), popover: null, hosts: {}, connected: false };
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
const assistant = node('section', 'pane', stage);
assistant.dataset.pane = 'assistant';
const assistantHead = node('header', 'assistant-head', assistant);
assistantHead.dataset.grab = 'assistant';
const assistantTitle = node('span', '', assistantHead);
const keepButton = button('Keep Assistant on screen', icons.full, 'icon-button assistant-keep');
keepButton.hidden = true;
assistantHead.append(keepButton);
const assistantThread = node('div', 'thread', assistant);
const assistantInner = node('div', 'thread-inner', assistantThread);
const assistantVoice = node('div', 'voice-host', assistantThread);
const assistantComposer = node('div', 'composer-host', assistant);
const showAssistantWorking = mountWorkingRow(assistantComposer);
const chatHistory = new Map();
const usePane = key => { state.layout = layout.markUsed(state.layout, key); };
const cards = Object.fromEntries(['chat', 'chat2'].map((key, slot) => [key, mountChatCard(stage, {
  key, slot, icons, node, button, sessions, store, cc, config, drafts, history: chatHistory,
  paintTranscript, attachTranscript, requestHistory, catalogLoader, hostPresentation,
  terminalDeps: { Terminal, FitAddon, Unicode11Addon, WebglAddon },
  onClose: closeCard, onFullScreen: toggleChatFullScreen, onStarted: admitChat, onUse: usePane,
  onSwitch: openChatSwitch, nameFor: chatName, recorderOptions: microphone.recorderOptions,
})]));
const designCard = node('section', 'card design-card', stage); designCard.hidden = true;
designCard.setAttribute('aria-label', 'Design');
designCard.dataset.pane = 'dash';
const designViewer = createDesignViewer(designCard, {
  onNavigate: onDesignNavigate,
  onFullScreen: toggleDashboardFullScreen,
  onClose: closeDashboard,
});
designCard.querySelector('.design-bar').dataset.grab = 'dash';
const melodyCard = node('section', 'card melody-card', stage);
melodyCard.setAttribute('aria-label', 'Melody');
melodyCard.dataset.pane = 'dash';
melodyCard.hidden = true;
let melodyViewer;
let designOpen = false;
let currentDesignPath = null;
const lanesCard = node('section', 'card lanes-card', stage); lanesCard.hidden = true; lanesCard.dataset.pane = 'lanes';
const brand = button('Assistant', '<span class="logo"></span>', 'rail-button brand on'); const group = node('div', 'rail-group', rail); const lanesButton = button('Lanes', icons.lanes, 'rail-button'); const dashboardsButton = button('Dashboards', icons.dashboards, 'rail-button'); const chatsButton = button('Chats', icons.chats, 'rail-button'); const questionsButton = button('Questions', icons.questions, 'rail-button'); group.append(lanesButton, dashboardsButton, chatsButton, questionsButton); rail.prepend(brand); node('div', 'rail-spacer', rail); const limitsButton = button('Limits and machines', icons.limits, 'rail-button'); const settingsButton = button('Settings', icons.settings, 'rail-button'); rail.append(limitsButton, settingsButton);
const popover = node('div', 'popover', root.querySelector('.next-app')); popover.hidden = true; popover.id = 'next-workspace-popover';
chatsButton.classList.add('chats-button');
chatsButton.setAttribute('aria-expanded', 'false');
chatsButton.setAttribute('aria-controls', popover.id);
let assistantComposerControl = null; let assistantComposerStream = null; let limitsControl = null;
let unmountQuestionList = null;
function mountAssistantComposer(id) { if (!id || id === assistantComposerStream) return; assistantComposerControl?.dispose(); assistantComposerStream = id; assistantComposerControl = mountComposer({ host: assistantComposer, transcript: assistantVoice, streamId: id, drafts, cc, chatStore: store, env: window, recorderOptions: microphone.recorderOptions }); }
function closePopover() {
  settingsFields?.destroy(); settingsFields = null;
  chatList?.destroy(); chatList = null;
  if (switchSeat) cards[switchSeat].title.setAttribute('aria-expanded', 'false');
  switchSeat = null;
  chatsButton.setAttribute('aria-expanded', 'false');
  unmountQuestionList?.(); unmountQuestionList = null; limitsControl?.destroy(); limitsControl = null; state.popover = null; popover.hidden = true; dashboardsButton.classList.remove('open'); dashboardsButton.setAttribute('aria-expanded', 'false'); chatsButton.classList.remove('open'); questionsButton.classList.remove('open'); limitsButton.classList.remove('open'); settingsButton.classList.remove('open'); }
function togglePopover(name) { if (state.popover === name) { closePopover(); return; } closePopover(); state.popover = name; popover.hidden = false; popover.classList.remove('chat-switch'); ({ dashboards: dashboardsButton, chats: chatsButton, questions: questionsButton, limits: limitsButton, settings: settingsButton })[name].classList.add('open'); popover.classList.toggle('bottom', name === 'limits' || name === 'settings'); popover.classList.toggle('questions-popover', name === 'questions'); popover.classList.toggle('limits-popover', name === 'limits'); renderPopover(); anchorPopover(); }
function sessions() { return store.getState().sessions || []; }
function visibleChats() {
  return filterSidebarSessions(sessions()).filter(s => s.session_kind !== 'assistant_composite' &&
    !hiddenClosed.has(s.stream_id) && !s.closed && s.status !== 'closed' && s.session_state !== 'closed' && s.state !== 'closed');
}
function chatName(s) { return pendingNames.get(s.stream_id)?.name || s.display_name || s.title || s.session_name; }
function draftFor(s) { return s ? store.getState().drafts?.[s.stream_id] : undefined; }
function working(s) { return s && isWorking(s, draftFor(s), store.getTurnPhase?.(s.stream_id)); }
function target(s) {
  const hostId = (config.hostIds || ['local']).find(id =>
    hostPresentation.streamHost(config, id).toLowerCase() === String(s.host || '').toLowerCase())
    || s.hostId || s.host;
  return [hostId, s.session_name];
}
async function renameChat(id, name) {
  const s = visibleChats().find(row => row.stream_id === id);
  if (!s || isProtected(s) || pendingNames.has(id) || pendingDeletes.has(id)) return;
  const args = target(s);
  if (!args.every(Boolean)) return;
  const pending = { name, confirmed: false, complete: false };
  pendingNames.set(id, pending);
  render();
  try {
    if (!(await cc.chatRename(...args, name))?.ok) pendingNames.delete(id);
    else { pending.complete = true; if (pending.confirmed) pendingNames.delete(id); }
  } catch { pendingNames.delete(id); }
  render();
}
async function deleteChat(id) {
  const s = visibleChats().find(row => row.stream_id === id);
  if (!s || isProtected(s) || pendingNames.has(id) || pendingDeletes.has(id)) return;
  const [hostId, sessionName] = target(s);
  if (!hostId || !sessionName) return;
  pendingDeletes.add(id);
  render();
  try {
    if ((await cc.chatClose(hostId, sessionName, { operatorConfirm: true, force: true }))?.ok) {
      if (sessions().some(row => row.stream_id === id)) hiddenClosed.add(id);
      seen.forget(id);
      const seat = Object.keys(cards).find(key => cards[key].id === id);
      if (seat) closeCard(seat);
    }
  } catch {
    // A refusal or failed transport leaves the row and its card alone.
  } finally {
    pendingDeletes.delete(id);
    render();
  }
}
function beforeStoreFrame(frame) {
  if (!frame || frame.connected === false || !Array.isArray(frame.sessions) ||
      !['snapshot', 'session.inventory'].includes(frame.type)) return;
  const rows = frame.sessions;
  const ids = new Set(rows.map(s => s.stream_id));
  for (const id of hiddenClosed) if (!ids.has(id)) hiddenClosed.delete(id);
  for (const s of rows) {
    const p = pendingNames.get(s.stream_id);
    if (p && (s.display_name || s.title || s.session_name) === p.name) {
      p.confirmed = true;
      if (p.complete) pendingNames.delete(s.stream_id);
    }
  }
  inventorySeen ||= rows.length > 0;
}
function paintChatStatus() {
  const a = assistantSession();
  const active = new Set(Object.values(cards).filter(card => !card.element.hidden && card.id !== NEW_CHAT).map(card => card.id));
  if (a && !assistant.hidden) active.add(a.stream_id);
  seen.update(sessions().filter(s => !hiddenClosed.has(s.stream_id)), active, !document.hidden, inventorySeen);
  const unread = visibleChats().filter(s => seen.unread(s)).length;
  if (unread) chatsButton.dataset.count = String(unread); else delete chatsButton.dataset.count;
  chatsButton.setAttribute('aria-label', unread ? 'Chats, ' + unread + ' unread' : 'Chats');
  brand.classList.toggle('assistant-unread', assistant.hidden && seen.unread(a));
  clock.retain(new Set(sessions().map(s => s.stream_id)));
  paintWorking();
  if (chatList) paintChatList();
}
function paintWorking() {
  const a = assistantSession();
  brand.classList.toggle('assistant-working', !!working(a));
  const tick = (s, draw) => {
    const now = Date.now();
    draw(working(s), clock.elapsed(s, draftFor(s), s ? store.getTurnPhase?.(s.stream_id) : undefined,
      s ? store.getWorkingElapsedMs?.(s.stream_id, now) : null, now));
  };
  tick(a, showAssistantWorking);
  for (const card of Object.values(cards)) tick(card.session(), card.showWorking);
}
function paintChatList() {
  const selected = switchSeat ? [cards[switchSeat].id] : Object.values(cards).filter(c => !c.element.hidden).map(c => c.id);
  chatList.update(visibleChats().sort((a,b) => String(b.last_activity || b.last_event_at || '').localeCompare(String(a.last_activity || a.last_event_at || '')))
    .map(s => ({ id: s.stream_id, name: chatName(s), working: !!working(s), unread: seen.unread(s),
      selected: selected.includes(s.stream_id), protectedRow: isProtected(s),
      pending: pendingNames.has(s.stream_id) || pendingDeletes.has(s.stream_id) })));
}
function openChatSwitch(seat) {
  if (switchSeat === seat) { closePopover(); return; }
  closePopover();
  switchSeat = seat;
  state.popover = 'switch';
  cards[seat].title.setAttribute('aria-expanded', 'true');
  popover.hidden = false;
  popover.classList.remove('bottom', 'limits-popover', 'questions-popover');
  popover.classList.add('chat-switch');
  renderPopover();
  anchorPopover();
}
function chooseChat(id) {
  const seat = switchSeat;
  closePopover();
  if (!seat) { openChat(id || NEW_CHAT); return; }
  state.layout = layout.switchChat(state.layout, seat, id || NEW_CHAT);
  syncLayout();
  cards[seat].focus();
  render();
}
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
  const html = view.renderTranscriptTimelineHtml(detail, undefined, { showDashboardLinks: true, streamId: id, allowQueuedSendNow: true });
  const targets = element === assistantInner ? laneNameTargets() : [];
  const linkVersion = JSON.stringify(targets);
  if (element.dataset.rendered === html && element.dataset.linkVersion === linkVersion) {
    decorateSendLines(element, store);
    return;
  }
  const scroller = element.parentElement;
  const stick = !element.dataset.rendered || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
  element.innerHTML = html || '<div class="empty">No messages yet.</div>';
  decorateSendLines(element, store);
  element.dataset.rendered = html;
  element.dataset.linkVersion = linkVersion;
  if (element === assistantInner) {
    linkAssistantNames(element, targets);
    for (const update of element.querySelectorAll('.slot-chat-lane-update[data-lane-id]')) {
      update.tabIndex = 0;
      update.setAttribute('role', 'button');
    }
  }
  markDesignLinks(element, designCard.hidden ? null : currentDesignPath);
  hydrateDesignPreviews(element, key => cc.chatFetchBlob(key));
  hydrateImageAttachments(element, key => cc.chatFetchBlob(key));
  void view.hydrateFileAttachments(element);
  if (stick) scroller.scrollTop = scroller.scrollHeight;
}
function attachTranscript(transcript, source) { transcript.addEventListener('click', event => {
  const designLink = event.target.closest('[data-next-design-link]');
  if (designLink) {
    followDesignLink(source, designLink.dataset.nextDesignLink);
    return;
  }
  const laneLink = event.target.closest('.next-lane-name-link');
  if (laneLink && transcript === assistantInner) { openLaneTarget(laneLink.dataset.laneId, laneLink.dataset.goalId); return; }
  const laneUpdate = event.target.closest('.slot-chat-lane-update[data-lane-id]');
  if (laneUpdate && transcript === assistantInner) { openLaneTarget(laneUpdate.dataset.laneId); return; }
  const copy = event.target.closest('.slot-chat-copy-btn');
  if (copy) void navigator.clipboard.writeText(copy.dataset.copyText || '');
  const retry = event.target.closest('.slot-chat-send-retry');
  if (retry) store.retryOptimisticSend(retry.dataset.optimisticId);
  const sendNow = event.target.closest('.slot-chat-send-now');
  if (sendNow && store.canSendQueuedNow(sendNow.dataset.streamId, sendNow.dataset.optimisticId)) store.sendQueuedNow(sendNow.dataset.streamId, sendNow.dataset.optimisticId);
}); }
attachTranscript(assistantInner, 'assistant');
assistantInner.addEventListener('keydown', event => { const update = event.target.closest('.slot-chat-lane-update[data-lane-id]'); if (update && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); openLaneTarget(update.dataset.laneId); } });
function renderPopover() {
  if (state.popover === 'chats' || state.popover === 'switch') {
    if (!chatList) {
      popover.replaceChildren();
      chatList = mountChatList(popover, { choose: chooseChat, rename: renameChat, remove: deleteChat });
    }
    if (!switchSeat) chatsButton.setAttribute('aria-expanded', 'true');
    paintChatList();
    return;
  }
  if (state.popover === 'settings' && settingsFields) return;
  if (state.popover === 'limits' && limitsControl) { limitsControl.update({ limits: store.getLimits(), hosts: state.hosts }); return; }
  popover.replaceChildren();
  if (state.popover === 'dashboards') {
    for (const [name, open] of [['Design', openDesign], ['Melody', openMelody]]) {
      const row = button(name, name);
      row.onclick = () => open();
      popover.append(row);
    }
  } else if (state.popover === 'questions') {
    unmountQuestionList = questionsControl?.mountList(popover) || null;
  } else if (state.popover === 'settings') {
    const heading = node('div', 'theme-heading', popover);
    heading.textContent = 'Theme';
    const grid = node('div', 'themes', popover);
    const sidebar = node('div', 'sidebar-setting', popover);
    node('div', 'theme-heading', sidebar).textContent = 'Sidebar';
    const choices = node('div', 'sidebar-choices', sidebar);
    choices.setAttribute('role', 'group');
    choices.setAttribute('aria-label', 'Sidebar');
    const themeChoices = new Map();
    const railChoices = new Map();
    const paintSettings = () => {
      const currentTheme = document.documentElement.dataset.theme || 'dark';
      for (const [name, control] of themeChoices) {
        control.classList.toggle('on', currentTheme === name);
        control.setAttribute('aria-pressed', String(currentTheme === name));
      }
      for (const [name, control] of railChoices)
        control.setAttribute('aria-pressed', String(state.layout.rail === name));
    };
    for (const theme of ['dark', 'light', 'cosmic']) {
      const row = button(theme, `<span class="theme-preview sw-${theme}" aria-hidden="true"><span class="theme-mini"><i></i><b></b></span></span><span>${theme[0].toUpperCase() + theme.slice(1)}</span>`, 'theme-choice');
      row.onclick = () => {
        document.documentElement.classList.add('swap');
        document.documentElement.dataset.theme = theme;
        saveSetting('pentacle.next.theme', theme);
        paintSettings();
        requestAnimationFrame(() => document.documentElement.classList.remove('swap'));
      };
      themeChoices.set(theme, row);
      grid.append(row);
    }
    for (const value of ['left', 'top']) {
      const label = value === 'left' ? 'Left' : 'Top';
      const choice = button(label, label, 'sidebar-choice');
      choice.onclick = () => {
        state.layout = layout.setRail(state.layout, value);
        saveSetting(railKey, value);
        syncLayout();
        paintSettings();
        choice.focus();
      };
      railChoices.set(value, choice);
      choices.append(choice);
    }
    paintSettings();
    settingsFields = mountSettingsFields(popover, {reading, microphone});
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
  assistantTitle.textContent = assistantName;
  keepButton.setAttribute('aria-label', `Keep ${assistantName} on screen`);
  keepButton.title = keepButton.getAttribute('aria-label');
  for (const card of Object.values(cards)) card.render();
  paintChatStatus();
  const meters = document.createElement('span'); meters.className = 'rail-meters'; meters.setAttribute('aria-hidden', 'true'); for (const limit of store.getLimits()) { const bar = document.createElement('i'); bar.style.height = `${Math.max(3, Math.round((limit.pct || 0) * 0.13) + 3)}px`; meters.append(bar); } limitsButton.replaceChildren(meters);
  if (state.popover && state.popover !== 'questions') renderPopover();
}
function syncLayout(admitted = null) {
  document.documentElement.dataset.rail = state.layout.rail;
  const visible = layout.fits(state.layout, innerWidth);
  if ((visible.assistant || innerWidth < layout.PHONE_WIDTH) && state.layout.overlay) state.layout = layout.dismissAssistant(state.layout);
  const overlay = state.layout.overlay;
  if (!assistant.hidden && !visible.assistant && !overlay) void assistantComposerControl?.cancelRecording();
  assistant.hidden = !visible.assistant && !overlay;
  assistant.classList.toggle('assistant-overlay', overlay);
  keepButton.hidden = !overlay;
  for (const [key, card] of Object.entries(cards)) if (card.id !== state.layout[key]) card.saveHistory();
  for (const [key, card] of Object.entries(cards)) {
    const id = state.layout[key];
    card.setStream(id, id && admitted?.stream_id === id ? admitted : null);
    card.setVisible(visible.cards.includes(key), state.layout.solo === key);
  }
  designCard.hidden = !visible.cards.includes('dash') || !state.layout.dash.startsWith('design:');
  const melodyVisible = visible.cards.includes('dash') && state.layout.dash === 'melody';
  if (melodyVisible === melodyCard.hidden) {
    melodyCard.hidden = !melodyVisible;
    if (melodyVisible) {
      melodyViewer ??= createMelodyViewer(melodyCard, { onFullScreen: toggleDashboardFullScreen, onClose: closeDashboard });
      melodyViewer.show();
      melodyCard.querySelector('.melody-head').dataset.grab = 'dash';
    } else {
      melodyViewer?.hide();
    }
  }
  const dashboardVisible = !designCard.hidden || melodyVisible;
  stage.classList.toggle('has-design', dashboardVisible);
  const full = designCard.querySelector('.design-full');
  const fullLabel = state.layout.solo === 'dash' ? 'Leave full screen' : 'Full screen';
  const melodyFull = melodyCard.querySelector('[data-action="fullscreen"]');
  if (melodyFull) {
    melodyFull.setAttribute('aria-label', fullLabel);
    melodyFull.title = fullLabel;
  }
  full.setAttribute('aria-label', fullLabel);
  full.title = fullLabel;
  lanesCard.hidden = !visible.cards.includes('lanes');
  lanesCard.classList.toggle('alone', !lanesCard.hidden && visible.cards.length === 1 && assistant.hidden);
  stageLayout.paint();
  brand.classList.toggle('on', visible.assistant || overlay);
  brand.setAttribute('aria-pressed', String(visible.assistant || overlay));
  chatsButton.classList.toggle('on', visible.cards.some(key => key === 'chat' || key === 'chat2'));
  dashboardsButton.classList.toggle('on', dashboardVisible);
  dashboardsButton.setAttribute('aria-pressed', String(dashboardVisible));
  lanesButton.classList.toggle('on', !lanesCard.hidden);
  if (designOpen && !state.layout.dash?.startsWith('design:')) {
    designOpen = false;
    currentDesignPath = null;
    designViewer.close();
    history.replaceState(history.state, '', '/next/');
  }
  if (state.layout.dash !== 'melody' && location.hash === '#melody') {
    history.replaceState(history.state, '', '/next/');
  }
  if (switchSeat && cards[switchSeat].element.hidden) closePopover();
  paintChatStatus();
  anchorPopover();
  syncDesignLinkMarks();
}

function syncDesignLinkMarks() {
  const path = designCard.hidden ? null : currentDesignPath;
  markDesignLinks(assistantInner, path);
  for (const card of Object.values(cards)) markDesignLinks(card.transcript, path);
}

function onDesignNavigate(target) {
  const path = core.buildDesignPath(target ?? undefined, 'workspace');
  currentDesignPath = path;
  // Keep the shared slot keyed to what the viewer actually shows after stepping,
  // going Home or following a link whose screen no longer exists.
  state.layout = { ...state.layout, dash: 'design:' + path };
  replaceDesignAddress(window, target, 'workspace');
  syncDesignLinkMarks();
}

function openDesign(target = null) {
  const path = core.buildDesignPath(target ?? undefined, 'workspace');
  closePopover();
  state.layout = layout.show(state.layout, 'dash', 'design:' + path, innerWidth);
  openDesignViewer(target);
}

function followDesignLink(source, path) {
  const route = parseDesignRoute(path);
  if (route?.mode !== 'workspace') return;
  closePopover();
  state.layout = layout.follow(state.layout, source, 'design:' + path, innerWidth);
  if (state.layout.dash) openDesignViewer(route.target);
  else syncLayout();
}

function openDesignViewer(target) {
  currentDesignPath = null;
  syncLayout();
  designOpen = true;
  void designViewer.open(target);
}

function toggleDashboardFullScreen() {
  state.layout = layout.toggleFullScreen(state.layout, 'dash', innerWidth);
  syncLayout();
}

function openMelody() {
  closePopover();
  state.layout = layout.show(state.layout, 'dash', 'melody', innerWidth);
  syncLayout();
  history.replaceState(history.state, '', '/next/#melody');
}

function closeDashboard() {
  state.layout = layout.shut(state.layout, 'dash', innerWidth);
  syncLayout();
}

const stageLayout = mountStageLayout({
  stage, panes: () => ({ assistant, chat: cards.chat.element, chat2: cards.chat2.element,
    dash: melodyCard.hidden ? designCard : melodyCard, lanes: lanesCard }),
  getState: () => state.layout,
  onLayout: next => { state.layout = next; syncLayout(); },
  onLift: () => { closePopover(); dismissOverlay(); },
});
function dismissOverlay() {
  if (!state.layout.overlay) return;
  state.layout = layout.dismissAssistant(state.layout);
  syncLayout();
}
function anchorPopover() {
  if (!state.popover) return;
  if (switchSeat) {
    const rect = cards[switchSeat].title.getBoundingClientRect();
    const low = cards[switchSeat].title.parentElement.getBoundingClientRect().bottom;
    popover.style.setProperty('--switch-x', Math.max(8, Math.min(rect.left - 8, innerWidth - 324)) + 'px');
    popover.style.setProperty('--switch-y', Math.min(low + 6, innerHeight - 70) + 'px');
    return;
  }
  const anchor = ({ dashboards: dashboardsButton, chats: chatsButton, questions: questionsButton,
    limits: limitsButton, settings: settingsButton })[state.popover].getBoundingClientRect();
  const top = rail.getBoundingClientRect().bottom;
  const maxLeft = innerWidth - popover.offsetWidth - 8;
  const maxTop = innerHeight - popover.offsetHeight - 8;
  popover.style.setProperty('--anchor-x', `${Math.max(8, Math.min(anchor.left, maxLeft))}px`);
  popover.style.setProperty('--anchor-y', `${Math.max(8, Math.min(anchor.top, maxTop))}px`);
  popover.style.setProperty('--rail-bottom', `${top}px`);
}
function openChat(id) {
  state.layout = layout.show(state.layout, 'chat', id, innerWidth);
  syncLayout();
  cards[state.layout.last]?.focus();
  render();
}
function openNewChat() { openChat(NEW_CHAT); }
function admitChat(key, id, session) {
  state.layout = layout.replaceChat(state.layout, key, id);
  syncLayout(session);
  cards[state.layout.last]?.focus();
  render();
}
function closeCard(key) {
  state.layout = layout.shut(state.layout, key, innerWidth);
  syncLayout();
  render();
}
function toggleChatFullScreen(key) {
  state.layout = layout.toggleFullScreen(state.layout, key, innerWidth);
  syncLayout();
}
brand.onclick = () => {
  closePopover();
  const wasOnStage = layout.fits(state.layout, innerWidth).assistant;
  const wasOverlay = state.layout.overlay;
  state.layout = layout.toAssistant(state.layout, innerWidth);
  syncLayout();
  if (!wasOnStage && !wasOverlay && !assistant.hidden && innerWidth >= layout.PHONE_WIDTH) assistantComposerControl?.focus();
};
keepButton.onclick = () => {
  state.layout = layout.keepAssistant(state.layout, innerWidth);
  syncLayout();
  if (!assistant.hidden) assistantComposerControl?.focus();
};
lanesButton.onclick = () => { closePopover(); state.layout = layout.fits(state.layout, innerWidth).cards.includes('lanes') ? layout.shut(state.layout, 'lanes', innerWidth) : layout.show(state.layout, 'lanes', true, innerWidth); syncLayout(); };
dashboardsButton.onclick = () => {
  if (innerWidth < layout.PHONE_WIDTH) {
    closePopover();
    if (designCard.hidden) openDesign();
    else closeDashboard();
    return;
  }
  togglePopover('dashboards');
  dashboardsButton.setAttribute('aria-expanded', String(state.popover === 'dashboards'));
};
chatsButton.onclick = () => togglePopover('chats');
questionsButton.onclick = () => togglePopover('questions');
limitsButton.onclick = () => togglePopover('limits');
settingsButton.onclick = () => togglePopover('settings');
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && state.popover && chatList) {
    if (chatList.escape()) return;
    const anchor = switchSeat ? cards[switchSeat].title : chatsButton;
    closePopover(); anchor.focus(); return;
  }
  if (event.key === 'Escape' && state.layout.overlay) { event.preventDefault(); dismissOverlay(); return; }
  if (state.popover && event.key === 'Escape') { closePopover(); return; }
  if (!designCard.hidden && (designCard.contains(event.target) || event.target === document.body) && designViewer.handleKey(event)) {
    event.preventDefault();
    return;
  }
  if (event.key === 'Escape' && state.layout.solo && innerWidth >= layout.PHONE_WIDTH) {
    state.layout = layout.toggleFullScreen(state.layout, state.layout.solo, innerWidth);
    syncLayout();
  }
});
document.addEventListener('click', event => { const path = event.composedPath?.() || []; if (state.popover === 'switch') { if (!path.includes(cards[switchSeat].title) && !path.includes(popover)) closePopover(); return; } if (state.popover && !path.includes(popover) && !path.includes(rail) && !popover.contains(event.target) && !rail.contains(event.target)) closePopover(); });
document.addEventListener('pointerdown', event => {
  if (state.layout.overlay && !event.composedPath().includes(assistant) && !event.composedPath().includes(brand)) dismissOverlay();
});
document.addEventListener('click', event => {
  if (event.detail === 0 && state.layout.overlay && !event.composedPath().includes(assistant) && !event.composedPath().includes(brand)) dismissOverlay();
});
window.addEventListener('resize', () => { stageLayout.cancel(); syncLayout(); });
assistant.addEventListener('focusin', () => usePane('assistant'));
assistant.addEventListener('pointerdown', () => usePane('assistant'));
for (const dashboard of [designCard, melodyCard]) {
  dashboard.addEventListener('pointerdown', () => { state.layout = layout.markUsed(state.layout, 'dash'); });
  dashboard.addEventListener('focusin', () => { state.layout = layout.markUsed(state.layout, 'dash'); });
}
const theme = readSetting('pentacle.next.theme'); if (['dark','light','cosmic'].includes(theme)) document.documentElement.dataset.theme = theme;
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
cc.onChatStreamFrame(frame => { beforeStoreFrame(frame); store.applyFrame(frame); applyLaneFrame(frame); questionNotifications.frame(frame); if (frame?.type === 'hosts.stats' && frame.hosts) state.hosts = frame.hosts; if (state.popover === 'limits') renderPopover(); if (frame?.connected === true || frame?.type === '__reconnect') { readySignals++; if (recoveryEpoch !== null) void loadSnapshot().catch(() => {}); } });
async function loadSnapshot() {
  if (recoveryPending) return; recoveryPending = true;
  const signal = readySignals;
  try {
    const connection = cc.webConnectionState?.(); const snapshot = await cc.getChatStreamState();
    const current = cc.webConnectionState?.(); if (connection && (!current?.open || current.epoch !== connection.epoch)) return;
    state.connected = !!snapshot?.connected; state.hosts = snapshot?.hosts_stats || {}; const snapshotFrame = { type: 'snapshot', ...(snapshot || {}) }; beforeStoreFrame(snapshotFrame); store.applyFrame(snapshotFrame); applyLaneFrame({ type: 'snapshot', ...(snapshot || {}) }); questionNotifications.frame(snapshot);
    void questionNotifications.refreshIdentities();
    if (typeof cc.notificationList === 'function') void cc.notificationList({ states: ['open'] }).then(reply => { if (Array.isArray(reply?.notifications)) questionNotifications.ingest(reply.notifications); }).catch(() => {});
    if (snapshot?.connected && recoveryEpoch !== null && current?.epoch === recoveryEpoch) {
      const unresolved = Object.values(store.getState().optimisticSends || {}).filter(send => ['queued','dispatched','indeterminate'].includes(send.status));
      for (const id of new Set(unresolved.map(send => send.stream_id))) { const result = await cc.requestStreamEvents({ streamId:id, limit:1 }); if (!result?.ok) return; }
      if (cc.webConnectionState?.()?.epoch === recoveryEpoch) { store.webHostReady?.(recoveryEpoch); recoveryEpoch = null; }
      if (assistantSession()?.stream_id) requestHistory(assistantSession().stream_id); for (const card of Object.values(cards)) if (card.id && card.id !== NEW_CHAT) requestHistory(card.id);
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
const designRoute = initialDesignRoute(window, 'workspace');
if (designRoute) openDesign(designRoute.target);
else if (location.hash === '#melody') openMelody();
else syncLayout();
document.addEventListener('visibilitychange', paintChatStatus);
window.setInterval(paintWorking, 1000);
void loadSnapshot().catch(() => render());
