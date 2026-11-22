import { createInstrument, pitchName } from './melody_instrument.js';

const PREFIX = 'pentacle.melody.composition.';
const INITIAL_GAP = 250;
const copyNotes = notes => notes.map(({ midi, durationMs, gapMs }) => ({ midi, durationMs, gapMs }));
const validScore = score => score && typeof score.id === 'string' && score.id && typeof score.name === 'string' && score.name.trim() && Number.isFinite(score.defaultGapMs) && score.defaultGapMs > 0 && Array.isArray(score.notes) && score.notes.every(n => Number.isInteger(n.midi) && n.midi >= 24 && n.midi <= 83 && Number.isFinite(n.durationMs) && n.durationMs > 0 && Number.isFinite(n.gapMs) && n.gapMs >= 0);
const numberValue = (input, min) => input.value.trim() !== '' && Number.isFinite(Number(input.value)) && Number(input.value) >= min ? Number(input.value) : null;

export function createMelodyViewer(root, { onFullScreen = () => {}, onClose = () => {} } = {}) {
  root.classList.add('melody-viewer');
  root.innerHTML = `<div class="melody-head"><span>Melody</span><button class="melody-icon" data-action="fullscreen" title="Full screen" aria-label="Full screen">⤢</button><button class="melody-icon" data-action="shell-close" title="Close dashboard" aria-label="Close dashboard">×</button></div>
    <div class="melody-scroll"><div class="melody-content"><h1>Melody</h1>
    <div class="melody-toolbar"><button data-action="load">Load</button><button data-action="save-as">Save as</button><button data-action="save">Save</button><button data-action="fresh">Close</button></div>
    <div class="melody-message" role="status" hidden></div><div class="melody-dialog" hidden></div>
    <div class="melody-instrument-mount"></div>
    <section class="melody-section melody-composition"><div class="melody-section-head"><div><strong>Composition</strong> <span class="melody-name">· Untitled</span></div><button class="melody-primary melody-play" data-action="play">▶ &nbsp; Play composition</button></div>
    <div class="melody-gap-default"><label>Default gap <input type="number" min="1" step="any" value="250" aria-label="Default gap in milliseconds"> ms</label></div>
    <div class="melody-strip-wrap"><span class="melody-strip-unit">gap ms</span><div class="melody-strip" aria-label="Composition notes"></div></div><div class="melody-editor"></div></section>
    <p class="melody-storage">Saved melodies stay in this browser on this site.</p></div></div>`;
  const $ = selector => root.querySelector(selector);
  const message = $('.melody-message'), dialog = $('.melody-dialog'), strip = $('.melody-strip'), editor = $('.melody-editor'), gapInput = $('.melody-gap-default input');
  let notes = [], defaultGapMs = INITIAL_GAP, currentId = null, currentName = '', selected = -1, playing = false, savedSnapshot = '', featured = null;
  const snapshot = () => JSON.stringify({ notes, defaultGapMs });
  savedSnapshot = snapshot();
  const dirty = () => snapshot() !== savedSnapshot;
  const setMessage = text => { message.textContent = text; message.hidden = !text; };
  const button = (label, action, disabled = false) => { const b = document.createElement('button'); b.textContent = label; b.dataset.action = action; b.disabled = disabled; return b; };
  const instrument = createInstrument($('.melody-instrument-mount'), {
    getDefaultGap: () => defaultGapMs,
    appendNotes: incoming => {
      if (!Array.isArray(incoming) || !incoming.length) return;
      stop();
      incoming.forEach((n, i) => notes.push({ midi: n.midi, durationMs: n.durationMs, gapMs: notes.length ? (i === 0 ? defaultGapMs : n.gapMs) : 0 }));
      selected = -1; setMessage(''); render();
    },
    onPlayEnd: () => { playing = false; render(); }
  });
  function stop() { instrument.stop(); playing = false; }
  function render() {
    $('.melody-name').textContent = `· ${currentName || 'Untitled'}${dirty() ? ' *' : ''}`;
    const play = $('.melody-play'); play.disabled = !notes.length; play.textContent = playing ? '■  Stop' : '▶  Play composition';
    gapInput.value = defaultGapMs;
    strip.replaceChildren();
    if (!notes.length) { const empty = document.createElement('span'); empty.className = 'melody-strip-empty'; empty.textContent = 'Play notes above and add them here'; strip.append(empty); }
    notes.forEach((note, index) => {
      if (index) { const gap = button(String(note.gapMs), 'edit-gap'); gap.className = 'melody-gap-caption'; gap.dataset.index = index; gap.title = `Edit ${note.gapMs} ms gap`; gap.setAttribute('aria-label', `Edit gap before ${pitchName(note.midi)}: ${note.gapMs} milliseconds`); strip.append(gap); }
      const b = button(pitchName(note.midi), 'select'); b.className = 'melody-note' + (index === selected ? ' is-selected' : ''); b.dataset.index = index; b.style.setProperty('--note-width', `${Math.max(36, Math.min(150, note.durationMs * 0.14))}px`); b.title = `${pitchName(note.midi)}, ${note.durationMs} ms`; strip.append(b);
    });
    editor.replaceChildren();
    if (selected < 0 || !notes[selected]) return;
    const note = notes[selected], label = document.createElement('strong'); label.textContent = `Selected: ${pitchName(note.midi)}`; editor.append(label);
    const gapLabel = document.createElement('label'); gapLabel.className = 'melody-edit-gap'; gapLabel.textContent = 'Previous gap ';
    const input = document.createElement('input'); input.type = 'number'; input.min = '0'; input.step = 'any'; input.value = selected ? note.gapMs : 0; input.disabled = selected === 0; input.setAttribute('aria-label', 'Previous gap in milliseconds'); gapLabel.append(input, ' ms'); editor.append(gapLabel);
    input.addEventListener('change', () => { const value = numberValue(input, 0); if (value === null) { input.value = note.gapMs; setMessage('Enter a nonnegative gap in milliseconds.'); return; } stop(); note.gapMs = value; setMessage(''); render(); });
    editor.append(button('♭ Lower', 'lower', note.midi <= 24), button('♯ Raise', 'raise', note.midi >= 83), button('← Earlier', 'earlier', selected === 0), button('Later →', 'later', selected === notes.length - 1), button('Delete', 'delete'));
  }
  function listLocal() {
    const result = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i); if (!key || !key.startsWith(PREFIX)) continue;
      const item = JSON.parse(localStorage.getItem(key)); if (!validScore(item) || key !== PREFIX + item.id) throw new Error('A saved melody could not be read.'); result.push(item);
    }
    return result;
  }
  function showDialog(title, build) {
    dialog.replaceChildren(); dialog.hidden = false;
    const heading = document.createElement('strong'); heading.textContent = title; dialog.append(heading); build(dialog);
  }
  function closeDialog() { dialog.hidden = true; dialog.replaceChildren(); }
  function save(asNew, done = () => {}) {
    if (asNew || !currentId) {
      showDialog(asNew ? 'Save as' : 'Name this melody', box => {
        const input = document.createElement('input'); input.type = 'text'; input.placeholder = 'Melody name'; input.value = asNew ? '' : currentName; input.setAttribute('aria-label', 'Melody name');
        const saveButton = button('Save', 'dialog-save'), cancel = button('Cancel', 'dialog-cancel'); box.append(input, saveButton, cancel); input.focus();
        const submit = async () => {
          const name = input.value.trim(); if (!name) { setMessage('Enter a name for your melody.'); input.focus(); return; }
          try {
            await featuredReady;
            const all = listLocal(); if (all.some(item => item.name.toLocaleLowerCase() === name.toLocaleLowerCase()) || (featured && featured.name.toLocaleLowerCase() === name.toLocaleLowerCase())) { setMessage('That name already exists. Choose another name.'); return; }
            const id = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
            localStorage.setItem(PREFIX + id, JSON.stringify({ id, name, defaultGapMs, notes: copyNotes(notes) }));
            currentId = id; currentName = name; savedSnapshot = snapshot(); closeDialog(); setMessage('Saved in this browser.'); render(); done();
          } catch { setMessage('Could not save the melody. Your work is still open.'); }
        };
        saveButton.onclick = submit; input.onkeydown = e => { if (e.key === 'Enter') submit(); }; cancel.onclick = closeDialog;
      });
      return;
    }
    try { localStorage.setItem(PREFIX + currentId, JSON.stringify({ id: currentId, name: currentName, defaultGapMs, notes: copyNotes(notes) })); savedSnapshot = snapshot(); setMessage('Saved in this browser.'); render(); done(); }
    catch { setMessage('Could not save the melody. Your work is still open.'); }
  }
  function protect(next) {
    if (!dirty()) { next(); return; }
    showDialog('Save changes before continuing?', box => {
      const saveButton = button('Save', 'confirm-save'), discard = button('Discard', 'confirm-discard'), cancel = button('Cancel', 'confirm-cancel'); box.append(saveButton, discard, cancel);
      saveButton.onclick = () => { closeDialog(); save(false, next); }; discard.onclick = () => { closeDialog(); next(); }; cancel.onclick = closeDialog;
    });
  }
  function fresh() { stop(); notes = []; defaultGapMs = INITIAL_GAP; currentId = null; currentName = ''; selected = -1; savedSnapshot = snapshot(); instrument.clearHistory(); closeDialog(); setMessage(''); render(); }
  const featuredReady = fetch('/api/melody/featured', { cache: 'no-store' }).then(async response => {
    if (response.status === 404) return null;
    if (!response.ok) throw new Error('load');
    const data = await response.json(); if (!validScore(data)) throw new Error('invalid');
    featured = data; return data;
  }).catch(() => null);
  async function loadList() {
    let local;
    try { local = listLocal(); } catch { setMessage('Could not read saved melodies. Your work is still open.'); return; }
    try {
      const response = await fetch('/api/melody/featured', { cache: 'no-store' });
      if (response.status === 404) featured = null;
      else if (!response.ok) throw new Error('load');
      else { const data = await response.json(); if (!validScore(data)) throw new Error('invalid'); featured = data; }
    } catch { featured = null; setMessage('Featured melody could not be loaded. Your saved melodies are still available.'); }
    const entries = [...local]; if (featured && !entries.some(entry => entry.id === featured.id)) entries.push(featured);
    entries.sort((a, b) => a.name.localeCompare(b.name));
    showDialog('Load a melody', box => {
      const description = document.createElement('span'); description.className = 'melody-dialog-help'; description.textContent = entries.length ? 'Choose a saved melody.' : 'No saved melodies yet.'; box.append(description);
      const list = document.createElement('div'); list.className = 'melody-load-list'; box.append(list);
      entries.forEach(entry => { const b = button(entry.name, 'load-entry'); list.append(b); b.onclick = () => { closeDialog(); protect(() => { stop(); notes = copyNotes(entry.notes); if (notes.length) notes[0].gapMs = 0; defaultGapMs = entry.defaultGapMs; currentId = entry.id; currentName = entry.name; selected = -1; savedSnapshot = snapshot(); instrument.clearHistory(); closeDialog(); setMessage(''); render(); }); }; });
      const cancel = button('Cancel', 'dialog-cancel'); cancel.onclick = closeDialog; box.append(cancel);
    });
  }
  gapInput.addEventListener('change', () => { const value = numberValue(gapInput, Number.MIN_VALUE); if (value === null) { gapInput.value = defaultGapMs; setMessage('Enter a positive default gap in milliseconds.'); return; } stop(); defaultGapMs = value; setMessage(''); render(); });
  root.addEventListener('click', event => {
    const target = event.target.closest('button[data-action]'); if (!target || target.disabled) return;
    const action = target.dataset.action;
    if (action === 'fullscreen') onFullScreen();
    else if (action === 'shell-close') { stop(); instrument.hide(); onClose(); }
    else if (action === 'play') { if (playing) { stop(); render(); } else if (notes.length) { playing = true; render(); instrument.play(copyNotes(notes)); } }
    else if (action === 'save') save(false);
    else if (action === 'save-as') save(true);
    else if (action === 'load') loadList();
    else if (action === 'fresh') protect(fresh);
    else if (action === 'select' || action === 'edit-gap') { selected = Number(target.dataset.index); render(); if (action === 'edit-gap') editor.querySelector('input')?.focus(); }
    else if (['lower', 'raise', 'earlier', 'later', 'delete'].includes(action) && selected >= 0) {
      stop(); const note = notes[selected];
      if (action === 'lower') note.midi--;
      if (action === 'raise') note.midi++;
      if (action === 'delete') { notes.splice(selected, 1); if (notes.length) notes[0].gapMs = 0; selected = -1; }
      if (action === 'earlier' || action === 'later') { const other = selected + (action === 'earlier' ? -1 : 1); const gap = notes[selected].gapMs, otherGap = notes[other].gapMs; [notes[selected], notes[other]] = [notes[other], notes[selected]]; notes[selected].gapMs = gap; notes[other].gapMs = otherGap; selected = other; }
      setMessage(''); render();
    }
  });
  render();
  return { show() { root.hidden = false; }, hide() { stop(); instrument.hide(); render(); root.hidden = true; } };
}
