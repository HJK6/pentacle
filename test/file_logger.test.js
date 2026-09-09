const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { createFileLogger, _isBrokenPipe } = require('../main/file_logger');

function tempLogPath() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'file-logger-'));
  return { dir, logPath: path.join(dir, 'pentacle.log') };
}

function sizeOrZero(filePath) {
  try { return fs.statSync(filePath).size; } catch { return 0; }
}

function readLines(filePath) {
  return fs.readFileSync(filePath, 'utf8').trim().split('\n').filter(Boolean);
}

function restoreConsole(t) {
  const originals = {
    log: console.log,
    info: console.info,
    warn: console.warn,
    error: console.error,
  };
  t.after(() => {
    console.log = originals.log;
    console.info = originals.info;
    console.warn = originals.warn;
    console.error = originals.error;
  });
}

function diffNewListeners(emitter, eventName, before) {
  return emitter.listeners(eventName).filter((listener) => !before.includes(listener));
}

test('rotates on each write and keeps active plus one backup bounded', () => {
  const { logPath } = tempLogPath();
  const logger = createFileLogger({ logPath, maxBytes: 256, maxLineBytes: 64 });
  const line = 'x'.repeat(31);

  for (let i = 0; i < 40; i += 1) logger.write(`${i}:${line}`);

  const activeSize = sizeOrZero(logPath);
  const backupSize = sizeOrZero(`${logPath}.1`);
  assert.ok(activeSize <= 256 + 64, `active size ${activeSize} exceeded bound`);
  assert.ok(backupSize > 0, 'expected one backup');
  assert.ok(activeSize + backupSize <= (2 * 256) + 64,
    `total size ${activeSize + backupSize} exceeded bound`);
  assert.deepEqual(fs.readdirSync(path.dirname(logPath)).sort(), ['pentacle.log', 'pentacle.log.1']);
});

test('seeds byte count from an existing file and rotates before first append', () => {
  const { logPath } = tempLogPath();
  fs.writeFileSync(logPath, 'a'.repeat(300));

  const logger = createFileLogger({ logPath, maxBytes: 256, maxLineBytes: 64 });
  logger.write('new line');

  const activeSize = sizeOrZero(logPath);
  const backupSize = sizeOrZero(`${logPath}.1`);
  assert.ok(activeSize <= 256 + 64, `active size ${activeSize} exceeded bound`);
  assert.ok(backupSize <= 256, `backup size ${backupSize} exceeded bound`);
  assert.ok(activeSize + backupSize <= (2 * 256) + 64,
    `total size ${activeSize + backupSize} exceeded bound`);
  assert.equal(fs.readFileSync(logPath, 'utf8'), 'new line\n');
});

test('caps a far-oversized pre-existing log backup to the tail', () => {
  const { logPath } = tempLogPath();
  const tail = Array.from({ length: 256 }, (_, i) => String.fromCharCode(65 + (i % 26))).join('');
  const original = `${'h'.repeat(10000 - tail.length)}${tail}`;
  fs.writeFileSync(logPath, original);

  const logger = createFileLogger({ logPath, maxBytes: 256, maxLineBytes: 64 });
  logger.write('new line');

  const activeSize = sizeOrZero(logPath);
  const backupSize = sizeOrZero(`${logPath}.1`);
  assert.ok(activeSize <= 256 + 64, `active size ${activeSize} exceeded bound`);
  assert.ok(backupSize <= 256, `backup size ${backupSize} exceeded bound`);
  assert.ok(activeSize + backupSize <= (2 * 256) + 64,
    `total size ${activeSize + backupSize} exceeded bound`);
  assert.equal(fs.readFileSync(`${logPath}.1`, 'utf8'), tail);
  assert.notEqual(fs.readFileSync(`${logPath}.1`, 'utf8'), original.slice(0, 256));
});

test('clamps maxLineBytes to maxBytes so small maxBytes stays bounded', () => {
  const { logPath } = tempLogPath();
  const logger = createFileLogger({ logPath, maxBytes: 10, maxLineBytes: 64 });

  for (let i = 0; i < 6; i += 1) logger.write('123456789');

  const activeSize = sizeOrZero(logPath);
  const backupSize = sizeOrZero(`${logPath}.1`);
  assert.ok(activeSize <= 20, `active size ${activeSize} exceeded bound`);
  assert.ok(backupSize <= 10, `backup size ${backupSize} exceeded bound`);
  assert.ok(activeSize + backupSize <= (2 * 10) + 10,
    `total size ${activeSize + backupSize} exceeded bound`);
});

