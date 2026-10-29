#!/usr/bin/env node
'use strict';
// Offline staging helper for the exact-scope cutover. Never invoked by the host.
const fs = require('node:fs');
const path = require('node:path');
function install({ binDir, inboxDir, candidate, root = path.join(__dirname, '..') }) {
  if (!path.isAbsolute(binDir) || !path.isAbsolute(inboxDir) || !/^[0-9a-f]{40}$/.test(candidate)) throw new Error('invalid install arguments');
  const versionDir = path.join(binDir, `review-item-${candidate}`);
  fs.mkdirSync(binDir, { recursive: true, mode: 0o700 });
  fs.mkdirSync(versionDir, { mode: 0o700 });
  fs.mkdirSync(path.join(versionDir, 'scripts'), { mode: 0o700 });
  fs.mkdirSync(path.join(versionDir, 'main'), { mode: 0o700 });
  fs.copyFileSync(path.join(root, 'scripts/review-item.js'), path.join(versionDir, 'scripts/review-item.js'), fs.constants.COPYFILE_EXCL);
  fs.copyFileSync(path.join(root, 'main/review_store.js'), path.join(versionDir, 'main/review_store.js'), fs.constants.COPYFILE_EXCL);
  fs.writeFileSync(path.join(versionDir, 'VERSION'), `${candidate}\n`, { flag: 'wx', mode: 0o400 });
  for (const file of ['scripts/review-item.js', 'main/review_store.js']) fs.chmodSync(path.join(versionDir, file), 0o400);
  for (const sub of ['scripts', 'main']) fs.chmodSync(path.join(versionDir, sub), 0o500);
  fs.chmodSync(versionDir, 0o500);
  const wrapper = path.join(binDir, 'review-item');
  const quote = x => `'${x.replaceAll("'", "'\\''")}'`;
  fs.writeFileSync(wrapper, `#!/bin/sh\nfor arg do\n  if [ "$arg" = '--dir' ]; then echo 'caller --dir refused' >&2; exit 2; fi\ndone\nexec /opt/homebrew/bin/node ${quote(path.join(versionDir, 'scripts/review-item.js'))} --dir ${quote(inboxDir)} "$@"\n`, { flag: 'wx', mode: 0o500 });
  return { wrapper, versionDir };
}
if (require.main === module) {
  const [binDir, inboxDir, candidate] = process.argv.slice(2);
  try { console.log(JSON.stringify(install({ binDir, inboxDir, candidate }))); }
  catch { console.error('install_failed'); process.exitCode = 1; }
}
module.exports = { install };
