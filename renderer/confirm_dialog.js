// In-DOM, promise-based confirm dialog for the renderer.
//
// WHY this exists: the renderer used to gate destructive actions with the
// browser-native window.confirm(). On Windows/Electron, dismissing a native JS
// dialog leaves Chromium swallowing the Space (and sometimes Enter) key until
// the OS window blurs and refocuses -- the "spacebar stops working after a
// popup" focus trap. A pure-DOM modal never invokes the native dialog manager.
// Focus is captured before the modal opens and RESTORED to the prior element on
// close, so the terminal regains focus and Space keeps working.
//
// Loaded as a classic <script> (attaches root.confirmDialog) AND require()-able
// (module.exports), mirroring the dashboards' UMD pattern. nodeIntegration is
// on, but relative require() from a <script src> renderer is brittle across asar
// packaging, so dashboards consume the global, not require(). No native dialog
// API (alert/confirm/prompt) may be introduced here without reintroducing the
// bug; the .focus() calls below are intentional (a DOM element, not a native
// dialog) and are paired with focus restoration on close.

(function (root) {
  'use strict';

  const OVERLAY_ID = 'confirm-overlay';

  // confirmDialog(message, opts) -> Promise<boolean>
  // - opts.doc: DOM document seam (defaults to the global document) for tests.
  // - opts.confirmLabel / opts.cancelLabel: button text.
  // Resolves true when the user confirms (Confirm button or Enter), false when
  // they cancel (Cancel button, Esc, or a click on the backdrop). Renders into
  // the shared .modal-overlay/.modal markup so styling matches the app's modals.
  function confirmDialog(message, opts = {}) {
    const doc = opts.doc || (typeof document !== 'undefined' ? document : null);
    if (!doc || !doc.body) return Promise.resolve(false);

    const confirmLabel = opts.confirmLabel || 'Confirm';
    const cancelLabel = opts.cancelLabel || 'Cancel';
    // Remember who had focus so we can hand it back (keeps the terminal/Space
    // working once the modal closes).
    const prevActive = doc.activeElement;

    return new Promise((resolve) => {
      // Defensive: only one confirm at a time -- drop any stale overlay.
      const stale = doc.getElementById(OVERLAY_ID);
      if (stale && stale.parentNode) stale.parentNode.removeChild(stale);

      const overlay = doc.createElement('div');
      overlay.className = 'modal-overlay';
      overlay.id = OVERLAY_ID;
      overlay.setAttribute('role', 'dialog');
      overlay.setAttribute('aria-modal', 'true');

      const modal = doc.createElement('div');
      modal.className = 'modal';

      const msg = doc.createElement('div');
      msg.className = 'modal-subtitle';
      msg.textContent = String(message == null ? '' : message);

      const actions = doc.createElement('div');
      actions.className = 'modal-actions';

      const cancelBtn = doc.createElement('button');
      cancelBtn.className = 'sb-btn';
      cancelBtn.textContent = cancelLabel;

      const confirmBtn = doc.createElement('button');
      confirmBtn.className = 'sb-btn sb-btn-blue';
      confirmBtn.textContent = confirmLabel;

      actions.appendChild(cancelBtn);
      actions.appendChild(confirmBtn);
      modal.appendChild(msg);
      modal.appendChild(actions);
      overlay.appendChild(modal);
      doc.body.appendChild(overlay);

      let settled = false;
      function close(result) {
        if (settled) return;
        settled = true;
        doc.removeEventListener('keydown', onKey, true);
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        // Restore focus to whatever held it before we opened. Guard for jsdom
        // and for nodes that were detached while the modal was open.
        try {
          if (prevActive && typeof prevActive.focus === 'function') prevActive.focus();
        } catch (_) { /* focus restore is best-effort */ }
        resolve(result);
      }

      function onKey(e) {
        if (e.key === 'Escape') { e.preventDefault(); close(false); }
        else if (e.key === 'Enter') { e.preventDefault(); close(true); }
      }

      cancelBtn.addEventListener('click', () => close(false));
      confirmBtn.addEventListener('click', () => close(true));
      // A click on the backdrop (outside the .modal) cancels.
      overlay.addEventListener('mousedown', (e) => { if (e.target === overlay) close(false); });
      doc.addEventListener('keydown', onKey, true);

      // Focus the confirm button so Enter/Esc work immediately. This is a DOM
      // element, not a native dialog -- no focus trap -- and focus is handed
      // back to prevActive in close().
      try { if (typeof confirmBtn.focus === 'function') confirmBtn.focus(); } catch (_) { /* no-op */ }
    });
  }

  if (root) root.confirmDialog = confirmDialog;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { confirmDialog };
  }
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : null));
