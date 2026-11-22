const NAMES = ['C', 'C♯', 'D', 'D♯', 'E', 'F', 'F♯', 'G', 'G♯', 'A', 'A♯', 'B'];
const BLACK = new Set([1, 3, 6, 8, 10]);
export function pitchName(midi) { return `${NAMES[((midi % 12) + 12) % 12]}${Math.floor(midi / 12) - 1}`; }

export function createInstrument(root, { getDefaultGap, appendNotes, onPlayEnd }) {
  root.innerHTML = `
    <section class="melody-instrument">
      <div class="melody-instrument-heading">
        <h3>Piano</h3>
        <div class="melody-octaves">
          <button type="button" data-octave="-1" aria-label="Previous octave">‹</button>
          <span></span>
          <button type="button" data-octave="1" aria-label="Next octave">›</button>
        </div>
      </div>
      <div class="melody-piano" aria-label="Piano"></div>
      <p class="melody-piano-help">Use arrow keys to choose a note and hold Space to play.</p>
      <div class="melody-instrument-heading">
        <h3>History</h3>
        <div class="melody-history-actions">
          <button type="button" class="melody-clear-history">Clear history</button>
          <button type="button" class="melody-append-selection" disabled>Append selection</button>
        </div>
      </div>
      <p class="melody-history-help">Click a note to append it, or drag to select notes. Gaps in ms.</p>
      <div class="melody-history-scroll">
        <div class="melody-history" aria-label="Played note history">
          <div class="melody-history-bars"></div>
          <div class="melody-history-labels"></div>
          <div class="melody-marquee" hidden></div>
        </div>
      </div>
    </section>`;
  const piano = root.querySelector('.melody-piano');
  const octaveLabel = root.querySelector('.melody-octaves span');
  const scroll = root.querySelector('.melody-history-scroll');
  const history = root.querySelector('.melody-history');
  const bars = root.querySelector('.melody-history-bars');
  const labels = root.querySelector('.melody-history-labels');
  const marquee = root.querySelector('.melody-marquee');
  const appendButton = root.querySelector('.melody-append-selection');
  let octave = 3, focusedMidi = 60, active = null, audio = null, liveVoice = null;
  let events = [], selected = new Set(), lastRelease = null, restLimit = 0, frame = 0;
  let scheduled = [], playTimer = null, playGeneration = 0, playing = false, drag = null;
  const now = () => performance.now();
  const defaultGap = () => Math.max(1, Number(getDefaultGap()) || 250);
  const duration = e => e.end == null ? Math.max(0, now() - e.startedAt) : e.durationMs;
  const context = () => {
    if (!audio) { const Audio = window.AudioContext || window.webkitAudioContext; audio = new Audio(); }
    if (audio.state === 'suspended') audio.resume();
    return audio;
  };
  function voice(midi, start, end) {
    const ctx = context(), gain = ctx.createGain();
    gain.connect(ctx.destination);
    const oscillators = [1, 2, 3, 4].map((harmonic, i) => {
      const osc = ctx.createOscillator(), partial = ctx.createGain();
      osc.type = 'sine'; osc.frequency.setValueAtTime(440 * 2 ** ((midi - 69) / 12) * harmonic, start);
      partial.gain.value = [0.72, 0.18, 0.07, 0.03][i]; osc.connect(partial); partial.connect(gain);
      osc.start(start); return osc;
    });
    gain.gain.setValueAtTime(0, start);
    gain.gain.linearRampToValueAtTime(0.28, start + 0.008);
    gain.gain.exponentialRampToValueAtTime(0.17, start + 0.12);
    gain.gain.setTargetAtTime(0.012, start + 0.12, 2.5);
    const v = { gain, oscillators, ended: false };
    if (end != null) releaseVoice(v, end);
    return v;
  }
  function releaseVoice(v, time) {
    if (!v || v.ended) return;
    v.ended = true;
    const t = Math.max(audio.currentTime, time);
    v.gain.gain.cancelScheduledValues(t);
    v.gain.gain.setTargetAtTime(0, t, 0.003);
    v.oscillators.forEach(o => { try { o.stop(t + 0.025); } catch (_) {} });
  }
  function silenceVoice(v) {
    if (!v) return;
    // Disconnect scheduled voices too: their previous release may already be scheduled.
    try { v.gain.disconnect(); } catch (_) {}
    v.oscillators.forEach(o => { try { o.stop(); } catch (_) {} });
  }
  function stopPlayback(notify = true) {
    ++playGeneration; clearTimeout(playTimer); playTimer = null;
    scheduled.forEach(silenceVoice); scheduled = [];
    const wasPlaying = playing; playing = false;
    if (wasPlaying && notify && onPlayEnd) onPlayEnd();
  }
  function endActive(owner, t = now()) {
    if (!active || (owner != null && active.owner !== owner)) return;
    const e = active.event;
    e.durationMs = Math.max(1, Math.round(t - e.startedAt)); e.end = e.onset + e.durationMs;
    lastRelease = t; restLimit = defaultGap() * 3;
    releaseVoice(liveVoice, audio.currentTime); liveVoice = null; active = null;
    updateKeys(); renderHistory(true); animate();
  }
  function start(midi, owner) {
    const t = now();
    stopPlayback();
    if (active) endActive(null, t);
    const previous = events[events.length - 1];
    const gap = previous ? (t - lastRelease >= restLimit ? Math.round(restLimit / 3) : Math.max(0, Math.round(t - lastRelease))) : 0;
    const e = { midi, gapMs: gap, onset: previous ? previous.end + gap : 0, startedAt: t, end: null, durationMs: 0 };
    events.push(e); active = { owner, event: e }; focusedMidi = midi;
    liveVoice = voice(midi, context().currentTime, null);
    updateKeys(); renderHistory(true); animate();
  }
  function updateKeys() {
    piano.querySelectorAll('[data-midi]').forEach(key => {
      const midi = Number(key.dataset.midi); key.classList.toggle('is-playing', !!active && active.event.midi === midi);
      key.tabIndex = midi === focusedMidi ? 0 : -1;
    });
  }
  function renderPiano() {
    octaveLabel.textContent = `C${octave} – B${octave + 1}`;
    root.querySelector('[data-octave="-1"]').disabled = octave === 1;
    root.querySelector('[data-octave="1"]').disabled = octave === 4;
    piano.innerHTML = '';
    for (let oct = octave; oct <= octave + 1; oct++) {
      const group = document.createElement('div'); group.className = 'melody-piano-octave';
      let whiteIndex = 0;
      for (let pc = 0; pc < 12; pc++) {
        const midi = (oct + 1) * 12 + pc, black = BLACK.has(pc), key = document.createElement('button');
        key.type = 'button'; key.className = `melody-key ${black ? 'black' : 'white'}`;
        key.dataset.midi = midi; key.setAttribute('aria-label', pitchName(midi)); key.textContent = pitchName(midi);
        if (black) key.style.left = `${whiteIndex / 7 * 100}%`; else whiteIndex++;
        group.appendChild(key);
      }
      piano.appendChild(group);
    }
    updateKeys();
  }
  // Create each caption and bar once so animation never replaces focused controls.
  function renderHistory(follow = false) {
    const i = events.length - 1;
    const e = events[i];
    if (e) {
      if (!e.bar) {
        e.bar = document.createElement('div');
        e.bar.className = 'melody-history-bar';
        e.bar.style.left = `${e.onset * 0.09}px`;
        bars.appendChild(e.bar);
        if (i) {
          const gap = document.createElement('span');
          gap.className = 'melody-history-gap';
          gap.textContent = String(e.gapMs);
          labels.appendChild(gap);
        }
        e.note = document.createElement('button');
        e.note.type = 'button';
        e.note.className = 'melody-history-note';
        e.note.dataset.index = i;
        e.note.textContent = pitchName(e.midi);
        labels.appendChild(e.note);
      }
      updateHistoryNote(e);
    }
    appendButton.disabled = selected.size === 0;
    updateHistoryTail(follow);
  }
  function updateHistoryNote(e) {
    const d = duration(e);
    e.bar.style.width = `${Math.max(1, d * 0.09)}px`;
    e.note.style.width = `${Math.max(38, d * 0.09)}px`;
    e.note.disabled = e.end == null;
    e.note.title = `${pitchName(e.midi)} · ${Math.round(d)} ms`;
    e.note.classList.toggle('selected', selected.has(e));
  }
  function updateHistoryTail(follow) {
    const last = events[events.length - 1];
    if (active) updateHistoryNote(active.event);
    const elapsed = last ? last.onset + duration(last) : 0;
    const tail = active || lastRelease == null ? 0 : Math.min(now() - lastRelease, restLimit);
    bars.style.width = `${Math.max(1, (elapsed + tail) * 0.09)}px`;
    history.style.minWidth = `max(100%, ${(elapsed + tail) * 0.09}px)`;
    if (follow) scroll.scrollLeft = scroll.scrollWidth;
  }
  function animate() {
    if (frame) return;
    const tick = () => {
      frame = 0;
      updateHistoryTail(true);
      if (active || (lastRelease != null && now() - lastRelease < restLimit)) {
        frame = requestAnimationFrame(tick);
      }
    };
    frame = requestAnimationFrame(tick);
  }
  function copySelection(chosen) {
    const list = events.filter(e => chosen.has(e) && e.end != null);
    if (!list.length) return;
    appendNotes(list.map((e, i) => ({ midi: e.midi, durationMs: e.durationMs, gapMs: i ? e.onset - list[i - 1].end : 0 })));
  }
  root.querySelectorAll('[data-octave]').forEach(button => button.addEventListener('click', () => {
    endActive(); octave = Math.max(1, Math.min(4, octave + Number(button.dataset.octave)));
    focusedMidi = Math.max((octave + 1) * 12, Math.min((octave + 3) * 12 - 1, focusedMidi)); renderPiano();
  }));
  piano.addEventListener('pointerdown', e => {
    const key = e.target.closest('[data-midi]'); if (!key || e.button !== 0) return;
    e.preventDefault(); key.focus(); key.setPointerCapture(e.pointerId); start(Number(key.dataset.midi), `pointer:${e.pointerId}`);
  });
  piano.addEventListener('pointerup', e => endActive(`pointer:${e.pointerId}`));
  piano.addEventListener('pointercancel', e => endActive(`pointer:${e.pointerId}`));
  piano.addEventListener('lostpointercapture', e => endActive(`pointer:${e.pointerId}`));
  piano.addEventListener('keydown', e => {
    const key = e.target.closest('[data-midi]'); if (!key) return;
    if (e.key === ' ' || e.key === 'Spacebar') { e.preventDefault(); if (!e.repeat) start(Number(key.dataset.midi), 'keyboard'); }
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault(); endActive('keyboard'); focusedMidi = Math.max(24, Math.min(83, Number(key.dataset.midi) + (e.key === 'ArrowRight' ? 1 : -1)));
      if (focusedMidi < (octave + 1) * 12) octave = Math.max(1, octave - 1);
      if (focusedMidi >= (octave + 3) * 12) octave = Math.min(4, octave + 1);
      renderPiano(); piano.querySelector(`[data-midi="${focusedMidi}"]`).focus();
    }
  });
  piano.addEventListener('keyup', e => { if (e.key === ' ' || e.key === 'Spacebar') { e.preventDefault(); endActive('keyboard'); } });
  window.addEventListener('blur', () => { endActive(); stopPlayback(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) { endActive(); stopPlayback(); } });
  piano.addEventListener('focusout', e => { if (!piano.contains(e.relatedTarget)) endActive('keyboard'); });
  history.addEventListener('pointerdown', e => {
    if (e.button !== 0) return;
    const rect = history.getBoundingClientRect();
    drag = { x: e.clientX - rect.left, y: e.clientY - rect.top, moved: false, pointer: e.pointerId, note: e.target.closest('[data-index]') };
    history.setPointerCapture(e.pointerId);
  });
  history.addEventListener('pointermove', e => {
    if (!drag || drag.pointer !== e.pointerId) return;
    const rect = history.getBoundingClientRect(), x = e.clientX - rect.left, y = e.clientY - rect.top;
    if (Math.hypot(x - drag.x, y - drag.y) > 4) drag.moved = true;
    if (!drag.moved) return;
    const left = Math.min(x, drag.x), top = Math.min(y, drag.y), right = Math.max(x, drag.x), bottom = Math.max(y, drag.y);
    Object.assign(marquee.style, { left: `${left}px`, top: `${top}px`, width: `${right-left}px`, height: `${bottom-top}px` }); marquee.hidden = false;
    selected.clear(); labels.querySelectorAll('[data-index]').forEach(note => {
      const r = note.getBoundingClientRect(), l = r.left - rect.left, t = r.top - rect.top;
      if (!note.disabled && l <= right && l + r.width >= left && t <= bottom && t + r.height >= top) selected.add(events[Number(note.dataset.index)]);
      note.classList.toggle('selected', selected.has(events[Number(note.dataset.index)]));
    });
    appendButton.disabled = !selected.size;
  });
  history.addEventListener('pointerup', e => {
    if (!drag || drag.pointer !== e.pointerId) return;
    if (!drag.moved && drag.note && !drag.note.disabled) copySelection(new Set([events[Number(drag.note.dataset.index)]]));
    drag = null; marquee.hidden = true;
  });
  history.addEventListener('pointercancel', () => { drag = null; marquee.hidden = true; });
  labels.addEventListener('click', e => {
    // Pointer clicks are handled at pointerup, after distinguishing a drag.
    if (e.detail !== 0) return;
    const note = e.target.closest('[data-index]');
    if (note && !note.disabled) copySelection(new Set([events[Number(note.dataset.index)]]));
  });
  function clearHistory() {
    endActive();
    events = [];
    selected.clear();
    lastRelease = null;
    bars.replaceChildren();
    labels.replaceChildren();
    renderHistory();
  }
  root.querySelector('.melody-clear-history').addEventListener('click', clearHistory);
  appendButton.addEventListener('click', () => copySelection(selected));
  renderPiano(); renderHistory();
  return {
    play(notes) {
      endActive(); stopPlayback(false); if (!notes.length) return;
      const ctx = context(), generation = ++playGeneration; playing = true;
      const begin = ctx.currentTime + 0.03; let offset = 0;
      notes.forEach((note, i) => {
        if (i) offset += Math.max(0, Number(note.gapMs) || 0) / 1000;
        const end = offset + Math.max(1, Number(note.durationMs) || 1) / 1000;
        scheduled.push(voice(note.midi, begin + offset, begin + end)); offset = end;
      });
      playTimer = setTimeout(() => { if (generation === playGeneration) { scheduled = []; playing = false; if (onPlayEnd) onPlayEnd(); } }, (offset + 0.06) * 1000);
    },
    stop() { endActive(); stopPlayback(); },
    hide() { endActive(); stopPlayback(); },
    clearHistory
  };
}
