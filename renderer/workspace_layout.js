'use strict';

// Pane-count workspace preferences. UI state only: the daemon remains
// authoritative for session lifetime, so a restore never creates a session.
// Derived from PR #55 by Sai Yeluru.
const WORKSPACE_STORAGE_KEY = 'pentacle.workspace.v1';
const DEFAULT_PANE_COUNT = 1;
const validSlot = value => Number.isInteger(value) && value >= 0 && value < 4;
const validCount = value => Number.isInteger(value) && value >= 1 && value <= 4;

function normalizeWorkspace(value = {}) {
  const raw = value && typeof value === 'object' ? value : {};
  const paneCount = validCount(raw.paneCount) ? raw.paneCount : DEFAULT_PANE_COUNT;
  const activeSlot = validSlot(raw.activeSlot) ? raw.activeSlot : 0;
  return {
    paneCount, activeSlot,
    visibleSlots: visibleWorkspaceSlots(paneCount, activeSlot, raw.visibleSlots),
    bindings: Array.from({ length: 4 }, (_, index) => {
      const item = Array.isArray(raw.bindings) ? raw.bindings[index] : null;
      if (!item || typeof item.name !== 'string' || !item.name || item.name.length > 512
        || typeof item.hostId !== 'string' || !item.hostId || item.hostId.length > 128) return null;
      const source = typeof item.assistantSourceId === 'string' && item.assistantSourceId
        && item.assistantSourceId.length <= 512 ? { assistantSourceId: item.assistantSourceId } : {};
      return {
        name: item.name, hostId: item.hostId,
        mode: ['chat', 'terminal', 'status'].includes(item.mode) ? item.mode : 'chat',
        draft: typeof item.draft === 'string' ? item.draft.slice(0, 100000) : '',
        ...source,
      };
    }),
  };
}

// Returns { saved, workspace }: `saved` is false when the browser has no
// stored choice yet (first visit), which is what enables the assistant default.
function loadWorkspace(storage) {
  let raw = null;
  try { raw = storage?.getItem(WORKSPACE_STORAGE_KEY) ?? null; } catch { raw = null; }
  if (raw === null) return { saved: false, workspace: normalizeWorkspace() };
  try { return { saved: true, workspace: normalizeWorkspace(JSON.parse(raw)) }; } catch {
    return { saved: false, workspace: normalizeWorkspace() };
  }
}

function visibleWorkspaceSlots(paneCount, activeSlot, previousSlots = []) {
  const count = validCount(paneCount) ? paneCount : DEFAULT_PANE_COUNT;
  const active = validSlot(activeSlot) ? activeSlot : 0;
  // Visibility is a selection, not a function of focus. Keep it stable when
  // focusing an already-visible pane; only resizing or revealing a hidden pane
  // replaces a member.
  const slots = [...new Set(Array.isArray(previousSlots) ? previousSlots.filter(validSlot) : [])].slice(0, count);
  for (let slot = 0; slots.length < count && slot < 4; slot++) {
    if (!slots.includes(slot)) slots.push(slot);
  }
  if (!slots.includes(active)) slots[slots.length - 1] = active;
  return slots;
}

// Where a newly opened session goes: the first empty visible pane, otherwise
// the focused pane (which is always visible).
function placementSlot(workspace, slots) {
  const empty = workspace.visibleSlots.find(slot => !slots[slot]);
  return empty === undefined ? workspace.activeSlot : empty;
}

// A session without chat-stream state (a plain terminal) has no transcript to
// show, so it opens in Terminal regardless of the Default view setting.
function defaultSessionView(defaultMode, streamSession) {
  return streamSession ? defaultMode : 'terminal';
}

// The assistant composite a first visit opens: exactly one visible composite
// in the daemon inventory, otherwise none (no guessing between several).
function uniqueAssistantComposite(sessions) {
  const composites = (Array.isArray(sessions) ? sessions : []).filter(session =>
    session && session.session_kind === 'assistant_composite'
    && session.visibility !== 'hidden' && session.closed_at == null && session.status !== 'closed');
  return composites.length === 1 ? composites[0] : null;
}

module.exports = {
  WORKSPACE_STORAGE_KEY, DEFAULT_PANE_COUNT, normalizeWorkspace, loadWorkspace,
  visibleWorkspaceSlots, placementSlot, defaultSessionView, uniqueAssistantComposite,
};
