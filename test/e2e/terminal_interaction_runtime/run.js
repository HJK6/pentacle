'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { runOwnedElectron } = require('./supervisor');
const output = path.resolve(process.env.PENTACLE_RUNTIME_OUTPUT || fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-terminal-results-')));
const executable = process.env.PENTACLE_RUNTIME_ELECTRON || require('electron');
let child;
const relay = signal => child?.kill(signal);
process.on('SIGINT', () => relay('SIGINT'));
process.on('SIGTERM', () => relay('SIGTERM'));
runOwnedElectron({ executable, args: [__dirname], output, onChild: value => { child = value; } }).then(result => {
  console.log(JSON.stringify({ postExitCleanup: path.join(output, 'post-exit-cleanup.json'), exitCode: result.exitCode }));
  process.exitCode = result.exitCode;
}).catch(error => { console.error(error); process.exitCode = 2; });
