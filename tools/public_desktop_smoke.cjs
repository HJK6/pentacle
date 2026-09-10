'use strict';
const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const root = process.env.PENTACLE_SMOKE_ROOT;
if (!root || !/^\d+$/.test(process.env.PENTACLE_SMOKE_PID || '')) throw new Error('Run tools/public_desktop_smoke.py');
process.env.PENTACLE_CONFIG = path.join(root, 'pentacle.config.js');
require('../main.js');
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const timeout = setTimeout(() => { console.error('Smoke timed out'); app.exit(1); }, 90000);
app.whenReady().then(async () => {
  try {
    let win;
    for (let i = 0; i < 150; i++) {
      win = BrowserWindow.getAllWindows()[0];
      if (win && !win.webContents.isLoading() && await win.webContents.executeJavaScript('!!window.PentacleChatStore')) break;
      await wait(100);
    }
    const execute = code => win.webContents.executeJavaScript(code);
    for (let i = 0; i < 100; i++) {
      if ((await execute('window.cc.getChatStreamState()')).connected) break;
      await wait(100);
    }
    const spawned = await execute("newSession({hostId:'local',provider:'claude',model:'claude-opus-4-8',effort:'high'})");
    if (!spawned?.sessionName) throw new Error('The UI did not return a real session');
    await execute(`updateSlotViewMode(${spawned.slot}, 'chat')`);
    let ready = false;
    for (let i = 0; i < 200; i++) {
      const state = await execute('window.cc.getChatStreamState()');
      ready = state.sessions.some((session) => session.stream_id === spawned.streamId && session.bootstrap_state === 'ready');
      if (ready) break;
      await wait(100);
    }
    if (!ready) throw new Error('Spawn never reached ready inventory');
    const marker = 'public-smoke-' + Date.now();
    const sent = await execute(`window.cc.chatSend('local',${JSON.stringify(spawned.sessionName)},${JSON.stringify(marker)})`);
    if (!sent.ok) throw new Error('Send was rejected: ' + sent.error);
    let rendered = false;
    for (let i = 0; i < 200; i++) {
      rendered = (await execute('document.body.innerText')).includes('Public fixture assistant: ' + marker);
      if (rendered) break;
      await wait(100);
    }
    if (!rendered) throw new Error('Actual assistant transcript is missing from the DOM');
    process.kill(Number(process.env.PENTACLE_SMOKE_PID), 'SIGTERM');
    for (let i = 0; i < 100; i++) {
      if (!(await execute('window.cc.getChatStreamState()')).connected) break;
      await wait(100);
    }
    const failed = await execute(`window.cc.chatSend('local',${JSON.stringify(spawned.sessionName)},'must not be fabricated')`);
    if (failed.ok !== false || !failed.error) throw new Error('Disconnected send must fail explicitly');
    fs.writeFileSync(path.join(root, 'proof.json'), JSON.stringify({ spawned, marker, assistantRendered: rendered, disconnectedRejected: true }, null, 2));
    console.log('PUBLIC_DESKTOP_SMOKE_PASS');
    clearTimeout(timeout); app.exit(0);
  } catch (error) {
    console.error(error); clearTimeout(timeout); app.exit(1);
  }
});