test('truncates oversized lines by UTF-8 byte length before writing', () => {
  const { logPath } = tempLogPath();
  const logger = createFileLogger({ logPath, maxBytes: 256, maxLineBytes: 64 });

  logger.write('a'.repeat(200));

  const body = fs.readFileSync(logPath, 'utf8');
  assert.ok(Buffer.byteLength(body, 'utf8') <= 64);
  assert.match(body, /…\[truncated\]\n$/);
});

test('console mirror swallows throwing passthrough and disables it after first throw', (t) => {
  restoreConsole(t);
  const { logPath } = tempLogPath();
  let origCalls = 0;
  console.error = () => {
    origCalls += 1;
    throw new Error('broken pipe');
  };

  const logger = createFileLogger({ logPath, maxBytes: 256, maxLineBytes: 256, now: () => 0 });
  logger.installConsoleMirror();

  assert.doesNotThrow(() => console.error('first'));
  assert.doesNotThrow(() => console.error('second'));
  assert.equal(origCalls, 1);
  assert.deepEqual(readLines(logPath), [
    '[1970-01-01T00:00:00.000Z] [error] first',
    '[1970-01-01T00:00:00.000Z] [error] second',
  ]);
});

test('crash handlers write to file only, keep process alive, and dedupe repeats', (t) => {
  restoreConsole(t);
  const { logPath } = tempLogPath();
  let ts = 0;
  let consoleCalls = 0;
  console.error = () => { consoleCalls += 1; };
  console.warn = () => { consoleCalls += 1; };
  console.info = () => { consoleCalls += 1; };
  console.log = () => { consoleCalls += 1; };

  const beforeUncaught = process.listeners('uncaughtException');
  const beforeUnhandled = process.listeners('unhandledRejection');
  const exitBefore = process.exit;
  let exitCalls = 0;
  process.exit = () => { exitCalls += 1; };
  t.after(() => { process.exit = exitBefore; });

  const logger = createFileLogger({
    logPath,
    maxBytes: 4096,
    maxLineBytes: 1024,
    now: () => ts,
    dedupeWindowMs: 1000,
  });
  logger.installConsoleMirror();
  logger.installCrashHandlers();

  const newUncaught = diffNewListeners(process, 'uncaughtException', beforeUncaught);
  const newUnhandled = diffNewListeners(process, 'unhandledRejection', beforeUnhandled);
  t.after(() => {
    for (const listener of newUncaught) process.removeListener('uncaughtException', listener);
    for (const listener of newUnhandled) process.removeListener('unhandledRejection', listener);
  });
  assert.equal(newUncaught.length, 1);
  assert.equal(newUnhandled.length, 1);

  const err = new Error('same fault');
  newUncaught[0](err);
  for (let i = 0; i < 4; i += 1) newUncaught[0](err);
  ts = 1500;
  newUncaught[0](err);
  newUnhandled[0](new Error('different fault'));

  const text = fs.readFileSync(logPath, 'utf8');
  assert.equal(consoleCalls, 0);
  assert.equal(exitCalls, 0);
  assert.match(text, /\[uncaught\] Error: same fault/);
  assert.match(text, /Error: Error: same fault \(x5\)/);
  assert.match(text, /\[unhandledRejection\] Error: different fault/);
  assert.ok(readLines(logPath).length < 7);
});

test('broken-pipe predicate and stdio guard registration', (t) => {
  assert.equal(_isBrokenPipe({ code: 'EPIPE' }), true);
  assert.equal(_isBrokenPipe({ code: 'EOF' }), true);
  assert.equal(_isBrokenPipe({ code: 'ERR_STREAM_DESTROYED' }), true);
  assert.equal(_isBrokenPipe({ code: 'ERR_STREAM_WRITE_AFTER_END' }), true);
  assert.equal(_isBrokenPipe({ code: 'ECONNRESET' }), false);
  assert.equal(_isBrokenPipe(null), false);

  const { logPath } = tempLogPath();
  const stdoutBefore = process.stdout.listeners('error');
  const stderrBefore = process.stderr.listeners('error');
  const logger = createFileLogger({ logPath });
  logger.installStdioGuards();

  const newStdout = diffNewListeners(process.stdout, 'error', stdoutBefore);
  const newStderr = diffNewListeners(process.stderr, 'error', stderrBefore);
  t.after(() => {
    for (const listener of newStdout) process.stdout.removeListener('error', listener);
    for (const listener of newStderr) process.stderr.removeListener('error', listener);
  });
  assert.equal(newStdout.length, 1);
  assert.equal(newStderr.length, 1);
});

test('bad paths are best-effort and never throw from public methods', (t) => {
  restoreConsole(t);
  const badPath = path.join(os.tmpdir(), `missing-${Date.now()}`, 'pentacle.log');
  console.error = () => {};

  assert.doesNotThrow(() => {
    const logger = createFileLogger({ logPath: badPath });
    logger.write('line');
    logger.installConsoleMirror();
    console.error('still no throw');
  });
});
