// A deterministic dashboard fixture that can run without a shared dashboard
// package. If a public dashboard library is present, it may render the board;
// otherwise this module renders a small text-only fallback.
(function (root) {
  'use strict';

  function render(refs, data) {
    if (!refs || !refs.container) return;
    const message = String((data && data.demo && data.demo.message) || 'fixture ready');
    const library = root && root.PublicDashboardLibrary;
    if (library && typeof library.renderBoard === 'function' && library.boards && library.boards.demo) {
      library.renderBoard(refs.container, library.boards.demo, { message }, { mode: 'interactive' });
      return;
    }
    refs.container.textContent = '';
    const line = refs.container.ownerDocument.createElement('p');
    line.className = 'dashboard-fixture-message';
    line.textContent = message;
    refs.container.appendChild(line);
  }

  function mount(container) {
    const refs = { container };
    render(refs, { demo: { message: 'desktop fixture ready' } });
    return refs;
  }

  function update(refs, data) { render(refs, data); }

  function unmount(refs) {
    if (refs && refs.container) refs.container.textContent = '';
  }

  const dashboard = {
    id: 'shared-demo',
    name: 'Shared Demo',
    description: 'A deterministic dashboard fixture for local renderer tests.',
    color: 'var(--green, #56d364)',
    mount,
    update,
    unmount,
    pollFn: () => ({ demo: { message: 'desktop fixture update' } }),
    pollInterval: 5000,
    idlePollInterval: 30000,
    idleFn: () => false,
  };

  if (root && root.DASHBOARDS) root.DASHBOARDS.push(dashboard);
  if (typeof module !== 'undefined' && module.exports) module.exports = { dashboard, mount, update, unmount, render };
})(typeof window !== 'undefined' ? window : globalThis);
