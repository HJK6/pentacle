// Configured viewer adapter. "Loaded" means navigation completed, not that a
// cross-origin model or authenticated page was verified (see dashboards_view.md).
(function(root) {
  'use strict';
  const LOAD_TIMEOUT_MS = 15000;

  function viewerUrl(config) {
    const value = config?.dashboards?.modeler3d?.url;
    if (typeof value !== 'string' || !value.trim()) return null;
    try {
      const url = new URL(value);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null;
      return url.href;
    } catch { return null; }
  }

  function mount(container, { config = {} } = {}) {
    const doc = container.ownerDocument;
    const shell = doc.createElement('section');
    shell.className = 'modeler-3d';
    const header = doc.createElement('header');
    header.className = 'modeler-header';
    const title = doc.createElement('h1');
    title.textContent = '3D Modeler';
    const controls = doc.createElement('div');
    controls.className = 'modeler-controls';
    const open = doc.createElement('a');
    open.dataset.modelerOpen = '';
    open.textContent = 'Open in new window';
    open.target = '_blank';
    open.rel = 'noopener noreferrer';
    const reload = doc.createElement('button');
    reload.type = 'button';
    reload.dataset.modelerReload = '';
    reload.textContent = 'Reload';
    controls.append(open, reload);
    header.append(title, controls);
    const status = doc.createElement('p');
    status.className = 'modeler-status';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    const openError = doc.createElement('p');
    openError.className = 'modeler-open-error';
    openError.setAttribute('role', 'alert');
    openError.hidden = true;
    const viewport = doc.createElement('div');
    viewport.className = 'modeler-viewport';
    shell.append(header, status, openError, viewport);
    container.appendChild(shell);
    const refs = { container, shell, frame: null, timer: null, generation: 0, disposed: false, cleanupFrame: null };

    function setState(state, message) {
      shell.dataset.modelerState = state;
      status.textContent = message;
    }
    function clearFrame() {
      refs.generation++;
      if (refs.timer !== null) clearTimeout(refs.timer);
      refs.timer = null;
      refs.cleanupFrame?.();
      refs.cleanupFrame = null;
      refs.frame?.remove();
      refs.frame = null;
    }
    function load() {
      if (refs.disposed) return;
      clearFrame();
      openError.hidden = true;
      openError.textContent = '';
      const url = viewerUrl(config);
      open.removeAttribute('href');
      open.setAttribute('aria-disabled', String(!url));
      if (!url) {
        setState('unconfigured', 'Viewer unconfigured. Set dashboards.modeler3d.url to an absolute HTTP(S) viewer URL without embedded credentials in local configuration.');
        return;
      }
      open.href = url;
      setState('loading', 'Loading viewer…');
      const generation = refs.generation;
      const frame = doc.createElement('iframe');
      frame.title = '3D Modeler viewer';
      frame.setAttribute('sandbox', 'allow-scripts allow-same-origin');
      frame.setAttribute('referrerpolicy', 'no-referrer');
      function finish(state, message) {
        if (refs.disposed || refs.generation !== generation || shell.dataset.modelerState !== 'loading') return;
        if (refs.timer !== null) clearTimeout(refs.timer);
        refs.timer = null;
        setState(state, message);
      }
      const onLoad = () => finish('loaded', 'Loaded');
      const onError = () => finish('blocked', 'Viewer could not be loaded. The host may refuse framing or require authentication. Open it in a new window or reload.');
      frame.addEventListener('load', onLoad);
      frame.addEventListener('error', onError);
      refs.cleanupFrame = () => {
        frame.removeEventListener('load', onLoad);
        frame.removeEventListener('error', onError);
      };
      refs.frame = frame;
      refs.timer = setTimeout(() => finish('blocked', 'Viewer load timed out. The host may be unreachable, refuse framing, or require authentication. Open it in a new window or reload.'), LOAD_TIMEOUT_MS);
      frame.src = url;
      viewport.appendChild(frame);
    }
    function onOpen(event) {
      const url = viewerUrl(config);
      if (!url || refs.disposed) { event.preventDefault(); return; }
      // Electron denies normal new windows; its existing bridge opens the URL
      // in the user's browser. Browser-only callers retain a real anchor.
      if (typeof root.cc?.openExternal !== 'function') return;
      event.preventDefault();
      const generation = refs.generation;
      const failed = () => {
        if (refs.disposed || generation !== refs.generation) return;
        openError.textContent = 'Could not open a new window. Open the configured viewer in your browser.';
        openError.hidden = false;
      };
      try { Promise.resolve(root.cc.openExternal(url)).then(result => { if (!result?.ok) failed(); }, failed); }
      catch { failed(); }
    }
    open.addEventListener('click', onOpen);
    reload.addEventListener('click', load);
    refs.dispose = () => {
      refs.disposed = true;
      clearFrame();
      open.removeEventListener('click', onOpen);
      reload.removeEventListener('click', load);
      shell.remove();
    };
    load();
    return refs;
  }
  function update() { /* Viewer content owns its rendering; no polling. */ }
  function unmount(refs) { refs?.dispose?.(); }
  const dashboard = { id: 'modeler-3d', name: '3D Modeler',
    description: 'Explore current 3D models in your configured viewer.',
    color: 'var(--cosmic-green)', mount, update, unmount };
  if (root?.DASHBOARDS) root.DASHBOARDS.push(dashboard);
  if (typeof module !== 'undefined' && module.exports) module.exports = { dashboard, mount, update, unmount };
})(typeof window !== 'undefined' ? window : globalThis);
