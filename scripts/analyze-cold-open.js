#!/usr/bin/env node
'use strict';

// Cold-open telemetry analyzer.
//
// Usage:
//   PENTACLE_PERF_TELEMETRY=1 npm start         # run once
//   node scripts/analyze-cold-open.js           # summarize most recent run
//   node scripts/analyze-cold-open.js <path>    # summarize specific JSONL
//
// Resolves the perf-telemetry dir under the platform userData path (matches
// main/perf_telemetry.js). Reads either the newest .jsonl file or the
// explicit path passed as argv[2]. Emits a timeline plus a summary table
// for the cold-open critical path.

const fs = require('fs');
const path = require('path');
const os = require('os');

function userDataDir() {
  if (process.platform === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Application Support', 'Pentacle');
  }
  if (process.platform === 'win32') {
    return path.join(process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming'), 'Pentacle');
  }
  return path.join(os.homedir(), '.config', 'Pentacle');
}

function newestJsonlIn(dir) {
  const entries = fs.readdirSync(dir).filter((f) => f.endsWith('.jsonl'));
  if (!entries.length) return null;
  const withStats = entries.map((name) => {
    const full = path.join(dir, name);
    return { name, full, mtime: fs.statSync(full).mtimeMs };
  });
  withStats.sort((a, b) => b.mtime - a.mtime);
  return withStats[0].full;
}

function loadJsonl(filePath) {
  const raw = fs.readFileSync(filePath, 'utf8');
  return raw.split('\n').filter(Boolean).map((line) => {
    try { return JSON.parse(line); } catch { return null; }
  }).filter(Boolean);
}

function pad(value, width) {
  const s = String(value);
  return s.length >= width ? s : s + ' '.repeat(width - s.length);
}

function fmtMs(ms) {
  if (ms == null || Number.isNaN(Number(ms))) return '—';
  return Number(ms).toFixed(1) + 'ms';
}

function pickFirst(records, event) {
  return records.find((r) => r.event === event);
}

function pickAll(records, event) {
  return records.filter((r) => r.event === event);
}

function elapsedBetween(records, fromEvent, toEvent) {
  const a = pickFirst(records, fromEvent);
  const b = pickFirst(records, toEvent);
  if (!a || !b) return null;
  return Number(b.ts_ms) - Number(a.ts_ms);
}

function main() {
  let filePath = process.argv[2];
  if (!filePath) {
    const dir = path.join(userDataDir(), 'perf-telemetry');
    if (!fs.existsSync(dir)) {
      console.error('No telemetry dir at', dir);
      console.error('Run with PENTACLE_PERF_TELEMETRY=1 first.');
      process.exit(1);
    }
    filePath = newestJsonlIn(dir);
    if (!filePath) {
      console.error('No .jsonl files in', dir);
      process.exit(1);
    }
  }
  const records = loadJsonl(filePath);
  if (!records.length) {
    console.error('Empty log:', filePath);
    process.exit(1);
  }

  console.log('Telemetry file:', filePath);
  const init = pickFirst(records, 'harness:init');
  if (init) {
    const d = init.details || {};
    console.log(`  startup_id=${d.startup_id}  platform=${d.platform}  pid=${d.pid}`);
  }
  console.log('');
  console.log('Timeline (ms since harness init):');
  for (const r of records) {
    const ev = pad(r.event, 42);
    const ts = pad(`${r.ts_ms}`, 10);
    const details = r.details ? JSON.stringify(r.details) : '';
    console.log(`  ${ts}  ${ev}  ${details}`);
  }
  console.log('');
  console.log('Cold-open critical path:');
  const stages = [
    ['app-ready → window-shown',         'main:app-ready',                    'main:create-window-end'],
    ['window-shown → chatstream-spawn',  'main:create-window-end',            'main:ensure-chatstream-server-end'],
    ['chatstream-spawn → ws-open',       'main:ensure-chatstream-server-end', 'chat-stream:ws-open'],
    ['ws-open → snapshot',               'chat-stream:ws-open',               'chat-stream:snapshot-received'],
    ['cfg-ready start→end',              'renderer:cfg-ready-start',          'renderer:cfg-ready-end'],
    ['cfg-ready end → fetch-sessions',   'renderer:cfg-ready-end',            'renderer:fetch-sessions-start'],
    ['fetch-sessions RPC duration',      'renderer:fetch-sessions-start',     'renderer:fetch-sessions-rpc-done'],
    ['rpc-done → first non-empty side',  'renderer:fetch-sessions-rpc-done',  'renderer:first-non-empty-sidebar'],
    ['TOTAL (app-ready → sidebar)',      'main:app-ready',                    'renderer:first-non-empty-sidebar'],
  ];
  for (const [label, from, to] of stages) {
    const ms = elapsedBetween(records, from, to);
    console.log(`  ${pad(label, 38)}  ${fmtMs(ms)}`);
  }
  console.log('');
  const byHostStarts = pickAll(records, 'tmux:list-by-host:host-ok').concat(pickAll(records, 'tmux:list-by-host:host-err'));
  if (byHostStarts.length) {
    console.log('Per-host tmux list elapsed (first cold-open call):');
    for (const r of byHostStarts) {
      const d = r.details || {};
      const status = r.event.endsWith(':host-ok') ? 'ok ' : 'err';
      console.log(`  ${pad(d.host || '?', 12)} ${status}  ${fmtMs(d.elapsed_ms)}`);
    }
  }
}

main();
