(function (root, factory) {
  'use strict';

  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root && root.DASHBOARDS && !root.DASHBOARDS.some(board => board.id === 'travel-engine')) {
    root.DASHBOARDS.push(api.createDashboard({ windowRef: root }));
  }
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';

  const READY_TIMEOUT_MS = 10_000;
  const SANDBOX = 'allow-scripts allow-same-origin allow-forms allow-modals';
  const READY_MESSAGE = 'travel-engine:ready';
  const OPEN_EXTERNAL_MESSAGE = 'travel-engine:open-external';

  function validateEngineUrl(raw, pageLocation) {
    if (typeof raw !== 'string' || !raw.trim()) {
      return { ok: false, error: 'Travel Engine is not configured. Add travelEngine.url to the private Pentacle config and restart the app.' };
    }

    const value = raw.trim();
    if (!/^https?:\/\/[^/?#]+\/?$/i.test(value)) {
      return { ok: false, error: 'travelEngine.url must be an absolute HTTP(S) origin with an optional trailing slash.' };
    }

    let parsed;
    try { parsed = new URL(value); } catch {
      return { ok: false, error: 'travelEngine.url is not a valid HTTP(S) origin.' };
    }
    if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password) {
      return { ok: false, error: 'travelEngine.url must use HTTP(S), include a host, and contain no credentials.' };
    }
    if (pageLocation?.protocol === 'https:' && parsed.protocol !== 'https:') {
      return { ok: false, error: 'Travel Engine must use HTTPS when Pentacle is served over HTTPS.' };
    }
    const pageOrigin = pageLocation?.origin;
    if (pageOrigin && pageOrigin !== 'null' && pageOrigin === parsed.origin) {
      return { ok: false, error: 'Travel Engine must use a different origin from Pentacle.' };
    }
    return { ok: true, url: parsed.href, origin: parsed.origin };
  }

  function validateExternalUrl(raw) {
    if (typeof raw !== 'string' || !raw.trim()) return null;
    try {
      const parsed = new URL(raw);
      if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password) return null;
      return parsed.href;
    } catch { return null; }
  }

  function addText(doc, parent, tag, className, value) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    node.textContent = value;
    parent.appendChild(node);
    return node;
  }

  function renderStatus(refs, title, detail, retryVisible = false, unavailable = false) {
    const doc = refs.document;
    refs.status.replaceChildren();
    refs.status.className = `travel-engine-status${unavailable ? ' is-unavailable' : ''}`;
    refs.status.setAttribute('role', unavailable ? 'alert' : 'status');
    refs.status.setAttribute('aria-live', 'polite');
    addText(doc, refs.status, 'h2', 'travel-engine-status-title', title);
    addText(doc, refs.status, 'p', 'travel-engine-status-detail', detail);
    if (retryVisible) {
      const retry = addText(doc, refs.status, 'button', 'travel-engine-retry', 'Retry');
      retry.type = 'button';
      retry.addEventListener('click', refs.retry);
    }
    refs.status.hidden = false;
    refs.iframe.hidden = refs.ready !== true;
  }

  function createDashboard({ windowRef, timeoutMs = READY_TIMEOUT_MS } = {}) {
    const win = windowRef || (typeof window !== 'undefined' ? window : null);

    function mount(container) {
      const doc = container.ownerDocument;
      const config = win?.HOST?.travelEngineConfig;
      const validated = validateEngineUrl(config?.url, win?.location);
      const refs = {
        container, document: doc, windowRef: win, iframe: null, status: null,
        timer: null, retryTimer: null, ready: false, active: true,
        origin: validated.ok ? validated.origin : null, url: validated.ok ? validated.url : null,
        retry: () => {}, onMessage: null,
      };
      container.classList.add('travel-engine-dashboard');

      if (!validated.ok) {
        const panel = doc.createElement('section');
        panel.className = 'travel-engine-panel';
        addText(doc, panel, 'h2', 'travel-engine-title', 'Travel Engine');
        addText(doc, panel, 'p', 'travel-engine-message', validated.error);
        container.replaceChildren(panel);
        return refs;
      }

      const panel = doc.createElement('section');
      panel.className = 'travel-engine-panel';
      const status = doc.createElement('div');
      const iframe = doc.createElement('iframe');
      iframe.className = 'travel-engine-frame';
      iframe.title = 'Travel Engine';
      iframe.setAttribute('sandbox', SANDBOX);
      iframe.setAttribute('allow', 'clipboard-write');
      iframe.setAttribute('referrerpolicy', 'no-referrer');
      iframe.style.cssText = 'display:block;width:100%;height:calc(100vh - 170px);min-height:480px;border:0;background:#fff';
      panel.append(status, iframe);
      container.replaceChildren(panel);
      refs.iframe = iframe;
      refs.status = status;

      function clearTimers() {
        if (refs.timer !== null) win.clearTimeout(refs.timer);
        if (refs.retryTimer !== null) win.clearTimeout(refs.retryTimer);
        refs.timer = null;
        refs.retryTimer = null;
      }

      function startLoad() {
        if (!refs.active) return;
        refs.ready = false;
        renderStatus(refs, 'Connecting to Travel Engine', `Waiting for the service at ${refs.origin}.`);
        iframe.hidden = true;
        iframe.src = refs.url;
        refs.timer = win.setTimeout(() => {
          refs.timer = null;
          if (!refs.active || refs.ready) return;
          renderStatus(refs, 'Travel Engine is unavailable',
            `No ready response arrived from ${refs.origin} within ${timeoutMs / 1000} seconds. Check that the service is running, then retry.`, true, true);
        }, timeoutMs);
      }

      refs.retry = () => {
        clearTimers();
        refs.ready = false;
        renderStatus(refs, 'Connecting to Travel Engine', `Retrying the service at ${refs.origin}.`);
        iframe.hidden = true;
        iframe.src = 'about:blank';
        refs.retryTimer = win.setTimeout(() => {
          refs.retryTimer = null;
          startLoad();
        }, 0);
      };

      refs.onMessage = event => {
        if (!refs.active || event.source !== iframe.contentWindow || event.origin !== refs.origin) return;
        const data = event.data;
        if (!data || typeof data !== 'object' || Array.isArray(data)) return;
        if (data.type === READY_MESSAGE && data.version === 1) {
          refs.ready = true;
          if (refs.timer !== null) win.clearTimeout(refs.timer);
          refs.timer = null;
          status.hidden = true;
          iframe.hidden = false;
          return;
        }
        if (!refs.ready || data.type !== OPEN_EXTERNAL_MESSAGE || typeof data.url !== 'string') return;
        const url = validateExternalUrl(data.url);
        if (!url || typeof win.cc?.openExternal !== 'function') return;
        try { Promise.resolve(win.cc.openExternal(url)).catch(() => {}); } catch { /* host denied the external link */ }
      };

      win.addEventListener('message', refs.onMessage);
      startLoad();
      return refs;
    }

    function unmount(refs) {
      if (!refs) return;
      refs.active = false;
      if (refs.timer !== null) win.clearTimeout(refs.timer);
      if (refs.retryTimer !== null) win.clearTimeout(refs.retryTimer);
      if (refs.onMessage) win.removeEventListener('message', refs.onMessage);
      refs.container?.classList.remove('travel-engine-dashboard');
      refs.container?.replaceChildren();
    }

    return {
      id: 'travel-engine',
      name: 'Travel Engine',
      description: 'Search flights, manage points, and review booking options.',
      color: 'var(--blue, #58a6ff)',
      mount,
      unmount,
    };
  }

  return {
    READY_TIMEOUT_MS,
    SANDBOX,
    READY_MESSAGE,
    OPEN_EXTERNAL_MESSAGE,
    validateEngineUrl,
    validateExternalUrl,
    createDashboard,
  };
});
