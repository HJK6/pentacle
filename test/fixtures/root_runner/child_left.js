'use strict';
const { spawn } = require('node:child_process');
const child = spawn(process.execPath, ['-e', "process.on('SIGTERM',()=>{});setInterval(()=>{},1000)"], { stdio: 'inherit' });
console.log('SYNTHETIC_CHILD_PID=' + child.pid);
setTimeout(() => process.exit(0), 75);
