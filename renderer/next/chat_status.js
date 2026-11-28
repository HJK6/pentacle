'use strict';

const { modelLabel } = require('../chat_presentation');
const { parseWorkingSeconds } = require('../chat_ui_state');

const SEEN_KEY = 'pentacle.next.chat.seen';
const composite = session => session?.session_kind === 'assistant_composite';
const hasConfirmedWork = (session, draft, phase) => !!session
  && !!(session.working || (!composite(session) && (draft?.raw?.working || phase === 'working')));

function formatElapsed(ms) {
  if (!Number.isFinite(ms)) return '';
  const seconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(seconds / 60);
  const two = n => String(n).padStart(2, '0');
  return seconds < 60 ? seconds + 's' : minutes < 60 ? minutes + 'm ' + two(seconds % 60) + 's'
    : Math.floor(minutes / 60) + 'h ' + two(minutes % 60) + 'm';
}

function isWorking(session, draft, phase = 'idle') {
  return hasConfirmedWork(session, draft, phase) || (!!session && !composite(session) && phase === 'pending');
}

// Each daemon report restates the turn's start to within a heartbeat's delay.
// The first one sets the clock, which then counts on its own. A later report
// replaces the start only when it disagrees by more than that delay.
const RESYNC_MS = 2500;

function workingClock() {
  const starts = new Map();
  const steady = (previous, reported) =>
    previous?.reported && Math.abs(previous.start - reported) <= RESYNC_MS ? previous.start : reported;
  return {
    retain(ids) { for (const id of starts.keys()) if (!ids.has(id)) starts.delete(id); },
    // Milliseconds until a shown time next changes, or null when none is running.
    untilNextSecond(now) {
      let wait = null;
      for (const { start, shown } of starts.values()) {
        if (shown !== now) continue;
        const next = 1000 - ((now - start) % 1000 + 1000) % 1000;
        wait = wait === null ? next : Math.min(wait, next);
      }
      return wait;
    },
    elapsed(session, draft, turn, elapsed, now) {
      const id = session?.stream_id;
      if (composite(session)) {
        if (!session.working || !Number.isFinite(elapsed)) { starts.delete(id); return ''; }
        const start = steady(starts.get(id), now - elapsed);
        starts.set(id, { start, reported: true, shown: now });
        return formatElapsed(now - start);
      }
      if (!id || !hasConfirmedWork(session, draft, turn?.phase)) { starts.delete(id); return ''; }

      const generation = session.session_generation;
      const turnStart = turn?.sentAt ?? turn?.firstServerEventAt;
      const optimisticId = turn?.optimisticId;
      const endedAt = turn?.endedAt;
      const previous = starts.get(id);
      const sameTurn = previous?.generation === generation && previous?.endedAt === endedAt
        && (previous?.turnStart == null || previous.turnStart === turnStart)
        && previous?.optimisticId === optimisticId;
      const start = elapsed != null ? steady(sameTurn ? previous : null, now - elapsed) : (sameTurn ? previous?.start : undefined) ??
        now - 1000 * (parseWorkingSeconds(draft?.raw?.working_label) ?? parseWorkingSeconds(session.working_label) ?? 0);
      starts.set(id, { generation, turnStart, optimisticId, endedAt, start, shown: now,
        reported: elapsed != null || (sameTurn && !!previous?.reported) });
      return formatElapsed(now - start);
    },
  };
}

function modelAndEffort(session) {
  const model = session?.effective_model || session?.model;
  if (!model || composite(session)) return '';
  const effort = session.effective_effort || session.effort;
  return [modelLabel(model), effort !== 'default' && effort].filter(Boolean).join(' · ');
}

