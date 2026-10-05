'use strict';
const path = require('node:path');
const { parseArgs, run } = require('./lib/test-runner');

run(path.resolve(__dirname, '..'), parseArgs(process.argv.slice(2)))
  .then(summary => { process.exitCode = summary.exit_code; })
  .catch(error => { console.error(`test runner failed: ${error.message}`); process.exitCode = 1; });
