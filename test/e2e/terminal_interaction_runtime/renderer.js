const { ipcRenderer } = require("electron");
(async () => {
  const config = await ipcRenderer.invoke("fixture:config");
  const base = config.dependencyRoot + "/";
  const { Terminal } = require(base + "@xterm/xterm");
  const { WebglAddon } = require(base + "@xterm/addon-webgl");
  const { FitAddon } = require(base + "@xterm/addon-fit");
  const { Unicode11Addon } = require(base + "@xterm/addon-unicode11");
  const fs = require("fs"), path = require("path"), crypto = require("crypto");
  const sourceRoot = config.sourceRoot;
  const css = document.createElement("link");
  css.rel = "stylesheet";
  css.href = require("url").pathToFileURL(path.join(base, "@xterm/xterm/css/xterm.css")).href;
  document.head.appendChild(css);
  await new Promise((resolve, reject) => {
    css.onload = resolve;
    css.onerror = reject;
  });
  const { createTerminalPaste } = require(path.join(sourceRoot, "renderer/terminal_paste"));
  const source = fs.readFileSync(path.join(sourceRoot, "renderer/app.js"), "utf8");
  const begin = source.indexOf("  const terminalPaste = createTerminalPaste({");
  const end = source.indexOf("\n  state.terminals[slot] = { term, fitAddon };", begin);
  if(begin<0||end<=begin)throw Error("Cannot locate production terminal wiring");
  const wiring = source.slice(begin, end);
  const wheelBegin = source.indexOf("document.addEventListener('wheel', (e) => {");
  const wheelEnd = source.indexOf("\n}, { passive: false, capture: true });", wheelBegin) + "\n}, { passive: false, capture: true });".length;
  if(wheelBegin<0||wheelEnd<=wheelBegin)throw Error("Cannot locate production wheel wiring");
  const wheelWiring = source.slice(wheelBegin, wheelEnd);
  window.term = new Terminal({ fontFamily: "'SFMono-Regular', 'SF Mono', '.SF NS Mono', 'Menlo', 'Monaco', monospace", fontSize: 13, cursorBlink: true, allowProposedApi: true, scrollback: 0 });
  const fitAddon = new FitAddon();
  term.loadAddon(fitAddon);
  const unicode = new Unicode11Addon();
  term.loadAddon(unicode);
  term.unicode.activeVersion = "11";
  term.open(document.getElementById("term-0"));
  try {
    const addon = new WebglAddon();
    addon.onContextLoss(() => addon.dispose());
    term.loadAddon(addon);
    window.fixtureWebgl = true;
  } catch (e) {
    window.fixtureWebgl = false;
  }
  fitAddon.fit();
  const slot = 0, gen = 1;
  const state = { slots: [{}], slotGen: [gen], terminals: [{ term }], botSlots: [], slotViewModes: ["terminal"], wheelThrottles: [0] };
  window.keyEvents = [];
  window.pasteEvents = [];
  window.dataEvents = [];
  window.selectionEvents = [];
  window.mouseEvents = [];
  window.wheelEvents = [];
  term.textarea.addEventListener("keydown", (e) => keyEvents.push({ key: e.key, meta: e.metaKey, ctrl: e.ctrlKey }), true);
  term.element.addEventListener("paste", (e) => pasteEvents.push({ trusted: e.isTrusted }), true);
  document.addEventListener("wheel", (e) => wheelEvents.push({ dy: e.deltaY, trusted: e.isTrusted }), true);
  term.element.addEventListener("mousedown", (e) => mouseEvents.push({ type: e.type, x: e.clientX, y: e.clientY, trusted: e.isTrusted }), true);
  term.onSelectionChange(() => selectionEvents.push({ text: term.getSelection(), at: Date.now() }));
  term.onData((data) => dataEvents.push(data));
  eval(wiring);
  eval(wheelWiring);
  window.fixturePendingRenders=0;window.fixtureLastRendered=Date.now();
  window.cc.onPtyData((_,data)=>{window.fixturePendingRenders++;term.write(data,()=>{window.fixturePendingRenders--;window.fixtureLastRendered=Date.now()})});
  const host = config.mode;
  window.fixtureAttach = async function(name = "paste-fixture") {
    state.slots[slot].paneId = await window.cc.createPty(0, name, host, term.cols, term.rows);
    term.focus();
    return state.slots[slot].paneId;
  };
  window.fixtureInfo = () => ({ selection: term.getSelection(), selectionEvents, data: dataEvents, mouseEvents, wheelEvents, keys: keyEvents, bracketed: term.modes.bracketedPasteMode, mouseTracking: term.modes.mouseTrackingMode, webgl: window.fixtureWebgl, lines: Array.from({ length: term.rows }, (_, i) => term.buffer.active.getLine(term.buffer.active.viewportY + i)?.translateToString(true) || "") });
  window.fixtureTarget = () => {
    const info = fixtureInfo();
    const row = info.lines.findIndex((line) => line.startsWith("HISTORY_SENTINEL_"));
    const token = info.lines[row]?.split(" ")[0];
    const rect = term.element.querySelector(".xterm-screen").getBoundingClientRect();
    const cell = term._core._renderService.dimensions.css.cell;
    return { row, token, x: rect.x + cell.width * 0.2, y: rect.y + cell.height * (row + 0.5), endX: rect.x + cell.width * (token?.length + 0.2), cellWidth: cell.width, cellHeight: cell.height, rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height } };
  };
  window.fixtureAttach().then((pane) => ipcRenderer.send("fixture:ready", { pane, cols: term.cols, rows: term.rows, wiringSha: crypto.createHash("sha256").update(wiring).digest("hex"), wheelWiringSha: crypto.createHash("sha256").update(wheelWiring).digest("hex") })).catch((e) => ipcRenderer.send("fixture:error", e.message));
})().catch((error) => ipcRenderer.send("fixture:error", error.stack));
