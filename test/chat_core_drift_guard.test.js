'use strict';

// Public contract guard for the platform-neutral chat core.
//
// The shared chat core owns event interpretation, reconciliation, and session
// selection. Desktop adapters may call those functions, but must not copy the
// canonical implementations into renderer/ or main/. Keeping this boundary
// explicit makes the adapter safe to reuse in another UI.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = path.join(__dirname, '..');
const coreRoot = path.join(repoRoot, 'pentacle-chat-core');
const coreSrcRoot = path.join(coreRoot, 'src');

const CANONICAL_SYMBOLS = [
  'interpretEvent',
  'coalesceEvents',
  'optimisticMatchesUser',
  'applyEvent',
  'selectSession',
];

const SKIP_DIR_NAMES = new Set([
  'node_modules',
  'dist',
  'chat-core',
  '.cache',
  'artifacts',
  'test',
]);

const SOURCE_EXTS = new Set(['.js', '.ts', '.tsx', '.mjs', '.cjs']);

function walk(dir, out = []) {
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch (_) {
    return out;
  }
  for (const entry of entries) {
    if (entry.isDirectory()) {
      if (SKIP_DIR_NAMES.has(entry.name)) continue;
      walk(path.join(dir, entry.name), out);
    } else if (entry.isFile() && SOURCE_EXTS.has(path.extname(entry.name))) {
      out.push(path.join(dir, entry.name));
    }
  }
  return out;
}

function stripCommentsAndStrings(source) {
  let out = '';
  let i = 0;
  const keepNewlines = (chunk) => chunk.replace(/[^\n]/g, ' ');
  while (i < source.length) {
    const c = source[i];
    const next = source[i + 1];
    if (c === '/' && next === '/') {
      const end = source.indexOf('\n', i);
      const stop = end === -1 ? source.length : end;
      out += keepNewlines(source.slice(i, stop));
      i = stop;
    } else if (c === '/' && next === '*') {
      const end = source.indexOf('*/', i + 2);
      const stop = end === -1 ? source.length : end + 2;
      out += keepNewlines(source.slice(i, stop));
      i = stop;
    } else if (c === '"' || c === "'" || c === '`') {
      const quote = c;
      let j = i + 1;
      while (j < source.length) {
        if (source[j] === '\\') {
          j += 2;
          continue;
        }
        if (source[j] === quote) {
          j += 1;
          break;
        }
        j += 1;
      }
      out += keepNewlines(source.slice(i, j));
      i = j;
    } else {
      out += c;
      i += 1;
    }
  }
  return out;
}

function definitionRegexFor(symbol) {
  const escaped = symbol.replace(/[.*+?^()|[\]\\]/g, '\\$&');
  return new RegExp(
    `^\\s*(?:export\\s+)?(?:async\\s+)?function\\s+${escaped}\\s*\\(` +
    `|^\\s*(?:export\\s+)?(?:const|let|var)\\s+${escaped}\\s*=`,
  );
}

const DEFINITION_REGEXES = CANONICAL_SYMBOLS.map((symbol) => ({
  symbol,
  regex: definitionRegexFor(symbol),
}));

function findDefinitions() {
  const files = [
    ...walk(path.join(repoRoot, 'renderer')),
    ...walk(path.join(repoRoot, 'main')),
  ];
  const found = [];
  for (const file of files) {
    const rel = path.relative(repoRoot, file).split(path.sep).join('/');
    const lines = fs.readFileSync(file, 'utf8').split(/\r?\n/);
    for (let i = 0; i < lines.length; i += 1) {
      const code = lines[i].replace(/\/\/.*$/, '');
      for (const { symbol, regex } of DEFINITION_REGEXES) {
        if (regex.test(code)) found.push(`${rel}:${i + 1}: defines ${symbol}`);
      }
    }
  }
  return found;
}

const coreFiles = () => walk(coreSrcRoot);

test('desktop adapters do not reimplement public chat-core functions', () => {
  assert.deepEqual(
    findDefinitions(),
    [],
    'import canonical behavior from the public chat-core package',
  );
});

test('public chat-core source stays platform-neutral', () => {
  assert.ok(coreFiles().length > 0, 'public core source is present');
  const bannedImports = ['electron', 'react-native', 'react-dom'];
  const bannedGlobals = ['document', 'window', 'navigator', 'localStorage', 'HTMLElement'];
  const offenders = [];

  for (const file of coreFiles()) {
    const rel = path.relative(repoRoot, file).split(path.sep).join('/');
    const lines = fs.readFileSync(file, 'utf8').split(/\r?\n/);
    for (let i = 0; i < lines.length; i += 1) {
      const code = lines[i].replace(/\/\/.*$/, '');
      for (const name of bannedImports) {
        if (new RegExp(`(?:from\\s+|require\\(\\s*)['"]${name}(?:/[^'"]*)?['"]`).test(code)) {
          offenders.push(`${rel}:${i + 1}: imports ${name}`);
        }
      }
      const stripped = stripCommentsAndStrings(lines[i]);
      for (const name of bannedGlobals) {
        if (new RegExp(`(^|[^\\w$])(?:globalThis\\.)?${name}\\s*[.\\[]`).test(stripped)) {
          offenders.push(`${rel}:${i + 1}: uses ${name}`);
        }
      }
    }
  }

  assert.deepEqual(offenders, []);
});

