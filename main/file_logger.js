'use strict';































const fs = require('node:fs');

const DEFAULT_MAX_BYTES = 5 * 1024 * 1024;
const DEFAULT_MAX_LINE_BYTES = 64 * 1024;
const DEFAULT_DEDUPE_WINDOW_MS = 1000;
const TRUNCATED_MARKER = ' …[truncated]';
const BROKEN_PIPE_CODES = new Set([
  'EPIPE',
  'EOF',
  'ERR_STREAM_DESTROYED',
  'ERR_STREAM_WRITE_AFTER_END',
]);

function _isBrokenPipe(err) {
  try {
    return !!(err && BROKEN_PIPE_CODES.has(err.code));
  } catch {
    return false;
  }
}

function byteLength(value) {
  return Buffer.byteLength(value, 'utf8');
}

function positiveInteger(value, fallback) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return fallback;
  return Math.floor(n);
}

function truncateUtf8(value, maxBytes) {
  let out = '';
  let used = 0;
  for (const ch of String(value)) {
    const n = byteLength(ch);
    if (used + n > maxBytes) break;
    out += ch;
    used += n;
  }
  return out;
}

function truncateLine(line, maxLineBytes) {
  const text = String(line);
  if (byteLength(text + '\n') <= maxLineBytes) return text;

  const bodyBudget = Math.max(0, maxLineBytes - byteLength('\n'));
  const markerBytes = byteLength(TRUNCATED_MARKER);
  if (markerBytes >= bodyBudget) return truncateUtf8(TRUNCATED_MARKER, bodyBudget);

  return truncateUtf8(text, bodyBudget - markerBytes) + TRUNCATED_MARKER;
}

function formatArgs(args) {
  return args.map((arg) => {
    if (typeof arg === 'string') return arg;
    if (arg && arg.stack) return arg.stack;
    try { return JSON.stringify(arg); } catch { return String(arg); }
  }).join(' ');
}

function rawCrashValue(value) {
  if (value && value.stack) return String(value.stack);
  return String(value);
}

function formatCrashValue(value) {
  return rawCrashValue(value).replace(/\r?\n/g, '\\n');
}

function crashSignature(kind, value) {
  const name = value && value.name ? String(value.name) : typeof value;
  const text = rawCrashValue(value);
  const firstLine = text.split(/\r?\n/, 1)[0] || String(value);
  const summary = `${name}: ${firstLine}`;
  return {
    sig: `${kind}:${summary}`,
    summary,
  };
}

