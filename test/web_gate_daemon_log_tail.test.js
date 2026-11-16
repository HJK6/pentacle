'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { daemonLogTail } = require('./e2e/lib/daemon_log_tail');

function withLog(content, check) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'daemon-tail-test-'));
  const file = path.join(directory, 'daemon.log');
  try {
    fs.writeFileSync(file, content);
    check(file);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
}

function assertBounded(tail) {
  assert.ok(Buffer.byteLength(tail, 'utf8') <= 4096, 'tail stays within byte cap');
  assert.ok(tail.split('\n').length <= 40, 'tail stays within line cap');
}

test('large log retains its last line within both caps', () => {
  const lines = Array.from({ length: 200 }, (_, i) => `${String(i).padStart(3, '0')}:${'x'.repeat(295)}`);
  withLog(lines.join('\n') + '\n', file => {
    const tail = daemonLogTail(file, []);
    assertBounded(tail);
    assert.ok(tail.endsWith(lines.at(-1) + '\n'), 'last complete line retained');
  });
});

test('line cap applies even when forty lines fit in the byte cap', () => {
  withLog(Array.from({ length: 100 }, (_, i) => `line-${i}`).join('\n'), file => {
    const tail = daemonLogTail(file, []);
    assert.equal(tail.split('\n').length, 40);
    assert.equal(tail.split('\n')[0], 'line-60');
    assert.ok(tail.endsWith('line-99'));
  });
});

test('all repeated and overlapping secret values are masked, including the last line', () => {
  const secret = 'synthetic-fixture-token';
  const extended = secret + '-extended';
  withLog(`${secret} ${secret}\n${extended}\nlast ${secret} ${extended}`, file => {
    const tail = daemonLogTail(file, [secret, extended]);
    assert.ok(!tail.includes(secret), 'fixture token absent');
    assert.ok(!tail.includes(extended), 'extended fixture token absent');
    assert.equal(tail, '[REDACTED] [REDACTED]\n[REDACTED]\nlast [REDACTED] [REDACTED]');
  });
});

test('a secret straddling the read window cannot leave a secret fragment', () => {
  const secret = 'window-fixture-' + 'q'.repeat(64);
  const prefix = 'prefix\n';
  const content = prefix + secret + '\n' + 'x'.repeat(65490) + '\nlast-line\n';
  const windowStart = Buffer.byteLength(content) - 65536;
  assert.ok(windowStart > prefix.length && windowStart < prefix.length + secret.length, 'fixture crosses window start');
  withLog(content, file => {
    const tail = daemonLogTail(file, [secret]);
    assertBounded(tail);
    for (let i = 0; i <= secret.length - 16; i++) {
      assert.ok(!tail.includes(secret.slice(i, i + 16)), 'no secret fragment survives');
    }
    assert.ok(tail.endsWith('last-line\n'));
  });
});

test('missing file returns only its error code without throwing', () => {
  withLog('', file => {
    assert.equal(daemonLogTail(file + '-missing', []), '<daemon.log unavailable: ENOENT>');
  });
});

test('unreadable file returns only its error code without throwing', { skip: process.getuid?.() === 0 }, () => {
  withLog('inaccessible', file => {
    fs.chmodSync(file, 0);
    try {
      assert.equal(daemonLogTail(file, []), '<daemon.log unavailable: EACCES>');
    } finally {
      fs.chmodSync(file, 0o600);
    }
  });
});

test('multibyte tail is cut on a UTF-8 character boundary', () => {
  withLog('é🙂界'.repeat(2000), file => {
    const tail = daemonLogTail(file, []);
    assertBounded(tail);
    assert.ok(!tail.includes('\uFFFD'), 'no replacement character from a split code point');
    assert.doesNotThrow(() => new TextDecoder('utf-8', { fatal: true }).decode(Buffer.from(tail)));
    assert.ok(tail.endsWith('é🙂界'));
  });
});

test('one ten-thousand-byte line remains bounded and retains its end', () => {
  withLog('x'.repeat(9990) + 'final-text', file => {
    const tail = daemonLogTail(file, []);
    assertBounded(tail);
    assert.equal(Buffer.byteLength(tail), 4096);
    assert.ok(tail.endsWith('final-text'));
  });
});

test('empty secrets and empty secret values leave short content unchanged', () => {
  const content = 'first line\nlast line\n';
  withLog(content, file => {
    assert.equal(daemonLogTail(file, []), content);
    assert.equal(daemonLogTail(file, ['', '']), content);
  });
});
