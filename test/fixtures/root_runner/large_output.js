'use strict';
process.stdout.write('x'.repeat(2 * 1024 * 1024) + '\n');
console.log('SYNTHETIC_FINAL_TAIL_MARKER');
setInterval(() => {}, 1000);
