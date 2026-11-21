'use strict';

// Shared terminal construction for both web views. Connection and slot
// lifecycle remain with the caller, which owns the PTY bridge.
function createTerminalView(container, theme, { Terminal, FitAddon, Unicode11Addon, WebglAddon }) {
  const term = new Terminal({ theme, fontFamily: "'SFMono-Regular', 'SF Mono', '.SF NS Mono', 'Menlo', 'Monaco', monospace", fontSize: 13, cursorBlink: true, allowProposedApi: true, scrollback: 0 });
  const fitAddon = new FitAddon();
  const unicode11Addon = new Unicode11Addon();
  term.loadAddon(fitAddon); term.loadAddon(unicode11Addon); term.unicode.activeVersion = '11';
  term.open(container);
  try {
    const webglAddon = new WebglAddon();
    webglAddon.onContextLoss(() => webglAddon.dispose());
    term.loadAddon(webglAddon);
  } catch (error) { console.warn('[App] WebGL renderer unavailable, using DOM fallback:', error.message); }
  return { term, fitAddon };
}
module.exports = { createTerminalView };
