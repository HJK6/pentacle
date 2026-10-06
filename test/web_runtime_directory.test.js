'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { createRequire } = require('node:module');
const { withRuntimeDirectory, execWithRuntimeDirectory } = require('./e2e/lib/runtime_directory');

for (const keep of [false, true]) {
  for (const failure of [null, 'setup', 'scenario', 'cleanup']) {
    test('actual gate isolates initial/restart paths, keep=' + keep + ', failure=' + failure, async () => {
      const root = fs.mkdtempSync(path.join(os.tmpdir(), 'th-h3-gate-unit-'));
      const env = { PENTACLE_RUNTIME_DIR: '/synthetic/caller-runtime' };
      const observations = []; const children = []; const openFds = [];
      const filename = path.resolve(__dirname, 'e2e/web_gate.js');
      const realRequire = createRequire(filename);
      const fakeNet = {
        createServer() {
          const s = new EventEmitter(); s.listen = (p, h, cb) => cb(); s.address = () => ({ port: 42420 }); s.close = cb => cb(); return s;
        },
        connect() { const s = new EventEmitter(); s.destroy = () => {}; queueMicrotask(() => s.emit('connect')); return s; },
      };
      function spawn(command, args, options) {
        const p = new EventEmitter(); p.pid = 42421 + children.length; p.exitCode = null; p.signalCode = null;
        p.stdout = new EventEmitter(); p.stderr = new EventEmitter();
        p.kill = signal => { p.signalCode = signal; p.emit('exit', null, signal); };
        children.push(p);
        if (args[0].endsWith('/server')) observations.push(options.env.PENTACLE_RUNTIME_DIR);
        else assert.equal(command, 'synthetic-chrome');
        return p;
      }
      const fakeFs = Object.create(fs);
      fakeFs.rmSync = (target, options) => {
        if (failure === 'cleanup' && path.basename(target).startsWith('pentacle-web-runtime-')) throw new Error('synthetic runtime cleanup failure');
        return fs.rmSync(target, options);
      };
      fakeFs.openSync = (...args) => { const fd = fs.openSync(...args); openFds.push(fd); return fd; };
      const quiet = { log() {}, error() {} };
      const context = { module: { exports: {} }, __dirname: path.join(root, 'e2e'), process: { env, execPath: process.execPath },
        console: quiet, Buffer, Date, setTimeout, clearTimeout,
        require(name) {
          if (name === 'fs') return fakeFs;
          if (name === 'os') return { ...os, tmpdir: () => root };
          if (name === 'net') return fakeNet;
          if (name === 'child_process') return { spawn, execFileSync: cmd => { assert.equal(cmd, 'which'); return 'synthetic-chrome'; } };
          if (name === './lib/runtime_directory') return { withRuntimeDirectory: run => withRuntimeDirectory(run, { env, tempRoot: root, filesystem: fakeFs }) };
          if (name === '../../server') return { main: async () => {
            observations.push(env.PENTACLE_RUNTIME_DIR);
            assert.ok(fs.existsSync(env.PENTACLE_RUNTIME_DIR));
            fs.writeFileSync(path.join(env.PENTACLE_RUNTIME_DIR, 'desktop-runtime.json'), 'synthetic');
            if (failure === 'setup') throw new Error('synthetic setup failure');
            return { port: 42420, close: async () => {} };
          } };
          if (name === './lib/cdp') return { connect: async () => ({ consoleLines: [], close() {} }), sleep: async () => {} };
          if (name === './lib/web_scenarios') return { SCENARIOS: [['synthetic-restart', async ctx => {
            await ctx.restartHost();
            if (failure === 'scenario') throw new Error('synthetic scenario failure');
          }]] };
          return realRequire(name);
        } };
      // Supply a fake executable name; no Chrome process or socket is opened.
      env.PENTACLE_CHROME = 'synthetic-chrome';
      try {
        vm.runInNewContext(fs.readFileSync(filename, 'utf8'), context, { filename });
        const code = await context.module.exports.run({ profile: '/synthetic/profile', keep, timeoutMs: 100, cdpPort: 42422 });
        assert.equal(code, failure ? 1 : 0);
        assert.equal(observations.length, failure === 'setup' ? 1 : 2);
        assert.ok(observations.every(dir => dir === observations[0]));
        assert.notEqual(observations[0], '/synthetic/caller-runtime');
        assert.equal(fs.existsSync(observations[0]), failure === 'cleanup');
        const runDir = fs.readdirSync(path.join(root, 'e2e/runs'))[0];
        const verdict = JSON.parse(fs.readFileSync(path.join(root, 'e2e/runs', runDir, 'web_gate/verdict.json')));
        if (failure === 'cleanup') { assert.equal(verdict.status, 'FAIL'); assert.match(verdict.cleanup_error, /synthetic runtime cleanup failure/); }
        assert.equal(env.PENTACLE_RUNTIME_DIR, '/synthetic/caller-runtime');
        for (const child of children.filter(p => p !== children[0])) assert.notEqual(child.signalCode, null);
      } finally {
        for (const fd of openFds) { try { fs.closeSync(fd); } catch {} }
        fs.rmSync(root, { recursive: true, force: true });
      }
    });
  }
}


test('chained child gate runs in its own owned runtime directory, removed afterwards', async () => {
  const scopeEnv = { PENTACLE_RUNTIME_DIR: '/synthetic/caller-runtime' };
  const seen = [];
  const exec = (command, args, options) => {
    const directory = options.env.PENTACLE_RUNTIME_DIR;
    seen.push({ command, args, directory, exists: fs.statSync(directory).isDirectory(), browser: options.env.PENTACLE_TEST_BROWSER, stdio: options.stdio });
    fs.writeFileSync(path.join(directory, 'desktop-runtime.json'), '{"synthetic":true}');
    return 'synthetic-child-result';
  };
  const result = await execWithRuntimeDirectory(exec, 'synthetic-node', ['synthetic-gate.cjs'],
    { stdio: 'inherit', env: { PENTACLE_TEST_BROWSER: 'synthetic-chrome', PENTACLE_RUNTIME_DIR: '/synthetic/inherited' } }, { env: scopeEnv });
  assert.equal(result, 'synthetic-child-result');
  assert.equal(seen.length, 1);
  assert.equal(seen[0].exists, true);
  assert.ok(path.basename(seen[0].directory).startsWith('pentacle-web-runtime-'));
  assert.notEqual(seen[0].directory, '/synthetic/inherited');
  assert.deepEqual([seen[0].command, seen[0].args, seen[0].browser, seen[0].stdio], ['synthetic-node', ['synthetic-gate.cjs'], 'synthetic-chrome', 'inherit']);
  assert.equal(fs.existsSync(seen[0].directory), false);
  assert.equal(scopeEnv.PENTACLE_RUNTIME_DIR, '/synthetic/caller-runtime');
  await assert.rejects(execWithRuntimeDirectory(() => { throw new Error('synthetic child failure'); }, 'n', [], {}, { env: scopeEnv }), /synthetic child failure/);
  assert.equal(scopeEnv.PENTACLE_RUNTIME_DIR, '/synthetic/caller-runtime');
});

test('web gate launches the history-retention stage through the owned runtime scope', () => {
  const source = fs.readFileSync(path.resolve(__dirname, 'e2e/web_gate.js'), 'utf8');
  const launch = source.slice(source.indexOf("web_chat_history_retention_gate.cjs") - 200, source.indexOf("web_chat_history_retention_gate.cjs"));
  assert.match(launch, /await execWithRuntimeDirectory\(execFileSync, process\.execPath/);
});
