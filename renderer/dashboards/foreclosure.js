// ── Foreclosure Pipeline Dashboard (desktop adapter) ─────────
// Self-registering — pushes to window.DASHBOARDS on load.
//
// The board itself (DOM, stage flow, drill-down modals, skiptrace gate, batch
// selector, distribution pills, optimistic gate guard) now lives ONCE in the
// shared triforce-dashboards definition layer (loaded from the
// triforce-dashboards submodule before this file). The Pi
// renders the same definition in display mode. This adapter:
//   • renders the shared board in INTERACTIVE mode,
//   • injects the desktop's chat_streamd IPC actions via ctx.actions
//     (setBatchGate / refetch / refreshNow) — the shared lib never touches
//     window.cc directly,
//   • keeps the desktop's data poll (window.cc.getPipelineStats, pinned to the
//     selected batch).
//
// Opt-in adapter; deployment configuration is supplied by the caller.

(function() {
'use strict';

// The shared board (classic UMD global, loaded before this script). When
// present we delegate all rendering to the ONE canonical foreclosure
// definition; falls back to a notice if the global is somehow absent.
function sharedBoard() {
  const TD = (typeof window !== 'undefined' && window.TriforceDashboards) || null;
  return TD && TD.boards && TD.boards.foreclosure ? TD : null;
}

// Late-bound IPC actions — read window.cc at call time so a reconnect that
// swaps the bridge is picked up, and so unit tests can stub window.cc.
function foreclosureActions() {
  return {
    // `setting` is optional (defaults downstream to auto_submit_skipmatrix) so
    // the current skiptrace-gate callers pass (batch, gate) unchanged while a
    // future pay-gate control can pass the third arg.
    setBatchGate: (batch, gate, setting) =>
      (window.cc && typeof window.cc.setBatchGate === 'function')
        ? window.cc.setBatchGate(batch, gate, setting)
        : Promise.resolve({ ok: false, error: 'setBatchGate unavailable' }),
    refetch: () => {
      if (typeof window.retryDashboardPoll === 'function') window.retryDashboardPoll();
    },
    refreshNow: () => {
      if (typeof window.refreshDashboardNow === 'function') window.refreshDashboardNow();
      else if (typeof window.retryDashboardPoll === 'function') window.retryDashboardPoll();
    },
  };
}

function mount(container) {
  const TD = sharedBoard();
  if (TD) {
    return TD.mountBoard(container, TD.boards.foreclosure, undefined, {
      mode: 'interactive',
      actions: foreclosureActions(),
    });
  }
  if (container) {
    container.innerHTML = '<div class="pipeline-loading">Foreclosure dashboard unavailable — shared dashboard layer not loaded.</div>';
  }
  return { __fallback: true };
}

function update(refs, data) {
  const TD = sharedBoard();
  if (TD && refs && !refs.__fallback) TD.updateBoard(refs, TD.boards.foreclosure, data);
}

function unmount(refs) {
  const TD = sharedBoard();
  if (TD && refs && !refs.__fallback) TD.unmountBoard(refs, TD.boards.foreclosure);
}

// Dashboard is "idle" when every stage in the canonical list is complete.
// In that state there's nothing to refresh and we drop to the slow poll
// interval until a new batch starts running a stage again.
function _isIdle(data) {
  const stages = data && data.pipeline_stages;
  if (!stages || !stages.length) return false;
  return stages.every(s => s.state === 'complete');
}

// ── Self-register ──
// Show when we have a Dashboard Hub configured. Older configs also surface it
// in explicitly configured remote-client mode for compatibility.
const _isClient = !!(window.HOST && window.HOST.isClient);
const _hasRemote = !!(window.HOST && window.HOST.hasRemote);
const _hasDashboardHub = !!(window.HOST && window.HOST.hasDashboardHub);
const _showForeclosure = _hasDashboardHub
  || (_isClient && _hasRemote);
if (_showForeclosure) {
  window.DASHBOARDS.push({
    id: 'foreclosure-pipeline', retired: true,
    name: 'Foreclosure Pipeline',
    description: 'Scraping + skiptrace flow with stage status',
    color: 'var(--green)',
    mount, update, unmount,
    pollFn: () => {
      const root = document.querySelector('.foreclosure-dashboard');
      const pinned = root && root.dataset.selectedBatch;
      return window.cc.getPipelineStats(pinned || undefined);
    },
    pollInterval: 10000,        // 10s when a stage is active
    idlePollInterval: 60000,    // 60s when everything is complete
    idleFn: _isIdle,
  });
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { mount, update, unmount };
}

})();
