#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');

const { computeBuild } = require('../build.js');

const ROOT = path.join(__dirname, '..');

function firstDifference(expected, actual) {
  const maxComparable = Math.min(expected.length, actual.length);

  for (let index = 0; index < maxComparable; index += 1) {
    if (expected[index] !== actual[index]) {
      return index;
    }
  }

  return expected.length === actual.length ? -1 : maxComparable;
}

function lineColumn(buffer, index) {
  const before = buffer.subarray(0, index).toString('utf8');
  const lines = before.split('\n');

  return {
    line: lines.length,
    column: Buffer.byteLength(lines[lines.length - 1], 'utf8') + 1,
  };
}

function compareFile(label, expectedText, relativePath) {
  const expected = Buffer.from(expectedText, 'utf8');
  const actualPath = path.join(ROOT, relativePath);
  const actual = fs.readFileSync(actualPath);

  if (expected.equals(actual)) {
    return null;
  }

  const index = firstDifference(expected, actual);
  const location = index >= 0 ? lineColumn(actual, index) : { line: 1, column: 1 };

  return `${label} differs: expected ${expected.length} bytes, found ${actual.length} bytes; first difference near ${relativePath}:${location.line}:${location.column}`;
}

const build = computeBuild();
const mismatches = [
  compareFile('dist/dashboard-library.js', build.js, 'dist/dashboard-library.js'),
  compareFile('dist/dashboard-library.css', build.css, 'dist/dashboard-library.css'),
  compareFile('VERSION', `${build.version}\n`, 'VERSION'),
].filter(Boolean);

if (mismatches.length > 0) {
  console.error('dist drift detected. Run `npm run build` and commit the updated artifacts.');
  for (const mismatch of mismatches) {
    console.error(`- ${mismatch}`);
  }
  process.exit(1);
}

console.log(`OK: committed dist/ and VERSION match fresh build @ VERSION=${build.version}`);
