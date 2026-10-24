'use strict';

const fs = require('node:fs');

function daemonLogTail(logPath, secrets = []) {
  let fd;
  try {
    fd = fs.openSync(logPath, 'r');
    const size = fs.fstatSync(fd).size;
    const start = Math.max(0, size - 65536);
    const buffer = Buffer.alloc(size - start);
    const read = fs.readSync(fd, buffer, 0, buffer.length, start);
    let window = buffer.subarray(0, read);
    if (start > 0) {
      const newline = window.indexOf(10);
      window = newline < 0 ? Buffer.alloc(0) : window.subarray(newline + 1);
    }
    let text = window.toString('utf8');
    for (const secret of secrets.filter(value => typeof value === 'string' && value.length > 0)
      .sort((a, b) => b.length - a.length)) {
      text = text.split(secret).join('[REDACTED]');
    }
    const tail = Buffer.from(text.split('\n').slice(-40).join('\n'), 'utf8');
    let offset = Math.max(0, tail.length - 4096);
    while (offset < tail.length && (tail[offset] & 0xc0) === 0x80) offset += 1;
    return tail.subarray(offset).toString('utf8');
  } catch (error) {
    const code = typeof error.code === 'string' && /^[A-Z0-9_]+$/.test(error.code) ? error.code : 'UNKNOWN';
    return `<daemon.log unavailable: ${code}>`;
  } finally {
    if (fd !== undefined) {
      try { fs.closeSync(fd); } catch { /* retain the read result */ }
    }
  }
}

module.exports = { daemonLogTail };
