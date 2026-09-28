// ── Agent Stream (chat-stream) desktop adapter ───────────────
// The board (session list + transcript timeline + stats, with local session
// selection) now lives ONCE in the shared triforce-dashboards layer. This
// adapter renders it interactive and keeps the desktop data poll
// (window.cc.getChatStreamState). No window.cc writes — session selection is
// local navigation, so the same definition renders display-mode on the Pi.
(function() {
'use strict';

function sharedBoard() {
  const TD = (typeof window !== 'undefined' && window.TriforceDashboards) || null;
  return TD && TD.boards && TD.boards['chat-stream'] ? TD : null;
}

function mount(container) {
  const TD = sharedBoard();
  if (TD) return TD.mountBoard(container, TD.boards['chat-stream'], undefined, { mode: 'interactive' });
  if (container) container.innerHTML = '<div style="padding:18px;color:#7f9187;">Agent Stream unavailable — shared dashboard layer not loaded.</div>';
  return { __fallback: true };
}
function update(refs, data) {
  const TD = sharedBoard();
  if (TD && refs && !refs.__fallback) TD.updateBoard(refs, TD.boards['chat-stream'], data);
}
function unmount(refs) {
  const TD = sharedBoard();
  if (TD && refs && !refs.__fallback) TD.unmountBoard(refs, TD.boards['chat-stream']);
}

window.DASHBOARDS.push({
  id: 'chat-stream',
  name: 'Agent Stream',
  description: 'Merged event stream from configured providers',
  color: 'var(--cyan, #2dd4bf)',
  mount,
  update,
  unmount,
  pollFn: () => window.cc.getChatStreamState(),
  pollInterval: 1500,
  idlePollInterval: 4000,
  idleFn: (data) => !data || !data.connected,
});

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { mount, update, unmount };
}

})();
