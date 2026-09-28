const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

// Bans native blocking dialogs (alert/confirm/prompt) across the ENTIRE renderer
// tree (renderer/**/*.js, recursive), including renderer/dashboards/.
//
// WHY the ban: native window.alert/confirm/prompt open Chromium's native dialog
// manager. On Windows/Electron, dismissing one leaves the renderer swallowing
// the Space (and sometimes Enter) key until the window blurs/refocuses — the
// "spacebar stops working after a popup" bug. Use renderer/toast.js (showToast)
// for notifications and renderer/confirm_dialog.js (confirmDialog) for
// promise-based confirmations instead.
//
// The vendor/ subtree (third-party bundles, e.g. triforce-dashboards.js) is
// excluded: it is not our source and may legitimately reference these names.

const rendererDir = path.join(__dirname, '..', 'renderer');

function rendererJsFiles(dir = rendererDir) {
  const out = [];
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (e.name === 'vendor' || e.name === 'node_modules' || e.name === 'dist') continue;
    const full = path.join(dir, e.name);
    if (e.isDirectory()) out.push(...rendererJsFiles(full));
    else if (e.isFile() && e.name.endsWith('.js')) out.push(full);
  }
  return out;
}

// A native dialog CALL: alert|confirm|prompt immediately followed by `(`, with a
// non-identifier boundary before it (so `window.alert(` and bare `alert(` both
// match, while identifiers like `isSystemHelperPrompt(` — capital P — and
// `confirmButton(` do not).
const NATIVE_DIALOG_CALL = /(^|[^\w$])(alert|confirm|prompt)\s*\(/;

test('no native dialog calls (alert/confirm/prompt) anywhere in renderer/**/*.js', () => {
  const offenders = [];
  for (const file of rendererJsFiles()) {
    const src = fs.readFileSync(file, 'utf8');
    src.split(/\r?\n/).forEach((line, i) => {
      const code = line.replace(/\/\/.*$/, ''); // ignore line comments
      if (NATIVE_DIALOG_CALL.test(code)) {
        offenders.push(`${path.relative(rendererDir, file)}:${i + 1}: ${line.trim()}`);
      }
    });
  }
  assert.deepEqual(
    offenders, [],
    `native dialog calls must be replaced with showToast (renderer/toast.js) or `
      + `confirmDialog (renderer/confirm_dialog.js):\n${offenders.join('\n')}`,
  );
});

test('renderer/app.js surfaces chat lifecycle errors without native dialogs', () => {
  const src = fs.readFileSync(path.join(rendererDir, 'app.js'), 'utf8');
  assert.match(src, /require\('\.\/toast'\)/, 'app.js must require ./toast');
  // Close and rename failures use a transient toast.
  for (const msg of ['Failed to close chat', 'Failed to rename chat']) {
    const re = new RegExp(`showToast\\(result\\?\\.error \\|\\| '${msg.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}'`);
    assert.match(src, re, `expected showToast(...) for "${msg}"`);
  }
  // Start-chat failure intentionally uses the new-session modal's INLINE error
  // surface (newSessionError + updateNewSessionStatus), not a toast — the modal
  // is already open so the error belongs inline. This is still a non-native UX
  // (the native-dialog ban above covers the safety property).
  assert.match(
    src,
    /newSessionError = result\?\.error\?\.message \|\| result\?\.error \|\| 'Failed to start chat'/,
    'start-chat failure must set the inline newSessionError',
  );
  assert.match(
    src,
    /updateNewSessionStatus\(newSessionError, true\)/,
    'inline newSessionError must be surfaced via updateNewSessionStatus',
  );
});

test('pi-control gates clear-output actions on the in-DOM confirmDialog, not native confirm', () => {
  const src = fs.readFileSync(path.join(rendererDir, 'dashboards', 'pi-control.js'), 'utf8');
  // The runtime seam binds to the global confirmDialog (renderer/confirm_dialog.js).
  assert.match(src, /win\.confirmDialog/, 'pi-control must bind confirmAction to window.confirmDialog');
  // Both clear flows await the promise-based confirm.
  assert.match(src, /await runtime\.confirmAction\(`Clear output/, 'clearOutput must await confirmAction');
  assert.match(src, /await runtime\.confirmAction\('Clear all agent outputs\?'\)/, 'clearAllOutputs must await confirmAction');
});
