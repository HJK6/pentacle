'use strict';
// npm's node-pty archive may strip the macOS spawn-helper executable bit.
// Restore it in the installed dependency before the first terminal attachment.
const fs = require('node:fs');
const path = require('node:path');
if (process.platform === 'darwin') {
  let root;
  try { root = path.dirname(require.resolve('node-pty/package.json')); } catch { process.exit(0); }
  for (const relative of [`prebuilds/darwin-${process.arch}/spawn-helper`, 'build/Release/spawn-helper']) {
    const helper = path.join(root, relative);
    if (fs.existsSync(helper)) fs.chmodSync(helper, fs.statSync(helper).mode | 0o111);
  }
}
