'use strict';

// UI preferences only. The daemon remains authoritative for session lifetime.
const WORKSPACE_STORAGE_KEY = 'pentacle.workspace.v1';
const validSlot = value => Number.isInteger(value) && value >= 0 && value < 4;
function normalizeWorkspace(value = {}) {
  const raw = value && typeof value === 'object' ? value : {};
  const paneCount = Number.isInteger(raw.paneCount) && raw.paneCount >= 1 && raw.paneCount <= 4 ? raw.paneCount : 1;
  const activeSlot = validSlot(raw.activeSlot) ? raw.activeSlot : 0;
  return {
    paneCount, activeSlot,
    visibleSlots: visibleWorkspaceSlots(paneCount, activeSlot, raw.visibleSlots),
    bindings: Array.from({ length: 4 }, (_, index) => {
      const item = Array.isArray(raw.bindings) ? raw.bindings[index] : null;
      if (!item || typeof item.name !== 'string' || !item.name || item.name.length > 512
        || typeof item.hostId !== 'string' || !item.hostId || item.hostId.length > 128) return null;
      return {
        name: item.name, hostId: item.hostId,
        mode: ['chat', 'terminal', 'status'].includes(item.mode) ? item.mode : 'chat',
        draft: typeof item.draft === 'string' ? item.draft.slice(0, 100000) : '',
      };
    }),
  };
}
function visibleWorkspaceSlots(paneCount, activeSlot, previousSlots = []) {
  const count = Number.isInteger(paneCount) && paneCount >= 1 && paneCount <= 4 ? paneCount : 1;
  const active = validSlot(activeSlot) ? activeSlot : 0;
  // Visibility is a selection, not a function of focus. Keep it stable when
  // focusing an already-visible pane; only resizing or revealing a hidden pane
  // replaces a member. Old preferences without visibleSlots migrate naturally.
  const slots = [...new Set(Array.isArray(previousSlots) ? previousSlots.filter(validSlot) : [])].slice(0, count);
  for (let slot = 0; slots.length < count && slot < 4; slot++) {
    if (!slots.includes(slot)) slots.push(slot);
  }
  if (!slots.includes(active)) slots[slots.length - 1] = active;
  return slots;
}
function defaultSessionView(chatEnabled, session) {
  return chatEnabled && session?.provider !== 'terminal' ? 'chat' : 'terminal';
}
module.exports = { WORKSPACE_STORAGE_KEY, normalizeWorkspace, visibleWorkspaceSlots, defaultSessionView };
