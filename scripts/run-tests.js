'use strict';

const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = path.resolve(__dirname, '..');
const entries = process.argv.slice(2)
  .flatMap((pattern) => {
    if (!pattern.includes('*')) {
      return fs.existsSync(path.join(repoRoot, pattern)) ? [pattern] : [];
    }
    const directory = path.dirname(pattern);
    const [prefix, suffix] = path.basename(pattern).split('*');
    return fs.readdirSync(path.join(repoRoot, directory), { withFileTypes: true })
      .filter((entry) => entry.isFile() && entry.name.startsWith(prefix) && entry.name.endsWith(suffix))
      .map((entry) => path.join(directory, entry.name));
  })
  .sort();
if (!entries.length) throw new Error('no tests matched');

const esbuild = path.join(
  repoRoot,
  'node_modules',
  '.bin',
  process.platform === 'win32' ? 'esbuild.cmd' : 'esbuild',
);
const cacheRoot = path.join(repoRoot, 'node_modules', '.cache');
fs.mkdirSync(cacheRoot, { recursive: true });
const outDir = fs.mkdtempSync(path.join(cacheRoot, 'pentacle-tests-'));
const tests = entries.filter((entry) => entry.endsWith('.js'));

try {
  for (const entry of entries.filter((candidate) => candidate.endsWith('.ts'))) {
    const outFile = path.join(outDir, `${path.basename(entry, '.ts')}.cjs`);
    execFileSync(esbuild, [
      path.join(repoRoot, entry),
      '--bundle',
      '--platform=node',
      '--format=cjs',
      '--target=node18',
      '--external:node:*',
      '--external:jsdom',
      `--outfile=${outFile}`,
    ], { stdio: 'inherit', cwd: repoRoot, shell: process.platform === 'win32' });
    tests.push(outFile);
  }
  execFileSync(process.execPath, ['--test', ...tests], { stdio: 'inherit', cwd: repoRoot });
} finally {
  fs.rmSync(outDir, { recursive: true, force: true });
}
