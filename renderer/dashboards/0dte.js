// ── 0DTE Trading desktop adapter ─────────────────────────────
// The board (trader selector + P&L stats + last-scan + positions tables) now
// lives ONCE in the shared triforce-dashboards layer. This adapter renders it
// interactive, injects the chat_streamd data actions via ctx.actions
// (listTraders → window.cc.list0dteTraders, fetchStats → window.cc.get0dteStats),
// and keeps the desktop poll (window.cc.get0dteStats for the selected trader).
// The trader selector is interactiveOnly — the Pi renders the selected trader
// display-only.
(function() {
'use strict';

const _LS_KEY = 'pentacle.0dte.selected_trader';

function sharedBoard() {
  const TD = (typeof window !== 'undefined' && window.TriforceDashboards) || null;
  return TD && TD.boards && TD.boards['0dte-trading'] ? TD : null;
}

function zdteActions() {
  return {
    listTraders: () =>
      (window.cc && typeof window.cc.list0dteTraders === 'function')
        ? window.cc.list0dteTraders()
        : Promise.resolve({ traders: [] }),
    fetchStats: (trader) =>
      (window.cc && typeof window.cc.get0dteStats === 'function')
        ? window.cc.get0dteStats(trader)
        : Promise.resolve({ error: 'get0dteStats unavailable' }),
  };
}

function mount(container) {
  const TD = sharedBoard();
  if (TD) return TD.mountBoard(container, TD.boards['0dte-trading'], undefined, { mode: 'interactive', actions: zdteActions() });
  if (container) container.innerHTML = '<div style="padding:24px;color:#888;">0DTE Trading unavailable — shared dashboard layer not loaded.</div>';
  return { __fallback: true };
}
function update(refs, data) {
  const TD = sharedBoard();
  if (TD && refs && !refs.__fallback) TD.updateBoard(refs, TD.boards['0dte-trading'], data);
}
function unmount(refs) {
  const TD = sharedBoard();
  if (TD && refs && !refs.__fallback) TD.unmountBoard(refs, TD.boards['0dte-trading']);
}

function pollFn(refs) {
  const trader = (refs && refs.selectedTrader)
    || (typeof localStorage !== 'undefined' && localStorage.getItem(_LS_KEY))
    || undefined;
  return window.cc.get0dteStats(trader);
}

window.DASHBOARDS.push({
  id: '0dte-trading',
  name: '0DTE Trading',
  description: 'SPX iron condor pipeline — multi-trader live snapshots',
  color: 'var(--blue)',
  mount, update, unmount, pollFn,
  pollInterval: 5000,
});

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { mount, update, unmount, pollFn };
}

})();
