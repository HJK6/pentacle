'use strict';

// UI preferences only. The daemon remains authoritative for session lifetime.
const WORKSPACE_STORAGE_KEY = 'pentacle.workspace.v1';
const validSlot = value => Number.isInteger(value) && value >= 0 && value < 4;
function normalizeWorkspace(value = {}) {
  const raw = value && typeof value === 'object' ? value : {};
  return {
    paneCount: Number.isInteger(raw.paneCount) && raw.paneCount >= 1 && raw.paneCount <= 4 ? raw.paneCount : 1,
    activeSlot: validSlot(raw.activeSlot) ? raw.activeSlot : 0,
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
function visibleWorkspaceSlots(paneCount, activeSlot) {
  const normalized = normalizeWorkspace({ paneCount, activeSlot });
  const slots = Array.from({ length: normalized.paneCount }, (_, i) => i);
  if (!slots.includes(normalized.activeSlot)) slots[slots.length - 1] = normalized.activeSlot;
  return slots;
}
function defaultSessionView(chatEnabled, session) {
  return chatEnabled && session?.provider !== 'terminal' ? 'chat' : 'terminal';
}
module.exports = { WORKSPACE_STORAGE_KEY, normalizeWorkspace, visibleWorkspaceSlots, defaultSessionView };
