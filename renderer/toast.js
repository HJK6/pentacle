// In-DOM toast notifications for the renderer.
//
// WHY this exists: the renderer used to surface errors with the browser-native
// window.alert(). On Windows/Electron, dismissing a native JS dialog leaves
// Chromium swallowing the Space (and sometimes Enter) key until the OS window
// blurs and refocuses — the "spacebar stops working after an error popup" bug.
// A pure-DOM toast never invokes the native dialog manager and never takes
// keyboard focus, so the terminal keeps focus and Space keeps working.
//
// CommonJS to match every other renderer module (app.js require()s this); the
// main window runs nodeIntegration:true, contextIsolation:false, so require is
// available at runtime. No native dialog API (alert/confirm/prompt) and no
// element .focus() may be introduced here without reintroducing the bug.

const CONTAINER_ID = 'toast-container';

// Find the #toast-container (declared in index.html) or create it on demand so
// the module also works in tests / before the static markup is present.
function ensureContainer(doc) {
  let container = doc.getElementById(CONTAINER_ID);
  if (!container) {
    container = doc.createElement('div');
    container.id = CONTAINER_ID;
    container.setAttribute('aria-live', 'polite');
    (doc.body || doc.documentElement).appendChild(container);
  }
  return container;
}

// showToast(message, { type, timeoutMs, doc }) -> the created toast node.
// - type: visual variant ('error' by default), drives the toast-<type> class.
// - timeoutMs: auto-dismiss delay; <= 0 disables auto-dismiss.
// - doc: DOM document seam (defaults to the global document) for jsdom tests.
// Deliberately takes NO keyboard focus: no .focus() call, no tabbable/auto-
// focused control. Click anywhere on a toast to dismiss it early.
function showToast(message, opts = {}) {
  const {
    type = 'error',
    timeoutMs = 6000,
    doc = (typeof document !== 'undefined' ? document : null),
  } = opts;
  if (!doc) return null;

  const container = ensureContainer(doc);

  const toast = doc.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.setAttribute('role', 'status');
  toast.textContent = message == null ? '' : String(message);

  const remove = () => {
    if (toast._dismissTimer) {
      clearTimeout(toast._dismissTimer);
      toast._dismissTimer = null;
    }
    if (toast.parentNode) toast.parentNode.removeChild(toast);
  };
  toast.addEventListener('click', remove);

  container.appendChild(toast);

  if (timeoutMs > 0) {
    // Ambient setTimeout (controllable by node:test mock.timers in tests).
    toast._dismissTimer = setTimeout(remove, timeoutMs);
  }

  return toast;
}

module.exports = { showToast };
