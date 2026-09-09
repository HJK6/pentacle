#!/usr/bin/env node
'use strict';

// Capture deterministic visual baselines from the generated local state pages.
// The script uses an offscreen Electron window and never connects to a service
// or reads live session data. It is safe to run against synthetic fixtures.
//
// Run: npm run mockups:cosmic:png
// When invoked with plain node, this file re-execs itself under Electron because
// webContents.capturePage requires an Electron main process.

const path = require('node:path');
const fs = require('node:fs');

const repoRoot = path.resolve(__dirname, '..');
const outDir = path.join(repoRoot, '.ui-review', 'desktop-chat');

// Under node, regenerate the local HTML fixtures, then re-exec under Electron.
if (!process.versions.electron) {
  const { execFileSync } = require('node:child_process');
  execFileSync(process.execPath, [path.join(repoRoot, 'test', 'generate_cosmic_chat_states.js')], {
    stdio: 'inherit',
    cwd: repoRoot,
  });
  const electron = require('electron');
  try {
    execFileSync(electron, [__filename], {
      stdio: 'inherit',
      cwd: repoRoot,
      env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' },
    });
  } catch (err) {
    console.error('[capture] electron capture failed:', err.message);
    console.error('[capture] The HTML fixtures and manifest remain under .ui-review/desktop-chat/.');
    console.error('[capture] PNG baselines can be regenerated with the visual test command.');
    process.exit(err.status || 1);
  }
  process.exit(0);
}

// Electron main: capture each fixture page offscreen.
const { app, BrowserWindow } = require('electron');

const STATES = ['empty', 'populated', 'working', 'question_single', 'question_multiselect', 'markdown_code', 'markdown_table'];
const WIDTH = 760;

async function captureState(win, state) {
  const file = path.join(outDir, `${state}.html`);
  if (!fs.existsSync(file)) throw new Error(`missing state page: ${file}`);

  await win.loadFile(file);
  const height = await win.webContents.executeJavaScript(
    `new Promise((resolve) => {
       const done = () => {
         const h = Math.max(360, Math.ceil(document.body.scrollHeight) + 8);
         resolve(h);
       };
       if (document.fonts && document.fonts.ready) {
         document.fonts.ready.then(() => setTimeout(done, 120));
       } else {
         setTimeout(done, 200);
       }
     })`,
  );
  win.setContentSize(WIDTH, Math.min(height, 2400));
  await new Promise((r) => setTimeout(r, 180));

  const image = await win.webContents.capturePage();
  const png = image.toPNG();
  const out = path.join(outDir, `${state}.png`);
  fs.writeFileSync(out, png);
  return { state, out, bytes: png.length };
}

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    width: WIDTH,
    height: 900,
    show: false,
    webPreferences: { offscreen: true, sandbox: false },
  });

  let failed = false;
  for (const state of STATES) {
    try {
      const r = await captureState(win, state);
      process.stdout.write(`[capture] ${r.state} -> ${path.relative(repoRoot, r.out)} (${r.bytes} bytes)\n`);
    } catch (err) {
      failed = true;
      process.stderr.write(`[capture] FAILED ${state}: ${err.message}\n`);
    }
  }
  win.destroy();
  app.exit(failed ? 1 : 0);
});
