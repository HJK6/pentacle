'use strict';

const { mountComposer } = require('./composer');
const { mountNewChatForm } = require('./new_chat');
const { statusRollup, nextMode } = require('./status_card');
const { createTerminalView, disposeTerminalView } = require('../terminal_view');
const { createTerminalPaste } = require('../terminal_paste');
const { mountWorkingRow, modelAndEffort } = require('./chat_status');
const NEW_CHAT = '__new__';

// One card owns its DOM, recorder, form generation and PTY slot. The shared
// store remains the source for transcript/status, and drafts stay per stream.
function mountChatCard(stage, { key, slot, icons, node, button, sessions, store, cc, config,
  drafts, history, paintTranscript, attachTranscript, transcriptPlace, catalogLoader,
  hostPresentation, terminalDeps, onClose, onFullScreen, onStarted, onUse, onSwitch, nameFor, recorderOptions, onMode,
  onRename, onDelete, canEdit, isPending }) {
  const env = stage.ownerDocument.defaultView;
  const card = node('section', 'card chat-card', stage);
  card.dataset.pane = key;
  card.hidden = true;
  const head = node('header', 'card-head', card);
  head.dataset.grab = key;
  const heading = node('div', 'card-title', head);
  const title = button('Switch chat', '', 'next-chat-title');
  const titleName = node('span', '', title);
  const chevron = node('i', 'next-title-chevron', title);
  chevron.setAttribute('aria-hidden', 'true');
  heading.append(title);
  title.setAttribute('aria-expanded', 'false');
  title.setAttribute('aria-controls', 'next-workspace-popover');
  const model = node('div', 'next-chat-model', heading);
  model.hidden = true;
  title.onclick = () => onSwitch(key);
  const statusButton = button('Status', icons.status, 'icon-button');
  const terminalButton = button('Terminal', icons.terminal, 'icon-button');
  const renameButton = button('Rename', icons.rename, 'icon-button');
  const deleteButton = button('Delete', icons.trash, 'icon-button');
  const fullButton = button('Full screen', icons.full, 'icon-button fullscreen');
  const closeButton = button('Close', icons.close, 'icon-button');
  head.append(statusButton, terminalButton, renameButton, deleteButton, fullButton, closeButton);
  // Rename and Delete follow the chat list: Enter or blur saves, Escape cancels,
  // and Delete asks in place before closing the session.
  const controls = [statusButton, terminalButton, renameButton, deleteButton, fullButton, closeButton];
  let editing = null, asking = null, wasSolo = false;

  const body = node('div', 'thread', card);
  const inner = node('div', 'thread-inner', body);
  const voice = node('div', 'voice-host', body);
  const terminalMount = node('div', 'terminal', card);
  const statusMount = node('div', 'status', card);
  const composerMount = node('div', 'composer-host', card);
  const newChatMount = node('div', 'new-chat-host', card);
  const showWorking = mountWorkingRow(composerMount);
  for (const el of [terminalMount, statusMount, newChatMount]) el.hidden = true;

  let selected = null, admitted = null, mode = 'thread', composer = null;
  let newChatControl = null, selection = 0, restoredScroll = null;
  let terminal = null, terminalGeneration = 0, terminalQueue = Promise.resolve();
  const session = () => sessions().find(s => s.stream_id === selected) ??
    (admitted?.stream_id === selected ? admitted : null);
  const getSessionName = s => nameFor(s);

  function disposeTerminal() {
    terminalGeneration++;
    if (!terminal) return;
    const previous = terminal;
    terminal = null;
    disposeTerminalView(previous);
    terminalQueue = terminalQueue.catch(() => {}).then(() => cc.killPty(slot)).catch(() => {});
    terminalMount.replaceChildren();
  }

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
      titleName.textContent = getSessionName(s);
      title.setAttribute('aria-label', getSessionName(s));
      title.title = getSessionName(s);
      model.textContent = modelAndEffort(s);
      model.hidden = !model.textContent;
      if (!asking) {
        statusButton.hidden = !s.status_card;
        renameButton.hidden = deleteButton.hidden = !editable();
        renameButton.disabled = deleteButton.disabled = isPending(s.stream_id);
      }
      const place = !card.hidden && mode === 'thread' ? restoredScroll : null;
      if (paintTranscript(inner, s.stream_id, place)) restoredScroll = null;
      if (mode === 'status') renderStatus(s);
    }
  }

  function editable() {
    const s = selected && selected !== NEW_CHAT ? session() : null;
    return !!s && mode !== 'new' && canEdit(s);
  }

  function finishRename(edit, save, restoreFocus) {
    if (editing !== edit) return; // Escape may cause a subsequent blur.
    editing = null;
    const name = edit.input.value.trim();
    edit.input.remove();
    title.hidden = false;
    if (save && name && name !== edit.name) void onRename(edit.id, name);
    else render();
    // Enter and Escape return focus to the title; a blur leaves it where the reader put it.
    if (restoreFocus) title.focus();
  }

  function startRename() {
    const s = session();
    if (!editable() || editing || asking || isPending(s.stream_id)) return;
    const input = node('input', 'next-chat-edit');
    input.type = 'text';
    input.value = getSessionName(s);
    input.spellcheck = false;
    input.autocomplete = 'off';
    input.setAttribute('aria-label', 'Chat name');
    const edit = editing = { id: s.stream_id, name: input.value, input };
    input.addEventListener('blur', () => finishRename(edit, true, false));
    input.addEventListener('keydown', event => {
      if (event.isComposing || !['Enter', 'Escape'].includes(event.key)) return;
      event.preventDefault();
      event.stopPropagation();
      finishRename(edit, event.key === 'Enter', true);
    });
    title.hidden = true;
    heading.prepend(input);
    input.focus(); input.select();
  }

  function endAsk(restore) {
    if (!asking) return;
    asking.remove();
    asking = null;
    renderMode();
    if (restore) deleteButton.focus();
  }

  function startDelete() {
    const s = session();
    if (!editable() || editing || asking || isPending(s.stream_id)) return;
    const id = s.stream_id;
    const ask = asking = node('span', 'next-chat-ask');
    const question = node('span', 'next-chat-question', ask);
    question.textContent = 'Delete chat?';
    const cancel = button('Cancel', '', 'next-chat-cancel');
    cancel.textContent = 'Cancel';
    const confirm = button('Delete', '', 'next-chat-confirm');
    confirm.textContent = 'Delete';
    ask.append(cancel, confirm);
    cancel.onclick = () => endAsk(true);
    confirm.onclick = () => { endAsk(false); void onDelete(id); };
    for (const control of controls) control.hidden = true;
    head.append(ask);
    cancel.focus();
  }

  function renderMode() {
    const form = mode === 'new';
    const edit = editable();
    terminalButton.hidden = form;
    fullButton.hidden = closeButton.hidden = false;
    renameButton.hidden = deleteButton.hidden = !edit;
    statusButton.hidden = form || !session()?.status_card;
    body.hidden = mode !== 'thread';
    composerMount.hidden = mode === 'terminal' || form;
    terminalMount.hidden = mode !== 'terminal';
    statusMount.hidden = mode !== 'status';
    newChatMount.hidden = !form;
    terminalButton.classList.toggle('active', mode === 'terminal');
    statusButton.classList.toggle('active', mode === 'status');
    if (asking) for (const control of controls) control.hidden = true;
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
    if (previous === 'thread') history.set(selected, { scroll: transcriptPlace(inner) });
    mode = nextMode(mode, requested);
    if (mode === 'terminal') void composer?.cancelRecording();
    if (previous === 'terminal' && mode !== 'terminal') disposeTerminal();
    renderMode();
    if (mode === 'terminal') startTerminal();
    render();
    onMode?.();
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

  function saveHistory() {
    if (selected && selected !== NEW_CHAT) history.set(selected, {
      scroll: mode === 'thread' && !card.hidden ? transcriptPlace(inner) : history.get(selected)?.scroll ?? null,
    });
  }

  function setStream(id, seed) {
    if (id === selected) return;
    ++selection;
    if (editing) { const edit = editing; editing = null; edit.input.remove(); title.hidden = false; }
    endAsk(false);
    newChatControl?.destroy();
    newChatControl = null;
    disposeTerminal();
    void composer?.dispose();
    composerMount.replaceChildren();
    composer = null;
    selected = id;
    admitted = seed;
    titleName.textContent = id === NEW_CHAT ? 'New chat' : '';
    title.hidden = !id || id === NEW_CHAT;
    model.hidden = true;
    showWorking(false, '');
    inner.replaceChildren();
    delete inner.dataset.rendered;
    delete inner.dataset.linkVersion;
    inner.dataset.streamId = '';
    body.scrollTop = 0;
    const previous = id ? history.get(id) : null;
    restoredScroll = previous?.scroll ?? null;
    mode = id === NEW_CHAT ? 'new' : 'thread';
    renderMode();
    if (id === NEW_CHAT) void paintNewChat(selection);
    else if (id) {
      composer = mountComposer({ host: composerMount, transcript: voice, streamId: id, drafts, cc, chatStore: store, env, recorderOptions });
    }
    render();
  }

  function setVisible(visible, solo) {
    const wasVisible = !card.hidden;
    if (wasVisible && !visible) {
      if (selected && mode === 'thread') restoredScroll = transcriptPlace(inner);
      void composer?.cancelRecording();
      disposeTerminal();
    }
    // A pending Delete question never outlives a change of what the card shows.
    if (asking && (!visible || !wasVisible || solo !== wasSolo)) endAsk(false);
    wasSolo = solo;
    card.hidden = !visible;
    fullButton.setAttribute('aria-label', solo ? 'Leave full screen' : 'Full screen');
    fullButton.title = fullButton.getAttribute('aria-label');
    fullButton.innerHTML = solo ? icons.unfull : icons.full;
    if (visible && mode === 'terminal' && (!wasVisible || !terminal)) startTerminal();
    if (!wasVisible && visible) render();
  }

  attachTranscript(inner, key);
  for (const event of ['focusin', 'pointerdown']) card.addEventListener(event, () => onUse(key));
  closeButton.onclick = () => onClose(key);
  fullButton.onclick = () => onFullScreen(key);
  statusButton.onclick = () => setMode('status');
  terminalButton.onclick = () => setMode('terminal');
  renameButton.onclick = startRename;
  deleteButton.onclick = startDelete;
  // Escape answers an open Delete question before the page uses it (for example to leave full screen).
  card.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || !asking) return;
    event.preventDefault();
    event.stopPropagation();
    endAsk(true);
  });

  return { element: card, transcript: inner, title, render, setStream, setVisible, showWorking, session, saveHistory,
    // The working row is on screen only in a visible card whose composer shows.
    workingShown: () => !card.hidden && !composerMount.hidden,
    // After a layout pass: take a saved place that was waiting for final widths.
    settle() { if (restoredScroll !== null) render(); },
    writeTerminal(data) { terminal?.term.write(data); },
    focus() { composer?.focus(); }, get id() { return selected; } };
}

module.exports = { mountChatCard, NEW_CHAT };