function createFileLogger({
  logPath,
  maxBytes = DEFAULT_MAX_BYTES,
  maxLineBytes = DEFAULT_MAX_LINE_BYTES,
  now = () => Date.now(),
  dedupeWindowMs = DEFAULT_DEDUPE_WINDOW_MS,
} = {}) {
  const activeMaxBytes = positiveInteger(maxBytes, DEFAULT_MAX_BYTES);

  const activeMaxLineBytes = Math.min(positiveInteger(maxLineBytes, DEFAULT_MAX_LINE_BYTES), activeMaxBytes);
  const activeDedupeWindowMs = positiveInteger(dedupeWindowMs, DEFAULT_DEDUPE_WINDOW_MS);
  const backupPath = `${logPath}.1`;

  let fd = null;
  let bytesWritten = 0;
  let consoleInstalled = false;
  let consolePassthrough = true;
  let crashHandlersInstalled = false;
  let stdioGuardsInstalled = false;
  let lastCrashSig = null;
  let lastCrashKind = null;
  let lastCrashSummary = null;
  let lastCrashCount = 0;
  let lastCrashEmit = 0;

  function safeNow() {
    try {
      const n = Number(now());
      return Number.isFinite(n) ? n : Date.now();
    } catch {
      return Date.now();
    }
  }

  function iso(ts = safeNow()) {
    try {
      return new Date(ts).toISOString();
    } catch {
      return new Date().toISOString();
    }
  }

  function ensureOpen() {
    if (fd !== null) return;
    try {
      const st = fs.statSync(logPath);
      bytesWritten = st && Number.isFinite(st.size) ? st.size : 0;
    } catch {
      bytesWritten = 0;
    }
    fd = fs.openSync(logPath, 'a');
  }

  function capBackupToTail() {
    let backupFd = null;
    try {
      backupFd = fs.openSync(backupPath, 'r');
      const st = fs.fstatSync(backupFd);
      if (!st || st.size <= activeMaxBytes) return;

      const buf = Buffer.allocUnsafe(activeMaxBytes);
      const bytesRead = fs.readSync(backupFd, buf, 0, activeMaxBytes, st.size - activeMaxBytes);
      try { fs.closeSync(backupFd); } catch {}
      backupFd = null;
      fs.writeFileSync(backupPath, buf.subarray(0, bytesRead));
    } catch {
    } finally {
      if (backupFd !== null) {
        try { fs.closeSync(backupFd); } catch {}
      }
    }
  }

  function rotate() {
    if (fd !== null) {
      try { fs.closeSync(fd); } catch {}
      fd = null;
    }
    try { fs.rmSync(backupPath, { force: true }); } catch {}
    try { fs.renameSync(logPath, backupPath); } catch (err) {
      if (!err || err.code !== 'ENOENT') throw err;
    }
    capBackupToTail();
    fd = fs.openSync(logPath, 'a');
    bytesWritten = 0;
  }

  function write(line) {
    try {
      const truncated = truncateLine(line, activeMaxLineBytes);
      const payload = `${truncated}\n`;
      const n = byteLength(payload);
      ensureOpen();
      if (fd === null) return;
      if (bytesWritten > 0 && bytesWritten + n > activeMaxBytes) {
        rotate();
      }
      if (fd === null) return;
      fs.writeSync(fd, payload);
      bytesWritten += n;
    } catch {}
  }

  function installConsoleMirror() {
    try {
      if (consoleInstalled) return;
      consoleInstalled = true;
      for (const level of ['log', 'info', 'warn', 'error']) {
        if (typeof console[level] !== 'function') continue;
        const orig = console[level].bind(console);
        console[level] = (...args) => {
          try { write(`[${iso()}] [${level}] ${formatArgs(args)}`); } catch {}
          if (!consolePassthrough) return;
          try { orig(...args); } catch { consolePassthrough = false; }
        };
      }
    } catch {}
  }

  function flushCrashSummary(ts) {
    if (!lastCrashSig || lastCrashCount <= 1) return;
    write(`[${iso(ts)}] [${lastCrashKind}] ${lastCrashSummary} (x${lastCrashCount})`);
  }

  function recordCrash(kind, value) {
    try {
      const ts = safeNow();
      const { sig, summary } = crashSignature(kind, value);
      if (sig === lastCrashSig && ts - lastCrashEmit <= activeDedupeWindowMs) {
        lastCrashCount += 1;
        return;
      }

      flushCrashSummary(ts);
      write(`[${iso(ts)}] [${kind}] ${formatCrashValue(value)}`);
      lastCrashSig = sig;
      lastCrashKind = kind;
      lastCrashSummary = summary;
      lastCrashCount = 1;
      lastCrashEmit = ts;
    } catch {}
  }

  function installCrashHandlers() {
    try {
      if (crashHandlersInstalled) return;
      crashHandlersInstalled = true;
      process.on('uncaughtException', (err) => {
        recordCrash('uncaught', err);
      });
      process.on('unhandledRejection', (reason) => {
        recordCrash('unhandledRejection', reason);
      });
    } catch {}
  }

  function installStdioGuards() {
    try {
      if (stdioGuardsInstalled) return;
      stdioGuardsInstalled = true;
      const handler = (err) => {
        if (!_isBrokenPipe(err)) throw err;
      };
      process.stdout.on('error', handler);
      process.stderr.on('error', handler);
    } catch {}
  }

  try { ensureOpen(); } catch {}

  return {
    write,
    installConsoleMirror,
    installCrashHandlers,
    installStdioGuards,
    _isBrokenPipe,
  };
}

module.exports = {
  createFileLogger,
  _isBrokenPipe,
};
