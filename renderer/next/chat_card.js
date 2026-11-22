'use strict';

const { mountComposer } = require('./composer');
const { mountNewChatForm } = require('./new_chat');
const { statusRollup, nextMode } = require('./status_card');
const { createTerminalView, disposeTerminalView } = require('../terminal_view');
const { createTerminalPaste } = require('../terminal_paste');
const NEW_CHAT = '__new__';

// One card owns its DOM, recorder, form generation and PTY slot. The shared
// store remains the source for transcript/status, and drafts stay per stream.
function mountChatCard(stage, { key, slot, icons, node, button, sessions, store, cc, config,
  drafts, history, paintTranscript, attachTranscript, requestHistory, catalogLoader,
  hostPresentation, terminalDeps, onClose, onFullScreen, onStarted, onUse }) {
  const env = stage.ownerDocument.defaultView;
  const card = node('section', 'card chat-card', stage);
  card.dataset.pane = key;
  card.hidden = true;
  const head = node('header', 'card-head', card);
  head.dataset.grab = key;
  const title = node('div', 'card-title', head);
  const statusButton = button('Status', icons.status, 'icon-button');
  const terminalButton = button('Terminal', icons.terminal, 'icon-button');
  const fullButton = button('Full screen', icons.full, 'icon-button fullscreen');
  const closeButton = button('Close', icons.close, 'icon-button');
  head.append(statusButton, terminalButton, fullButton, closeButton);

  const body = node('div', 'thread', card);
  const inner = node('div', 'thread-inner', body);
  const voice = node('div', 'voice-host', body);
  const terminalMount = node('div', 'terminal', card);
  const statusMount = node('div', 'status', card);
  const composerMount = node('div', 'composer-host', card);
  const newChatMount = node('div', 'new-chat-host', card);
  for (const el of [terminalMount, statusMount, newChatMount]) el.hidden = true;

  let selected = null, admitted = null, mode = 'thread', composer = null;
  let newChatControl = null, selection = 0, restoredScroll = null;
  let terminal = null, terminalGeneration = 0, terminalQueue = Promise.resolve();
  const session = () => sessions().find(s => s.stream_id === selected) ??
    (admitted?.stream_id === selected ? admitted : null);
  const getSessionName = s => s.display_name || s.title || s.session_name;

  function disposeTerminal() {
    terminalGeneration++;
    if (!terminal) return;
    const previous = terminal;
    terminal = null;
    disposeTerminalView(previous);
    terminalQueue = terminalQueue.catch(() => {}).then(() => cc.killPty(slot)).catch(() => {});
    terminalMount.replaceChildren();
  }

  cc.onPtyData((number, data) => { if (number === slot && terminal) terminal.term.write(data); });
  async function openTerminal(s) {
    if (terminal || card.hidden || !s) return;
    const generation = ++terminalGeneration;
    await terminalQueue;
    if (generation !== terminalGeneration || card.hidden || mode !== 'terminal') return;
    terminalMount.replaceChildren();
    const style = env.getComputedStyle(stage.ownerDocument.documentElement);
    const css = name => style.getPropertyValue(name).trim();
    const view = createTerminalView(terminalMount, { background: css('--term-bg'), foreground: css('--term-text'), cursor: css('--accent') }, terminalDeps);
    terminal = view;
    await new Promise(resolve => env.requestAnimationFrame(resolve));
    if (generation !== terminalGeneration) return;
    view.fitAddon.fit();
    const isCurrent = () => generation === terminalGeneration && !!terminal?.paneId;
    const paste = createTerminalPaste({ term: view.term, readClipboard: () => cc.readClipboard(), pastePty: text => cc.pastePty(slot, text), isCurrent });
    view.term.element.addEventListener('paste', paste.nativePaste, { capture: true });
    view.term.attachCustomKeyEventHandler(event => paste.key(event, /mac/i.test(env.navigator.platform)));
    view.term.onData(data => { if (isCurrent() && !paste.captureData(data)) { cc.exitCopyMode(slot); cc.writePty(slot, data); } });
    const hostId = (config.hostIds || ['local']).find(id => hostPresentation.streamHost(config, id).toLowerCase() === String(s.host || '').toLowerCase()) || s.hostId || s.host;
    terminalQueue = Promise.resolve(cc.createPty(slot, s.session_name || s.name, hostId, view.term.cols, view.term.rows));
    const paneId = await terminalQueue;
    if (generation !== terminalGeneration) return;
    view.paneId = paneId;
    view.observer = new env.ResizeObserver(() => {
      if (!isCurrent()) return;
      const {cols, rows} = view.term;
      view.fitAddon.fit();
      if (cols !== view.term.cols || rows !== view.term.rows) cc.resizePty(slot, view.term.cols, view.term.rows);
    });
    view.observer.observe(terminalMount);
  }

  function renderStatus(s) {
    const c = s.status_card;
    statusMount.replaceChildren();
    if (!c) return;
    const plan = Array.isArray(c.plan) ? c.plan.filter(Boolean) : [];
    const rollup = statusRollup(c);
    const top = node('div', 'status-top', statusMount);
    const chip = node('span', 'status-chip', top);
    chip.textContent = rollup.label;
    chip.hidden = !plan.length;
    if (c.updated_at && Number.isFinite(Date.parse(c.updated_at))) {
      const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(c.updated_at)) / 1000));
      node('span', 'status-age', top).textContent = `updated ${seconds < 60 ? 'just now' : seconds < 3600 ? `${Math.floor(seconds / 60)}m ago` : `${Math.floor(seconds / 3600)}h ago`}`;
    }
    const add = (heading, text) => {
      if (!text) return;
      node('h2', '', statusMount).textContent = heading;
      node('p', '', statusMount).textContent = text;
    };
    add('Goal', c.goal);
    if (plan.length) {
      node('h2', '', statusMount).textContent = 'Plan';
      const bar = node('div', 'meter', statusMount);
      node('span', '', bar).style.width = `${rollup.percent}%`;
      const list = node('ol', '', statusMount);
      for (const step of plan) {
        const item = node('li', step.status || 'pending', list);
        node('span', 'status-glyph', item).textContent = step.status === 'done' ? '✓' : step.status === 'active' ? '◉' : '○';
        item.append(document.createTextNode(step.text));
      }
    }
    add('Latest update', c.update);
  }

  function render() {
    const s = selected && selected !== NEW_CHAT ? session() : null;
    if (mode !== 'new' && s) {
      title.textContent = getSessionName(s);
      statusButton.hidden = !s.status_card;
      paintTranscript(inner, s.stream_id);
      if (restoredScroll !== null && !card.hidden && mode === 'thread') {
        body.scrollTop = restoredScroll;
        restoredScroll = null;
      }
      if (mode === 'status') renderStatus(s);
    }
  }

  function renderMode() {
    const form = mode === 'new';
    terminalButton.hidden = form;
    statusButton.hidden = form || !session()?.status_card;
    body.hidden = mode !== 'thread';
    composerMount.hidden = mode === 'terminal' || form;
    terminalMount.hidden = mode !== 'terminal';
    statusMount.hidden = mode !== 'status';
    newChatMount.hidden = !form;
    terminalButton.classList.toggle('active', mode === 'terminal');
    statusButton.classList.toggle('active', mode === 'status');
  }

  function startTerminal() {
    const pending = openTerminal(session());
    const generation = terminalGeneration;
    void pending.catch(error => {
      if (generation !== terminalGeneration) return;
      disposeTerminal();
      if (mode === 'terminal' && !card.hidden) terminalMount.textContent = error.message || 'Unable to open terminal';
    });
  }

  function setMode(requested) {
    const s = session();
    if (!s) return;
    const previous = mode;
    if (previous === 'thread') history.set(selected, { mode: previous, scroll: body.scrollTop });
    mode = nextMode(mode, requested);
    if (mode === 'terminal') void composer?.cancelRecording();
    if (previous === 'terminal' && mode !== 'terminal') disposeTerminal();
    renderMode();
    if (mode === 'terminal') startTerminal();
    render();
  }

  async function paintNewChat(generation) {
    newChatMount.textContent = 'Loading…';
    try {
      const response = await catalogLoader.load({ refresh: true });
      if (generation !== selection) return;
      if (!response?.ok) throw new Error(response?.error || 'Unable to load models');
      const hosts = Array.isArray(config.hostIds) && config.hostIds.length ? config.hostIds : ['local'];
      newChatControl = mountNewChatForm(newChatMount, {
        catalog: response.catalog, machineOptions: hosts.map(id => ({ id, label: hostPresentation.hostLabel(config, id) })),
        localHostId: config.chatStream?.localHost || config.localHostId || 'local', accountLabels: config.chatStream?.accountLabels || {},
        spawn: request => cc.chatSpawnV2(request), store,
        onStarted: ({ streamId, session }) => { if (generation === selection) onStarted(key, streamId, session); },
      });
    } catch (error) {
      if (generation === selection) newChatMount.textContent = error.message || 'Unable to load models';
    }
  }

  function setStream(id, seed) {
    if (id === selected) return;
    ++selection;
    if (selected && selected !== NEW_CHAT) history.set(selected, {
      mode, scroll: mode === 'thread' && !card.hidden ? body.scrollTop : history.get(selected)?.scroll ?? null,
    });
    newChatControl?.destroy();
    newChatControl = null;
    disposeTerminal();
    void composer?.dispose();
    composerMount.replaceChildren();
    composer = null;
    selected = id;
    admitted = seed;
    title.textContent = '';
    inner.replaceChildren();
    delete inner.dataset.rendered;
    delete inner.dataset.linkVersion;
    inner.dataset.streamId = '';
    body.scrollTop = 0;
    const previous = id ? history.get(id) : null;
    restoredScroll = previous?.scroll ?? null;
    mode = id === NEW_CHAT ? 'new' : previous?.mode ?? 'thread';
    renderMode();
    if (id === NEW_CHAT) void paintNewChat(selection);
    else if (id) {
      composer = mountComposer({ host: composerMount, transcript: voice, streamId: id, drafts, cc, chatStore: store, env });
      requestHistory(id);
    }
    render();
  }

  function setVisible(visible, solo) {
    const wasVisible = !card.hidden;
    if (wasVisible && !visible) {
      if (selected && mode === 'thread') restoredScroll = body.scrollTop;
      void composer?.cancelRecording();
      disposeTerminal();
    }
    card.hidden = !visible;
    fullButton.setAttribute('aria-label', solo ? 'Leave full screen' : 'Full screen');
    fullButton.title = fullButton.getAttribute('aria-label');
    if (visible && mode === 'terminal' && (!wasVisible || !terminal)) startTerminal();
    if (!wasVisible && visible) render();
  }

  attachTranscript(inner, key);
  for (const event of ['focusin', 'pointerdown']) card.addEventListener(event, () => onUse(key));
  closeButton.onclick = () => onClose(key);
  fullButton.onclick = () => onFullScreen(key);
  statusButton.onclick = () => setMode('status');
  terminalButton.onclick = () => setMode('terminal');

  return { element: card, transcript: inner, render, setStream, setVisible,
    focus() { composer?.focus(); }, get id() { return selected; } };
}

module.exports = { mountChatCard, NEW_CHAT };
