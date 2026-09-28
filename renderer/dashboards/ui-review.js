// ── UI Review desktop adapter ────────────────────────────────
// The board (stats + filters + artifact list + iframe preview) now lives ONCE
// in the shared triforce-dashboards layer. This adapter renders it interactive
// (filter controls wired) and keeps the desktop poll
// (window.cc.listUiReviewArtifacts). The Pi renders the same definition in
// display mode (filters hidden — the wall is passive).
(function() {
'use strict';

function sharedBoard() {
  const TD = (typeof window !== 'undefined' && window.TriforceDashboards) || null;
  return TD && TD.boards && TD.boards['ui-review'] ? TD : null;
}

function mount(container) {
  const TD = sharedBoard();
  if (TD) return TD.mountBoard(container, TD.boards['ui-review'], undefined, { mode: 'interactive' });
  if (container) container.innerHTML = '<div style="padding:18px;color:#80958a;">UI Review unavailable — shared dashboard layer not loaded.</div>';
  return { __fallback: true };
}
function update(refs, data) {
  const TD = sharedBoard();
  if (TD && refs && !refs.__fallback) TD.updateBoard(refs, TD.boards['ui-review'], data);
}
function unmount(refs) {
  const TD = sharedBoard();
  if (TD && refs && !refs.__fallback) TD.unmountBoard(refs, TD.boards['ui-review']);
}

window.DASHBOARDS.push({
  id: 'ui-review',
  name: 'UI Review',
  description: 'Visual QA artifacts from repos on this machine',
  color: '#7ef0ba',
  mount,
  update,
  unmount,
  pollFn: () => window.cc.listUiReviewArtifacts(),
  pollInterval: 5000,
  idlePollInterval: 15000,
  idleFn: () => true,
});

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { mount, update, unmount };
}

})();
