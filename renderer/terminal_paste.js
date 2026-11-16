'use strict';

// xterm formats multiline/bracketed paste synchronously through onData. Capture
// those bytes so the main process can leave tmux history before delivering them.
function createTerminalPaste({ term, readClipboard, pastePty, isCurrent, onError = console.warn }) {
  let captured = null;
  let pending = Promise.resolve();
  function paste() {
    pending = pending.then(async () => {
      if (!isCurrent()) return;
      const text = await readClipboard();
      if (!isCurrent() || typeof text !== 'string' || !text) return;
      const chunks = [];
      captured = chunks;
      try { term.paste(text); } finally { captured = null; }
      if (chunks.length && isCurrent()) await pastePty(chunks.join(''));
    }).catch((error) => onError('[paste] terminal paste failed:', error));
    return pending;
  }
  return {
    paste,
    captureData(data) {
      if (!captured) return false;
      captured.push(data);
      return true;
    },
    key(event, isMac) {
      if (event.type !== 'keydown') return true;
      const v = String(event.key).toLowerCase() === 'v';
      if (!v || !(event.metaKey || (!isMac && event.ctrlKey))) return true;
      event.preventDefault();
      event.stopPropagation();
      void paste();
      return false;
    },
    nativePaste(event) {
      event.preventDefault();
      event.stopImmediatePropagation();
      void paste();
    },
  };
}
module.exports = { createTerminalPaste };
