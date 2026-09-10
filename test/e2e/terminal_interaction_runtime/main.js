const { app, BrowserWindow, ipcMain, clipboard, Menu, ClipboardItem } = require("electron");
const fs = require("fs"), path = require("path"), crypto = require("crypto");
const { execFile } = require("child_process");
const run = require("util").promisify(execFile);
const {cleanupOwnedFixtures,isVerifiedAbsent}=require("./cleanup");
if (process.platform !== "darwin" || process.env.PENTACLE_RUNTIME_CLIPBOARD !== "1") {
  console.error("Requires macOS and explicit PENTACLE_RUNTIME_CLIPBOARD=1.");
  app.exit(2);
  return;
}
const os = require("os");
const sourceRoot = path.resolve(process.env.PENTACLE_RUNTIME_SOURCE || path.join(__dirname, "../../.."));
const dependencyRoot = path.resolve(process.env.PENTACLE_RUNTIME_DEPENDENCIES || path.join(sourceRoot, "node_modules"));
const output = path.resolve(process.env.PENTACLE_RUNTIME_OUTPUT || fs.mkdtempSync(path.join(os.tmpdir(), "pentacle-terminal-results-")));
fs.mkdirSync(output, { recursive: true });
const socket = "pentacle-regression-" + crypto.randomBytes(8).toString("hex");
const remoteTarget = process.env.PENTACLE_RUNTIME_SSH_TARGET;
const mode = remoteTarget ? "remote" : "local";
const remotePort = process.env.PENTACLE_RUNTIME_SSH_PORT || "22";
const localTmux = process.env.PENTACLE_RUNTIME_TMUX || "tmux";
const remoteTmux = process.env.PENTACLE_RUNTIME_REMOTE_TMUX || "tmux";
const localPython = process.env.PENTACLE_RUNTIME_PYTHON || "python3";
const remotePython = process.env.PENTACLE_RUNTIME_REMOTE_PYTHON || "python3";
const native = require(path.join(dependencyRoot, "node-pty"));
const root = fs.mkdtempSync(path.join(os.tmpdir(), "pentacle-terminal-fixture-"));
const quote = (s) => "'" + String(s).replace(/'/g, "'\\''") + "'";
const sshArgs = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-p", remotePort, "--", remoteTarget].filter((x) => x !== void 0);
const wrapper = path.join(root, "tmux-fixture");
try { fs.writeFileSync(wrapper, "#!/bin/sh\nexec " + quote(localTmux) + " -L " + quote(socket) + ' "$@"\n', { mode: 0o700 }); } catch(error) { fs.rmSync(root,{recursive:true,force:true}); throw error; }
let fixtureRoot = root, fixtureTmux = wrapper, rawPath = path.join(root, "raw.bin");
let remoteDirectoryOwned = false;
const fixtureRun = (file, args) => mode === "local" ? run(file, args, { timeout: 5e3 }) : run("ssh", [...sshArgs, [file, ...args].map(quote).join(" ")], { timeout: 5e3 });
const tmux = (...args) => fixtureRun(fixtureTmux, args);
const readRaw = async () => mode === "local" ? fs.existsSync(rawPath) ? fs.readFileSync(rawPath) : Buffer.alloc(0) : Buffer.from((await fixtureRun(remotePython, ["-c", 'import base64,sys; print(base64.b64encode(open(sys.argv[1],"rb").read()).decode())', rawPath])).stdout.trim(), "base64");
try { app.setPath("userData", path.join(root, "profile")); } catch(error) { fs.rmSync(root,{recursive:true,force:true}); throw error; }
const receipt = { electron: process.versions.electron, kind: "terminal-interaction-" + mode, files: {}, tests: [], commands: [] };
try { for (const file of ["renderer/app.js", "renderer/terminal_paste.js", "main/terminal_adapter.js", "main/clipboard_ipc_bridge.js", "preload.js"]) receipt.files[file] = crypto.createHash("sha256").update(fs.readFileSync(path.join(sourceRoot, file))).digest("hex"); } catch(error) { fs.rmSync(root,{recursive:true,force:true}); throw error; }
receipt.harness={}; for(const file of ["main.js","renderer.js","index.html","raw.py","cleanup.js"]) receipt.harness[file]=crypto.createHash("sha256").update(fs.readFileSync(path.join(__dirname,file))).digest("hex");
let win, saved = null, ownedText = null, dispose = null, finishing = false, ownsFixture = false;
let activeCommands=0,lastCommandFinished=Date.now();
const delay = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitFor(predicate, label) {
  for (let i = 0; i < 50; i++) {
    if (await predicate()) return;
    await delay(100);
  }
  throw Error("Timed out: " + label);
}
async function preserve() {
  saved = await Promise.all((await clipboard.read()).map(async (item) => new ClipboardItem(Object.fromEntries(await Promise.all(item.types.map(async (type) => [type, await item.getType(type)]))))));
  receipt.clipboardSnapshotComplete = true;
}
async function restore() {
  if (saved !== null && ownedText !== null) {
    if (await clipboard.readText() === ownedText) {
      await clipboard.write(saved);
      receipt.clipboardRestored = true;
    } else receipt.clipboardRestored = "skipped newer clipboard";
    ownedText = null;
  }
}
async function finish(code) {
  if (finishing) return;
  finishing = true;
  const cleanup=await cleanupOwnedFixtures({
    serverClaimed:ownsFixture,
    restoreClipboard:restore,
    disposePtys:()=>dispose?.(),
    killServer:()=>tmux("kill-server"),
    verifyServerAbsent:async()=>{try{await tmux("list-sessions");return false}catch(error){if(isVerifiedAbsent(error))return true;throw error}},
    removeRemote:remoteDirectoryOwned?()=>fixtureRun("rm",["-rf",fixtureRoot]):null,
    removeLocal:()=>fs.rmSync(root,{recursive:true,force:true}),
  });
  receipt.cleanup=cleanup;
  if(cleanup.errors.length){code=2;receipt.cleanupRecovery={localRoot:root,remoteRoot:remoteDirectoryOwned?fixtureRoot:null,socket,remoteTarget};}
  receipt.exitCode = code;
  fs.writeFileSync(path.join(output, "receipt.json"), JSON.stringify(receipt, null, 2));
  console.log(JSON.stringify({ receipt: path.join(output, "receipt.json"), passed: receipt.tests.filter((t) => t.passed).length, failed: receipt.tests.filter((t) => !t.passed).length, exitCode: code }));
  app.exit(code);
}
ipcMain.on("fixture:error", (_, error) => {
  receipt.error = error;
  void finish(2);
});
async function evalJs(code) {
  return win.webContents.executeJavaScript(code);
}
function nativeKey(keyCode, modifiers = [], character = null) {
  win.webContents.sendInputEvent({ type: "keyDown", keyCode, modifiers });
  if (character) win.webContents.sendInputEvent({ type: "char", keyCode: character, modifiers });
  win.webContents.sendInputEvent({ type: "keyUp", keyCode, modifiers });
}
async function modeValue(pane) {
  return (await tmux("display-message", "-p", "-t", pane, "#{pane_in_mode}")).stdout.trim();
}
async function focusedAndSettled(){app.focus({steal:true});win.focus();win.webContents.focus();await evalJs("term.focus()");await waitFor(()=>evalJs("document.hasFocus() && document.activeElement===term.textarea"),"foreground terminal focus");await waitFor(()=>evalJs("fixturePendingRenders===0 && Date.now()-fixtureLastRendered>350"),"parsed terminal output settled");await waitFor(()=>activeCommands===0&&Date.now()-lastCommandFinished>350,"attach/control commands settled");}
async function drag(doubleClick = false) {
  await focusedAndSettled();
  await evalJs("term.clearSelection();term.focus();dataEvents.length=0;selectionEvents.length=0;mouseEvents.length=0");
  const target = await evalJs("fixtureTarget()");
  if (target.row < 0) throw Error("No history sentinel visible");
  win.webContents.sendInputEvent({ type: "mouseMove", x: Math.round(target.x), y: Math.round(target.y) });
  if (doubleClick) {
    const x = Math.round(target.x + target.cellWidth * 4);
    for (const clickCount of [1, 2]) {
      win.webContents.sendInputEvent({ type: "mouseDown", x, y: Math.round(target.y), button: "left", clickCount });
      win.webContents.sendInputEvent({ type: "mouseUp", x, y: Math.round(target.y), button: "left", clickCount });
      await delay(40);
    }
  } else {
    win.webContents.sendInputEvent({ type: "mouseDown", x: Math.round(target.x), y: Math.round(target.y), button: "left", clickCount: 1 });
    await delay(50);
    for (let i = 1; i <= 8; i++) {
      win.webContents.sendInputEvent({ type: "mouseMove", x: Math.round(target.x + (target.endX - target.x) * i / 8), y: Math.round(target.y), button: "left", modifiers: ["leftButtonDown"] });
      await delay(20);
    }
    win.webContents.sendInputEvent({ type: "mouseUp", x: Math.round(target.endX), y: Math.round(target.y), button: "left", clickCount: 1 });
  }
  const immediate = await evalJs("fixtureInfo()");
  await delay(250);
  const settled = await evalJs("fixtureInfo()");
  if (immediate.selection !== target.token || settled.selection !== target.token) throw Error("Selection mismatch " + JSON.stringify({ expected: target.token, immediate: immediate.selection, settled: settled.selection, data: settled.data }));
  return { target, selected: target.token, immediateSelection: immediate.selection, settledSelection: settled.selection, mouseTracking: settled.mouseTracking, mouseDataCount: settled.data.length };
}
async function copySelection(text, native2 = false) {
  const focus=await evalJs("({document:document.hasFocus(),textarea:document.activeElement===term.textarea,selection:term.getSelection()})");if(!win.isFocused()||!focus.document||!focus.textarea)throw Error("Harness focus lost before copy");if(focus.selection!==text)throw Error("Selection changed before copy: "+JSON.stringify(focus));
  if (await clipboard.readText() !== ownedText) throw Error("Clipboard changed outside fixture");
  // A no-op copy must fail even when consecutive selections contain the same text.
  ownedText="PENTACLE_COPY_CONTROL_"+crypto.randomBytes(8).toString("hex");
  await clipboard.writeText(ownedText);
  if (native2) Menu.sendActionToFirstResponder("copy:");
  else nativeKey("C", ["meta"]);
  await waitFor(async()=>await clipboard.readText()===text,"clipboard copy completion").catch(()=>{});
  const matches = await clipboard.readText() === text;
  if (matches) ownedText = text;
  if (!matches) throw Error("Copied clipboard mismatch: "+JSON.stringify({focus:await evalJs("({document:document.hasFocus(),selection:term.getSelection()})"),stillOwned:await clipboard.readText()===ownedText}));
  return { clipboardMatchesSelection: true, native: native2 };
}
async function inputExact(action, expected) {
  const before = await readRaw();
  await evalJs("dataEvents.length=0");
  await action();
  const started=Date.now();
  await waitFor(async()=>(await readRaw()).length>before.length,"raw input receipt").catch(()=>{});
  await delay(150);
  const after = await readRaw();
  const bytes = after.subarray(before.length);
  if (!bytes.equals(Buffer.from(expected))) throw Error("Raw input mismatch " + JSON.stringify({ expectedHex: Buffer.from(expected).toString("hex"), actualHex: bytes.toString("hex") }));
  return { actualHex: bytes.toString("hex"), data: await evalJs("dataEvents"), elapsedMs:Date.now()-started };
}
async function scroll(pane) {
  await evalJs("term.clearSelection();term.focus();wheelEvents.length=0");
  await focusedAndSettled();
  const before = await evalJs("fixtureTarget()");
  win.webContents.sendInputEvent({ type: "mouseWheel", x: Math.round(before.x + 60), y: Math.round(before.y + 60), deltaY: 250, deltaX: 0, canScroll: true });
  await waitFor(async () => await modeValue(pane) === "1", "wheel entered copy-mode");
  await delay(200);
  const after = await evalJs("fixtureTarget()");
  const wheels = await evalJs("wheelEvents");
  if (after.token === before.token) throw Error("Wheel did not expose earlier history");
  return { before: before.token, after: after.token, wheels };
}
async function cell(name, action) {
  try {
    receipt.tests.push({ name, passed: true, ...await action() });
  } catch (e) {
    receipt.tests.push({ name, passed: false, error: e.message });
    await tmux("copy-mode", "-q", "-t", receipt.renderer.pane).catch(() => {
    });
    await evalJs("term.clearSelection()").catch(() => {
    });
  }
}
async function sizes(pane) {
  return (await tmux("display-message", "-p", "-t", pane, "#{window_width}x#{window_height}")).stdout.trim();
}
async function sizeTests(data) {
  for (const policy of ["latest", "smallest"]) {
    await cell("two-client-resize-" + policy, async () => {
      await tmux("set-window-option", "-g", "window-size", policy);
      await tmux("set-window-option", "-u", "-t", "paste-fixture:", "window-size");
      data.pane = await evalJs("fixtureAttach()");
      await delay(200);
      const first = await sizes(data.pane);
      const command = mode === "local" ? { file: wrapper, args: ["attach-session", "-t", "=paste-fixture"] } : { file: "ssh", args: ["-tt", ...sshArgs, [fixtureTmux, "attach-session", "-t", "=paste-fixture"].map(quote).join(" ")] };
      let sidecar;
      try {
        sidecar = native.spawn(command.file, command.args, { name: "xterm-256color", cols: 80, rows: 24, cwd: root, env: process.env });
        sidecar.onData(() => {});
        await waitFor(async()=>(await tmux("list-clients","-t","paste-fixture:","-F","#{client_width}")).stdout.trim().split("\n").length===2,"second client attached");
        await delay(100);
        const withSecond = await sizes(data.pane);
        await evalJs("term.resize(132,40);window.cc.resizePty(0,132,40)");
        await delay(350);
        const resized = await sizes(data.pane);
        const unrelated = (await tmux("display-message","-p","-t","untouched-fixture:","#{window-size}")).stdout.trim();
        const effective = (await tmux("display-message", "-p", "-t", data.pane, "#{window-size}")).stdout.trim();
        return { passed: resized === "132x39" && unrelated===policy, unrelatedPolicy:unrelated, initial: first, withSecond, resized, requested: "132x40", expectedPane: "132x39", effectivePolicy: effective, globalPolicy: (await tmux("show-window-option", "-gv", "window-size")).stdout.trim() };
      } finally {
        sidecar?.kill();
        await delay(100);
        await evalJs("term.resize(120,36);window.cc.resizePty(0,120,36)");
      }
    });
  }
}
ipcMain.on("fixture:ready", async (_, data) => {
  try {
    receipt.renderer = data;
    await waitFor(() => evalJs("term.modes.bracketedPasteMode"), "raw fixture ready");
    app.focus({ steal: true });
    win.show();
    win.focus();
    await delay(300);
    await focusedAndSettled();
    receipt.mouse = { global: (await tmux("show-option", "-gv", "mouse")).stdout.trim(), attached: (await tmux("show-option", "-Av", "-t", data.pane, "mouse")).stdout.trim(), unrelated: (await tmux("show-option", "-Av", "-t", "untouched-fixture:", "mouse")).stdout.trim() };
    await preserve();
    ownedText = "PENTACLE_SELECTION_CLIPBOARD_INITIAL";
    await clipboard.writeText(ownedText);
    await cell("drag-copy-cmd-v", async () => {
      const selection = await drag();
      const copy = await copySelection(selection.selected);
      const pasted = await inputExact(() => nativeKey("V", ["meta"]), "\x1B[200~" + selection.selected + "\x1B[201~");
      return { selection, copy, pasted };
    });
    await cell("double-click-native-copy-native-paste", async () => {
      const selection = await drag(true);
      const copy = await copySelection(selection.selected, true);
      const pasted = await inputExact(() => Menu.sendActionToFirstResponder("paste:"), "\x1B[200~" + selection.selected + "\x1B[201~");
      return { selection, copy, pasted };
    });
    await cell("wheel-history-drag-copy-paste", async () => {
      const wheel = await scroll(data.pane);
      const selection = await drag();
      const selectedInCopyMode = await modeValue(data.pane);
      const copy = await copySelection(selection.selected);
      const pasted = await inputExact(() => nativeKey("V", ["meta"]), "\x1B[200~" + selection.selected + "\x1B[201~");
      if (await modeValue(data.pane) !== "0") throw Error("Paste left copy-mode active");
      return { wheel, selection, selectedInCopyMode, copy, pasted };
    });
    await cell("page-up-page-down",async()=>{await focusedAndSettled();await evalJs("term.clearSelection()");const before=await evalJs("fixtureTarget()");nativeKey("PageUp");await waitFor(async()=>await modeValue(data.pane)==="1","PageUp entered copy-mode");await waitFor(async()=>{const now=await evalJs("fixtureTarget()");return Number(before.token.split("_").pop())-Number(now.token.split("_").pop())===15},"PageUp moved15lines");nativeKey("PageDown");await waitFor(async()=>(await evalJs("fixtureTarget()")).token===before.token,"PageDown restored view");await tmux("copy-mode","-q","-t",data.pane);return {before:before.token,after:(await evalJs("fixtureTarget()")).token}});
    await cell("ordinary-key-from-live", async () => {
      await evalJs("term.clearSelection();term.focus()");
      return inputExact(() => nativeKey("X", [], "x"), "x");
    });
    await cell("rapid-live-typing-without-extra-clear",async()=>{await evalJs("term.clearSelection();term.focus()");const clearCount=()=>receipt.commands.filter(c=>c.args.join(" ").includes("copy-mode")&&c.args.join(" ").includes("-q")).length;const before=clearCount();const text="rapidtyping1234567890";const input=await inputExact(()=>{for(const char of text)nativeKey(char.toUpperCase(),[],char)},text);const additionalClearCommands=clearCount()-before;if(additionalClearCommands!==0)throw Error("Live typing issued redundant clear commands: "+additionalClearCommands);return {input,additionalClearCommands}});
    await cell("ctrl-c-from-live", async () => {
      await evalJs("term.clearSelection();term.focus()");
      return inputExact(() => nativeKey("C", ["control"]), "\x03");
    });
    for (const [name, key, mods, char, expected] of [["ordinary-key-after-scroll", "X", [], "x", "x"], ["ctrl-c-after-scroll", "C", ["control"], null, "\x03"], ["ctrl-enter-after-scroll", "Enter", ["control"], null, "\x1B[13;5u"]]) {
      await cell(name, async () => {
        const wheel = await scroll(data.pane);
        const input = await inputExact(() => nativeKey(key, mods, char), expected);
        return { wheel, input, inMode: await modeValue(data.pane) };
      });
    }
    await cell("wheel-burst-before-first-key",async()=>{await focusedAndSettled();await evalJs("term.clearSelection();term.focus()");const before=await evalJs("fixtureTarget()");const started=Date.now();for(let i=0;i<6;i++){win.webContents.sendInputEvent({type:"mouseWheel",x:Math.round(before.x+60),y:Math.round(before.y+60),deltaY:250,deltaX:0,canScroll:true});await delay(60)}await waitFor(async()=>{const current=await evalJs("fixtureTarget()");return Number(before.token.split("_").pop())-Number(current.token.split("_").pop())===60},"all wheel burst lines applied");const scrollElapsedMs=Date.now()-started;if(scrollElapsedMs>3000)throw Error("Wheel burst exceeded 3 seconds");const input=await inputExact(()=>nativeKey("Z",[],"z"),"z");return {scrollElapsedMs,input,inMode:await modeValue(data.pane)}});
    await cell("reattach-drag-copy-paste", async () => {
      await evalJs("fixtureAttach()");
      await delay(300);
      const selection = await drag();
      const copy = await copySelection(selection.selected);
      const pasted = await inputExact(() => nativeKey("V", ["meta"]), "\x1B[200~" + selection.selected + "\x1B[201~");
      return { selection, copy, pasted };
    });
    await cell("replacement-drag-copy-paste", async () => {
      const primaryBefore=await readRaw();
      data.pane = await evalJs('fixtureAttach("replacement-fixture")');
      const primaryPath=rawPath; rawPath=fixtureRoot+"/raw-replacement.bin";
      await delay(300);
      const selection = await drag();
      const copy = await copySelection(selection.selected);
      const pasted = await inputExact(() => nativeKey("V", ["meta"]), "\x1B[200~" + selection.selected + "\x1B[201~");
      const replacementPath=rawPath;rawPath=primaryPath;const primaryUnchanged=(await readRaw()).equals(primaryBefore);rawPath=replacementPath;if(!primaryUnchanged)throw Error("Replacement input reached prior session");
      return { pane: data.pane, selection, copy, pasted, primaryUnchanged };
    });
    receipt.finalMouse = { global: (await tmux("show-option", "-gv", "mouse")).stdout.trim(), attached: (await tmux("show-option", "-Av", "-t", data.pane, "mouse")).stdout.trim(), unrelated: (await tmux("show-option", "-Av", "-t", "untouched-fixture:", "mouse")).stdout.trim() };
    if(receipt.finalMouse.global!=="on"||receipt.finalMouse.attached!=="off"||receipt.finalMouse.unrelated!=="on")throw Error("Session mouse scope changed unexpectedly");
    await sizeTests(data);
    receipt.final = await evalJs("fixtureInfo()");
    await finish(receipt.tests.every((t) => t.passed) ? 0 : 1);
  } catch (e) {
    receipt.error = e.stack;
    await finish(2);
  }
});
app.whenReady().then(async () => {
  try {
    if (remoteTarget) {
      const { stdout } = await run("ssh", [...sshArgs, "mktemp -d /tmp/pentacle-terminal-fixture.XXXXXXXX"], { timeout: 5e3 });
      fixtureRoot = stdout.trim();
      if (!/^\/tmp\/pentacle-terminal-fixture\.[A-Za-z0-9]+$/.test(fixtureRoot)) throw Error("Unexpected remote temp directory");
      remoteDirectoryOwned = true;
      fixtureTmux = fixtureRoot + "/tmux-fixture";
      rawPath = fixtureRoot + "/raw.bin";
      const remoteWrapper = "#!/bin/sh\nexec " + quote(remoteTmux) + " -L " + quote(socket) + ' "$@"\n';
      for (const [name, contents] of [["tmux-fixture", remoteWrapper], ["raw.py", fs.readFileSync(path.join(__dirname, "raw.py"), "utf8")]]) {
        await fixtureRun(remotePython, ["-c", 'import base64,sys;open(sys.argv[1],"wb").write(base64.b64decode(sys.argv[2]))', fixtureRoot + "/" + name, Buffer.from(contents).toString("base64")]);
      }
      await fixtureRun("chmod", ["700", fixtureTmux]);
    }
    await fixtureRun(mode === "local" ? localPython : remotePython, ["-c", 'import sys;open(sys.argv[1],"wb").close()', rawPath]);
    const rawScript = mode === "local" ? path.join(__dirname, "raw.py") : fixtureRoot + "/raw.py";
    const rawCommand = [mode === "local" ? localPython : remotePython, rawScript, rawPath].map(quote).join(" ");
    const replacementPath=fixtureRoot+"/raw-replacement.bin";await fixtureRun(mode==="local"?localPython:remotePython,["-c",'import sys;open(sys.argv[1],"wb").close()',replacementPath]);
    const replacementCommand=[mode==="local"?localPython:remotePython,rawScript,replacementPath].map(quote).join(" ");
    ownsFixture = true; // The unique server may be created even if the command reply times out.
    await tmux("-f", "/dev/null", "new-session", "-d", "-s", "paste-fixture", "-x", "120", "-y", "36", rawCommand);
    await tmux("set-option", "-g", "mouse", "on");
    await tmux("new-session", "-d", "-s", "untouched-fixture", "sleep 120");
    await tmux("new-session", "-d", "-s", "replacement-fixture", "-x", "120", "-y", "36", replacementCommand);
    receipt.fixture = { socket, mode, tmuxVersion: (await tmux("-V")).stdout.trim() };
    require(path.join(sourceRoot, "main/clipboard_ipc_bridge")).registerClipboardIpc(ipcMain, clipboard);
    const at = remoteTarget?.lastIndexOf("@") ?? -1;
    const host = at < 0 ? remoteTarget : remoteTarget.slice(at + 1), user = at < 0 ? void 0 : remoteTarget.slice(0, at);
    dispose = require(path.join(sourceRoot, "main/terminal_adapter")).registerTerminalIpc(ipcMain, { tmux: wrapper, chatStream: { localHost: "local" }, hosts: { remote: { host, user, port: Number(remotePort), tmux: fixtureTmux } } }, {}, { pty: native, execute: async (file, args, opts) => {
      receipt.commands.push({ file, args });
      activeCommands++;try{return await run(file,args,opts)}finally{activeCommands--;lastCommandFinished=Date.now()}
    } });
    ipcMain.handle("fixture:config", () => ({ sourceRoot, dependencyRoot, mode }));
    win = new BrowserWindow({ width: 950, height: 700, show: true, webPreferences: { nodeIntegration: true, contextIsolation: false, sandbox: false, preload: path.join(sourceRoot, "preload.js") } });
    await win.loadFile(path.join(__dirname, "index.html"));
  } catch (error) {
    receipt.error = error.stack;
    await finish(2);
  }
});
setTimeout(() => {
  receipt.timeout = true;
  void finish(2);
}, 12e4).unref();
process.on("SIGTERM", () => void finish(2));
process.on("SIGINT", () => void finish(2));
