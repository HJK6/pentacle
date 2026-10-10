// Generic hosted panel. A frame load proves navigation, not app health.
(function(root) {
  'use strict';
  const urls = root.DashboardCatalogLoader || require('./catalog_loader');
  const INVALIDATE = 'pentacle:hosted-dashboard-invalidate';
  function mount(container, { hostedUrl, name = 'Dashboard', getHostedPolicy = () => root.hostedDashboardPolicy } = {}) {
    const doc = container.ownerDocument;
    const shell = doc.createElement('section'); shell.className = 'modeler-3d';
    const header = doc.createElement('header'); header.className = 'modeler-header';
    const title = doc.createElement('h1'); title.textContent = name;
    const controls = doc.createElement('div'); controls.className = 'modeler-controls';
    // A button avoids a cached href becoming an ungated browser navigation.
    const open = doc.createElement('button'); open.type = 'button'; open.dataset.modelerOpen = '';
    open.textContent = 'Open in new window';
    const reload = doc.createElement('button'); reload.type = 'button'; reload.dataset.modelerReload = ''; reload.textContent = 'Reload';
    controls.append(open, reload); header.append(title, controls);
    const status = doc.createElement('p'); status.className = 'modeler-status'; status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const openError = doc.createElement('p'); openError.className = 'modeler-open-error'; openError.setAttribute('role', 'alert'); openError.hidden = true;
    const viewport = doc.createElement('div'); viewport.className = 'modeler-viewport';
    shell.append(header, status, openError, viewport); container.appendChild(shell);
    const refs = { container, shell, frame: null, timer: null, generation: 0, disposed: false, cleanupFrame: null };
    const denied = 'Dashboard unavailable: hosted dashboards require current identity mode.';
    function setState(state, message) {
      shell.dataset.modelerState = state;
      container.dataset.boardState = state === 'loaded' ? 'ready' : state === 'loading' ? 'loading' : state === 'unavailable' ? 'unavailable' : 'error';
      status.textContent = message; reload.textContent = state === 'blocked' ? 'Retry' : 'Reload';
      root.PentacleHarness?.emit?.('dashboard:state', { subsystem: 'dashboards', bug_ref: 'hosted_dashboards_registry', data: { state } });
    }
    function clearFrame() {
      refs.generation++; clearTimeout(refs.timer); refs.timer = null; refs.cleanupFrame?.(); refs.cleanupFrame = null;
      refs.frame?.remove(); refs.frame = null;
    }
    function publishMode(mode) {
      root.hostedDashboardAuthMode = mode;
      root.dispatchEvent(new root.Event('pentacle:hosted-dashboard-mode'));
    }
    function invalidate() { if (refs.disposed) return; publishMode('unknown'); clearFrame(); setState('unavailable', denied); }
    async function admission(generation) {
      let config;
      try {
        const reply = await root.fetch('/api/config', { cache: 'no-store', credentials: 'same-origin' });
        if (!reply.ok) throw Error('config unavailable'); config = await reply.json();
      } catch {}
      if (refs.disposed || refs.generation !== generation) return null;
      publishMode(config?.hostedDashboardAuthMode === 'identity' ? 'identity' : 'unknown');
      if (config?.hostedDashboardAuthMode !== 'identity') { clearFrame(); setState('unavailable', denied); return null; }
      const url = urls.admitHostedUrl(hostedUrl, getHostedPolicy());
      if (!url) { clearFrame(); setState('unavailable', 'Dashboard unavailable: hosted URL policy is unconfigured or refuses this URL.'); return null; }
      return url;
    }
    async function load() {
      if (refs.disposed) return;
      clearFrame(); openError.hidden = true; setState('loading', 'Loading dashboard…');
      const generation = refs.generation;
      const url = await admission(generation); if (!url) return;
      const frame = doc.createElement('iframe'); frame.title = `${name} viewer`;
      frame.setAttribute('sandbox', 'allow-scripts allow-same-origin'); frame.setAttribute('referrerpolicy', 'no-referrer');
      frame.setAttribute('allow', 'xr-spatial-tracking; fullscreen'); frame.setAttribute('loading', 'lazy');
      function finish(state, message) {
        if (refs.disposed || refs.generation !== generation || shell.dataset.modelerState !== 'loading') return;
        clearTimeout(refs.timer); refs.timer = null; setState(state, message);
      }
      const onLoad = () => finish('loaded', 'Loaded');
      const onError = () => finish('blocked', 'Could not open dashboard. Retry.');
      frame.addEventListener('load', onLoad); frame.addEventListener('error', onError);
      refs.cleanupFrame = () => { frame.removeEventListener('load', onLoad); frame.removeEventListener('error', onError); };
      refs.frame = frame; refs.timer = setTimeout(() => finish('blocked', 'Could not open dashboard: navigation timed out. Retry.'), 15000);
      // Both admission results are current at the sole iframe assignment.
      frame.src = url; viewport.appendChild(frame);
    }
    async function onOpen(event) {
      event.preventDefault(); if (refs.disposed) return;
      const generation = refs.generation, url = await admission(generation); if (!url) return;
      const failed = () => { if (!refs.disposed && refs.generation === generation) { openError.textContent = 'Could not open a new window.'; openError.hidden = false; } };
      try {
        if (typeof root.cc?.openExternal === 'function') { if (!(await root.cc.openExternal(url))?.ok) failed(); }
        else if (!root.open(url, '_blank', 'noopener,noreferrer')) failed();
      } catch { failed(); }
    }
    open.addEventListener('click', onOpen); reload.addEventListener('click', load); root.addEventListener(INVALIDATE, invalidate);
    refs.dispose = () => {
      if (refs.disposed) return; refs.disposed = true; clearFrame();
      open.removeEventListener('click', onOpen); reload.removeEventListener('click', load); root.removeEventListener(INVALIDATE, invalidate); shell.remove();
    };
    void load(); return refs;
  }
  function update() {}
  function unmount(refs) { refs?.dispose?.(); }
  const dashboard = { mount, update, unmount };
  // Renderer implementation only: Modeler membership now belongs to the catalog.
  root.HostedDashboard = dashboard;
  if (typeof module !== 'undefined' && module.exports) module.exports = { dashboard, mount, update, unmount };
})(typeof window !== 'undefined' ? window : globalThis);