function createSeenMarks(storage) {
  let saved;
  try { saved = JSON.parse(storage?.getItem(SEEN_KEY)); } catch {}
  let initialized = Array.isArray(saved);
  const marks = new Map(initialized ? saved.filter(row => Array.isArray(row) &&
    row.length === 2 && typeof row[0] === 'string' && typeof row[1] === 'string') : []);
  const fingerprint = s => JSON.stringify([s.last_kind ?? '', s.last_text ?? '']);
  function persist() { try { storage?.setItem(SEEN_KEY, JSON.stringify([...marks])); } catch {} }
  return {
    update(sessions, visible, pageVisible, inventory = false) {
      if (!initialized && !(inventory && sessions.length)) return;
      const first = !initialized;
      initialized = true;
      let changed = first;
      for (const s of sessions) {
        if (!s.stream_id) continue;
        if (s.closed === true || s.session_state === 'closed' || s.status === 'closed' || s.state === 'closed') {
          changed = marks.delete(s.stream_id) || changed;
        } else if (first || (pageVisible && visible.has(s.stream_id))) {
          const mark = fingerprint(s);
          if (marks.get(s.stream_id) !== mark) { marks.set(s.stream_id, mark); changed = true; }
        }
      }
      if (changed) persist();
    },
    forget(id) { if (marks.delete(id)) persist(); },
    unread(s) {
      return initialized && !!s?.last_kind && s.last_kind !== 'USER' && s.closed !== true
        && s.session_state !== 'closed' && s.status !== 'closed' && s.state !== 'closed' && marks.get(s.stream_id) !== fingerprint(s);
    },
  };
}

const DRAWINGS = {
  sending: ['Sending', 'stream'],
  sent: ['Sent', 'check'],
  queued: ['Queued', 'chevron'],
  cancelled: ['Cancelled', null],
  failed: ['Failed sending', 'alert'],
  indeterminate: ['Sending', 'stream'],
};
function sendLine(state, sendNowAllowed) {
  const [label, glyph] = DRAWINGS[state] ?? [];
  return { label, glyph, sendNow: state === 'queued' && !!sendNowAllowed };
}

function mountWorkingRow(container) {
  const doc = container.ownerDocument;
  const row = doc.createElement('div');
  row.className = 'next-working';
  row.setAttribute('role', 'status');
  row.innerHTML = '<i class="next-ring" aria-hidden="true"></i><span>Working</span><span class="next-working-time"></span>';
  container.before(row);
  row.hidden = true;
  return (active, elapsed) => {
    row.hidden = !active || container.hidden;
    if (row.lastElementChild.textContent !== elapsed) row.lastElementChild.textContent = elapsed;
  };
}

// The shared renderer provides escaped correlation data. Keep a detached
// button only as long as its row lives, so capability-only changes can add or
// remove it without rebuilding the transcript or drifting from the store.
const sendNowButtons = new WeakMap();
function decorateSendLines(element, store) {
  for (const row of element.querySelectorAll('.slot-chat-send-status')) {
    const button = sendNowButtons.get(row) || row.querySelector('.slot-chat-send-now');
    if (button) sendNowButtons.set(row, button);
    const state = Object.keys(DRAWINGS).find(key => row.classList.contains('is-' + key));
    const line = sendLine(state, button && store.canSendQueuedNow(button.dataset.streamId, button.dataset.optimisticId));
    if (line.sendNow && !row.contains(button)) row.append(button);
    if (!line.sendNow) button?.remove();
    if (!line.glyph || row.querySelector('.next-send-glyph')) continue;
    const glyph = row.ownerDocument.createElement('span');
    glyph.className = 'next-send-glyph ' + line.glyph;
    glyph.setAttribute('aria-hidden', 'true');
    if (line.glyph === 'stream') {
      const run = row.ownerDocument.createElement('span');
      for (let i = 0; i < 4; i++) run.append(row.ownerDocument.createElement('i'));
      glyph.append(run);
    } else glyph.textContent = line.glyph === 'check' ? '✓' : line.glyph === 'alert' ? '!' : '›';
    const text = row.firstChild;
    if (text?.nodeType === 3) text.nodeValue = line.label;
    row.prepend(glyph);
  }
}

// Daemon notices and child reports arrive as agent rows; name each kind for its look.
function decorateAgentRows(element) {
  for (const id of element.querySelectorAll('.slot-chat-v3-agent-id')) {
    const row = id.closest('.slot-chat-v3-subagent');
    if (row) row.dataset.agentKind = id.textContent === 'Daemon' ? 'daemon' : id.textContent === 'Subagent activity' ? 'report' : 'agent';
  }
}

module.exports = { SEEN_KEY, formatElapsed, isWorking, hasConfirmedWork, workingClock, modelAndEffort, createSeenMarks, sendLine, mountWorkingRow, decorateSendLines, decorateAgentRows };
